"""LLM 调用（OpenAI 兼容端点）。

- 不使用环境代理（trust_env=False）：本机 Clash 等系统代理会让回环 / 直连请求异常。
- 有界重试：连接失败与 5xx / 429 重试一次；4xx 立即失败（不重试风暴）。
- 空文本按失败处理（推理型模型可能把预算烧在 reasoning_content 上，重试时加预算）。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx

from .config import LLMConfig
from .log import get_logger

log = get_logger("isekai.llm")

#: 单次补全的预算上限。推理型模型会把预算烧在 reasoning 上并返回空文本，
#: 重试时按倍加预算（生成世界包这类长产物需要 ≥8K 预算）。
MAX_COMPLETION_BUDGET = 32768


class LLMError(Exception):
    def __init__(self, code: str, message: str, *, retryable: bool = True) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.retryable = retryable


class LLMClient:
    def __init__(self, cfg: LLMConfig) -> None:
        self.cfg = cfg
        self._client: httpx.AsyncClient | None = None

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.cfg.base_url.rstrip("/"),
                timeout=self.cfg.timeout_s,
                trust_env=False,
                headers={"Authorization": f"Bearer {self.cfg.api_key}"},
            )
        return self._client

    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        max_tokens: int | None = None,
        timeout: float | None = None,
        temperature: float | None = None,
    ) -> str:
        if not self.cfg.api_key:
            raise LLMError("llm_not_configured", "未配置 LLM API Key", retryable=False)

        budget = max_tokens or self.cfg.max_tokens
        # 生成整份世界包这类长产物需要更长的等待；按调用覆盖超时
        request_timeout = timeout or self.cfg.timeout_s
        # 结构化产物用更低的温度（默认温度按对话场景设定，JSON 容易出残句）
        heat = self.cfg.temperature if temperature is None else temperature
        last: LLMError | None = None
        for attempt in (0, 1):
            try:
                response = await self._http().post(
                    "/chat/completions",
                    timeout=request_timeout,
                    json={
                        "model": self.cfg.model,
                        "messages": messages,
                        "max_tokens": budget,
                        "temperature": heat,
                        "stream": False,
                    },
                )
            except httpx.HTTPError as exc:
                last = LLMError("llm_unreachable", f"{type(exc).__name__}", retryable=True)
                log.warning("llm transport error attempt=%s: %s", attempt, type(exc).__name__)
                if attempt == 0:
                    await asyncio.sleep(0.5)
                    continue
                raise last from exc

            if response.status_code == 429 or response.status_code >= 500:
                last = LLMError("llm_unavailable", f"HTTP {response.status_code}", retryable=True)
                log.warning("llm status %s attempt=%s", response.status_code, attempt)
                if attempt == 0:
                    await asyncio.sleep(0.5)
                    continue
                raise last

            if response.status_code >= 400:
                raise LLMError("llm_rejected", f"HTTP {response.status_code}", retryable=False)

            try:
                data = json.loads(response.content.decode("utf-8", "replace"))
                choice = data["choices"][0]
                content = choice["message"]["content"] or ""
                finish = choice.get("finish_reason")
            except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
                raise LLMError("llm_bad_response", "响应缺少 choices[0].message.content") from exc

            log.debug("llm done len=%s finish=%s budget=%s", len(content), finish, budget)
            if not content.strip():
                last = LLMError("empty_completion", "模型返回空文本", retryable=True)
                if attempt == 0:
                    budget = min(budget * 2, MAX_COMPLETION_BUDGET)
                    continue
                raise last
            if finish == "length":
                # 被长度上限截断：整段产物不可用，按可重试错误处理并提高预算
                last = LLMError("truncated_completion", f"输出被截断（{len(content)} 字符，预算 {budget}）", retryable=True)
                if attempt == 0:
                    budget = min(budget * 2, MAX_COMPLETION_BUDGET)
                    continue
                raise last
            return content.strip()

        raise last or LLMError("llm_failed", "未知失败", retryable=True)

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


class FakeLLM:
    """开发 / 测试用：按脚本返回文本，不联网（ISEKAI_LLM_FAKE=1 时启用）。"""

    def __init__(self, replies: list[str] | None = None, *, fail_with: LLMError | None = None) -> None:
        self.replies = list(replies or ["（占位回复）"])
        self.calls: list[list[dict[str, Any]]] = []
        self.fail_with = fail_with
        self.delay_s = 0.0
        self.cfg: Any = None  # 由设置面写入（settings.set 生效路径与真实客户端一致）

    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        max_tokens: int | None = None,
        timeout: float | None = None,
        temperature: float | None = None,
    ) -> str:
        self.calls.append(messages)
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        if self.fail_with is not None:
            raise self.fail_with
        index = min(len(self.calls) - 1, len(self.replies) - 1)
        return self.replies[index]

    async def aclose(self) -> None:
        return None
