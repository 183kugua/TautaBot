"""tautaBot 插件核心：支持两种形态。

1. 目录包（推荐）—— plugins/<name>/：
     metadata.yaml     name / version / author / description
     main.py           代码（能力约定见下）
     config.yaml       插件自己的配置（ctx.config 从这读，管理页可视化编辑）
     requirements.txt  可选，依赖清单（「插件」页可一键安装）
     README.md         可选

2. 单文件（兼容）—— plugins/<name>.py，配置仍存全局 config.yaml 的 plugins 段。

main.py 能力约定：
  TOOLS = [{"name","description","params","mutating","execute"}]   # 注册多个工具
  execute(args, ctx)   # 单工具写法
  COMMANDS = [{"name","description","execute"}]                    # 斜杠命令
  ROUTES  = [{"path","fn","methods"}]                              # 程序调用口
  on_load(ctx) / on_unload() / on_message(evt) / on_response(evt)  # 钩子
  INTERVAL = 60 + tick(ctx)                                        # 定时任务
  CONFIG_SCHEMA = {...}                                            # 配置字段提示
  ctx = {config, data_dir, log}
"""
from __future__ import annotations

import importlib.util
import logging
import os
import re
import threading

import yaml

log = logging.getLogger("tauta.plugins")
_TICKERS: dict[str, threading.Event] = {}
_HOOKS: dict[str, dict] = {}
_LOADED: dict[str, object] = {}
_COMMANDS: dict[str, list] = {}
_ROUTES: dict[str, list] = {}
_META: dict[str, dict] = {}          # file -> {version, author, kind, requires}
_CFG_PATHS: dict[str, str] = {}      # file -> 插件配置文件路径（保存用）


def _tool_name(text: str) -> str:
    return re.sub(r"[^a-z0-9_]", "_", (text or "").lower())


def _read_yaml(path: str):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f)
    except (OSError, yaml.YAMLError):
        return None


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


def _load_module(path: str, unique: str):
    spec = importlib.util.spec_from_file_location("tauta_plugin_" + unique, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _collect_tools(name, mod, file, ctx) -> list[dict]:
    tools = []
    spec_list = getattr(mod, "TOOLS", None)
    if isinstance(spec_list, list) and spec_list:
        for i, t in enumerate(spec_list):
            f = t.get("execute")
            if not callable(f):
                raise ValueError(f"TOOLS[{i}].execute 不可调用")
            cfg = ctx["config"]

            def run(args=None, _f=f, _c=cfg, _two=_takes_cfg(f), **kw):
                if args is None and kw:
                    args = kw
                return str(_f(args or {}, _c) if _two else _f(args or {}))

            tools.append({
                "name": str(t.get("name") or f"{name}_{i}"),
                "description": (t.get("description") or "").strip() or "（没写说明）",
                "parameters": t.get("params") or {"type": "object", "properties": {}},
                "mutating": bool(t.get("mutating", getattr(mod, "MUTATING", False))),
                "fn": run,
            })
    elif callable(getattr(mod, "execute", None)):
        f = getattr(mod, "execute")
        cfg = ctx["config"]

        def run1(args=None, _f=f, _c=cfg, _two=_takes_cfg(f), **kw):
            if args is None and kw:
                args = kw
            return str(_f(args or {}, _c) if _two else _f(args or {}))

        tools.append({"name": name,
                      "description": (getattr(mod, "DESCRIPTION", "") or "").strip()
                                     or "（没写说明）",
                      "parameters": getattr(mod, "PARAMS", None)
                                    or {"type": "object", "properties": {}},
                      "mutating": bool(getattr(mod, "MUTATING", False)),
                      "fn": run1})
    for t in tools:
        t.update({"plugin": True, "plugin_file": file})
    return tools


def _collect_extras(name, mod, file, ctx, tools) -> None:
    hooks = {k: getattr(mod, k) for k in ("on_load", "on_unload", "on_message", "on_response")
             if callable(getattr(mod, k, None))}
    _HOOKS[file] = hooks

    cmds = []
    for c in getattr(mod, "COMMANDS", None) or []:
        f = c.get("execute")
        if not callable(f):
            raise ValueError(f"命令 {c.get('name')} 的 execute 不可调用")
        two = _takes_cfg(f)
        cmds.append({
            "name": str(c.get("name") or "").lstrip("/"),
            "description": (c.get("description") or "").strip(),
            "execute": (lambda text, _f=f, _c=ctx["config"], _t=two:
                        str(_f(text, _c) if _t else _f(text))),
        })
    if cmds:
        _COMMANDS[file] = cmds

    rts = []
    for r in getattr(mod, "ROUTES", None) or []:
        f = r.get("fn")
        if not callable(f):
            raise ValueError(f"路由 {r.get('path')} 的 fn 不可调用")
        two = _takes_cfg(f)
        rts.append({
            "path": str(r.get("path") or "").strip("/"),
            "methods": [m.upper() for m in (r.get("methods") or ["GET"])],
            "fn": (lambda params, _f=f, _c=ctx["config"], _t=two:
                   (_f(params, _c) if _t else _f(params))),
        })
    if rts:
        _ROUTES[file] = rts

    for t in tools:
        t["plugin_config_schema"] = _cfg_schema(mod)


def _cfg_schema(mod):
    sch = getattr(mod, "CONFIG_SCHEMA", None)
    return sch if isinstance(sch, dict) else None


def _start_ticker(name, mod, file, ctx, interval, enabled_check) -> None:
    tick = getattr(mod, "tick", None)
    if interval < 5 or not callable(tick):
        return
    stop = threading.Event()
    _TICKERS[file] = stop

    def work(_tick=tick, _ctx=ctx, _stop=stop, _file=file,
             _chk=enabled_check, _interval=interval):
        while not _stop.wait(_interval):
            try:
                if _chk and not _chk(_file):
                    continue
                _tick(_ctx)
            except Exception as e:  # noqa: BLE001
                log.warning("tick %s: %s", _file, e)

    threading.Thread(target=work, daemon=True, name=f"tauta-tick-{name}").start()


def stop_ticker(file: str) -> None:
    ev = _TICKERS.pop(file, None)
    if ev:
        ev.set()


def _unload_all(registry: dict) -> None:
    for file in list(_HOOKS):
        stop_ticker(file)
        f = _HOOKS[file].get("on_unload")
        if f:
            try:
                f()
            except Exception as e:  # noqa: BLE001
                log.warning("on_unload %s: %s", file, e)
    _HOOKS.clear(); _COMMANDS.clear(); _ROUTES.clear()
    _META.clear(); _CFG_PATHS.clear()
    for name in [n for n, t in list(registry.items()) if t.get("plugin")]:
        registry.pop(name, None)


def _register_one(key: str, main_path: str, ctx: dict, registry: dict,
                  meta: dict, requires: list, enabled_check) -> None:
    name = _tool_name(meta.get("name") or key)
    mod = _load_module(main_path, name)
    tools = _collect_tools(name, mod, key, ctx)
    taken = [t["name"] for t in tools
             if t["name"] in registry and not registry[t["name"]].get("plugin")]
    if taken:
        raise ValueError(f"与内置工具重名：{', '.join(taken)}，已让位")
    for t in tools:
        registry[t["name"]] = t

    _collect_extras(name, mod, key, ctx, tools)
    _META[key] = {"version": str(meta.get("version") or "0.1"),
                  "author": str(meta.get("author") or ""),
                  "description": str(meta.get("description")
                                     or getattr(mod, "DESCRIPTION", "") or ""),
                  "kind": "package" if main_path.endswith(os.sep + "main.py") or
                          os.path.basename(main_path) == "main.py" else "file",
                  "requires": requires}
    _CFG_PATHS[key] = os.path.join(os.path.dirname(main_path), "config.yaml") \
        if os.path.basename(main_path) == "main.py" else ""

    f = _HOOKS.get(key, {}).get("on_load")
    if f:
        try:
            f(ctx)
        except Exception as e:  # noqa: BLE001
            log.warning("on_load %s: %s", key, e)

    interval = int(getattr(mod, "INTERVAL", 0) or 0)
    _start_ticker(name, mod, key, ctx, interval, enabled_check)
    _LOADED[key] = mod
    log.info("plugin loaded: %s (%s) tools=%d interval=%d",
             key, meta.get("version", "?"), len(tools), interval if interval >= 5 else 0)


def load(plugin_dir: str, registry: dict,
         conf: dict | None = None, data_root: str = "/opt/agent/data",
         enabled_check=None) -> tuple[list, list]:
    """扫描插件目录：目录包 + 单文件两种形态。返回 (loaded, errors)。"""
    loaded, errors = [], []
    conf = conf or {}
    if not os.path.isdir(plugin_dir):
        os.makedirs(plugin_dir, exist_ok=True)
        return loaded, errors

    _unload_all(registry)

    entries = sorted(os.listdir(plugin_dir))
    packages = {e for e in entries
                if os.path.isdir(os.path.join(plugin_dir, e))
                and os.path.isfile(os.path.join(plugin_dir, e, "main.py"))}

    for fn in entries:
        if fn.startswith("_"):
            continue
        path = os.path.join(plugin_dir, fn)
        try:
            if fn in packages:                                     # ---- 目录包 ----
                meta = _read_yaml(os.path.join(path, "metadata.yaml")) or {}
                requires = [l.strip() for l in
                            open(os.path.join(path, "requirements.txt"), encoding="utf-8")
                            .read().splitlines() if l.strip() and not l.startswith("#")] \
                    if os.path.isfile(os.path.join(path, "requirements.txt")) else []
                pcfg = _read_yaml(os.path.join(path, "config.yaml")) or {}
                name = _tool_name(meta.get("name") or fn)
                ctx = _make_ctx(name, fn, pcfg, data_root)
                _register_one(fn, os.path.join(path, "main.py"), ctx, registry,
                              {"name": meta.get("name") or fn,
                               "version": meta.get("version"),
                               "author": meta.get("author"),
                               "description": meta.get("description")},
                              requires, enabled_check)
                _CFG_PATHS[fn] = os.path.join(path, "config.yaml")
                _META[fn]["kind"] = "package"
                loaded.append(fn)
                continue

            if not fn.endswith(".py"):                             # ---- 单文件 ----
                continue
            name = _tool_name(fn)
            mod = _load_module(path, name)
            ctx = _make_ctx(name, fn, (conf.get(fn) or {}).get("config") or {}, data_root)
            _register_one(fn, path, ctx, registry,
                          {"name": name, "description": getattr(mod, "DESCRIPTION", "")},
                          [], enabled_check)
            _META[fn]["kind"] = "file"
            loaded.append(fn)
        except Exception as e:  # noqa: BLE001
            errors.append({"file": fn, "error": str(e)[:200]})
            log.warning("plugin failed: %s → %s", fn, e)
    return loaded, errors


def find_command(name: str) -> dict | None:
    for cmds in _COMMANDS.values():
        for c in cmds:
            if c["name"] == name:
                return c
    return None


def list_commands() -> list[dict]:
    return [{"name": c["name"], "description": c["description"], "file": f}
            for f, cmds in _COMMANDS.items() for c in cmds]


def invoke_route(file: str, route: str, params: dict) -> dict:
    if file.endswith(".py"):
        file = file[:-3]                     # 目录包与单文件名写法兼容
    for r in _ROUTES.get(file, []):
        if r["path"] == route.strip("/"):
            out = r["fn"](params or {})
            return {"ok": True, "result": out if isinstance(out, (dict, list)) else str(out)}
    raise KeyError(f"插件 {file} 没有 /{route} 路由")


def emit(hook: str, payload: dict) -> None:
    for file, hs in list(_HOOKS.items()):
        f = hs.get(hook)
        if not f:
            continue
        try:
            f(dict(payload))
        except Exception as e:  # noqa: BLE001
            log.warning("hook %s@%s: %s", hook, file, e)


def config_path(file: str) -> str | None:
    return _CFG_PATHS.get(file)


def meta(file: str) -> dict:
    m = _META.get(file, {})
    return {"hooks": sorted(k for k in _HOOKS.get(file, {}) if k != "on_unload"),
            "commands": [c["name"] for c in _COMMANDS.get(file, [])],
            "routes": [r["path"] for r in _ROUTES.get(file, [])],
            "version": m.get("version", ""), "author": m.get("author", ""),
            "kind": m.get("kind", ""), "requires": m.get("requires", []),
            "interval": next((int(getattr(mod, "INTERVAL", 0) or 0)
                              for f, mod in _LOADED.items() if f == file
                              and int(getattr(mod, "INTERVAL", 0) or 0) >= 5), 0)}
