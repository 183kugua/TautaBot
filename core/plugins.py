"""tautaBot 插件核心：plugins/ 目录下每个 .py 都能做很多事。

插件可用能力（约定即文档）：
  TOOLS = [{"name","description","params","mutating","execute"}]   # 注册多个工具
  execute(args, ctx)   # 单工具写法（ctx 含 config/data_dir/log）
  on_load(ctx) / on_unload()          # 生命周期
  on_message(evt) / on_response(evt)  # 对话事件广播（只读观察）
  INTERVAL = 60 + tick(ctx)           # 定时任务（秒，>=5 才生效）
  ctx = {config, data_dir, log}       # 插件配置、数据目录、专用 logger
"""
from __future__ import annotations

import importlib.util
import logging
import os
import re
import threading

log = logging.getLogger("tauta.plugins")
_TICKERS: dict[str, threading.Event] = {}
_HOOKS: dict[str, dict] = {}
_LOADED: dict[str, object] = {}


def _tool_name(path: str) -> str:
    base = os.path.splitext(os.path.basename(path))[0]
    return re.sub(r"[^a-z0-9_]", "_", base.lower())


def _takes_cfg(fn) -> bool:
    import inspect
    try:
        return len(inspect.signature(fn).parameters) >= 2
    except Exception:  # noqa: BLE001
        return False


def _make_ctx(name: str, file: str, conf: dict, data_root: str) -> dict:
    dd = os.path.join(data_root, "plugin_data", name)
    os.makedirs(dd, exist_ok=True)
    return {"config": conf or {}, "data_dir": dd,
            "log": logging.getLogger(f"tauta.plugins.{name}")}


def _collect_tools(name, mod, file, ctx) -> list[dict]:
    tools = []
    spec_list = getattr(mod, "TOOLS", None)
    if isinstance(spec_list, list) and spec_list:
        for i, t in enumerate(spec_list):
            f = t.get("execute")
            if not callable(f):
                raise ValueError(f"TOOLS[{i}].execute 不可调用")
            cfg = ctx["config"]
            def run(args, _f=f, _c=cfg, _two=_takes_cfg(f)):
                return str(_f(args or {}, _c) if _two else _f(args or {}))
            tools.append({
                "name": str(t.get("name") or f"{name}_{i}"),
                "description": (t.get("description") or "").strip() or "（插件没写说明）",
                "parameters": t.get("params") or {"type": "object", "properties": {}},
                "mutating": bool(t.get("mutating", getattr(mod, "MUTATING", False))),
                "fn": run,
            })
    else:
        f = getattr(mod, "execute", None)
        if not callable(f):
            raise ValueError("缺少 execute(args) 或 TOOLS 列表")
        cfg = ctx["config"]
        def run1(args, _f=f, _c=cfg, _two=_takes_cfg(f)):
            return str(_f(args or {}, _c) if _two else _f(args or {}))
        tools.append({"name": name,
                      "description": (getattr(mod, "DESCRIPTION", "") or "").strip()
                                     or "（插件没写说明）",
                      "parameters": getattr(mod, "PARAMS", None)
                                    or {"type": "object", "properties": {}},
                      "mutating": bool(getattr(mod, "MUTATING", False)),
                      "fn": run1})
    for t in tools:
        t.update({"plugin": True, "plugin_file": file})
    return tools


def _config_schema(mod):
    """插件可声明 CONFIG_SCHEMA（JSON Schema），管理页据此渲染提示。"""
    sch = getattr(mod, "CONFIG_SCHEMA", None)
    return sch if isinstance(sch, dict) else None


def stop_ticker(file: str) -> None:
    ev = _TICKERS.pop(file, None)
    if ev:
        ev.set()


def load(plugin_dir: str, registry: dict,
         conf: dict | None = None, data_root: str = "/opt/agent/data",
         enabled_check=None) -> tuple[list, list]:
    """扫描目录：注册多工具、挂钩子、起定时线程。返回 (loaded, errors)。"""
    loaded, errors = [], []
    conf = conf or {}
    if not os.path.isdir(plugin_dir):
        os.makedirs(plugin_dir, exist_ok=True)
        return loaded, errors

    for file in list(_HOOKS):                       # 卸载旧插件：停 ticker、调 on_unload
        stop_ticker(file)
        f = _HOOKS[file].get("on_unload")
        if f:
            try:
                f()
            except Exception as e:  # noqa: BLE001
                log.warning("on_unload %s: %s", file, e)
    _HOOKS.clear()
    for name in [n for n, t in list(registry.items()) if t.get("plugin")]:
        registry.pop(name, None)

    for fn in sorted(os.listdir(plugin_dir)):
        if not fn.endswith(".py") or fn.startswith("_"):
            continue
        path = os.path.join(plugin_dir, fn)
        name = _tool_name(path)
        try:
            spec = importlib.util.spec_from_file_location("tauta_plugin_" + name, path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)

            pconf = (conf.get(fn) or {})
            ctx = _make_ctx(name, fn, pconf.get("config") or {}, data_root)
            tools = _collect_tools(name, mod, fn, ctx)

            taken = [t["name"] for t in tools
                     if t["name"] in registry and not registry[t["name"]].get("plugin")]
            if taken:
                raise ValueError(f"与内置工具重名：{', '.join(taken)}，已让位")
            for t in tools:
                registry[t["name"]] = t
            registry.setdefault("__meta__", {})  # 占位防误用

            hooks = {k: f for k in ("on_load", "on_unload", "on_message", "on_response")
                     if callable(getattr(mod, k, None))}
            _HOOKS[fn] = hooks
            for t in tools:
                t["plugin_config_schema"] = _config_schema(mod)
            _LOADED[fn] = mod
            f = hooks.get("on_load")
            if f:
                try:
                    f(ctx)
                except Exception as e:  # noqa: BLE001
                    log.warning("on_load %s: %s", fn, e)

            interval = int(getattr(mod, "INTERVAL", 0) or 0)
            tick = getattr(mod, "tick", None)
            if interval >= 5 and callable(tick):
                stop = threading.Event()
                _TICKERS[fn] = stop

                def work(_tick=tick, _ctx=ctx, _stop=stop, _file=fn,
                         _chk=enabled_check, _interval=interval):
                    while not _stop.wait(_interval):
                        try:
                            if _chk and not _chk(_file):
                                continue
                            _tick(_ctx)
                        except Exception as e:  # noqa: BLE001
                            log.warning("tick %s: %s", _file, e)
                threading.Thread(target=work, daemon=True,
                                 name=f"tauta-tick-{name}").start()

            loaded.append(name)
            log.info("plugin loaded: %s (%s) tools=%d hooks=%s interval=%d",
                     name, fn, len(tools), list(hooks), interval if interval >= 5 else 0)
        except Exception as e:  # noqa: BLE001
            errors.append({"file": fn, "error": str(e)[:200]})
            log.warning("plugin failed: %s → %s", fn, e)
    return loaded, errors


def emit(hook: str, payload: dict) -> None:
    """向所有插件广播对话事件，异常一律吞掉。"""
    for file, hs in list(_HOOKS.items()):
        f = hs.get(hook)
        if not f:
            continue
        try:
            f(dict(payload))
        except Exception as e:  # noqa: BLE001
            log.warning("hook %s@%s: %s", hook, file, e)


def meta(file: str) -> dict:
    return {"hooks": sorted(k for k in _HOOKS.get(file, {}) if k != "on_unload"),
            "interval": next((int(getattr(m, "INTERVAL", 0) or 0)
                              for f, m in _LOADED.items() if f == file
                              and int(getattr(m, "INTERVAL", 0) or 0) >= 5), 0)}
