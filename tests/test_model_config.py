import httpx
import pytest
from fastapi.testclient import TestClient

from navigator.main import create_app
from navigator.model import ModelClient, ModelConfig


@pytest.mark.parametrize("base_url", [
    "http://[bad",
    "http://model.test:bad/v1",
    "http://model.test:99999/v1",
    "http://model.test/private\npath",
])
def test_invalid_model_url_is_rejected_without_disclosing_configuration(tmp_path, base_url):
    calls = []

    def transport(request):
        calls.append(request)
        raise AssertionError("Invalid model configuration must not issue a request")

    adapter = ModelClient(ModelConfig(base_url, "private-model", "private-api-key"),
                          transport=httpx.MockTransport(transport))
    with TestClient(create_app(tmp_path / "invalid-url.db", model_client=adapter)) as client:
        health = client.get("/api/health")
        assert health.status_code == 200
        assert health.json()["model_configured"] is False
        response = client.post("/api/search", json={"query": "每周复盘", "mode": "model"})
        assert response.status_code == 502
        detail = response.json()["detail"]
        assert detail["code"] == "model_unconfigured"
        assert detail["message"] == "尚未配置模型服务地址和模型名称"
        assert detail["model_called"] is False and detail["model_calls"] == 0
        for secret in (base_url, "private-model", "private-api-key"):
            assert secret not in health.text and secret not in response.text
        assert calls == []


def test_transport_url_error_uses_sanitized_model_failure(tmp_path):
    def transport(request):
        raise httpx.InvalidURL("private-api-key in upstream URL diagnostic")

    adapter = ModelClient(ModelConfig("http://model.test/v1", "model", "private-api-key"),
                          transport=httpx.MockTransport(transport))
    with TestClient(create_app(tmp_path / "url-error.db", model_client=adapter)) as client:
        response = client.post("/api/search", json={"query": "每周复盘", "mode": "model"})
        assert response.status_code == 502
        assert response.json()["detail"]["code"] == "model_failure"
        assert response.json()["detail"]["model_called"] is True
        assert "private-api-key" not in response.text
        assert "upstream URL diagnostic" not in response.text
