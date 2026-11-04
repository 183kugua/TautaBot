"""掷骰子插件：roll sides=6 times=1 → 一组随机点数。"""
import random

DESCRIPTION = "掷骰子：roll(sides=6, times=1)，返回一组随机点数。"
PARAMS = {
    "type": "object",
    "properties": {
        "sides": {"type": "integer", "description": "骰子面数"},
        "times": {"type": "integer", "description": "掷几次（1-10）"},
    },
}


def execute(args):
    sides = int(args.get("sides") or 6)
    times = max(1, min(int(args.get("times") or 1), 10))
    rolls = [random.randint(1, sides) for _ in range(times)]
    return f"{times}d{sides} → " + " + ".join(map(str, rolls)) + f" = {sum(rolls)}"
