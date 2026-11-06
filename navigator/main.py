from __future__ import annotations

import os
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from .agent import AgentFailure, run_agent
from .model import ModelClient, ModelConfig
from .protocol import InvalidResult, Proposal, validate_result
from .store import Store


class Search(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=1, max_length=400)
    document_ids: list[str] | None = Field(default=None, max_length=200)
    entry_ids: list[str] | None = Field(default=None, max_length=20)
    mode: Literal["local", "model"] = "local"


class DocumentInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=120)
    body: str = Field(min_length=1, max_length=80000)
    entry_ids: list[str] = Field(default_factory=list, max_length=20)
    expected_version: int | None = Field(default=None, ge=0)


def create_app(db_path=None, model_client=None):
    store = Store(Path(db_path or os.getenv("KNOWLEDGE_DB", "data/knowledge.sqlite3")))
    model = model_client or ModelClient(ModelConfig.from_env())

    @asynccontextmanager
    async def lifespan(app):
        store.initialize()
        yield

    app = FastAPI(title="知径 · Knowledge Navigator", version="0.1.0", lifespan=lifespan)
    app.state.store, app.state.model = store, model

    @app.get("/api/health")
    def health():
        return {"status": "ok", "model_configured": model.config.configured,
                "default_mode": "local", "retrieval": "SQLite FTS5 / BM25 + Chinese bigrams"}

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
                             value.entry_ids, value.expected_version)
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

    @app.post("/api/search")
    async def search(value: Search):
        if not value.query.strip():
            raise HTTPException(422, "请输入检索问题")
        known_docs = {item["id"] for item in store.documents()}
        known_entries = store.entries()
        known_entry_ids = {item["id"] for item in known_entries}
        scope = list(dict.fromkeys(value.document_ids if value.document_ids is not None else sorted(known_docs)))
        entry_scope = set(value.entry_ids) if value.entry_ids is not None else known_entry_ids
        if not set(scope) <= known_docs:
            raise HTTPException(403, "检索范围包含不存在或不可访问的文档")
        if not entry_scope <= known_entry_ids:
            raise HTTPException(403, "请求包含白名单外的入口")
        allowed = [item for item in known_entries if item["id"] in entry_scope]
        trace = [{"step": "scope", "detail": f"已限定 {len(scope)} 份文档、{len(allowed)} 个入口"}]
        base = {"protocol_version": "1.1", "query": value.query, "mode": value.mode,
                "scope_document_ids": scope, "trace": trace}
        if value.mode == "model":
            try:
                result = await run_agent(value.query, scope, allowed, store, model, trace)
            except AgentFailure as exc:
                raise HTTPException(502, exc.detail) from exc
            return {**base, **result}
        evidence = store.retrieve(value.query, scope)
        trace.append({"step": "retrieve", "detail": f"FTS5 实际命中 {len(evidence)} 个片段"})
        if not evidence:
            return {**base, "status": "not_found", "answer": "所选文档内没有检索到证据，请调整关键词或范围。",
                    "citations": [], "entries": [], "model_called": False}
        linked = {entry for item in evidence for entry in item["entry_ids"]}
        proposal = Proposal(answer="本地检索找到了以下原文片段。未调用语言模型，请结合来源阅读。",
                            citation_ids=[item["citation_id"] for item in evidence],
                            entry_ids=sorted(linked & entry_scope))
        try:
            result = validate_result(proposal, evidence, scope, allowed, store)
        except InvalidResult as exc:
            raise HTTPException(409, str(exc)) from exc
        trace.append({"step": "validate", "detail": "引用版本、原文、文档范围和入口白名单校验通过"})
        return {**base, **result, "status": "matched", "model_called": value.mode == "model"}

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
