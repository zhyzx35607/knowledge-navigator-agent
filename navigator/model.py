"""Optional model adapter. Failures never turn into a claimed successful answer."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx


class ModelFailure(Exception):
    pass


@dataclass(frozen=True)
class ModelConfig:
    base_url: str = ""
    model: str = ""
    api_key: str = ""
    timeout: float = 30
    max_steps: int = 6

    def __post_init__(self):
        if not 3 <= self.max_steps <= 12:
            raise ValueError("MODEL_MAX_STEPS 必须在 3 到 12 之间")
        if not 0 < self.timeout <= 120:
            raise ValueError("MODEL_TIMEOUT 必须大于 0 且不超过 120 秒")

    @classmethod
    def from_env(cls):
        return cls(os.getenv("MODEL_BASE_URL", ""), os.getenv("MODEL_NAME", ""),
                   os.getenv("MODEL_API_KEY", ""), float(os.getenv("MODEL_TIMEOUT", "30")),
                   int(os.getenv("MODEL_MAX_STEPS", "6")))

    @property
    def configured(self):
        try:
            parsed = urlparse(self.base_url)
            endpoint = httpx.URL(self.base_url.rstrip("/") + "/chat/completions")
            return bool(self.model and parsed.scheme in {"http", "https"} and endpoint.host
                        and (parsed.port is None or 0 < parsed.port <= 65535)
                        and not parsed.username and not parsed.password and not parsed.query
                        and not parsed.fragment)
        except (ValueError, httpx.InvalidURL):
            return False


class ModelClient:
    def __init__(self, config: ModelConfig, transport=None):
        self.config, self.transport = config, transport

    async def next_action(self, messages):
        if not self.config.configured:
            raise ModelFailure("尚未配置模型服务地址和模型名称")
        headers = {"Content-Type": "application/json"}
        if self.config.api_key:
            headers["Authorization"] = "Bearer " + self.config.api_key
        try:
            async with httpx.AsyncClient(timeout=self.config.timeout, transport=self.transport,
                                         follow_redirects=False, trust_env=False) as client:
                response = await client.post(self.config.base_url.rstrip("/") + "/chat/completions",
                    headers=headers, json={"model": self.config.model, "temperature": 0,
                    "messages": messages})
                response.raise_for_status()
                content = response.json()["choices"][0]["message"]["content"]
                if not isinstance(content, str) or len(content) > 20000:
                    raise ValueError("bad content")
                return json.loads(content)
        except httpx.TimeoutException as exc:
            raise ModelFailure("模型请求超时；未生成回答，请重试或手动选择本地检索") from exc
        except (httpx.HTTPError, httpx.InvalidURL, ValueError, KeyError, IndexError, TypeError) as exc:
            raise ModelFailure("模型服务失败或未返回有效 JSON；未生成回答") from exc
