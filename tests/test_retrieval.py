import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from navigator.main import create_app
from navigator.model import ModelClient, ModelConfig
from navigator.retrieval import EmbeddingClient, EmbeddingConfig, Retriever, RetrievalFailure, cosine
from navigator.store import Store


def embedding_client(seen, model="test-embedding"):
    def handler(request):
        payload = json.loads(request.content)
        seen.append(payload)
        # Explicit deterministic transport fixture; these vectors are not a
        # claim about real-world semantic quality or a production fallback.
        return httpx.Response(200, json={"data": [
            {"index": index, "embedding": [1, 0] if "target" in text or text == "paraphrase" else [0, 1]}
            for index, text in enumerate(payload["input"])]})
    return EmbeddingClient(EmbeddingConfig("http://embedding.test/v1", model), httpx.MockTransport(handler))


def test_real_http_contract_cache_version_model_and_scope(tmp_path):
    seen = []
    store = Store(tmp_path / "dense.db")
    store.initialize()
    store.put("allowed", "target", "target orchidtoken", [])
    store.put("outside", "secret", "outside-scope-secret", [])
    retriever = Retriever(store, embedding_client(seen))
    hits, trace = asyncio.run(retriever.search("paraphrase", ["allowed"], "hybrid"))
    assert hits[0]["document_id"] == "allowed" and trace["path"] == "hybrid_rrf"
    assert trace["embedded_chunks"] == 1 and trace["embedding_calls"] == 2
    assert "outside-scope-secret" not in json.dumps(seen)
    _, cached = asyncio.run(retriever.search("paraphrase", ["allowed"], "hybrid"))
    assert cached["cache_hits"] == 1 and cached["embedded_chunks"] == 0 and cached["embedding_calls"] == 1
    store.put("allowed", "target", "target new version", [])
    hits, changed = asyncio.run(retriever.search("paraphrase", ["allowed"], "hybrid"))
    assert changed["cache_hits"] == 0 and hits[0]["version"] == 2
    _, new_model = asyncio.run(Retriever(store, embedding_client(seen, "another-model")).search("paraphrase", ["allowed"], "hybrid"))
    assert new_model["cache_hits"] == 0
    before = len(seen)
    assert asyncio.run(retriever.search("paraphrase", [], "hybrid"))[0] == []
    assert len(seen) == before


def test_unconfigured_hybrid_reports_actual_bm25_path(tmp_path):
    with TestClient(create_app(tmp_path / "fallback.db", embedding_client=EmbeddingClient(EmbeddingConfig()))) as client:
        result = client.post("/api/search", json={"query": "每周复盘", "retrieval_method": "hybrid"}).json()
        assert result["retrieval"]["requested"] == "hybrid"
        assert result["retrieval"]["path"] == "bm25" and "fallback_reason" in result["retrieval"]
        assert result["retrieval"]["embedding_calls"] == 0


@pytest.mark.parametrize("vector", [[], [0, 0], [True, 1], ["bad", 1]])
def test_invalid_vectors_fail_without_synthetic_results(tmp_path, vector):
    def handler(request):
        inputs = json.loads(request.content)["input"]
        return httpx.Response(200, json={"data": [{"index": index, "embedding": vector} for index in range(len(inputs))]})
    adapter = EmbeddingClient(EmbeddingConfig("http://embedding.test", "model", "secret-key"), httpx.MockTransport(handler))
    with TestClient(create_app(tmp_path / "bad-vector.db", embedding_client=adapter)) as client:
        response = client.post("/api/search", json={"query": "每周复盘", "retrieval_method": "hybrid"})
        assert response.status_code == 502 and "secret-key" not in response.text
        assert response.json()["detail"]["code"] == "retrieval_failure"
        assert len(client.get("/api/research").json()) == 1


def test_changed_candidates_fail_explicitly_and_record_failure(tmp_path, monkeypatch):
    seen = []
    with TestClient(create_app(tmp_path / "race.db", embedding_client=embedding_client(seen))) as client:
        store = client.app.state.store
        store.put("race", "target", "target orchidtoken", [])
        original = store.scoped_chunks

        def changed(scope):
            store.put("race", "target", "target changed", [])
            return original(scope)

        monkeypatch.setattr(store, "scoped_chunks", changed)
        response = client.post("/api/search", json={"query": "orchidtoken", "document_ids": ["race"], "retrieval_method": "hybrid"})
        assert response.status_code == 502 and response.json()["detail"]["code"] == "retrieval_failure"
        assert seen == [] and len(client.get("/api/research").json()) == 1


@pytest.mark.parametrize("scale", [1e200, 1e-200])
def test_cosine_does_not_overflow_or_underflow(scale):
    assert cosine([scale, scale], [scale, scale]) == pytest.approx(1)
    assert cosine([scale, 0], [0, scale]) == 0


def test_agent_continuation_context_is_used_but_cannot_supply_citations(tmp_path):
    messages_seen = []

    def model(request):
        messages = json.loads(request.content)["messages"]
        messages_seen.append(messages)
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({
            "action": "final", "answer": "reuse old citation", "citation_ids": ["weekly-review:v1:0"], "entry_ids": []})}}]})

    adapter = ModelClient(ModelConfig("http://model.test", "model"), httpx.MockTransport(model))
    with TestClient(create_app(tmp_path / "context.db", model_client=adapter)) as client:
        original = client.post("/api/search", json={"query": "每周复盘", "document_ids": ["weekly-review"]}).json()
        response = client.post("/api/search", json={"query": "继续", "mode": "model", "parent_research_id": original["research_id"]})
        assert response.status_code == 502 and response.json()["detail"]["code"] == "unread_citation"
        injected = json.loads(messages_seen[0][2]["content"])["previous_research"]
        assert injected["research_id"] == original["research_id"] and injected["citations"]
