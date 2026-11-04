"""代理循环：模型 ↔ 工具来回，事件以 dict 流式吐出。

0.6 起：
- 同一轮里的多个工具调用并行跑（默认 4 并发），按完成顺序回事件
- 每个工具结果带耗时 ms
- 超长工具结果自动落盘，只把头部喂回模型，省上下文
- 请求前按 token 预算裁剪旧消息
- 支持中途取消（should_stop）
"""
from __future__ import annotations

import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, Iterator

from .llm import LLMError
from .tools import REGISTRY, is_enabled, schemas

READONLY = os.environ.get("AGENT_READONLY", "0") == "1"
TOOL_TIMEOUT = float(os.environ.get("AGENT_TOOL_TIMEOUT", "30"))
PARALLEL_TOOLS = max(1, int(os.environ.get("AGENT_PARALLEL_TOOLS", "4")))
MAX_TOOL_OUTPUT = int(os.environ.get("AGENT_MAX_TOOL_OUTPUT", "6000"))


def estimate_tokens(text: str) -> int:
    """粗估 token：中日韩字符约 1.4 字/token，其余约 3.6 字/token。"""
    if not text:
        return 0
    cjk = sum(1 for ch in text if "\u2e80" <= ch <= "\u9fff")
    other = len(text) - cjk
    return int(cjk / 1.4 + other / 3.6) + 1


def _msg_tokens(m: dict) -> int:
    n = estimate_tokens(str(m.get("content") or ""))
    for img in m.get("images") or []:
        n += 800                       # 一张图按 ~800 token 估
    for call in m.get("tool_calls") or []:
        n += estimate_tokens(json.dumps(call, ensure_ascii=False))
    return n


def _run_with_timeout(fn, args: dict, timeout: float):
    """在独立线程里跑，超时先返回错误（线程只能善后，不强杀）。"""
    import threading

    box: dict = {}

    def work():
        try:
            box["ok"] = fn(**args)
        except Exception as e:  # noqa: BLE001
            box["err"] = e

    t = threading.Thread(target=work, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        raise TimeoutError(f"工具执行超过 {timeout:g} 秒")
    if "err" in box:
        raise box["err"]
    return box.get("ok")


class Agent:
    def __init__(self, llm, window, system_prompt: str = "",
                 max_steps: int = 8, max_tool_retries: int = 3,
                 tool_timeout: float | None = None, readonly: bool | None = None,
                 context_budget: int = 24000, max_tool_output: int | None = None,
                 tool_hook=None):
        self.llm = llm
        self.window = window
        self.system_prompt = system_prompt
        self.max_steps = max_steps
        self.max_tool_retries = max_tool_retries
        self.tool_timeout = TOOL_TIMEOUT if tool_timeout is None else tool_timeout
        self.readonly = READONLY if readonly is None else readonly
        self.context_budget = max(2000, int(context_budget or 24000))
        self.max_tool_output = (MAX_TOOL_OUTPUT if max_tool_output is None
                                else int(max_tool_output))
        self.fails: dict[str, int] = {}
        self._fail_lock = threading.Lock()
        self.tool_hook = tool_hook
        self.last_usage: dict | None = None
        self.last_trimmed = 0

    # ---------- 工具 ----------
    def _bump_fail(self, name: str) -> None:
        with self._fail_lock:
            self.fails[name] = self.fails.get(name, 0) + 1

    def _clear_fail(self, name: str) -> None:
        with self._fail_lock:
            self.fails.pop(name, None)

    def _spill(self, name: str, result: str) -> str:
        """太长就落盘，只把头部给模型。"""
        if len(result) <= self.max_tool_output:
            return result
        head = result[: self.max_tool_output // 2]
        tail = result[-800:]
        path = ""
        try:
            root = os.environ.get("AGENT_DATA", "/opt/agent/data")
            d = os.path.join(root, "tool_outputs")
            os.makedirs(d, exist_ok=True)
            stamp = time.strftime("%Y%m%d-%H%M%S")
            path = os.path.join(d, f"{stamp}-{name}-{os.getpid()}.txt")
            with open(path, "w", encoding="utf-8") as f:
                f.write(result)
        except OSError:
            path = ""
        note = (f"\n\n[输出共 {len(result)} 字，太长已截断"
                + (f"，全文在 {path}" if path else "") + "]")
        return head + "\n…\n" + tail + note

    def _call_tool(self, name: str, args: dict) -> str:
        entry = REGISTRY.get(name)
        if entry is None:
            return f"错误：没有名为 {name} 的工具"

        if not is_enabled(name):
            return f"错误：工具 {name} 已被停用，换别的办法或直接说明"

        if entry.get("mutating") and self.readonly:
            return f"错误：当前是只读模式，{name} 被禁用"

        with self._fail_lock:
            failed = self.fails.get(name, 0)
        if failed >= self.max_tool_retries:
            return (f"错误：工具 {name} 已连续失败 {self.max_tool_retries} 次，"
                    f"请换办法或直接告诉用户做不到")

        try:
            result = _run_with_timeout(entry["fn"], args, self.tool_timeout)
            self._clear_fail(name)
            return self._spill(name, "" if result is None else str(result))
        except Exception as e:  # 喂回模型，让它自己改
            self._bump_fail(name)
            return f"错误：{type(e).__name__}: {e}"

    # ---------- 历史组装 ----------
    @staticmethod
    def _to_llm(m: dict) -> dict:
        """历史里带图的用户消息，转成多模态 content 数组。"""
        imgs = m.get("images") or []
        if not imgs or m.get("role") != "user":
            return {"role": m.get("role"), "content": m.get("content")}
        parts: list[dict] = []
        if m.get("content"):
            parts.append({"type": "text", "text": m["content"]})
        parts.extend(
            {"type": "image_url", "image_url": {"url": u}} for u in imgs
        )
        return {"role": "user", "content": parts}

    def _history(self, head: list[dict]) -> list[dict]:
        """从新到旧装消息，装到预算为止；返回 (messages, 丢掉的条数)。"""
        used = sum(_msg_tokens(m) for m in head)
        picked: list[dict] = []
        dropped = 0
        for m in reversed(self.window.messages()):
            t = _msg_tokens(m)
            if used + t > self.context_budget and picked:
                dropped = len(self.window.messages()) - len(picked)
                break
            picked.append(self._to_llm(m))
            used += t
        else:
            dropped = len(self.window.messages()) - len(picked)
        picked.reverse()
        self.last_trimmed = dropped
        return head + picked

    def _system_messages(self) -> list[dict]:
        head: list[dict] = []
        if self.system_prompt:
            head.append({"role": "system", "content": self.system_prompt})
        summary = getattr(self.window, "summary", "")
        if summary:
            head.append({
                "role": "system",
                "content": "以下是更早对话的摘要，可以当作背景：\n" + summary,
            })
        return head

    # ---------- 主循环 ----------
    def run(self, user_text: str, images: list[str] | None = None,
            should_stop: Callable[[], bool] | None = None) -> Iterator[dict]:
        stopped = should_stop or (lambda: False)
        messages = self._history(self._system_messages())
        messages.append(self._to_llm(
            {"role": "user", "content": user_text, "images": images or []}))
        if self.last_trimmed:
            yield {"type": "notice",
                   "content": f"历史太长，这次只带了最近的消息（隐去 {self.last_trimmed} 条旧消息）"}

        final_text = ""
        think_all: list[str] = []
        calls_log: list[dict] = []
        try:
            for step in range(self.max_steps):
                if stopped():
                    yield {"type": "notice", "content": "已停止生成"}
                    return
                msg: dict | None = None
                spoken = ""

                yield {"type": "step", "step": step + 1,
                       "max": self.max_steps}

                for ev in self.llm.chat_stream(messages, tools=schemas()):
                    if stopped():
                        yield {"type": "notice", "content": "已停止生成"}
                        return
                    if ev["type"] == "delta":
                        spoken += ev["content"]
                        yield {"type": "delta", "content": ev["content"]}
                    elif ev["type"] == "message":
                        msg = ev["message"]
                    elif ev["type"] == "retry":
                        yield ev
                    elif ev["type"] == "thinking":
                        think_all.append(ev["content"])
                        yield ev
                    elif ev["type"] == "usage":
                        self.last_usage = ev["usage"]
                        yield {"type": "usage", "usage": ev["usage"]}

                if msg is None:
                    msg = {"role": "assistant", "content": spoken}
                if spoken:
                    yield {"type": "assistant_done", "content": spoken}
                messages.append(msg)

                calls = msg.get("tool_calls") or []
                if not calls:
                    final_text = msg.get("content") or spoken
                    break

                yield from self._run_calls(calls, messages, stopped)

            else:
                yield {"type": "notice",
                       "content": f"跑了 {self.max_steps} 步还没完，先停"}

        except LLMError as e:
            yield {"type": "error", "content": str(e)}
            return

        if final_text:
            entry = {"role": "user", "content": user_text}
            if images:
                entry["images"] = list(images)
            self.window.add(entry)
            a_msg = {"role": "assistant", "content": final_text}
            if think_all:
                a_msg["thinking"] = "".join(think_all)
            if calls_log:
                a_msg["calls"] = calls_log
            self.window.add(a_msg)

        limit = getattr(self.window, "limit", 20)
        if len(self.window.msgs) >= limit - 2:
            yield {"type": "notice",
                   "content": f"上下文快满了（已存 {len(self.window.msgs)} / {limit} 条）。"
                              f"可以点右上角「压缩」，把旧对话收成摘要。"}

        yield {"type": "done", "content": final_text}

    # ---------- 并行工具 ----------
    def _run_calls(self, calls: list[dict], messages: list[dict],
                   stopped) -> Iterator[dict]:
        jobs: list[dict] = []
        for i, call in enumerate(calls):
            fn = call.get("function", {})
            name = fn.get("name", "")
            raw = fn.get("arguments") or "{}"
            cid = call.get("id") or f"call_{i}"
            try:
                args = json.loads(raw)
                if not isinstance(args, dict):
                    raise ValueError("arguments 必须是对象")
            except (json.JSONDecodeError, ValueError) as e:
                jobs.append({"id": cid, "name": name, "args": {},
                             "bad": f"错误：arguments 不是合法 JSON（{e}）：{raw[:200]}"})
                continue
            jobs.append({"id": cid, "name": name, "args": args, "bad": ""})
            yield {"type": "tool_call", "id": cid, "name": name,
                   "args": args, "index": i, "total": len(calls)}

        running = [j for j in jobs if not j["bad"]]

        def one(job: dict) -> dict:
            t0 = time.monotonic()
            out = self._call_tool(job["name"], job["args"])
            rec = {"id": job["id"], "name": job["name"], "args": job["args"],
                   "result": out, "ms": int((time.monotonic() - t0) * 1000),
                   "ok": not str(out).startswith("错误")}
            calls_log.append({"name": rec["name"], "args": rec["args"],
                              "result": str(rec["result"])[:300],
                              "ok": rec["ok"]})
            if self.tool_hook:
                try:
                    self.tool_hook(rec)
                except Exception:  # 审计失败不能影响对话
                    pass
            return rec

        results: dict[str, dict] = {}
        if running:
            workers = min(PARALLEL_TOOLS, len(running))
            if workers > 1:
                with ThreadPoolExecutor(max_workers=workers) as pool:
                    futures = [pool.submit(one, j) for j in running]
                    for fut in as_completed(futures):
                        r = fut.result()
                        results[r["id"]] = r
                        yield {"type": "tool_result", **r}
            else:
                for j in running:
                    if stopped():
                        yield {"type": "notice", "content": "已停止生成"}
                        return
                    r = one(j)
                    results[r["id"]] = r
                    yield {"type": "tool_result", **r}

        for j in jobs:
            if j["bad"]:
                calls_log.append({"name": j["name"], "args": j["args"],
                                  "result": j["bad"], "ok": False})
                yield {"type": "tool_result", "id": j["id"], "name": j["name"],
                       "result": j["bad"], "ms": 0}
                outcome = j["bad"]
            else:
                outcome = results.get(j["id"], {}).get("result", "错误：工具没有返回")
            messages.append({
                "role": "tool",
                "tool_call_id": j["id"],
                "content": outcome or "（无输出）",
            })