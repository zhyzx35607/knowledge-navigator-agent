import base64
import hashlib
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from navigator.main import create_app
from navigator.store import Store


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(tmp_path / "workspace.db")) as client:
        yield client


def file(name="notes.md", content=b"orchidtoken notes"):
    return {"filename": name, "content_base64": base64.b64encode(content).decode()}


def test_import_collection_history_and_stale_exports_survive_restart(tmp_path):
    db = tmp_path / "persist.db"
    with TestClient(create_app(db)) as client:
        collection = client.post("/api/collections", json={"name": "学习资料"}).json()
        response = client.post("/api/documents/import", json={"files": [file(), file("second.TXT", b"secondtoken")], "collection_id": collection["id"]})
        assert response.status_code == 201 and response.json()["count"] == 2
        document_id = response.json()["imported"][0]["id"]
        result = client.post("/api/search", json={"query": "orchidtoken", "collection_ids": [collection["id"]], "entry_ids": []}).json()
        record_id = result["research_id"]
        assert result["citations"][0]["document_id"] == document_id
        assert client.get(f"/api/research/{record_id}").json()["evidence_status"] == "current"
        assert client.put(f"/api/documents/{document_id}", json={"title": "Updated", "body": "newtoken", "expected_version": 1}).status_code == 200
        assert client.app.state.store.get(document_id)["collection_id"] == collection["id"]
    with TestClient(create_app(db)) as client:
        record = client.get(f"/api/research/{record_id}").json()
        assert record["evidence_status"] == "stale" and record["stale_citation_count"] == 1
        assert record["citations"][0]["quote"] == "orchidtoken notes"
        assert record["citations"][0]["version"] == 1
        exported = client.get(f"/api/research/{record_id}/export?format=json")
        assert exported.json()["evidence_status"] == "stale" and "attachment" in exported.headers["content-disposition"]
        assert "文档已更新" in client.get(f"/api/research/{record_id}/export?format=md").text
        assert client.delete(f"/api/documents/{document_id}").status_code == 204
        assert client.get(f"/api/research/{record_id}").json()["citations"][0]["stale_reason"] == "文档已删除"
        assert client.delete(f"/api/research/{record_id}").status_code == 204
        assert client.get(f"/api/research/{record_id}").status_code == 404


@pytest.mark.parametrize("bad_file", [
    file("../escape.md"), file("C:\\escape.md"), file("unsupported.pdf"), file("empty.txt", b"  "),
    file("invalid.txt", b"\xff\xfe"), file("nul.txt", b"hello\0world"),
    {"filename": "bad.txt", "content_base64": "invalid@base64"},
    file("large.txt", b"a" * (256 * 1024 + 1)), file("long.txt", b"a" * 80001),
])
def test_invalid_batch_is_atomic(client, bad_file):
    before = client.get("/api/documents").json()
    response = client.post("/api/documents/import", json={"files": [file(), bad_file]})
    assert response.status_code == 422
    assert client.get("/api/documents").json() == before
    assert client.app.state.store.retrieve("orchidtoken", [item["id"] for item in before]) == []


def test_import_count_total_bytes_and_request_limits(client):
    assert client.post("/api/documents/import", json={"files": [file()] * 21}).status_code == 422
    content = ("汉" * 79999).encode()
    assert client.post("/api/documents/import", json={"files": [file(content=content)] * 9}).status_code == 422
    assert client.post("/api/documents/import", content=b" " * (3 * 1024 * 1024 + 1)).status_code == 413
    assert client.post("/api/documents/import", json={"files": [file()] * 20}).status_code == 201


def test_import_bom_unknown_collection_entry_and_database_rollback(client, monkeypatch):
    assert client.post("/api/documents/import", json={"files": [file()], "collection_id": "missing"}).status_code == 422
    assert client.post("/api/documents/import", json={"files": [file()], "entry_ids": ["admin"]}).status_code == 403
    result = client.post("/api/documents/import", json={"files": [file(content=b"\xef\xbb\xbfBOM text")]})
    assert client.app.state.store.get(result.json()["imported"][0]["id"])["body"] == "BOM text"
    store = client.app.state.store
    before = store.documents()
    original = store._put
    calls = 0

    def fail_second(*args):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise sqlite3.OperationalError("injected write failure")
        return original(*args)

    monkeypatch.setattr(store, "_put", fail_second)
    with pytest.raises(sqlite3.OperationalError):
        store.import_documents([{"filename": "a.txt", "body": "rollbacktoken"}] * 2)
    assert store.documents() == before
    with store.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM search_index WHERE tokens LIKE '%rollbacktoken%'").fetchone()[0] == 0


def test_collection_scope_intersection_and_delete_preserves_documents(client):
    collection = client.post("/api/collections", json={"name": "Research"}).json()
    doc = client.post("/api/documents", json={"title": "Scoped", "body": "RAG orchidtoken", "collection_id": collection["id"]}).json()
    assert client.post("/api/collections", json={"name": "Research"}).status_code == 409
    assert client.put(f"/api/collections/{collection['id']}", json={"name": "Renamed"}).status_code == 200
    result = client.post("/api/search", json={"query": "RAG", "collection_ids": [collection["id"]], "document_ids": ["retrieval-guide"]}).json()
    assert result["scope_document_ids"] == [] and result["status"] == "not_found"
    assert client.post("/api/search", json={"query": "RAG", "collection_ids": []}).json()["scope_document_ids"] == []
    assert client.post("/api/search", json={"query": "RAG", "collection_ids": ["missing"]}).status_code == 403
    assert client.delete(f"/api/collections/{collection['id']}").status_code == 204
    result = client.post("/api/search", json={"query": "orchidtoken", "collection_ids": ["unfiled"]}).json()
    assert result["citations"][0]["document_id"] == doc["id"]
    assert client.app.state.store.get(doc["id"])["collection_id"] is None


def test_old_schema_migration_is_idempotent_and_preserves_empty_library(tmp_path):
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as conn:
        conn.executescript("""CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT);
            INSERT INTO metadata VALUES ('seeded','1');
            CREATE TABLE documents(id TEXT PRIMARY KEY,title TEXT NOT NULL,body TEXT NOT NULL,
                entry_ids TEXT NOT NULL,version INTEGER NOT NULL,digest TEXT NOT NULL,updated_at TEXT NOT NULL);""")
        conn.execute("INSERT INTO documents VALUES (?,?,?,?,?,?,?)", ("legacy", "Legacy", "original content", "[]", 7,
                     hashlib.sha256(b"original content").hexdigest(), "2026-01-01"))
    store = Store(path)
    store.initialize()
    first = store.documents()
    store.initialize()
    assert store.documents() == first and len(first) == 1
    assert first[0]["version"] == 7 and first[0]["collection_id"] is None
    store.delete("legacy")
    store.initialize()
    assert store.documents() == []


def test_history_cannot_be_forged_and_failed_search_is_saved(client):
    assert client.post("/api/research", json={"answer": "forged"}).status_code == 405
    response = client.post("/api/search", json={"query": "每周复盘", "mode": "model"})
    assert response.status_code == 502
    record = client.get(f"/api/research/{response.json()['detail']['research_id']}").json()
    assert record["status"] == "failed" and record["code"] == "model_unconfigured"
    assert record["evidence_status"] == "none"


def test_continuation_uses_bounded_context_and_intersects_scope(client):
    original = client.post("/api/search", json={"query": "每周复盘", "document_ids": ["weekly-review"], "entry_ids": ["journal"]}).json()
    followup = client.post("/api/search", json={"query": "有哪些步骤", "parent_research_id": original["research_id"]}).json()
    assert followup["scope_document_ids"] == ["weekly-review"] and followup["scope_entry_ids"] == ["journal"]
    assert followup["continuation_context"]["research_id"] == original["research_id"]
    assert original["query"] in next(item for item in followup["trace"] if item["step"] == "retrieve")["input"]["query"]
    assert len(json.dumps(followup["continuation_context"], ensure_ascii=False)) < 3000
    empty = client.post("/api/search", json={"query": "RAG", "parent_research_id": original["research_id"], "document_ids": ["retrieval-guide"]}).json()
    assert empty["scope_document_ids"] == [] and empty["continuation_context"]["answer"] == "" and not empty["continuation_context"]["citations"]
    client.app.state.store.put("weekly-review", "changed", "new content", [])
    assert client.post("/api/search", json={"query": "继续", "parent_research_id": original["research_id"]}).status_code == 409
