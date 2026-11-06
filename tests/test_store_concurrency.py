from concurrent.futures import ThreadPoolExecutor
from threading import Event

from fastapi.testclient import TestClient

from navigator.main import create_app


def test_concurrent_edits_reject_stale_version_and_keep_index(tmp_path, monkeypatch):
    with TestClient(create_app(tmp_path / "concurrent.db")) as client:
        created = client.post("/api/documents", json={
            "title": "Original", "body": "originaltoken", "entry_ids": []}).json()
        document_id = created["id"]
        store = client.app.state.store
        original_put = store._put
        first_at_write, second_at_write, first_finished = Event(), Event(), Event()

        def overlap_writes(conn, id, title, body, entry_ids):
            if title == "First editor":
                first_at_write.set()
                # A competing stale writer can reach this point only if version
                # validation was not protected by the write transaction.
                second_at_write.wait(timeout=0.5)
            else:
                second_at_write.set()
                assert first_finished.wait(timeout=5)
            return original_put(conn, id, title, body, entry_ids)

        monkeypatch.setattr(store, "_put", overlap_writes)

        def edit(title, body):
            try:
                return client.put(f"/api/documents/{document_id}", json={
                    "title": title, "body": body, "entry_ids": [],
                    "expected_version": created["version"]})
            finally:
                if title == "First editor":
                    first_finished.set()

        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(edit, "First editor", "winningtoken")
            assert first_at_write.wait(timeout=5)
            second = pool.submit(edit, "Second editor", "losingtoken")
            winner, rejected = first.result(timeout=10), second.result(timeout=10)

        assert winner.status_code == 200
        assert rejected.status_code == 409
        current = next(item for item in client.get("/api/documents").json()
                       if item["id"] == document_id)
        assert current == winner.json()
        assert current["version"] == 2 and current["body"] == "winningtoken"
        result = client.post("/api/search", json={
            "query": "winningtoken", "document_ids": [document_id]}).json()
        assert result["status"] == "matched"
        assert result["citations"][0]["version"] == current["version"]
        assert result["citations"][0]["digest"] == current["digest"]
        for query in ("originaltoken", "losingtoken"):
            assert client.post("/api/search", json={
                "query": query, "document_ids": [document_id]}).json()["status"] == "not_found"
