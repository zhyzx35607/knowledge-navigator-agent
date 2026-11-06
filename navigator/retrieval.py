"""Scoped BM25 and optional real HTTP embeddings, fused with reciprocal ranks."""
from __future__ import annotations

import hashlib
import json
import math
import os
from dataclasses import dataclass
from time import perf_counter

import httpx

from .model import ModelConfig


class RetrievalFailure(Exception):
    pass


@dataclass(frozen=True)
class EmbeddingConfig:
    base_url: str = ""
    model: str = ""
    api_key: str = ""
    timeout: float = 30
    min_similarity: float = 0.5

    def __post_init__(self):
        if not -1 <= self.min_similarity <= 1:
            raise ValueError("EMBEDDING_MIN_SIMILARITY 必须在 -1 到 1 之间")

    @classmethod
    def from_env(cls):
        return cls(os.getenv("EMBEDDING_BASE_URL", ""), os.getenv("EMBEDDING_NAME", ""),
                   os.getenv("EMBEDDING_API_KEY", ""), float(os.getenv("EMBEDDING_TIMEOUT", "30")),
                   float(os.getenv("EMBEDDING_MIN_SIMILARITY", "0.5")))

    @property
    def configured(self):
        return ModelConfig(self.base_url, self.model, timeout=self.timeout).configured

    @property
    def fingerprint(self):
        return hashlib.sha256(json.dumps([self.base_url.rstrip("/"), self.model]).encode()).hexdigest()


class EmbeddingClient:
    def __init__(self, config, transport=None):
        self.config, self.transport = config, transport

    async def embed(self, inputs):
        headers = {"Authorization": f"Bearer {self.config.api_key}"} if self.config.api_key else {}
        try:
            async with httpx.AsyncClient(timeout=self.config.timeout, transport=self.transport,
                                         follow_redirects=False, trust_env=False) as client:
                response = await client.post(self.config.base_url.rstrip("/") + "/embeddings", headers=headers,
                                             json={"model": self.config.model, "input": inputs})
                response.raise_for_status()
                data = response.json()["data"]
            if len(data) != len(inputs) or {item["index"] for item in data} != set(range(len(inputs))):
                raise ValueError("invalid embedding indices")
            vectors = [item["embedding"] for item in sorted(data, key=lambda item: item["index"])]
            dimensions = len(vectors[0])
            if not 1 <= dimensions <= 8192:
                raise ValueError("invalid dimensions")
            if any(not isinstance(vector, list) or len(vector) != dimensions
                   or any(type(value) not in (int, float) or not math.isfinite(value) for value in vector)
                   or not any(vector) for vector in vectors):
                raise ValueError("invalid vector")
            return vectors
        except (httpx.HTTPError, httpx.InvalidURL, ValueError, KeyError, IndexError, TypeError) as exc:
            raise RetrievalFailure("向量服务失败或返回无效向量；本次检索已停止，未伪装为混合检索成功") from exc


def cosine(left, right):
    if len(left) != len(right):
        raise RetrievalFailure("向量维度不一致，请检查模型配置后重试")
    left_scale, right_scale = max(map(abs, left), default=0), max(map(abs, right), default=0)
    if not left_scale or not right_scale:
        return 0
    left, right = [value / left_scale for value in left], [value / right_scale for value in right]
    denominator = math.sqrt(sum(value * value for value in left)) * math.sqrt(sum(value * value for value in right))
    return sum(a * b for a, b in zip(left, right)) / denominator


class Retriever:
    def __init__(self, store, embedding_client=None):
        self.store = store
        self.embedding = embedding_client or EmbeddingClient(EmbeddingConfig.from_env())

    async def search(self, query, scope, method="bm25", limit=6):
        started = perf_counter()
        stats = {"requested": method, "path": "bm25", "cache_hits": 0, "embedded_chunks": 0,
                 "embedding_calls": 0, "scope_documents": len(scope)}
        lexical = self.store.retrieve(query, scope, limit=30 if method == "hybrid" else limit)
        stats["bm25_candidates"] = len(lexical)
        if method == "hybrid" and not self.embedding.config.configured:
            stats["fallback_reason"] = "未配置真实向量服务，仅执行 BM25"
        elif method == "hybrid" and scope:
            candidates = self.store.scoped_chunks(scope)
            if len(candidates) > 2000:
                raise RetrievalFailure("混合检索单次范围最多 2000 个片段，请缩小集合或文档范围")
            current_ids = {item["citation_id"] for item in candidates}
            if any(item["citation_id"] not in current_ids for item in lexical):
                raise RetrievalFailure("检索期间文档已更新，请重新检索")
            if candidates:
                keys = [hashlib.sha256(json.dumps([self.embedding.config.fingerprint, item["citation_id"],
                                                  item["digest"]]).encode()).hexdigest() for item in candidates]
                cached = self.store.cached_vectors(keys)
                stats["cache_hits"] = len(cached)
                missing = [(key, item) for key, item in zip(keys, candidates) if key not in cached]
                for offset in range(0, len(missing), 16):
                    batch = missing[offset:offset + 16]
                    vectors = await self.embedding.embed([item["title"] + "\n" + item["quote"] for _, item in batch])
                    stats["embedding_calls"] += 1
                    self.store.cache_vectors([(key, item["citation_id"], item["digest"], vector)
                                              for (key, item), vector in zip(batch, vectors)])
                    cached.update({key: vector for (key, _), vector in zip(batch, vectors)})
                    stats["embedded_chunks"] += len(batch)
                query_vector = (await self.embedding.embed([query]))[0]
                stats["embedding_calls"] += 1
                dense_scores = [(item, cosine(query_vector, cached[key])) for item, key in zip(candidates, keys)]
                dense = sorted((pair for pair in dense_scores if pair[1] >= self.embedding.config.min_similarity),
                               key=lambda pair: (-pair[1], pair[0]["citation_id"]))[:30]
                scores, by_id = {}, {item["citation_id"]: item for item in candidates}
                for ranking in (lexical, [item for item, _ in dense]):
                    for rank, item in enumerate(ranking, 1):
                        citation_id = item["citation_id"]
                        scores[citation_id] = scores.get(citation_id, 0) + 1 / (60 + rank)
                lexical = [{**by_id[citation_id], "rrf_score": scores[citation_id]} for citation_id in
                           sorted(scores, key=lambda citation_id: (-scores[citation_id], citation_id))[:limit]]
                stats.update(path="hybrid_rrf", dense_candidates=len(dense), fusion="RRF", rrf_constant=60,
                             min_similarity=self.embedding.config.min_similarity)
            else:
                stats.update(path="hybrid_rrf", dense_candidates=0)
        stats["elapsed_ms"] = round((perf_counter() - started) * 1000, 3)
        return lexical[:limit], stats
