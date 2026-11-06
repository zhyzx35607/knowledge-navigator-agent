from __future__ import annotations

import os
import json
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .agent import AgentFailure, run_agent
from .model import ModelClient, ModelConfig
from .protocol import InvalidResult, Proposal, validate_result
from .store import Store, UNCHANGED
from .importing import ImportBatch, MAX_REQUEST_BYTES, decode_files
from .exporting import research_markdown
from .retrieval import Retriever, RetrievalFailure


class Search(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=1, max_length=400)
    document_ids: list[str] | None = Field(default=None, max_length=200)
    entry_ids: list[str] | None = Field(default=None, max_length=20)
    collection_ids: list[str] | None = Field(default=None, max_length=100)
    retrieval_method: Literal["bm25", "hybrid"] = "bm25"
    parent_research_id: str | None = Field(default=None, max_length=64)
    mode: Literal["local", "model"] = "local"


class DocumentInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=120)
    body: str = Field(min_length=1, max_length=80000)
    entry_ids: list[str] = Field(default_factory=list, max_length=20)
    expected_version: int | None = Field(default=None, ge=0)
    collection_id: str | None = Field(default=None, max_length=64)


class CollectionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=80)


def create_app(db_path=None, model_client=None, embedding_client=None):
    store = Store(Path(db_path or os.getenv("KNOWLEDGE_DB", "data/knowledge.sqlite3")))
    model = model_client or ModelClient(ModelConfig.from_env())
    retriever = Retriever(store, embedding_client)

    @asynccontextmanager
    async def lifespan(app):
        store.initialize()
        yield

    app = FastAPI(title="知径 · Knowledge Navigator", version="0.2.0", lifespan=lifespan)
    app.state.store, app.state.model, app.state.retriever = store, model, retriever

    @app.get("/api/health")
    def health():
        return {"status": "ok", "model_configured": model.config.configured,
                "embedding_configured": retriever.embedding.config.configured,
                "default_mode": "local", "retrieval": "SQLite FTS5 / BM25 + optional dense RRF"}

    @app.get("/api/documents")
    def documents():
        return store.documents()

    @app.get("/api/entries")
    def entries():
        return store.entries()

    def save(document_id, value):
        if not value.title.strip() or not value.body.strip():
            raise HTTPException(422, "标题和正文不可为空白")
        try:
            return store.put(document_id, value.title.strip(), value.body.strip(),
                             value.entry_ids, value.expected_version,
                             value.collection_id if "collection_id" in value.model_fields_set else UNCHANGED)
        except PermissionError as exc:
            raise HTTPException(403, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/api/documents", status_code=201)
    def create_document(value: DocumentInput):
        return save(uuid.uuid4().hex, value)

    @app.put("/api/documents/{document_id}")
    def update_document(document_id: str, value: DocumentInput):
        if not store.get(document_id):
            raise HTTPException(404, "文档不存在")
        return save(document_id, value)

    @app.delete("/api/documents/{document_id}", status_code=204)
    def delete_document(document_id: str):
        if not store.delete(document_id):
            raise HTTPException(404, "文档不存在")

    @app.get("/api/collections")
    def collections():
        return store.collections()

    def save_collection(value, collection_id=None):
        if not value.name.strip():
            raise HTTPException(422, "集合名称不可为空白")
        try:
            return store.save_collection(value.name.strip(), collection_id)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.post("/api/collections", status_code=201)
    def create_collection(value: CollectionInput):
        return save_collection(value)

    @app.put("/api/collections/{collection_id}")
    def rename_collection(collection_id: str, value: CollectionInput):
        if collection_id not in {item["id"] for item in store.collections()}:
            raise HTTPException(404, "集合不存在")
        return save_collection(value, collection_id)

    @app.delete("/api/collections/{collection_id}", status_code=204)
    def delete_collection(collection_id: str):
        if not store.delete_collection(collection_id):
            raise HTTPException(404, "集合不存在")

    @app.post("/api/documents/import", status_code=201)
    async def import_documents(request: Request):
        payload = bytearray()
        async for chunk in request.stream():
            payload.extend(chunk)
            if len(payload) > MAX_REQUEST_BYTES:
                raise HTTPException(413, "请求过大；整批未导入")
        try:
            batch = ImportBatch.model_validate_json(bytes(payload))
        except ValidationError as exc:
            raise HTTPException(422, "导入请求无效：每批 1–20 个文件，文件名最多 120 字符；整批未导入") from exc
        try:
            items = decode_files(batch.files)
            imported = store.import_documents(items, batch.collection_id, batch.entry_ids)
        except PermissionError as exc:
            raise HTTPException(403, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        return {"imported": imported, "count": len(imported)}

    @app.post("/api/search")
    async def search(value: Search):
        if not value.query.strip():
            raise HTTPException(422, "请输入检索问题")
        documents = store.documents()
        known_docs = {item["id"] for item in documents}
        known_entries = store.entries()
        known_entry_ids = {item["id"] for item in known_entries}
        scope = list(dict.fromkeys(value.document_ids if value.document_ids is not None else sorted(known_docs)))
        entry_scope = set(value.entry_ids) if value.entry_ids is not None else known_entry_ids
        if not set(scope) <= known_docs:
            raise HTTPException(403, "检索范围包含不存在或不可访问的文档")
        if not entry_scope <= known_entry_ids:
            raise HTTPException(403, "请求包含白名单外的入口")
        continuation, previous = None, None
        if value.parent_research_id:
            previous = store.research(value.parent_research_id)
            if previous is None:
                raise HTTPException(404, "上一轮研究记录不存在")
            if previous["evidence_status"] == "stale":
                raise HTTPException(409, "上一轮引用已过期，请先重新检索；旧证据不会注入续问")
            scope = [document_id for document_id in scope if document_id in previous["scope_document_ids"]]
            entry_scope &= set(previous.get("scope_entry_ids", []))
        if value.collection_ids is not None:
            known_collections = {item["id"] for item in store.collections()} | {"unfiled"}
            if not set(value.collection_ids) <= known_collections:
                raise HTTPException(403, "检索范围包含不存在的集合")
            collection_docs = {item["id"] for item in documents
                               if (item["collection_id"] or "unfiled") in value.collection_ids}
            scope = [document_id for document_id in scope if document_id in collection_docs]
        if previous:
            continuation = {"research_id": previous["research_id"], "query": previous["query"][:400],
                            "answer": previous.get("answer", "")[:1200]
                            if set(scope) == set(previous["scope_document_ids"]) else "",
                            "citations": [{"citation_id": item["citation_id"], "quote": item["quote"][:180]}
                                          for item in previous.get("citations", []) if item["document_id"] in scope][:3]}
        allowed = [item for item in known_entries if item["id"] in entry_scope]
        trace = [{"step": "scope", "detail": f"已限定 {len(scope)} 份文档、{len(allowed)} 个入口"}]
        if continuation:
            trace.append({"step": "context", "detail": "显式继续上一轮：注入有界公开上下文，文档与入口范围取交集",
                          "parent_research_id": value.parent_research_id,
                          "context_chars": len(json.dumps(continuation, ensure_ascii=False))})
        base = {"protocol_version": "1.1", "query": value.query, "mode": value.mode,
                "scope_document_ids": scope, "scope_collection_ids": value.collection_ids,
                "scope_entry_ids": sorted(entry_scope), "trace": trace,
                "parent_research_id": value.parent_research_id, "continuation_context": continuation,
                "retrieval": {"requested": value.retrieval_method}}

        def recorded(result):
            response = {**base, **result}
            return {**response, **store.save_research(response)}

        if value.mode == "model":
            try:
                result = await run_agent(value.query, scope, allowed, store, model, trace, retriever,
                                         value.retrieval_method, continuation)
            except AgentFailure as exc:
                failure = recorded(exc.detail)
                raise HTTPException(502, failure) from exc
            return recorded(result)
        retrieval_query = value.query if not continuation else value.query + " " + continuation["query"][:180]
        try:
            evidence, retrieval = await retriever.search(retrieval_query, scope, value.retrieval_method)
        except RetrievalFailure as exc:
            failure = recorded({"status": "failed", "code": "retrieval_failure", "message": str(exc), "model_called": False})
            raise HTTPException(502, failure) from exc
        base["retrieval"] = retrieval
        trace.append({"step": "retrieve", "detail": f"{retrieval['path']} 命中 {len(evidence)} 个片段",
                      "input": {"query": retrieval_query}, "tool_ms": retrieval["elapsed_ms"], "retrieval": retrieval})
        if not evidence:
            return recorded({"status": "not_found", "answer": "所选文档内没有检索到证据，请调整关键词或范围。",
                    "citations": [], "entries": [], "model_called": False})
        linked = {entry for item in evidence for entry in item["entry_ids"]}
        proposal = Proposal(answer="本地检索找到了以下原文片段。未调用语言模型，请结合来源阅读。",
                            citation_ids=[item["citation_id"] for item in evidence],
                            entry_ids=sorted(linked & entry_scope))
        try:
            result = validate_result(proposal, evidence, scope, allowed, store)
        except InvalidResult as exc:
            failure = recorded({"status": "failed", "code": "invalid_result", "message": str(exc), "model_called": False})
            raise HTTPException(409, failure) from exc
        trace.append({"step": "validate", "detail": "引用版本、原文、文档范围和入口白名单校验通过"})
        return recorded({**result, "status": "matched", "model_called": False})

    @app.get("/api/research")
    def research_records():
        return store.research_records()

    @app.get("/api/research/{record_id}")
    def research(record_id: str):
        record = store.research(record_id)
        if record is None:
            raise HTTPException(404, "研究记录不存在")
        return record

    @app.get("/api/research/{record_id}/export")
    def export_research(record_id: str, format: Literal["md", "json"] = "md"):
        record = research(record_id)
        content = research_markdown(record) if format == "md" else json.dumps(record, ensure_ascii=False, indent=2)
        return Response(content, media_type="text/markdown" if format == "md" else "application/json",
                        headers={"Content-Disposition": f'attachment; filename="research-{record["research_id"]}.{format}"'})

    @app.delete("/api/research/{record_id}", status_code=204)
    def delete_research(record_id: str):
        if not store.delete_research(record_id):
            raise HTTPException(404, "研究记录不存在")

    @app.get("/api/workspace/{entry_id}")
    def workspace(entry_id: str):
        entry = next((item for item in store.entries() if item["id"] == entry_id), None)
        if not entry:
            raise HTTPException(404, "入口不在服务器白名单中")
        return {"entry": entry, "documents": [item for item in store.documents()
                                             if entry_id in item["entry_ids"]]}

    static = Path(__file__).with_name("static")
    app.mount("/static", StaticFiles(directory=static), name="static")

    @app.get("/")
    @app.get("/workspace/{entry_id}")
    def home(entry_id: str | None = None):
        if entry_id and entry_id not in {item["id"] for item in store.entries()}:
            raise HTTPException(404, "入口不存在")
        return FileResponse(static / "index.html")

    return app


app = create_app()
