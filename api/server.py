"""FastAPI 外壳：SSE 流式对话 + 工具列表 + 配置读写 + 可选访问令牌。"""
from __future__ import annotations

import ast
import asyncio
import json
import logging
import os
import shutil
import queue
import re
import sys
import threading
from pathlib import Path

import yaml
import time

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from agent.core import stats
from agent.core.llm import LLM, LLMError
from agent.core.loop import READONLY, Agent, estimate_tokens
from agent.core.tools import REGISTRY, is_enabled, schemas, set_disabled, set_scope
from agent.memory.window import Window
from agent.core import plugins as plugin_loader
from agent.core import plugin_templates as ptemplates

VERSION = "0.6.19"
START_TS = time.time()

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.yaml"
WEB_DIR = ROOT / "web"
DATA_DIR = Path(os.environ.get("AGENT_DATA", str(ROOT / "data")))
PLUGINS_DIR = Path(os.environ.get("AGENT_PLUGINS", str(ROOT / "plugins")))
ACCESS_TOKEN = os.environ.get("AGENT_ACCESS_TOKEN", "")

class _RingLog(logging.Handler):
    """内存环形日志 + 供 /api/logs/stream 轮询。"""
    def __init__(self, n: int = 600):
        super().__init__()
        self.buf: list[str] = []
        self._n = n
        self.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s: %(message)s", "%H:%M:%S"))

    def emit(self, record):
        try:
            self.buf.append(self.format(record))
            if len(self.buf) > self._n:
                del self.buf[:len(self.buf) - self._n]
        except Exception:  # noqa: BLE001
            pass

RING = _RingLog()
logging.getLogger().addHandler(RING)
logging.getLogger().setLevel(logging.INFO)
log = logging.getLogger("tauta")

app = FastAPI(title="agent")
app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")

PROVIDERS = [
    {"id": "openai", "label": "OpenAI", "base_url": "https://api.openai.com/v1"},
    {"id": "deepseek", "label": "DeepSeek", "base_url": "https://api.deepseek.com/v1"},
    {"id": "moonshot", "label": "Moonshot", "base_url": "https://api.moonshot.cn/v1"},
    {"id": "zhipu", "label": "智谱", "base_url": "https://open.bigmodel.cn/api/paas/v4"},
    {"id": "dashscope", "label": "通义", "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1"},
    {"id": "siliconflow", "label": "硅基流动", "base_url": "https://api.siliconflow.cn/v1"},
    {"id": "ollama", "label": "本地 Ollama", "base_url": "http://127.0.0.1:11434/v1"},
]

PRESETS = [
    {
        "id": "general",
        "title": "通用助手",
        "text": "你是一个能调用工具的智能体。需要外部信息时先调工具，不确定就说不确定，别编。",
    },
    {
        "id": "engineer",
        "title": "严谨工程师",
        "text": ("你是一个严谨的工程助手。回答前先确认事实，能查证就去查；"
                 "给出结论时说明依据，代码要能直接运行，不确定的部分明确标注。"),
    },
    {
        "id": "brief",
        "title": "极简回答",
        "text": "回答尽量短，能一句话说清就不要两句。不铺垫、不总结、不重复问题。",
    },
    {
        "id": "explorer",
        "title": "主动探索",
        "text": ("你是一个主动型智能体。遇到需要事实支撑的问题，优先调用工具去查，"
                 "而不是凭记忆回答；查完再给结论，并说明查了什么。"),
    },
    {
        "id": "teacher",
        "title": "耐心讲解",
        "text": ("你是一个耐心的讲解者。用具体例子代替抽象定义，"
                 "先讲是什么、再讲为什么，最后给一个能上手的小步骤。"),
    },
]

_sessions: dict[str, Window] = {}
_CANCELS: dict[str, threading.Event] = {}
_MODELS_CACHE: dict[str, tuple[float, dict]] = {}
_TOOL_LOG_LOCK = threading.Lock()
_SID_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")


def _prune_data(days: int = 30) -> None:
    """清掉太久以前的工具日志和截断输出，别把盘吃满。"""
    cutoff = time.time() - days * 86400
    for sub, suffix in (("tool_logs", ".jsonl"), ("tool_outputs", ".txt")):
        d = DATA_DIR / sub
        if not d.exists():
            continue
        for p in d.glob("*" + suffix):
            try:
                if p.stat().st_mtime < cutoff:
                    p.unlink()
            except OSError:
                pass


try:
    _prune_data()
except Exception:  # noqa: BLE001
    pass


def tool_recorder(session_id: str):
    """把每次工具调用记进按天的 JSONL，出问题能回头查。"""
    def rec(r: dict) -> None:
        d = DATA_DIR / "tool_logs"
        d.mkdir(parents=True, exist_ok=True)
        line = json.dumps({
            "ts": int(time.time()),
            "session": session_id,
            "name": r.get("name", ""),
            "args": str(r.get("args") or "")[:300],
            "ms": r.get("ms", 0),
            "ok": bool(r.get("ok", True)),
            "result": str(r.get("result") or "")[:300],
        }, ensure_ascii=False)
        with _TOOL_LOG_LOCK:
            with open(d / f"{time.strftime('%Y-%m-%d')}.jsonl", "a",
                      encoding="utf-8") as f:
                f.write(line + "\n")
    return rec


def load_config() -> dict:
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError):
        return {}


def save_config(cfg: dict) -> None:
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        yaml.safe_dump(cfg, f, allow_unicode=True, sort_keys=False)


def ensure_providers() -> dict:
    """兼容老配置：没有 providers 就用 model 里的地址造一个。"""
    cfg = load_config()
    if cfg.get("providers"):
        return cfg

    model = cfg.get("model") or {}
    pid = "p_default"
    cfg["providers"] = [{
        "id": pid,
        "label": "默认服务商",
        "base_url": model.get("base_url", ""),
        "api_key": model.get("api_key", ""),
    }]
    cfg["active_provider"] = pid
    save_config(cfg)
    return cfg


def build_llm_cfg() -> dict:
    """把「当前服务商」和「生成参数」拼成给 LLM 的配置。"""
    cfg = ensure_providers()
    model = dict(cfg.get("model") or {})
    active = cfg.get("active_provider")
    prov = next((p for p in cfg.get("providers") or []
                 if p.get("id") == active), None)
    if prov is None and cfg.get("providers"):
        prov = cfg["providers"][0]
    if prov:
        if prov.get("base_url"):
            model["base_url"] = prov["base_url"]
        if prov.get("api_key"):
            model["api_key"] = prov["api_key"]
        elif model.get("api_key") and not prov.get("api_key"):
            pass
        cfg["active_provider"] = prov.get("id")
    return model


def public_providers() -> dict:
    cfg = ensure_providers()
    out = []
    for p in cfg.get("providers") or []:
        out.append({
            "id": p.get("id"),
            "label": p.get("label") or p.get("id"),
            "base_url": p.get("base_url", ""),
            "has_key": bool(p.get("api_key") or os.environ.get("AGENT_API_KEY")),
            "model": p.get("model") or "",
            "models": p.get("models") or [],
            "active": p.get("id") == cfg.get("active_provider"),
        })
    return {"providers": out, "active": cfg.get("active_provider")}


@app.get("/api/provider/models")
def provider_models(provider_id: str | None = None):
    """问服务商要一份可用模型清单，直接列出来给前端点。"""
    cfg = ensure_providers()
    pid = provider_id or cfg.get("active_provider")
    prov = next((p for p in cfg.get("providers") or [] if p.get("id") == pid), None)
    if prov is None:
        raise HTTPException(404, "没有这个服务商")

    base = (prov.get("base_url") or "").rstrip("/")
    key = prov.get("api_key") or os.environ.get("AGENT_API_KEY") or ""
    if not base:
        raise HTTPException(400, "这个服务商还没填地址")
    if not key and "localhost" not in base and "127.0.0.1" not in base:
        raise HTTPException(400, "这个服务商还没填密钥，拉不到模型清单")

    url = base + "/models"
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    try:
        import httpx
        with httpx.Client(timeout=20, follow_redirects=True) as c:
            r = c.get(url, headers=headers)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"连不上 {url}：{e}")
    if r.status_code >= 400:
        raise HTTPException(502, f"服务商返回 {r.status_code}：{r.text[:180]}")

    try:
        data = r.json()
    except Exception:  # noqa: BLE001
        raise HTTPException(502, "返回的内容不是 JSON，可能地址填错了")

    raw = data.get("data") or data.get("models") or []
    ids = []
    for item in raw:
        if isinstance(item, str):
            ids.append(item)
        elif isinstance(item, dict):
            mid = item.get("id") or item.get("name") or item.get("model")
            if mid:
                ids.append(str(mid))
    ids = sorted({i for i in ids if i})
    return {"provider": pid, "models": ids, "count": len(ids)}


def get_window(sid: str) -> Window:
    sid = sid if _SID_RE.match(sid) else "default"
    if sid not in _sessions:
        limit = (load_config().get("memory") or {}).get("window_limit", 20)
        _sessions[sid] = Window(limit, str(DATA_DIR / "sessions" / f"{sid}.json"))
    return _sessions[sid]


# ---------------- 访问令牌 ----------------
@app.middleware("http")
async def gate(request: Request, call_next):
    path = request.url.path
    public = path in ("/", "/api/health") or path.startswith("/static")
    if ACCESS_TOKEN and path.startswith("/api") and not public:
        given = (request.headers.get("x-auth-token")
                 or request.query_params.get("token") or "")
        if given != ACCESS_TOKEN:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
    return await call_next(request)


# ---------------- 模型 ----------------
class ChatIn(BaseModel):
    message: str
    session_id: str = "default"
    images: list[str] = []


@app.get("/api/context")
def context_info(session_id: str = "default"):
    """当前会话大概占了多少上下文，给界面画个水位。"""
    w = get_window(session_id)
    cfg = load_config()
    budget = int((cfg.get("memory") or {}).get("context_budget", 24000))
    used = estimate_tokens(cfg.get("system_prompt", ""))
    used += estimate_tokens(w.summary)
    for m in w.messages():
        used += estimate_tokens(str(m.get("content") or ""))
        used += 800 * len(m.get("images") or [])
    return {"session": session_id, "tokens": used, "budget": budget,
            "percent": min(100, round(used * 100 / max(budget, 1))),
            "messages": len(w.messages()), "summary": bool(w.summary)}


def tools_cfg() -> dict:
    return load_config().get("tools") or {}


def sync_tools(cfg: dict | None = None) -> None:
    """把 config 里的停用名单和文件访问范围同步给注册表。"""
    cfg = cfg or load_config()
    t = cfg.get("tools") or {}
    set_disabled(t.get("disabled") or [])
    set_scope(t.get("scope") or "workspace")


def is_readonly() -> bool:
    return bool(tools_cfg().get("readonly", False))


class ConfigIn(BaseModel):
    model: dict | None = None
    memory: dict | None = None
    tools: dict | None = None
    ui: dict | None = None
    system_prompt: str | None = None


@app.get("/")
def index():
    return FileResponse(WEB_DIR / "index.html")


@app.post("/api/chat")
async def chat(body: ChatIn, request: Request):
    if body.session_id in _CANCELS:
        raise HTTPException(409, "这个会话还有回答在生成，先等它跑完，或者点停止")
    if len(body.message) > 20000:
        raise HTTPException(413, "这句话太长（超过 2 万字），拆成几段再发吧")
    if len(body.images) > 4:
        raise HTTPException(413, "一次最多带 4 张图")
    if sum(len(x) for x in body.images) > 16_000_000:
        raise HTTPException(413, "图片太大了，压缩一下再发")
    window = get_window(body.session_id)
    cfg = load_config()
    model = build_llm_cfg()
    tcfg = cfg.get("tools") or {}
    mcfg = cfg.get("memory") or {}
    sync_tools(cfg)
    agent = Agent(
        llm=LLM(model),
        window=window,
        system_prompt=cfg.get("system_prompt", ""),
        max_steps=model.get("max_steps", 8),
        max_tool_retries=model.get("max_tool_retries", 3),
        tool_timeout=float(tcfg.get("timeout", 30)),
        readonly=bool(tcfg.get("readonly", False)),
        context_budget=int(mcfg.get("context_budget", 24000)),
        max_tool_output=int(tcfg.get("max_output", 6000)),
        tool_hook=tool_recorder(body.session_id),
    )

    log.info("chat sid=%s model=%s msg=%.60s", body.session_id,
             model.get("name", "?"), body.message)
    if body.message.startswith("/"):
        def command_pump():
            out = ""
            try:
                parts = body.message.strip()[1:].split(None, 1)
                cname = parts[0] if parts else ""
                cargs = parts[1] if len(parts) > 1 else ""
                cmd = plugin_loader.find_command(cname)
                if cmd:
                    try:
                        out = cmd["execute"](cargs)
                    except Exception as e:  # noqa: BLE001
                        out = f"命令执行出错：{e}"
                elif cname in ("", "help"):
                    cmds = plugin_loader.list_commands()
                    lines = ["- /help —— 列出全部命令（内置）"]
                    lines += [f"- /{c['name']} —— {c['description']}（{c['file']}）"
                              for c in cmds]
                    out = ("**可用命令**\n\n" + "\n".join(lines)) if len(lines) > 1 \
                        else "还没有插件注册命令。去「插件」页装一个，或看 plugins/README.md 自己写。"
                else:
                    out = f"没有 /{cname} 这个命令。发 /help 看看有什么。"
                w = get_window(body.session_id)
                w.add({"role": "user", "content": body.message})
                w.add({"role": "assistant", "content": out})
            except Exception as e:  # noqa: BLE001
                out = f"命令处理出错：{e}"
                w = get_window(body.session_id)
                w.add({"role": "user", "content": body.message})
            finally:
                for piece in (out[i:i + 400] for i in range(0, len(out), 400)):
                    yield f"data: {json.dumps({'type': 'delta', 'content': piece}, ensure_ascii=False)}\n\n"
                yield f"data: {json.dumps({'type': 'assistant_done', 'content': out}, ensure_ascii=False)}\n\n"
                yield f"data: {json.dumps({'type': 'done', 'content': out}, ensure_ascii=False)}\n\n"
        return StreamingResponse(command_pump(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache"})

    cancel = threading.Event()
    _CANCELS[body.session_id] = cancel
    q: "queue.Queue[dict | None]" = queue.Queue()
    state = {"failed": False, "usage": False}

    def work() -> None:
        try:
            plugin_loader.emit("on_message", {"session": body.session_id,
                                              "text": body.message})
            for ev in agent.run(body.message, body.images,
                                should_stop=cancel.is_set):
                if ev.get("type") == "error":
                    state["failed"] = True
                if ev.get("type") == "done":
                    plugin_loader.emit("on_response", {"session": body.session_id,
                                                       "text": ev.get("content", "")})
                q.put(ev)
        except Exception as e:  # noqa: BLE001
            state["failed"] = True
            q.put({"type": "error", "content": f"服务内部错误: {e}"})
        finally:
            q.put(None)

    threading.Thread(target=work, daemon=True).start()

    async def pump():
        try:
            while True:
                try:
                    ev = await asyncio.to_thread(q.get, True, 15)
                except queue.Empty:
                    yield ": ping\n\n"            # 心跳，别让反代掐断
                    if await request.is_disconnected():
                        cancel.set()
                    continue
                if ev is None:
                    break
                if ev.get("type") == "usage" and ev.get("usage"):
                    stats.record(ev["usage"])
                    state["usage"] = True
                yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
                if await request.is_disconnected():
                    cancel.set()
        except asyncio.CancelledError:
            cancel.set()
            raise
        finally:
            cancel.set()
            _CANCELS.pop(body.session_id, None)
            if state["failed"]:
                if state["usage"]:
                    stats.record_failure()
                else:
                    stats.record(None, failed=True)
            else:
                w = get_window(body.session_id)
                if not w.title:
                    first = " ".join((body.message or "").split())[:18]
                    if first:
                        w.set_title(first)
                        log.info("auto title sid=%s → %r", body.session_id, first)
                mem = load_config().get("memory") or {}
                if mem.get("auto_compact"):
                    if len(w.msgs) >= w.limit - 2:
                        threading.Thread(
                            target=_compact_session,
                            args=(body.session_id,), daemon=True).start()

    return StreamingResponse(
        pump(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


class StopIn(BaseModel):
    session_id: str = "default"


@app.post("/api/chat/stop")
def chat_stop(body: StopIn):
    """让正在跑的那轮自己停下来（省 token，不是只断前端连接）。"""
    ev = _CANCELS.get(body.session_id)
    if ev is None:
        return {"ok": False, "reason": "没有正在生成的回答"}
    ev.set()
    return {"ok": True}


# ---------------- 元信息 ----------------
@app.get("/api/tools")
def list_tools():
    sync_tools()
    items = []
    for t in REGISTRY.values():
        item = {k: v for k, v in t.items() if k not in ("fn", "mutating", "plugin_file")}
        item["mutating"] = bool(t.get("mutating"))
        item["enabled"] = is_enabled(t["name"])
        items.append(item)
    return {"tools": items, "readonly": is_readonly()}


class ToggleIn(BaseModel):
    name: str
    enabled: bool


class PluginToggleIn(BaseModel):
    file: str
    enabled: bool


class PluginInstallIn(BaseModel):
    filename: str
    code: str


class PluginDeleteIn(BaseModel):
    file: str


class PluginConfigIn(BaseModel):
    file: str
    config: dict


def _reload_and_report() -> dict:
    global _PLUGIN_ERRORS
    loaded, errors = plugin_loader.load(str(PLUGINS_DIR), REGISTRY,
        {k: (v or {}).get("config") or {} for k, v in (load_config().get("plugins") or {}).items()},
        str(DATA_DIR),
        lambda file: any(is_enabled(n) for n, t in REGISTRY.items()
                         if t.get("plugin_file") == file))
    _PLUGIN_ERRORS = errors
    set_disabled(tools_cfg().get("disabled") or [])
    names = [n for n, t in REGISTRY.items() if t.get("plugin")]
    return {"ok": True, "loaded": loaded, "errors": errors, "tools": names}


@app.post("/api/plugins/install")
def install_plugin(body: PluginInstallIn):
    """从代码安装插件：校验文件名与语法，写入 plugins/ 后热加载。"""
    fname = (body.filename or "").strip().lower()
    if not re.match(r"^[a-z][a-z0-9_]{0,40}\.py$", fname):
        raise HTTPException(400, "文件名只能用小写字母/数字/下划线，且以 .py 结尾")
    code = body.code or ""
    if len(code) > 100_000:
        raise HTTPException(400, "插件代码超过 100KB 上限")
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        raise HTTPException(400, f"语法错误：{e.msg}（第 {e.lineno} 行）")
    has_exec = any(isinstance(n, ast.FunctionDef) and n.name == "execute" for n in tree.body)
    has_tools = any(isinstance(n, ast.Assign) and getattr(n.targets[0], "id", "") == "TOOLS"
                    for n in tree.body)
    if not has_exec and not has_tools:
        raise HTTPException(400, "缺少 execute(args) 函数或 TOOLS 工具列表")
    target = PLUGINS_DIR / fname
    if not str(target).startswith(str(PLUGINS_DIR)):
        raise HTTPException(400, "路径不合法")
    target.write_text(code, encoding="utf-8")
    log.info("plugin installed: %s (%d chars)", fname, len(code))
    rep = _reload_and_report()
    rep["file"] = fname
    return rep


@app.post("/api/plugins/delete")
def delete_plugin(body: PluginDeleteIn):
    fname = (body.file or "").strip()
    if not re.match(r"^[a-z][a-z0-9_]{0,40}\.py$", fname):
        raise HTTPException(400, "文件名不合法")
    target = PLUGINS_DIR / fname
    if not str(target).startswith(str(PLUGINS_DIR)) or not target.exists():
        raise HTTPException(404, "没有这个插件文件")
    target.unlink()
    log.info("plugin deleted: %s", fname)
    rep = _reload_and_report()
    rep["file"] = fname
    return rep


_PLUGIN_ERRORS: list = []

@app.get("/api/plugins")
def list_plugins():
    sync_tools()
    disabled = set(tools_cfg().get("disabled") or [])
    out: dict[str, dict] = {}
    for t in REGISTRY.values():
        if not t.get("plugin"):
            continue
        key = t["plugin_file"]
        e = out.setdefault(key, {
            "file": key, "name": t["name"], "description": t["description"],
            "mutating": t["mutating"], "tools": [],
            "enabled": t["name"] not in disabled})
        e["tools"].append(t["name"])
    for e in out.values():
        e.update(plugin_loader.meta(e["file"]))
        pcfg = (load_config().get("plugins") or {}).get(e["file"], {}).get("config")
        e["config"] = pcfg or {}
        e["config_schema"] = next((t.get("plugin_config_schema")
                                   for t in REGISTRY.values()
                                   if t.get("plugin_file") == e["file"]
                                   and t.get("plugin_config_schema")), None)
    return {"plugins": list(out.values()), "errors": _PLUGIN_ERRORS}


@app.get("/api/commands")
def list_commands():
    return {"commands": plugin_loader.list_commands()}


class BackupRestoreIn(BaseModel):
    name: str


@app.get("/api/backups")
def list_backups():
    bdir = DATA_DIR / "backups"
    out = []
    if bdir.exists():
        for p in sorted(bdir.iterdir(), reverse=True):
            if p.is_dir() and re.match(r"^\d{8}-\d{6}$", p.name):
                out.append({"name": p.name,
                            "sessions": len(list(p.glob("*.json"))),
                            "mtime": int(p.stat().st_mtime)})
    return {"backups": out}


@app.post("/api/backups/restore")
def restore_backup(body: BackupRestoreIn):
    """把某次启动备份里的会话文件覆盖回 data/sessions。"""
    if not re.match(r"^\d{8}-\d{6}$", body.name):
        raise HTTPException(400, "备份名不合法")
    src = DATA_DIR / "backups" / body.name
    if not src.exists():
        raise HTTPException(404, "这份备份里没有会话数据")
    dst = DATA_DIR / "sessions"
    dst.mkdir(parents=True, exist_ok=True)
    n = 0
    for f in src.glob("*.json"):
        shutil.copy2(f, dst / f.name)
        n += 1
    _sessions.clear()                      # 缓存清空，下次读盘
    log.info("backup restored: %s (%d sessions)", body.name, n)
    return {"ok": True, "restored": n}


@app.get("/api/plugins/catalog")
def plugin_catalog():
    """内置插件模板清单：一键安装用。"""
    return {"templates": [
        {"name": k, "description": v["description"],
         "version": v["version"], "author": v["author"],
         "installed": (PLUGINS_DIR / k).exists()}
        for k, v in ptemplates.TEMPLATES.items()]}


class TemplateInstallIn(BaseModel):
    name: str


@app.post("/api/plugins/install_template")
def install_plugin_template(body: TemplateInstallIn):
    t = ptemplates.TEMPLATES.get(body.name)
    if not t:
        raise HTTPException(404, "没有这个模板")
    dest = PLUGINS_DIR / t["name"]
    if dest.exists():
        raise HTTPException(409, f"{t['name']} 已经装过了（同名目录已存在）")
    for rel, content in t["files"].items():
        p = dest / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    log.info("plugin template installed: %s", t["name"])
    rep = _reload_and_report()
    rep["name"] = t["name"]
    return rep


class RouteInvokeIn(BaseModel):
    file: str
    route: str
    params: dict | None = None


@app.post("/api/plugins/invoke")
def invoke_plugin_route(body: RouteInvokeIn):
    """插件对外提供的能力走统一入口：鉴权与限流天然共用。"""
    try:
        return plugin_loader.invoke_route(body.file, body.route, body.params or {})
    except KeyError as e:
        raise HTTPException(404, str(e))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"插件路由执行失败：{e}")


@app.post("/api/plugins/config")
def save_plugin_config(body: PluginConfigIn):
    """保存插件配置：目录包写包内 config.yaml，单文件写全局 plugins 段。"""
    fname = (body.file or "").strip()
    if fname not in {t.get("plugin_file") for t in REGISTRY.values() if t.get("plugin")}:
        raise HTTPException(404, "没有这个插件")
    pkg_cfg = PLUGINS_DIR / fname / "config.yaml"
    if pkg_cfg.exists() or (PLUGINS_DIR / fname / "main.py").exists():
        yaml.safe_dump(body.config or {}, open(pkg_cfg, "w", encoding="utf-8"),
                       allow_unicode=True, sort_keys=False)
        where = str(pkg_cfg)
    else:
        cfg = load_config()
        plugs = cfg.get("plugins") or {}
        plugs[fname] = {"config": body.config or {}}
        cfg["plugins"] = plugs
        save_config(cfg)
        where = "config.yaml (plugins 段)"
    log.info("plugin config saved: %s keys=%s", fname, list(body.config or {}))
    rep = _reload_and_report()
    rep["file"] = fname
    rep["config"] = body.config or {}
    rep["where"] = where
    return rep


class PluginDepsIn(BaseModel):
    file: str


@app.post("/api/plugins/deps")
def install_plugin_deps(body: PluginDepsIn):
    """安装目录包 requirements.txt 里声明的依赖（pip，60 秒超时）。"""
    req = PLUGINS_DIR / body.file / "requirements.txt"
    if not req.exists():
        raise HTTPException(404, "这个插件没有 requirements.txt")
    r = subprocess.run([sys.executable, "-m", "pip", "install", "-r", str(req)],
                       capture_output=True, text=True, timeout=60)
    out = (r.stdout + r.stderr)[-1500:]
    log.info("plugin deps install rc=%s: %s", r.returncode, body.file)
    return {"ok": r.returncode == 0, "output": out}


class PluginZipIn(BaseModel):
    filename: str
    b64: str


@app.post("/api/plugins/install_zip")
def install_plugin_zip(body: PluginZipIn):
    """上传 zip 安装插件：包内需含 main.py 与 metadata.yaml（允许一层目录包裹）。"""
    import base64, io, zipfile
    name = re.sub(r"[^a-z0-9_]", "_", (body.filename or "plugin.zip")[:-4].lower())
    if not name or len(name) > 40:
        raise HTTPException(400, "zip 文件名不合法")
    try:
        zf = zipfile.ZipFile(io.BytesIO(base64.b64decode(body.b64)))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"zip 解不开：{e}")
    names = zf.namelist()
    if len(names) > 200 or any(len(n) > 240 for n in names):
        raise HTTPException(400, "zip 条目过多")
    root = ""
    if all(n.split("/")[0] in ("main.py", "metadata.yaml") or "/" in n for n in names if n):
        pass
    mains = [n for n in names if n.endswith("main.py")]
    if not mains:
        raise HTTPException(400, "zip 里没有 main.py")
    prefix = mains[0][:-len("main.py")]
    if any(not n.startswith(prefix) for n in names if n):
        raise HTTPException(400, "zip 结构不统一：main.py 必须与其它文件同层")
    if f"{prefix}metadata.yaml" not in names:
        raise HTTPException(400, "zip 里没有 metadata.yaml")
    dest = PLUGINS_DIR / name
    dest.mkdir(parents=True, exist_ok=True)
    for n in names:
        if not n.startswith(prefix) or n.endswith("/"):
            continue
        rel = n[len(prefix):]
        target = (dest / rel).resolve()
        if not str(target).startswith(str(dest.resolve())):
            raise HTTPException(400, "zip 里有越界路径，拒绝安装")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(zf.read(n))          # 手动剥前缀解压，防包套包
    log.info("plugin zip installed: %s (%d files)", name, len(names))
    rep = _reload_and_report()
    rep["file"] = name
    return rep


@app.post("/api/plugins/reload")
def reload_plugins():
    global _PLUGIN_ERRORS
    loaded, errors = plugin_loader.load(str(PLUGINS_DIR), REGISTRY,
        {k: (v or {}).get("config") or {} for k, v in (load_config().get("plugins") or {}).items()},
        str(DATA_DIR),
        lambda file: any(is_enabled(n) for n, t in REGISTRY.items()
                         if t.get("plugin_file") == file))
    _PLUGIN_ERRORS = errors
    t = tools_cfg()
    set_disabled(t.get("disabled") or [])
    names = [n for n, t2 in REGISTRY.items() if t2.get("plugin")]
    log.info("plugins reloaded: %s, errors=%s", loaded, errors)
    return {"ok": True, "loaded": loaded, "errors": errors, "tools": names}


@app.post("/api/plugins/toggle")
def toggle_plugin(body: PluginToggleIn):
    cfg = load_config()
    tcfg = cfg.get("tools") or {}
    disabled = set(tcfg.get("disabled") or [])
    names = [n for n, t in REGISTRY.items()
             if t.get("plugin") and t.get("plugin_file") == body.file]
    if not names:
        raise HTTPException(404, "没有这个插件")
    if body.enabled:
        disabled -= set(names)
    else:
        disabled |= set(names)
    tcfg["disabled"] = sorted(disabled)
    cfg["tools"] = tcfg
    save_config(cfg)
    set_disabled(tcfg["disabled"])
    plugin_loader.stop_ticker(body.file)
    if body.enabled:
        plugin_loader.load(str(PLUGINS_DIR), REGISTRY,
                           {k: (v or {}).get("config") or {} for k, v in (load_config().get("plugins") or {}).items()},
                           str(DATA_DIR),
                           lambda f: any(is_enabled(n) for n, t in REGISTRY.items()
                                         if t.get("plugin_file") == f))
    return {"ok": True, "enabled": body.enabled, "tools": sorted(names)}


@app.post("/api/tools/toggle")
def toggle_tool(body: ToggleIn):
    cfg = load_config()
    if body.name not in REGISTRY:
        raise HTTPException(404, "没有这个工具")
    off = list((cfg.get("tools") or {}).get("disabled") or [])
    if body.enabled and body.name in off:
        off.remove(body.name)
    elif not body.enabled and body.name not in off:
        off.append(body.name)
    cfg.setdefault("tools", {})["disabled"] = off
    save_config(cfg)
    sync_tools(cfg)
    return {"ok": True, "name": body.name, "enabled": body.enabled,
            "disabled": off}


@app.get("/api/config")
def read_config():
    cfg = json.loads(json.dumps(load_config()))
    key = (cfg.get("model") or {}).get("api_key") or ""
    if key:
        cfg["model"]["api_key"] = key[:4] + "****"
    cfg["_runtime"] = {"readonly": is_readonly(), "auth": bool(ACCESS_TOKEN)}
    return cfg


def _clamp_fields(d: dict, spec: dict) -> None:
    """把界面送来的数字卡进安全区间，坏值直接丢掉。"""
    for key, (lo, hi, typ) in spec.items():
        if key not in d or d[key] in (None, ""):
            continue
        try:
            d[key] = typ(max(lo, min(hi, float(d[key]))))
        except (TypeError, ValueError):
            d.pop(key, None)


@app.post("/api/config")
def update_config(body: ConfigIn):
    cfg = load_config()
    if body.model:
        cfg["model"] = {**(cfg.get("model") or {}), **body.model}
    for key in ("memory", "tools", "ui"):
        part = getattr(body, key)
        if part is not None:
            cfg[key] = {**(cfg.get(key) or {}), **part}
    if body.system_prompt is not None:
        cfg["system_prompt"] = body.system_prompt

    _clamp_fields(cfg.get("model") or {}, {
        "temperature": (0, 2, float), "top_p": (0, 1, float),
        "max_tokens": (0, 200000, int), "timeout": (10, 900, float),
        "max_steps": (1, 30, int), "max_tool_retries": (0, 10, int),
    })
    _clamp_fields(cfg.get("memory") or {}, {
        "window_limit": (2, 200, int), "context_budget": (2000, 200000, int),
    })
    _clamp_fields(cfg.get("tools") or {}, {
        "timeout": (5, 600, float), "max_output": (500, 100000, int),
    })

    # 窗口条数改了，正在用的窗口也跟着变
    limit = (cfg.get("memory") or {}).get("window_limit")
    if isinstance(limit, int) and limit > 0:
        for w in _sessions.values():
            w.limit = limit

    save_config(cfg)
    return {"ok": True, "readonly": is_readonly()}


class ProviderIn(BaseModel):
    id: str | None = None
    label: str
    base_url: str
    api_key: str | None = None
    model: str | None = None


class ActiveIn(BaseModel):
    id: str


class ToolRunIn(BaseModel):
    name: str
    args: dict = {}


class ModelPick(BaseModel):
    name: str


class TestIn(BaseModel):
    name: str | None = None


@app.get("/api/models")
def models():
    cfg = build_llm_cfg()
    key = f"{cfg.get('base_url')}|{cfg.get('name')}"
    hit = _MODELS_CACHE.get(key)
    if hit and time.time() - hit[0] < 90:
        return {**hit[1], "cached": True}
    try:
        data = LLM(cfg).list_models()
    except LLMError as e:
        return JSONResponse(
            {"error": str(e), "current": cfg.get("name", ""), "models": [], "count": 0},
            status_code=502,
        )
    _MODELS_CACHE[key] = (time.time(), data)
    return data


@app.post("/api/model/test")
def model_test(body: TestIn):
    cfg = build_llm_cfg()
    if body.name:
        cfg = {**cfg, "name": body.name}
    return LLM(cfg).test()


@app.post("/api/model/select")
def model_select(body: ModelPick):
    cfg = load_config()
    cfg["model"] = {**(cfg.get("model") or {}), "name": body.name}
    # 记在当前服务商名下，切回来还能接着用
    for p in cfg.get("providers") or []:
        if p.get("id") == cfg.get("active_provider"):
            p["model"] = body.name
            break
    save_config(cfg)
    return {"ok": True, "name": body.name}


# ---------------- 会话 ----------------
@app.get("/api/history")
def history(session_id: str = "default"):
    w = get_window(session_id)
    return {"messages": w.messages(), "summary": w.summary}


@app.post("/api/reset")
def reset(session_id: str = "default"):
    get_window(session_id).clear()
    return {"ok": True}


@app.get("/api/sessions/search")
def search_sessions(q: str = "", limit: int = 30):
    """跨会话全文搜索，返回带上下文的片段。"""
    needle = (q or "").strip().lower()
    if not needle:
        return {"query": q, "hits": [], "count": 0}

    hits: list[dict] = []
    d = DATA_DIR / "sessions"
    if d.exists():
        for p in sorted(d.glob("*.json"), key=lambda x: x.stat().st_mtime, reverse=True):
            if not _SID_RE.match(p.stem):
                continue
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            msgs = (data.get("messages") if isinstance(data, dict) else data) or []
            title = data.get("title") if isinstance(data, dict) else ""
            for i, m in enumerate(msgs):
                body = str(m.get("content") or "")
                low = body.lower()
                pos = low.find(needle)
                if pos < 0:
                    continue
                start = max(0, pos - 45)
                snippet = body[start:pos + len(needle) + 55].replace("\n", " ")
                hits.append({
                    "id": p.stem,
                    "title": title or "",
                    "role": m.get("role") or "",
                    "index": i,
                    "mtime": int(p.stat().st_mtime),
                    "snippet": ("…" if start else "") + snippet + "…",
                })
                if len(hits) >= max(1, min(limit, 100)):
                    return {"query": q, "hits": hits, "count": len(hits)}
    return {"query": q, "hits": hits, "count": len(hits)}


@app.get("/api/tool_logs")
def tool_logs(limit: int = 50, name: str | None = None):
    """最近的工具调用审计记录，新的在前。"""
    d = DATA_DIR / "tool_logs"
    items: list[dict] = []
    if d.exists():
        for p in sorted(d.glob("*.jsonl"), reverse=True):
            try:
                lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            for line in reversed(lines):
                if not line.strip():
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if name and rec.get("name") != name:
                    continue
                items.append(rec)
                if len(items) >= max(1, min(limit, 500)):
                    return {"items": items, "count": len(items)}
    return {"items": items, "count": len(items)}


@app.get("/api/tool_logs/summary")
def tool_logs_summary(days: int = 7):
    """按工具聚合：调用次数、失败次数、平均耗时。"""
    d = DATA_DIR / "tool_logs"
    agg: dict[str, dict] = {}
    files = sorted(d.glob("*.jsonl"), reverse=True)[:max(1, min(days, 90))] if d.exists() else []
    for p in files:
        try:
            lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            entry = agg.setdefault(rec.get("name", "?"),
                                   {"calls": 0, "failed": 0, "ms": 0})
            entry["calls"] += 1
            if not rec.get("ok", True):
                entry["failed"] += 1
            entry["ms"] += int(rec.get("ms") or 0)
    rows = []
    for name, v in sorted(agg.items(), key=lambda kv: -kv[1]["calls"]):
        rows.append({"name": name, "calls": v["calls"], "failed": v["failed"],
                     "avg_ms": round(v["ms"] / max(v["calls"], 1))})
    return {"days": len(files), "tools": rows}


@app.get("/api/tool_outputs")
def tool_outputs():
    """被截断后落盘的工具输出，方便回头翻全文。"""
    d = Path(os.environ.get("AGENT_DATA", "/opt/agent/data")) / "tool_outputs"
    items = []
    if d.exists():
        for p in sorted(d.glob("*.txt"), key=lambda x: x.stat().st_mtime, reverse=True)[:50]:
            items.append({"name": p.name, "size": p.stat().st_size,
                          "mtime": int(p.stat().st_mtime)})
    return {"items": items, "dir": str(d)}


@app.get("/api/tool_outputs/{name}")
def tool_output(name: str):
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,120}\.txt", name):
        raise HTTPException(400, "bad name")
    p = Path(os.environ.get("AGENT_DATA", "/opt/agent/data")) / "tool_outputs" / name
    if not p.exists():
        raise HTTPException(404, "没有这个文件")
    return {"name": name, "content": p.read_text(encoding="utf-8", errors="replace")[:20000]}


@app.get("/api/sessions")
def sessions():
    d = DATA_DIR / "sessions"
    items = []
    if d.exists():
        for p in sorted(d.glob("*.json"), key=lambda x: x.stat().st_mtime, reverse=True):
            if not _SID_RE.match(p.stem):
                continue
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                msgs = data.get("messages") if isinstance(data, dict) else data
                msgs = msgs or []
                preview = next((m.get("content", "") for m in reversed(msgs)
                                if m.get("role") == "user"), "")
                has_sum = bool(data.get("summary")) if isinstance(data, dict) else False
                title = (data.get("title") or "") if isinstance(data, dict) else ""
            except (OSError, json.JSONDecodeError):
                msgs, preview, has_sum = [], "", False
            if not msgs and not has_sum:
                continue                      # 空会话不列出
            items.append({
                "id": p.stem,
                "mtime": int(p.stat().st_mtime),
                "count": len(msgs),
                "preview": (preview or "").replace("\n", " ")[:40],
                "compacted": has_sum,
                "title": title,
            })
    return {"sessions": items}


class RenameIn(BaseModel):
    title: str


@app.post("/api/session/{sid}/rename")
def rename_session(sid: str, body: RenameIn):
    if not _SID_RE.match(sid):
        raise HTTPException(400, "bad id")
    w = get_window(sid)
    return {"ok": True, "title": w.set_title(body.title)}


@app.delete("/api/session/{sid}")
def drop_session(sid: str):
    if not _SID_RE.match(sid):
        return {"ok": False, "error": "bad id"}
    _sessions.pop(sid, None)
    p = DATA_DIR / "sessions" / f"{sid}.json"
    if p.exists():
        p.unlink()
    return {"ok": True}


@app.post("/api/session/{sid}/undo")
def undo_last(sid: str):
    """撤掉最后一轮问答，配合「重发」用，免得历史里留两份。"""
    if not _SID_RE.match(sid):
        raise HTTPException(400, "bad id")
    w = get_window(sid)
    msgs = w.messages()
    removed: list[dict] = []
    if msgs and msgs[-1].get("role") == "assistant":
        removed.append(msgs.pop())
    if msgs and msgs[-1].get("role") == "user":
        removed.append(msgs.pop())
    if removed:
        w.replace(msgs)
    return {"ok": bool(removed), "removed": len(removed),
            "left": len(msgs)}


@app.get("/api/archive/{sid}")
def list_archive(sid: str):
    """压缩时被折叠掉的原文，按时间列出来。"""
    if not _SID_RE.match(sid):
        raise HTTPException(400, "bad id")
    d = DATA_DIR / "archive"
    items = []
    if d.exists():
        for p in sorted(d.glob(f"{sid}-*.json"), reverse=True)[:30]:
            try:
                msgs = json.loads(p.read_text(encoding="utf-8")).get("messages") or []
            except (OSError, json.JSONDecodeError):
                msgs = []
            items.append({"name": p.name, "count": len(msgs),
                          "mtime": int(p.stat().st_mtime)})
    return {"session": sid, "items": items}


@app.get("/api/archive/{sid}/{name}")
def read_archive(sid: str, name: str):
    if not _SID_RE.match(sid) or not re.fullmatch(rf"{re.escape(sid)}-[0-9-]+\.json", name):
        raise HTTPException(400, "bad name")
    p = DATA_DIR / "archive" / name
    if not p.exists():
        raise HTTPException(404, "没有这个归档")
    return json.loads(p.read_text(encoding="utf-8"))


@app.get("/api/export")
def export_session(session_id: str = "default", fmt: str = "markdown"):
    """把一段会话导出成 markdown / json / html。"""
    msgs = get_window(session_id).messages()
    if fmt == "json":
        payload = json.dumps({"session": session_id, "messages": msgs},
                             ensure_ascii=False, indent=2)
        media, ext = "application/json", "json"
    elif fmt == "html":
        import html as _h

        rows = []
        for m in msgs:
            who = "我" if m.get("role") == "user" else "tautaBot"
            cls = "u" if m.get("role") == "user" else "a"
            body = _h.escape(m.get("content") or "").replace("\n", "<br>")
            rows.append(f'<div class="m {cls}"><b>{who}</b><p>{body}</p></div>')
        payload = (
            "<!doctype html><meta charset='utf-8'>"
            f"<title>会话 { _h.escape(session_id) }</title>"
            "<style>body{font:15px/1.7 system-ui,'PingFang SC',sans-serif;"
            "max-width:760px;margin:40px auto;padding:0 18px;background:#f7fbfe;color:#16202b}"
            ".m{margin:14px 0;padding:12px 16px;border-radius:12px}"
            ".m.u{background:#e7f3fc;margin-left:15%}.m.a{background:#fff;border:1px solid #dce8f2}"
            ".m b{font-size:12px;color:#5b7690}.m p{margin:4px 0 0}"
            "h1{font-size:20px}</style>"
            f"<h1>会话 {_h.escape(session_id)}</h1>" + "".join(rows)
        )
        media, ext = "text/html; charset=utf-8", "html"
    else:
        lines = [f"# 会话 {session_id}", ""]
        for m in msgs:
            who = "我" if m.get("role") == "user" else "助手"
            lines.append(f"**{who}**  ")
            lines.append((m.get("content") or "").strip())
            lines.append("")
        payload = "\n".join(lines)
        media, ext = "text/markdown; charset=utf-8", "md"

    from fastapi.responses import Response

    return Response(
        content=payload,
        media_type=media,
        headers={"Content-Disposition": f'attachment; filename="{session_id}.{ext}"'},
    )


@app.post("/api/tool/run")
def tool_run(body: ToolRunIn):
    sync_tools()
    entry = REGISTRY.get(body.name)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"没有名为 {body.name} 的工具")
    if entry.get("mutating") and is_readonly():
        return {"ok": False, "error": "当前是只读模式，这个工具被禁用", "ms": 0}

    started = time.monotonic()
    try:
        result = entry["fn"](**body.args)
        return {"ok": True, "result": "" if result is None else str(result),
                "ms": int((time.monotonic() - started) * 1000)}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}",
                "ms": int((time.monotonic() - started) * 1000)}


@app.get("/api/provider-templates")
def provider_templates():
    return {"providers": PROVIDERS}


@app.get("/api/providers")
def providers():
    return public_providers()


@app.post("/api/providers")
class ProvModelIn(BaseModel):
    id: str
    model: str
    remove: bool = False


@app.post("/api/providers/models")
def provider_toggle_model(body: ProvModelIn):
    """把服务商的某个模型加入/移出「已添加」清单。"""
    cfg = ensure_providers()
    prov = next((p for p in cfg.get("providers") or []
                 if p.get("id") == body.id), None)
    if prov is None:
        raise HTTPException(404, "没有这个服务商")
    lst = prov.get("models") or []
    if body.remove:
        lst = [m for m in lst if m != body.model]
    elif body.model and body.model not in lst:
        lst.append(body.model)
    prov["models"] = lst
    save_config(cfg)
    return {"ok": True, "models": lst}


def upsert_provider(body: ProviderIn):
    cfg = ensure_providers()
    items = cfg.get("providers") or []

    if body.id:
        for p in items:
            if p.get("id") == body.id:
                p["label"] = body.label
                p["base_url"] = body.base_url.rstrip("/")
                if body.api_key and "****" not in body.api_key:
                    p["api_key"] = body.api_key
                if body.model:
                    p["model"] = body.model
                break
        else:
            return JSONResponse({"ok": False, "error": "没有这个服务商"}, status_code=404)
        pid = body.id
    else:
        pid = "p_" + os.urandom(3).hex()
        items.append({
            "id": pid,
            "label": body.label,
            "base_url": body.base_url.rstrip("/"),
            "api_key": body.api_key or "",
        })
        if len(items) == 1:
            cfg["active_provider"] = pid

    cfg["providers"] = items
    save_config(cfg)
    return {"ok": True, "id": pid, **public_providers()}


@app.delete("/api/providers/{pid}")
def drop_provider(pid: str):
    cfg = ensure_providers()
    items = [p for p in (cfg.get("providers") or []) if p.get("id") != pid]
    if len(items) == len(cfg.get("providers") or []):
        return JSONResponse({"ok": False, "error": "没有这个服务商"}, status_code=404)
    if not items:
        return JSONResponse({"ok": False, "error": "至少要留一个服务商"}, status_code=400)

    cfg["providers"] = items
    if cfg.get("active_provider") == pid:
        cfg["active_provider"] = items[0]["id"]
    save_config(cfg)
    return {"ok": True, **public_providers()}


@app.post("/api/providers/active")
def set_active(body: ActiveIn):
    cfg = ensure_providers()
    if not any(p.get("id") == body.id for p in cfg.get("providers") or []):
        return JSONResponse({"ok": False, "error": "没有这个服务商"}, status_code=404)
    cfg["active_provider"] = body.id
    prov = next(p for p in cfg["providers"] if p.get("id") == body.id)
    if prov.get("model"):
        cfg["model"] = {**(cfg.get("model") or {}), "name": prov["model"]}
    save_config(cfg)
    return {"ok": True, "model": (cfg.get("model") or {}).get("name", ""),
            **public_providers()}


@app.get("/api/presets")
def presets():
    return {"presets": PRESETS}


def _compact_session(session_id: str) -> dict:
    """把前半段旧消息压成摘要，后半段保留原文。失败时返回原因。"""
    w = get_window(session_id)
    msgs = w.messages()
    if len(msgs) < 6:
        return {"ok": False, "reason": "消息太少，还没到需要压缩的地步"}

    half = len(msgs) // 2
    old, keep = msgs[:half], msgs[half:]

    lines = []
    if w.summary:
        lines.append("已有摘要：" + w.summary)
    for m in old:
        who = "我" if m.get("role") == "user" else "助手"
        lines.append(f"{who}：{(m.get('content') or '').strip()[:500]}")

    prompt = ("把下面这段对话压缩成要点摘要，不超过 200 字。"
              "只保留结论、事实、约定和未完成的事，删掉寒暄和重复内容。"
              "用第三人称陈述，不要用「我」称呼对话者。\n\n" + "\n".join(lines))

    cfg = build_llm_cfg()
    try:
        reply = LLM(cfg).chat([{"role": "user", "content": prompt}])
    except LLMError as e:
        return {"ok": False, "reason": str(e)}

    summary = (reply.get("content") or "").strip()
    if not summary:
        return {"ok": False, "reason": "模型没有返回摘要内容"}

    archive = ""
    try:
        ad = DATA_DIR / "archive"
        ad.mkdir(parents=True, exist_ok=True)
        name = f"{session_id}-{time.strftime('%Y%m%d-%H%M%S')}.json"
        (ad / name).write_text(
            json.dumps({"session": session_id, "messages": old},
                       ensure_ascii=False, indent=1), encoding="utf-8")
        archive = name
    except OSError:
        pass

    w.replace(keep, summary)
    return {"ok": True, "summary": summary, "kept": len(keep),
            "folded": len(old), "archive": archive}


@app.post("/api/compact")
def compact(session_id: str = "default"):
    return _compact_session(session_id)


@app.get("/api/status")
def system_status():
    """进程自身状态：跑多久、吃多少内存、有多少会话。"""
    import platform

    rss = 0
    try:
        with open("/proc/self/status", "r", encoding="utf-8") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    rss = int(line.split()[1])
                    break
    except OSError:
        pass

    sess_dir = DATA_DIR / "sessions"
    n_sessions = len(list(sess_dir.glob("*.json"))) if sess_dir.exists() else 0
    out_dir = Path(os.environ.get("AGENT_DATA", "/opt/agent/data")) / "tool_outputs"
    n_outputs = len(list(out_dir.glob("*.txt"))) if out_dir.exists() else 0
    data_bytes = 0
    if DATA_DIR.exists():
        for p in DATA_DIR.rglob("*"):
            if p.is_file():
                try:
                    data_bytes += p.stat().st_size
                except OSError:
                    pass
    up = int(time.time() - START_TS)

    return {
        "uptime_sec": up,
        "uptime": f"{up // 3600} 小时 {(up % 3600) // 60} 分" if up >= 3600
                   else f"{up // 60} 分 {up % 60} 秒",
        "rss_kb": rss,
        "rss_mb": round(rss / 1024, 1),
        "sessions": n_sessions,
        "tools": len(REGISTRY),
        "python": platform.python_version(),
        "platform": platform.system(),
        "running": len(_CANCELS),
        "tool_outputs": n_outputs,
        "data_mb": round(data_bytes / 1048576, 2),
        "version": VERSION,
    }


@app.get("/api/stats")
def usage_stats():
    return stats.snapshot()


def backup_sessions(keep: int = 5):
    """启动时把会话数据整体备份一份，保留最近 keep 份。"""
    src = DATA_DIR / "sessions"
    if not src.exists():
        return
    bdir = DATA_DIR / "backups"
    bdir.mkdir(parents=True, exist_ok=True)
    try:
        shutil.copytree(src, bdir / time.strftime("%Y%m%d-%H%M%S"))
    except OSError as e:
        log.warning("sessions backup failed: %s", e)
        return
    olds = sorted(p for p in bdir.iterdir() if p.is_dir())
    for old in olds[:-keep]:
        shutil.rmtree(old, ignore_errors=True)
    log.info("sessions backup ok → %s", time.strftime("%Y%m%d-%H%M%S"))



@app.get("/api/logs")
def logs_tail(n: int = 200):
    log.info("logs tail n=%d", n)
    return {"lines": RING.buf[-max(10, min(n, 600)):]}


@app.get("/api/logs/stream")
async def logs_stream(request: Request):
    """实时日志：0.8s 轮询环形缓冲，SSE 增量推送。"""
    async def gen():
        idx = max(0, len(RING.buf) - 80)
        while not await request.is_disconnected():
            lines = list(RING.buf)
            while idx < len(lines):
                yield f"data: {json.dumps({'line': lines[idx]}, ensure_ascii=False)}\n\n"
                idx += 1
            await asyncio.sleep(0.8)
    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache"})


@app.get("/api/health")
def health():
    return {
        "ok": True,
        "tools": len(REGISTRY),
        "readonly": is_readonly(),
        "auth": bool(ACCESS_TOKEN),
        "version": VERSION,
        "name": load_config().get("name", "汐枢"),
        "model": (load_config().get("model") or {}).get("name", ""),
        "provider": next((p.get("label") for p in (ensure_providers().get("providers") or [])
                          if p.get("id") == load_config().get("active_provider")), ""),
    }


if __name__ == "__main__":
    import uvicorn

    plugin_loader.load(str(PLUGINS_DIR), REGISTRY,
        {k: (v or {}).get("config") or {} for k, v in (load_config().get("plugins") or {}).items()},
        str(DATA_DIR),
        lambda file: any(is_enabled(n) for n, t in REGISTRY.items()
                         if t.get("plugin_file") == file))
    backup_sessions()

    cfg = load_config().get("server", {})
    uvicorn.run(app, host=cfg.get("host", "127.0.0.1"), port=cfg.get("port", 8080))
