"""示例：斜杠命令 + 程序路由。

聊天里输入 /roll 3d6 直接出结果（不经过模型）；
程序可以 POST /api/plugins/invoke {file:"demo_commands.py", route:"last"} 读最近一次结果。
"""
import random

DESCRIPTION = "演示插件：斜杠命令 /roll 与 /pick，外加一个程序调用口"
COMMANDS = [
    {"name": "roll", "description": "掷骰子，如 /roll 3d6",
     "execute": lambda text, ctx: _roll(text or "1d6")},
    {"name": "pick", "description": "从选项里挑一个，如 /pick 苹果 香蕉 梨",
     "execute": lambda text, ctx: f"就选「{random.choice(text.split())}」" if text.split() else "给点选项呗"},
    {"name": "echo", "description": "原样复述，/echo 你好",
     "execute": lambda text, ctx: text or "（空）"},
]

_LAST = {"route": "（无）", "params": {}, "result": ""}


def _roll(spec):
    try:
        n, rest = spec.lower().split("d")
        sides = int(rest)
        rolls = [random.randint(1, sides) for _ in range(max(1, min(int(n), 20)))]
        return f"{spec} → {' + '.join(map(str, rolls))} = {sum(rolls)}"
    except Exception:
        return "格式：/roll 次数d面数，例如 /roll 3d6"


ROUTES = [
    {"path": "last", "methods": ["GET"],
     "fn": lambda params, ctx: _LAST},
    {"path": "roll", "methods": ["POST"],
     "fn": lambda params, ctx: {"result": _roll((params or {}).get("spec") or "1d6")},
     },
]


def on_message(evt):
    pass
