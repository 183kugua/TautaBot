"""多厂商网关：统一走 OpenAI 兼容的 /chat/completions，支持流式。"""
from __future__ import annotations

import json
import os
import time
from typing import Any, Iterator

import httpx

# 连接超时短、读超时长（等模型吐字）


class LLMError(RuntimeError):
    pass


def explain_status(status: int, body: str) -> str:
    """把服务商的英文错误翻成人能看懂的一句话。"""
    hint = {
        400: "请求被服务商拒绝（400），多半是模型名或参数不对",
        401: "密钥无效或没有权限（401），去设置里检查 API Key",
        403: "这个密钥没有访问权限（403）",
        404: "接口地址不对（404），检查 base_url 是否少了 /v1",
        413: "内容太长，服务商拒收（413）",
        429: "被服务商限流了（429），过一会儿再试",
        500: "服务商内部出错了（500）",
        502: "服务商网关出错（502）",
        503: "服务商暂时不可用（503）",
    }.get(status, f"服务商返回 {status}")
    detail = (body or "").strip().replace("\n", " ")[:180]
    return f"{hint}｜{detail}" if detail else hint


class LLM:
    def __init__(self, cfg: dict):
        self.base_url = str(cfg["base_url"]).rstrip("/")
        self.api_key = os.environ.get("AGENT_API_KEY") or cfg.get("api_key") or ""
        self.model = cfg.get("name", "gpt-4o-mini")
        self.temperature = cfg.get("temperature", 0.7)
        self.top_p = cfg.get("top_p")
        self.max_tokens = cfg.get("max_tokens")
        self.timeout = float(cfg.get("timeout", 300))
        self.extra_headers = cfg.get("extra_headers") or {} if isinstance(
            cfg.get("extra_headers"), dict) else {}

    # ---------- 内部 ----------
    def _payload(self, messages, tools, stream: bool) -> dict:
        if not self.api_key:
            raise LLMError("没配 api_key：填 config.yaml 或设 AGENT_API_KEY")
        p: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
        }
        if self.top_p is not None:
            p["top_p"] = float(self.top_p)
        if self.max_tokens:
            p["max_tokens"] = int(self.max_tokens)
        if tools:
            p["tools"] = [{"type": "function", "function": t} for t in tools]
        if stream:
            p["stream"] = True
            # 部分服务商不认 stream_options，失败后会自动摘掉重试
            if not getattr(self, "_no_stream_options", False):
                p["stream_options"] = {"include_usage": True}
        return p

    def _headers(self) -> dict:
        h = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        h.update(self.extra_headers)
        return h

    @property
    def _timeout(self) -> httpx.Timeout:
        return httpx.Timeout(connect=10.0, read=self.timeout, write=30.0, pool=10.0)

    @property
    def _url(self) -> str:
        return f"{self.base_url}/chat/completions"

    # ---------- 非流式 ----------
    def chat(self, messages: list[dict], tools: list[dict] | None = None) -> dict:
        payload = self._payload(messages, tools, False)
        try:
            with httpx.Client(timeout=self._timeout) as client:
                resp = client.post(self._url, json=payload, headers=self._headers())
        except httpx.HTTPError as e:
            raise LLMError(f"网络错误: {e}") from e

        if resp.status_code >= 400:
            raise LLMError(explain_status(resp.status_code, resp.text))

        try:
            msg = resp.json()["choices"][0]["message"]
        except Exception as e:
            raise LLMError(f"返回格式异常: {resp.text[:300]}") from e

        return self._normalize(msg)

    # ---------- 流式 ----------
    def chat_stream(self, messages: list[dict], tools: list[dict] | None = None,
                    _retry_usage: bool = True) -> Iterator[dict]:
        """产出 {"type":"delta","content":...}，最后产出 {"type":"message","message":{...}}。

        0.6：网络抖动 / 429 / 5xx 自动退避重试（已吐字后不再重试，避免重复）。
        """
        payload = self._payload(messages, tools, True)
        text: list[str] = []
        calls: dict[int, dict] = {}
        attempts = 3

        for attempt in range(1, attempts + 1):
            try:
                with httpx.Client(timeout=self._timeout) as client:
                    with client.stream("POST", self._url, json=payload,
                                       headers=self._headers()) as resp:
                        if resp.status_code >= 400:
                            body = resp.read().decode("utf-8", "replace")
                            low = body.lower()
                            if (_retry_usage and resp.status_code in (400, 422)
                                    and ("stream_options" in low or "usage" in low)):
                                self._no_stream_options = True
                                yield from self.chat_stream(messages, tools,
                                                            _retry_usage=False)
                                return
                            retryable = resp.status_code in (408, 409, 425, 429,
                                                             500, 502, 503, 504)
                            if retryable and attempt < attempts and not text:
                                wait = min(2 ** attempt, 8)
                                yield {"type": "retry",
                                       "content": f"服务商返回 {resp.status_code}，"
                                                  f"{wait} 秒后重试（第 {attempt} 次）"}
                                time.sleep(wait)
                                continue
                            raise LLMError(explain_status(resp.status_code, body))

                        for line in resp.iter_lines():
                            if not line or not line.startswith("data:"):
                                continue
                            data = line[5:].strip()
                            if data == "[DONE]":
                                break
                            try:
                                chunk = json.loads(data)
                            except json.JSONDecodeError:
                                continue

                            usage = chunk.get("usage")
                            if usage:
                                yield {"type": "usage", "usage": usage}

                            choices = chunk.get("choices") or []
                            if not choices:
                                continue
                            delta = choices[0].get("delta") or {}

                            think = delta.get("reasoning_content") or delta.get("reasoning")
                            if think:
                                yield {"type": "thinking", "content": think}

                            piece = delta.get("content")
                            if piece:
                                text.append(piece)
                                yield {"type": "delta", "content": piece}

                            for tc in delta.get("tool_calls") or []:
                                slot = calls.setdefault(tc.get("index", 0),
                                                        {"id": "", "name": "",
                                                         "arguments": ""})
                                if tc.get("id"):
                                    slot["id"] = tc["id"]
                                fn = tc.get("function") or {}
                                if fn.get("name"):
                                    slot["name"] += fn["name"]
                                if fn.get("arguments"):
                                    slot["arguments"] += fn["arguments"]
                break
            except httpx.HTTPError as e:
                if attempt < attempts and not text:
                    wait = min(2 ** attempt, 8)
                    yield {"type": "retry",
                           "content": f"网络抖动（{type(e).__name__}），"
                                      f"{wait} 秒后重试（第 {attempt} 次）"}
                    time.sleep(wait)
                    continue
                raise LLMError(f"网络错误: {e}") from e

        msg: dict[str, Any] = {"role": "assistant", "content": "".join(text)}
        if calls:
            msg["tool_calls"] = [
                {
                    "id": slot["id"] or f"call_{i}",
                    "type": "function",
                    "function": {"name": slot["name"], "arguments": slot["arguments"] or "{}"},
                }
                for i, slot in sorted(calls.items())
            ]
        yield {"type": "message", "message": msg}

    # ---------- 模型管理 ----------
    def list_models(self) -> dict:
        """拉取服务商的 /models 列表。"""
        if not self.api_key:
            raise LLMError("没配 api_key")
        try:
            with httpx.Client(timeout=20) as client:
                r = client.get(f"{self.base_url}/models", headers=self._headers())
        except httpx.HTTPError as e:
            raise LLMError(f"网络错误: {e}") from e
        if r.status_code >= 400:
            raise LLMError(f"{r.status_code}: {r.text[:300]}")

        try:
            data = r.json().get("data") or []
        except Exception as e:
            raise LLMError(f"返回格式异常: {e}") from e

        models = sorted(
            ({"id": m.get("id", ""), "owned": m.get("owned_by", "") or "—"}
             for m in data if m.get("id")),
            key=lambda x: x["id"],
        )
        return {"models": models, "count": len(models), "current": self.model}

    def test(self, prompt: str = "Reply with the single word: ok") -> dict:
        """最小请求，返回延迟与用量。不抛异常，返回 ok=False。"""
        import time as _t

        body = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 16,
            "temperature": 0,
            "stream": False,
        }
        started = _t.monotonic()
        try:
            with httpx.Client(timeout=30) as client:
                r = client.post(self._url, json=body, headers=self._headers())
        except httpx.HTTPError as e:
            return {"ok": False, "error": f"网络错误: {e}",
                    "latency_ms": int((_t.monotonic() - started) * 1000),
                    "model": self.model}

        elapsed = int((_t.monotonic() - started) * 1000)
        if r.status_code >= 400:
            return {"ok": False, "error": explain_status(r.status_code, r.text),
                    "latency_ms": elapsed, "model": self.model}

        try:
            data = r.json()
            reply = (data["choices"][0]["message"].get("content") or "").strip()
        except Exception as e:
            return {"ok": False, "error": f"返回格式异常: {e}",
                    "latency_ms": elapsed, "model": self.model}

        return {
            "ok": True,
            "model": self.model,
            "latency_ms": elapsed,
            "reply": reply[:200],
            "usage": data.get("usage") or {},
        }

    # ---------- 工具 ----------
    @staticmethod
    def _normalize(msg: dict) -> dict:
        out: dict[str, Any] = {"role": "assistant", "content": msg.get("content") or ""}
        if msg.get("tool_calls"):
            out["tool_calls"] = msg["tool_calls"]
        return out
