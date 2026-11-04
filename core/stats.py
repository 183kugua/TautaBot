"""极简用量统计：按天累计调用次数与 token。存 data/stats.json。"""
from __future__ import annotations

import json
import os
import threading
from datetime import date
from pathlib import Path

_lock = threading.Lock()


def _path() -> Path:
    root = Path(os.environ.get("AGENT_DATA", "/opt/agent/data"))
    return root / "stats.json"


def _load() -> dict:
    p = _path()
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def record(usage: dict | None = None, failed: bool = False) -> None:
    """记一次调用。usage 可以是 OpenAI 的 usage 字典，也可以为空。"""
    today = date.today().isoformat()
    with _lock:
        data = _load()
        day = data.setdefault(today, {"calls": 0, "failed": 0,
                                      "prompt_tokens": 0, "completion_tokens": 0})
        day["calls"] += 1
        if failed:
            day["failed"] += 1
        if usage:
            day["prompt_tokens"] += int(usage.get("prompt_tokens") or 0)
            day["completion_tokens"] += int(usage.get("completion_tokens") or 0)
        data["_total"] = data.get("_total") or {"calls": 0, "failed": 0,
                                                "prompt_tokens": 0,
                                                "completion_tokens": 0}
        data["_total"]["calls"] += 1
        if failed:
            data["_total"]["failed"] = data["_total"].get("failed", 0) + 1
        if usage:
            data["_total"]["prompt_tokens"] += int(usage.get("prompt_tokens") or 0)
            data["_total"]["completion_tokens"] += int(usage.get("completion_tokens") or 0)
        try:
            _path().parent.mkdir(parents=True, exist_ok=True)
            _path().write_text(json.dumps(data, ensure_ascii=False, indent=1),
                               encoding="utf-8")
        except OSError:
            pass


def record_failure() -> None:
    """只补记一次失败，不重复计调用次数（那次调用已经记过了）。"""
    today = date.today().isoformat()
    with _lock:
        data = _load()
        day = data.setdefault(today, {"calls": 0, "failed": 0,
                                      "prompt_tokens": 0, "completion_tokens": 0})
        day["failed"] += 1
        total = data.setdefault("_total", {"calls": 0, "failed": 0,
                                           "prompt_tokens": 0, "completion_tokens": 0})
        total["failed"] = total.get("failed", 0) + 1
        try:
            _path().parent.mkdir(parents=True, exist_ok=True)
            _path().write_text(json.dumps(data, ensure_ascii=False, indent=1),
                               encoding="utf-8")
        except OSError:
            pass


def snapshot() -> dict:
    data = _load()
    today = date.today().isoformat()
    total = data.get("_total") or {}
    total.setdefault("calls", 0)
    total.setdefault("failed", 0)
    total.setdefault("prompt_tokens", 0)
    total.setdefault("completion_tokens", 0)
    return {
        "today": data.get(today) or {"calls": 0, "failed": 0,
                                     "prompt_tokens": 0, "completion_tokens": 0},
        "total": total,
        "days": {k: v for k, v in sorted(data.items(), reverse=True) if not k.startswith("_")},
    }
