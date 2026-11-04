"""无网络冒烟测试：假模型 + 真工具，跑一遍代理循环和几个接口。

运行：cd /opt && .venv/bin/python -m agent.tests.smoke
"""
from __future__ import annotations

import json
import os
import sys
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from agent.core.loop import Agent, estimate_tokens
from agent.core.tools import REGISTRY, set_disabled, set_scope

FAILS: list[str] = []


def check(cond: bool, label: str) -> None:
    print(("  ok   " if cond else "  FAIL ") + label)
    if not cond:
        FAILS.append(label)


class FakeWindow:
    def __init__(self, msgs=None):
        self.msgs = list(msgs or [])
        self.summary = ""
        self.limit = 20

    def messages(self):
        return list(self.msgs)

    def add(self, m):
        self.msgs.append(m)


class ScriptLLM:
    """按脚本逐轮吐事件。"""

    def __init__(self, rounds):
        self.rounds = rounds
        self.i = 0

    def chat_stream(self, messages, tools=None):
        rnd = self.rounds[min(self.i, len(self.rounds) - 1)]
        self.i += 1
        for ev in rnd:
            yield ev


def tool_round(calls):
    events = [{"type": "delta", "content": "先算一下。"}]
    msg = {"role": "assistant", "content": "先算一下。",
           "tool_calls": [
               {"id": f"call_{i}", "type": "function",
                "function": {"name": n, "arguments": json.dumps(a, ensure_ascii=False)}}
               for i, (n, a) in enumerate(calls)
           ]}
    events.append({"type": "message", "message": msg})
    return events


def text_round(text):
    return [{"type": "delta", "content": text},
            {"type": "message", "message": {"role": "assistant", "content": text}}]


def test_parallel_tools():
    print("并行工具：")
    with tempfile.TemporaryDirectory() as tmp:
        old = os.environ.get("AGENT_DATA")
        os.environ["AGENT_DATA"] = tmp
        window = FakeWindow()
        llm = ScriptLLM([
            tool_round([("calc", {"expression": "2**10"}),
                        ("current_time", {"tz_offset": 8}),
                        ("calc", {"expression": "1+1"})]),
            text_round("算完了。"),
        ])
        logged = []
        agent = Agent(llm, window, max_steps=4, readonly=False,
                      tool_hook=logged.append)
        events = list(agent.run("算一下"))
        calls = [e for e in events if e["type"] == "tool_call"]
        results = [e for e in events if e["type"] == "tool_result"]
        check(len(calls) == 3, "三个 tool_call 都发出")
        check(len(results) == 3, "三个 tool_result 都回来")
        check(all("ms" in r for r in results), "结果带耗时")
        check([r["name"] for r in results].count("calc") == 2, "同名工具各算各的")
        check(any("1024" in (r["result"] or "") for r in results), "calc 结果正确")
        check(window.msgs[-1]["content"] == "算完了。", "最终回答落进历史")
        check(len(logged) == 3 and all("ok" in r for r in logged), "工具审计钩子收到 3 条")
        if old is None:
            os.environ.pop("AGENT_DATA", None)
        else:
            os.environ["AGENT_DATA"] = old


def test_spill_and_trim():
    print("超长输出 / 上下文预算：")
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["AGENT_DATA"] = tmp
        window = FakeWindow([{"role": "user", "content": "很长" * 4000},
                             {"role": "assistant", "content": "嗯" * 4000}])
        llm = ScriptLLM([text_round("收到")])
        agent = Agent(llm, window, context_budget=2000, readonly=False)
        events = list(agent.run("在吗"))
        check(agent.last_trimmed >= 0, "裁剪计数可读")
        joined = json.dumps(events, ensure_ascii=False)
        check("历史太长" in joined, "触发裁剪提示")

        # 直接测落盘
        out = agent._spill("big", "x" * 20000)
        check("已截断" in out and len(out) < 20000, "超长工具输出被截断")
        files = os.listdir(os.path.join(tmp, "tool_outputs"))
        check(len(files) == 1, "全文确实落到 tool_outputs")


def test_estimate():
    print("token 估算：")
    check(estimate_tokens("") == 0, "空串为 0")
    check(0 < estimate_tokens("你好世界") < 10, "中文估算在合理范围")
    check(estimate_tokens("hello world") < estimate_tokens("你好你好你好"), "中文更费 token")


def test_retry():
    print("失败重试：")
    from agent.core import llm as llm_mod

    class Resp:
        def __init__(self, status, lines=None):
            self.status_code = status
            self.headers = {"content-type": "text/event-stream"}
            self._lines = lines or []

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"error":"busy"}'

        def iter_lines(self):
            for ln in self._lines:
                yield ln

    seq = [Resp(503), Resp(200, [
        'data: {"choices":[{"delta":{"content":"好"}}]}',
        'data: [DONE]'])]

    class Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def stream(self, *a, **k):
            return seq.pop(0)

    old_client, old_sleep = llm_mod.httpx.Client, llm_mod.time.sleep
    llm_mod.httpx.Client = lambda *a, **k: Client()
    llm_mod.time.sleep = lambda s: None
    try:
        gw = llm_mod.LLM({"base_url": "https://example.test/v1", "name": "m",
                          "api_key": "k"})
        events = list(gw.chat_stream([{"role": "user", "content": "hi"}]))
        check(any(e["type"] == "retry" for e in events), "遇到 503 会发 retry 事件")
        check(any(e.get("content") == "好" for e in events), "重试后正常收到内容")
        check(events[-1]["type"] == "message", "最后仍产出完整 message")
    finally:
        llm_mod.httpx.Client, llm_mod.time.sleep = old_client, old_sleep


def test_api():
    print("HTTP 接口：")
    from fastapi.testclient import TestClient
    from agent.api import server

    client = TestClient(server.app)
    r = client.get("/api/health")
    check(r.status_code == 200, "health 免鉴权可访问")
    check(r.json().get("version") == server.VERSION, "health 版本号一致")

    r = client.get("/api/tools")
    names = {t["name"] for t in r.json()["tools"]}
    check({"append_file", "run_shell", "http_post_json"} <= names, "新工具已注册")

    r = client.get("/api/tool_outputs")
    check(r.status_code == 200, "tool_outputs 列表可用")

    r = client.post("/api/chat/stop", json={"session_id": "nope"})
    check(r.json().get("ok") is False, "没有任务时 stop 返回 ok=false")

    r = client.get("/api/sessions/search", params={"q": "绝对不存在的词zzz"})
    check(r.status_code == 200 and r.json()["count"] == 0, "全文搜索空结果正常")


def test_errors():
    print("错误翻译：")
    from agent.core.llm import explain_status
    check("密钥" in explain_status(401, "invalid api key"), "401 提示密钥问题")
    check("v1" in explain_status(404, "not found"), "404 提示地址问题")
    check("限流" in explain_status(429, ""), "429 提示限流")
    check(explain_status(418, "") != "", "未知状态也有兜底文案")


def test_api2():
    print("会话操作 / 导出 / 配置钳制：")
    import pathlib
    from fastapi.testclient import TestClient
    from agent.api import server

    with tempfile.TemporaryDirectory() as tmp:
        old_data = server.DATA_DIR
        old_env = os.environ.get("AGENT_DATA")
        server.DATA_DIR = pathlib.Path(tmp)
        os.environ["AGENT_DATA"] = tmp
        try:
            client = TestClient(server.app)
            w = server.get_window("zzsmoke")
            w.add({"role": "user", "content": "问一句"})
            w.add({"role": "assistant", "content": "答一句"})

            r = client.post("/api/session/zzsmoke/undo")
            check(r.json().get("removed") == 2, "undo 撤掉一问一答")
            h = client.get("/api/history", params={"session_id": "zzsmoke"}).json()
            check(h["messages"] == [], "undo 后历史为空")

            r = client.post("/api/session/zzsmoke/undo")
            check(r.json().get("ok") is False, "空历史 undo 不报错")

            w.add({"role": "user", "content": "导出我"})
            logdir = pathlib.Path(tmp) / "tool_logs"
            logdir.mkdir(parents=True, exist_ok=True)
            (logdir / "2026-01-01.jsonl").write_text(
                '{"ts":1,"session":"zz","name":"calc","ms":5,"ok":true,"result":"2"}\n',
                encoding="utf-8")
            r = client.get("/api/tool_logs")
            check(r.json()["count"] == 1, "工具审计日志可读")
            r = client.get("/api/tool_logs/summary")
            check(r.json()["tools"][0]["name"] == "calc", "审计汇总正常")

            old_log = logdir / "old.jsonl"
            old_log.write_text("{}", encoding="utf-8")
            os.utime(old_log, (0, 0))
            server._prune_data(30)
            check(not old_log.exists(), "过期审计日志被清理")
            check((logdir / "2026-01-01.jsonl").exists(), "近期日志保留")

            r = client.get("/api/context", params={"session_id": "zzsmoke"})
            check(r.status_code == 200 and "percent" in r.json(), "上下文水位接口正常")

            r = client.post("/api/chat", json={"message": "长" * 20001})
            check(r.status_code == 413, "超长消息被拒绝（413）")

            r = client.get("/api/export", params={"session_id": "zzsmoke", "fmt": "html"})
            check(r.status_code == 200 and "text/html" in r.headers["content-type"],
                  "HTML 导出可用")
            check("导出我" in r.text, "HTML 里有消息内容")

            spec = {"temperature": (0, 2, float), "top_p": (0, 1, float),
                    "max_steps": (1, 30, int)}
            sample = {"temperature": 99, "top_p": -3, "max_steps": 999, "x": "坏值"}
            server._clamp_fields(sample, spec)
            check(sample["temperature"] == 2.0, "temperature 被钳到 2")
            check(sample["top_p"] == 0.0, "top_p 被钳到 0")
            check(sample["max_steps"] == 30, "max_steps 被钳到 30")
            check(sample.get("x") == "坏值", "范围外的字段不动")
        finally:
            server.DATA_DIR = old_data
            if old_env is None:
                os.environ.pop("AGENT_DATA", None)
            else:
                os.environ["AGENT_DATA"] = old_env


def test_tool_schema():
    print("工具 schema：")
    set_disabled([])
    set_scope("workspace")
    for name, entry in REGISTRY.items():
        schema = entry["parameters"]
        check(schema.get("type") == "object", f"{name} schema 是 object")
        for p in (schema.get("required") or []):
            check(p in schema["properties"], f"{name}.{p} 在 properties 里")
    print(f"  共 {len(REGISTRY)} 个工具")


if __name__ == "__main__":
    test_estimate()
    test_parallel_tools()
    test_spill_and_trim()
    test_tool_schema()
    test_retry()
    test_errors()
    test_api()
    test_api2()
    print()
    if FAILS:
        print(f"{len(FAILS)} 项失败：")
        for f in FAILS:
            print(" -", f)
        sys.exit(1)
    print("全部通过 ✓")