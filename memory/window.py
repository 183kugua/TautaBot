"""短期记忆窗口 + 长期摘要，落盘，重启不丢。"""
from __future__ import annotations

import json
import os
import threading


class Window:
    def __init__(self, limit: int = 20, path: str | None = None):
        self.limit = limit
        self.path = path
        self.msgs: list[dict] = []
        self.summary: str = ""
        self.title: str = ""
        self._lock = threading.Lock()
        self._load()

    # ---------- 磁盘 ----------
    def _load(self) -> None:
        if not self.path or not os.path.exists(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            return

        if isinstance(data, list):          # 旧格式兼容
            self.msgs = data[-self.limit:]
        elif isinstance(data, dict):
            self.msgs = (data.get("messages") or [])[-self.limit:]
            self.summary = data.get("summary") or ""
            self.title = data.get("title") or ""

    def _save(self) -> None:
        if not self.path:
            return
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"title": self.title, "summary": self.summary,
                           "messages": self.msgs},
                          f, ensure_ascii=False, indent=1)
            os.replace(tmp, self.path)
        except OSError:
            pass

    # ---------- 接口 ----------
    def set_title(self, title: str) -> str:
        with self._lock:
            self.title = (title or "").strip()[:40]
            self._save()
        return self.title

    def messages(self) -> list[dict]:
        return list(self.msgs)

    def add(self, msg: dict) -> None:
        with self._lock:
            self.msgs.append(msg)
            self._trim()
            self._save()

    def _trim(self) -> None:
        if len(self.msgs) > self.limit:
            self.msgs = self.msgs[-self.limit:]

    def clear(self) -> None:
        with self._lock:
            self.msgs.clear()
            self.summary = ""
            self._save()

    def replace(self, msgs: list[dict], summary: str | None = None) -> None:
        with self._lock:
            self.msgs = msgs[-self.limit:]
            if summary is not None:
                self.summary = summary
            self._save()
