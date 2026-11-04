"""插件加载器：plugins/ 目录下每个 .py 就是一个插件（一个工具）。

插件约定（模块级，任选）：
  NAME        工具名，缺省用文件名（仅保留 [a-z0-9_]）
  DESCRIPTION 给模型看的说明
  PARAMS      JSON Schema，缺省 = 无参数
  MUTATING    True = 会改东西，只读模式下被拦
  execute(args: dict) -> str   工具逻辑，必须返回字符串
"""
from __future__ import annotations

import importlib.util
import logging
import os
import re

log = logging.getLogger("tauta.plugins")


def _tool_name(path: str) -> str:
    base = os.path.splitext(os.path.basename(path))[0]
    return re.sub(r"[^a-z0-9_]", "_", base.lower())


def load(plugin_dir: str, registry: dict) -> tuple[list, list]:
    """扫描目录注册插件，返回 (loaded, errors)。重复调用 = 热重载。"""
    loaded, errors = [], []
    if not os.path.isdir(plugin_dir):
        os.makedirs(plugin_dir, exist_ok=True)
        return loaded, errors

    for name in [n for n, t in list(registry.items()) if t.get("plugin")]:
        registry.pop(name, None)          # 先摘旧插件

    for fn in sorted(os.listdir(plugin_dir)):
        if not fn.endswith(".py") or fn.startswith("_"):
            continue
        path = os.path.join(plugin_dir, fn)
        name = _tool_name(path)
        try:
            spec = importlib.util.spec_from_file_location("tauta_plugin_" + name, path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)

            fn_exec = getattr(mod, "execute", None)
            if not callable(fn_exec):
                raise ValueError("缺少 execute(args) 函数")
            if name in registry and not registry[name].get("plugin"):
                raise ValueError(f"与内置工具 {name} 重名，已让位")

            def run(args, _f=fn_exec):
                return str(_f(args or {}))

            registry[name] = {
                "name": name,
                "description": (getattr(mod, "DESCRIPTION", "") or "").strip()
                               or "（插件没写说明）",
                "parameters": getattr(mod, "PARAMS", None)
                              or {"type": "object", "properties": {}},
                "mutating": bool(getattr(mod, "MUTATING", False)),
                "fn": run,
                "plugin": True,
                "plugin_file": fn,
            }
            loaded.append(name)
            log.info("plugin loaded: %s (%s)", name, fn)
        except Exception as e:  # noqa: BLE001
            errors.append({"file": fn, "error": str(e)[:200]})
            log.warning("plugin failed: %s → %s", fn, e)
    return loaded, errors
