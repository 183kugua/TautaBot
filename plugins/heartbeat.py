"""示例：展示插件全部能力。

- TOOLS 注册两个工具
- INTERVAL + tick 每 60 秒跳一次心跳（写进数据目录）
- on_message / on_response 观察对话
- ctx.config 可在 config.yaml 的 plugins.heartbeat.config 里调
"""
import json
import logging
import os
import time

LOG = logging.getLogger("tauta.plugins.heartbeat")

DESCRIPTION = "心跳演示：定时器 + 多工具 + 对话钩子"
INTERVAL = 60

TOOLS = [
    {"name": "heartbeat_now",
     "description": "看一眼心跳状态：跳了多少次、最近一次是什么时候",
     "params": {"type": "object", "properties": {}},
     "execute": lambda args, ctx: json.dumps(_status(ctx), ensure_ascii=False)},
    {"name": "heartbeat_reset",
     "description": "把心跳计数清零",
     "params": {"type": "object", "properties": {}},
     "mutating": True,
     "execute": lambda args, ctx: _reset(ctx)},
]


def _f(ctx):
    return os.path.join(ctx["data_dir"], "heartbeats.json")


def _status(ctx):
    try:
        with open(_f(ctx), "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {"beats": 0, "last": ""}


def _reset(ctx):
    with open(_f(ctx), "w", encoding="utf-8") as f:
        json.dump({"beats": 0, "last": ""}, f)
    return "心跳已清零"


def tick(ctx):
    st = _status(ctx)
    st["beats"] = st.get("beats", 0) + 1
    st["last"] = time.strftime("%Y-%m-%d %H:%M:%S")
    with open(_f(ctx), "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False)
    ctx["log"].info("beat %d", st["beats"])


def on_message(evt):
    LOG.info("seen: %.40s", evt.get("text", ""))


def on_response(evt):
    LOG.info("answered: %.40s", evt.get("text", ""))
