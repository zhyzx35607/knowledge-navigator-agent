import json

import httpx
import pytest
from fastapi.testclient import TestClient

from navigator.main import create_app
from navigator.model import ModelClient, ModelConfig
from navigator.protocol import InvalidResult, Proposal, validate_result


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(tmp_path / "knowledge.db")) as session:
        yield session


def search(client, **kwargs):
    return client.post("/api/search", json={"query": "每周复盘", **kwargs})


def test_local_retrieves_actual_source_and_supported_entry(client):
    result = search(client).json()
    assert result["status"] == "matched"
    assert result["mode"] == "local" and result["model_called"] is False
    assert result["citations"][0]["document_id"] == "weekly-review"
    assert "星期五" in result["citations"][0]["quote"]
    docs = {item["id"]: item for item in client.get("/api/documents").json()}
    for citation in result["citations"]:
        assert citation["quote"] in docs[citation["document_id"]]["body"]
        assert citation["digest"] == docs[citation["document_id"]]["digest"]
    assert "journal" in [entry["id"] for entry in result["entries"]]


def test_scope_cannot_leak_other_documents(client):
    result = search(client, query="RAG", document_ids=["weekly-review"]).json()
    assert result["status"] == "not_found" and not result["citations"]
    result = search(client, document_ids=["learning-notes"], entry_ids=[]).json()
    assert all(item["document_id"] == "learning-notes" for item in result["citations"])
    assert result["entries"] == []


@pytest.mark.parametrize("payload", [{"document_ids": ["outside"]}, {"entry_ids": ["admin"]}])
def test_unknown_scope_rejected(client, payload):
    assert search(client, **payload).status_code == 403


def test_empty_scope_not_found_instead_of_all_documents(client):
    result = search(client, document_ids=[]).json()
    assert result["status"] == "not_found" and result["scope_document_ids"] == []


def test_unknown_terms_and_fts_syntax_are_safe(client):
    assert search(client, query="xyznotfound").json()["status"] == "not_found"
    assert search(client, query='" OR * NEAR() --').status_code == 200
    assert search(client, query="   ").status_code == 422


def test_document_lifecycle_keeps_fts_index_current(client):
    created = client.post("/api/documents", json={"title": "Orchid note", "body": "orchidtoken is green", "entry_ids": ["studio"]}).json()
    doc_id = created["id"]
    assert search(client, query="orchidtoken").json()["citations"][0]["document_id"] == doc_id
    updated = client.put(f"/api/documents/{doc_id}", json={"title": "Updated note", "body": "violettoken is blue", "entry_ids": ["learning"], "expected_version": 1})
    assert updated.status_code == 200 and updated.json()["version"] == 2
    assert search(client, query="orchidtoken").json()["status"] == "not_found"
    current = search(client, query="violettoken").json()
    assert current["citations"][0]["version"] == 2
    assert [entry["id"] for entry in current["entries"]] == ["learning"]
    assert client.put(f"/api/documents/{doc_id}", json={"title": "stale", "body": "stale", "expected_version": 1}).status_code == 409
    assert client.delete(f"/api/documents/{doc_id}").status_code == 204
    assert search(client, query="violettoken").json()["status"] == "not_found"
    assert client.delete(f"/api/documents/{doc_id}").status_code == 404


def test_document_cannot_register_an_unapproved_entry(client):
    assert client.post("/api/documents", json={"title": "bad", "body": "bad", "entry_ids": ["admin"]}).status_code == 403
    assert client.get("/workspace/admin").status_code == 404
    assert client.get("/api/workspace/admin").status_code == 404


def test_deleting_all_documents_does_not_reseed(tmp_path):
    db = tmp_path / "same.db"
    with TestClient(create_app(db)) as client:
        for doc in client.get("/api/documents").json():
            client.delete(f"/api/documents/{doc['id']}")
    with TestClient(create_app(db)) as client:
        assert client.get("/api/documents").json() == []


def test_model_unconfigured_is_an_error_not_fake_local_success(client):
    response = search(client, mode="model")
    assert response.status_code == 502 and "尚未配置" in response.json()["detail"]["message"]
    assert response.json()["detail"]["model_called"] is False
    assert "citations" not in response.json()


def model_session(tmp_path, handler, max_steps=6):
    adapter = ModelClient(ModelConfig("http://model.test/v1", "test-model", timeout=1, max_steps=max_steps),
                          transport=httpx.MockTransport(handler))
    return TestClient(create_app(tmp_path / "model.db", model_client=adapter))


def response_with(content):
    return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(content, ensure_ascii=False)}}]})


def last_tool(request):
    return json.loads(json.loads(request.content)["messages"][-1]["content"])


def read_first(request):
    tool = last_tool(request)
    assert tool["tool"] == "search"
    assert "preview" in tool["result"][0] and "quote" not in tool["result"][0]
    return {"action": "read", "chunk_ids": [tool["result"][0]["citation_id"]]}


def final_first(request):
    tool = last_tool(request)
    assert tool["tool"] == "read" and "quote" in tool["result"][0]
    return {"action": "final", "answer": "合成模型测试回答", "citation_ids": [tool["result"][0]["citation_id"]], "entry_ids": ["journal"]}


def scripted(actions, seen=None):
    """Explicit fake model transport; actual search/read still use SQLite."""
    pending = iter(actions)
    def handler(request):
        if seen is not None:
            seen.append(json.loads(request.content))
        action = next(pending)
        return response_with(action(request) if callable(action) else action)
    return handler


def good_sequence(final=final_first):
    return [{"action": "search", "query": "每周复盘"}, read_first, final]


def test_model_contract_with_deterministic_transport(tmp_path):
    seen = []
    with model_session(tmp_path, scripted(good_sequence(), seen)) as client:
        result = search(client, mode="model", document_ids=["weekly-review"]).json()
        assert result["model_called"] is True
        assert result["model_calls"] == 3 and len(seen) == 3
        assert result["citations"][0]["document_id"] == "weekly-review"
        assert result["entries"][0]["path"] == "/workspace/journal"
        assert [item["step"] for item in result["trace"]] == ["scope", "search", "read", "final"]
        assert result["trace"][1]["result"]["count"] > 0
        assert "answer" not in result["trace"][-1]["input"]
        assert all("reasoning" not in json.dumps(item) for item in result["trace"])


def test_agent_rewrites_search_after_zero_hits(tmp_path):
    def rewrite(request):
        assert last_tool(request)["result"] == []
        return {"action": "search", "query": "每周复盘"}
    actions = [{"action": "search", "query": "nothingmatchesxyz"}, rewrite, read_first, final_first]
    with model_session(tmp_path, scripted(actions)) as client:
        result = search(client, mode="model", query="怎么安排回顾", document_ids=["weekly-review"]).json()
        assert result["status"] == "matched" and result["model_calls"] == 4
        assert result["trace"][1]["result"]["count"] == 0
        assert result["trace"][2]["result"]["count"] > 0


@pytest.mark.parametrize("mutation", ["citation", "entry", "unsupported_entry", "extra_field"])
def test_model_invented_or_unauthorized_results_rejected(tmp_path, mutation):
    def final(request):
        proposal = final_first(request)
        if mutation == "citation": proposal["citation_ids"] = ["other:v1:0"]
        if mutation == "entry": proposal["entry_ids"] = ["admin"]
        if mutation == "unsupported_entry": proposal["entry_ids"] = ["studio"]
        if mutation == "extra_field": proposal["redirect_url"] = "https://invalid.example"
        return proposal
    with model_session(tmp_path, scripted(good_sequence(final))) as client:
        assert search(client, mode="model", document_ids=["weekly-review"]).status_code == 502


def test_model_entry_must_be_in_request_whitelist(tmp_path):
    with model_session(tmp_path, scripted(good_sequence())) as client:
        assert search(client, mode="model", entry_ids=[], document_ids=["weekly-review"]).status_code == 502


@pytest.mark.parametrize("scope", [["weekly-review"], []])
def test_model_search_always_uses_original_scope(tmp_path, scope):
    def abstain(request):
        assert last_tool(request)["result"] == []
        return {"action": "final", "answer": "这段无证据答案必须隐藏", "citation_ids": [], "entry_ids": []}
    actions = [{"action": "search", "query": "RAG"}, abstain]
    with model_session(tmp_path, scripted(actions)) as client:
        result = search(client, mode="model", document_ids=scope).json()
        assert result["status"] == "not_found" and result["citations"] == [] and result["entries"] == []
        assert "必须隐藏" not in result["answer"]
        assert result["scope_document_ids"] == scope and result["model_calls"] == 2


def test_model_cannot_change_document_scope_in_search_action(tmp_path):
    action = {"action": "search", "query": "RAG", "document_ids": ["retrieval-guide"]}
    with model_session(tmp_path, scripted([action])) as client:
        response = search(client, mode="model", document_ids=["weekly-review"])
        assert response.status_code == 502
        assert response.json()["detail"]["code"] == "invalid_action"


@pytest.mark.parametrize("chunk_id", ["made-up:v1:0", "retrieval-guide:v1:0", "weekly-review:v1:0"])
def test_read_cannot_use_undiscovered_or_out_of_scope_chunk(tmp_path, chunk_id):
    with model_session(tmp_path, scripted([{"action": "read", "chunk_ids": [chunk_id]}])) as client:
        response = search(client, mode="model", document_ids=["weekly-review"])
        assert response.status_code == 502
        assert response.json()["detail"]["code"] == "unknown_chunk"


def test_final_cannot_cite_search_preview_without_read(tmp_path):
    def unread_final(request):
        return {"action": "final", "answer": "不能直接引用摘要", "citation_ids": [last_tool(request)["result"][0]["citation_id"]], "entry_ids": []}
    with model_session(tmp_path, scripted([good_sequence()[0], unread_final])) as client:
        response = search(client, mode="model")
        assert response.status_code == 502 and response.json()["detail"]["code"] == "unread_citation"


def test_abstention_cannot_recommend_unsupported_entry(tmp_path):
    action = {"action": "final", "answer": "无证据", "citation_ids": [], "entry_ids": ["journal"]}
    with model_session(tmp_path, scripted([action])) as client:
        response = search(client, mode="model")
        assert response.status_code == 502 and response.json()["detail"]["code"] == "unsupported_entry"


def test_max_steps_stops_model_without_fake_success(tmp_path):
    seen = []
    actions = [{"action": "search", "query": "每周复盘"}] * 3
    with model_session(tmp_path, scripted(actions, seen), max_steps=3) as client:
        response = search(client, mode="model")
        detail = response.json()["detail"]
        assert response.status_code == 502 and detail["code"] == "max_steps"
        assert len(seen) == detail["model_calls"] == 3
        assert [item["step"] for item in detail["trace"]] == ["scope", "search", "search", "search", "rejected"]
        assert "citations" not in detail and "answer" not in detail


@pytest.mark.parametrize("max_steps", [2, 13])
def test_invalid_step_configuration_is_rejected(max_steps):
    with pytest.raises(ValueError, match="MODEL_MAX_STEPS"):
        ModelConfig(max_steps=max_steps)


@pytest.mark.parametrize("timing", ["before_read", "before_final"])
def test_agent_rejects_document_changed_between_tool_steps(tmp_path, timing):
    def change_then_read(request):
        if timing == "before_read":
            client.app.state.store.put("weekly-review", "new title", "new body", [])
        return read_first(request)
    def change_then_final(request):
        if timing == "before_final":
            client.app.state.store.put("weekly-review", "new title", "new body", [])
        return final_first(request)
    actions = [good_sequence()[0], change_then_read, change_then_final]
    with model_session(tmp_path, scripted(actions)) as client:
        response = search(client, mode="model", document_ids=["weekly-review"])
        assert response.status_code == 502
        assert response.json()["detail"]["code"] == ("stale_chunk" if timing == "before_read" else "invalid_result")


@pytest.mark.parametrize("failure", ["timeout", "http", "invalid_json"])
def test_model_failures_are_visible_without_fallback(tmp_path, failure):
    def handler(request):
        if failure == "timeout": raise httpx.ReadTimeout("internal diagnostic should not leak", request=request)
        if failure == "http": return httpx.Response(503, text="secret internal diagnostic")
        return httpx.Response(200, json={"choices": [{"message": {"content": "not json"}}]})
    with model_session(tmp_path, handler) as client:
        response = search(client, mode="model")
        assert response.status_code == 502
        assert "internal diagnostic" not in response.text
        assert response.json()["detail"]["model_called"] is True
        assert response.json()["detail"]["code"] == "model_failure"


@pytest.mark.parametrize("mutation", ["updated", "deleted", "forged_quote", "outside_scope"])
def test_protocol_rejects_stale_and_forged_evidence(client, mutation):
    store = client.app.state.store
    evidence = store.retrieve("每周复盘", ["weekly-review"])
    proposal = Proposal(answer="test", citation_ids=[evidence[0]["citation_id"]], entry_ids=[])
    scope = ["weekly-review"]
    if mutation == "updated": store.put("weekly-review", "changed", "changed content", [])
    if mutation == "deleted": store.delete("weekly-review")
    if mutation == "forged_quote": evidence[0]["quote"] = "fabricated quotation"
    if mutation == "outside_scope": scope = ["learning-notes"]
    with pytest.raises(InvalidResult):
        validate_result(proposal, evidence, scope, store.entries(), store)


def test_ui_and_workspace_routes(client):
    assert client.get("/").status_code == 200
    assert "知径" in client.get("/").text
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/workspace/journal").status_code == 200
    assert client.get("/api/workspace/journal").json()["documents"][0]["id"] == "weekly-review"
