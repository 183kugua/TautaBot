"""工具注册表：@tool 一挂，函数名 / docstring / 类型注解自动变 schema。"""
from __future__ import annotations

import glob
import inspect
import os
from typing import Any, Callable

REGISTRY: dict[str, dict] = {}
DISABLED: set[str] = set()
SCOPE: str = "workspace"          # workspace | full
_DANGER = ("/etc", "/boot", "/sys", "/proc", "/dev")

_PY2JSON = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
    list: "array",
    tuple: "array",
    dict: "object",
}


def _json_type(annotation: Any) -> str:
    if annotation is inspect._empty:
        return "string"
    return _PY2JSON.get(annotation, "string")


def _register(fn: Callable, mutating: bool) -> Callable:
    sig = inspect.signature(fn)
    props: dict[str, dict] = {}
    required: list[str] = []

    for pname, param in sig.parameters.items():
        props[pname] = {"type": _json_type(param.annotation)}
        if param.default is inspect._empty:
            required.append(pname)

    schema: dict[str, Any] = {"type": "object", "properties": props}
    if required:
        schema["required"] = required

    REGISTRY[fn.__name__] = {
        "name": fn.__name__,
        "description": (inspect.getdoc(fn) or "").strip(),
        "parameters": schema,
        "mutating": mutating,
        "fn": fn,
    }
    return fn


def tool(fn: Callable | None = None, *, mutating: bool = False):
    """装饰器。mutating=True 表示会改文件/发请求，只读模式下禁用。"""
    def wrapper(f: Callable) -> Callable:
        return _register(f, mutating)

    return wrapper if fn is None else wrapper(fn)


def set_disabled(names) -> None:
    """同步被停用的工具名，config 里的 tools.disabled。"""
    global DISABLED
    DISABLED = {str(n) for n in (names or []) if str(n) in REGISTRY}


def is_enabled(name: str) -> bool:
    return name in REGISTRY and name not in DISABLED


def set_scope(scope: str) -> None:
    global SCOPE
    SCOPE = "full" if scope == "full" else "workspace"


def schemas() -> list[dict]:
    """给 LLM 的 tools 参数，剔除内部字段和被停用的工具。"""
    return [
        {k: v for k, v in t.items() if k not in ("fn", "mutating", "plugin", "plugin_file")}
        for t in REGISTRY.values() if t["name"] not in DISABLED
    ]


WORKSPACE = os.path.abspath(
    os.environ.get("AGENT_WORKSPACE")
    or os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")
)


def _safe_path(path: str) -> str:
    full = os.path.abspath(os.path.expanduser(path))
    if SCOPE == "workspace":
        base = os.path.abspath(WORKSPACE)
        if not (full == base or full.startswith(base + os.sep)):
            raise PermissionError("只能用工作区内的路径（可在设置里开全盘访问）")
    elif full.startswith(_DANGER):
        raise PermissionError("这个目录是系统要害，全盘模式也不许碰")
    return full


# ---------------- 内置工具 ----------------
@tool
def list_files(pattern: str = "*") -> str:
    """列出工作区里匹配的文件路径，支持 ** 递归。"""
    hits = sorted(glob.glob(os.path.join(WORKSPACE, pattern), recursive=True))[:50]
    return "\n".join(os.path.relpath(h, WORKSPACE) for h in hits) or "（空）"


@tool
def read_file(path: str) -> str:
    """读取工作区内的一个文本文件。"""
    with open(_safe_path(path), "r", encoding="utf-8", errors="replace") as f:
        return f.read()[:8000]


@tool(mutating=True)
def write_file(path: str, content: str) -> str:
    """把文本写入工作区内的文件，覆盖原内容。"""
    full = _safe_path(path)
    os.makedirs(os.path.dirname(full) or WORKSPACE, exist_ok=True)
    with open(full, "w", encoding="utf-8") as f:
        f.write(content)
    return f"已写入 {path}（{len(content)} 字符）"


@tool
def calc(expression: str) -> str:
    """计算一个四则运算表达式，例如 (3+5)*2。"""
    allowed = set("0123456789+-*/(). ")
    if not set(expression) <= allowed:
        raise ValueError("只支持数字和 + - * / ( )")
    return str(eval(expression, {"__builtins__": {}}, {}))


@tool
def current_time(tz_offset: int = 8) -> str:
    """获取当前日期和时间，tz_offset 是时区小时数，默认东八区。"""
    from datetime import datetime, timedelta, timezone

    tz = timezone(timedelta(hours=tz_offset))
    now = datetime.now(tz)
    return now.strftime("%Y-%m-%d %H:%M:%S %A")


@tool
def search_text(keyword: str, pattern: str = "**/*") -> str:
    """在工作区文本文件里搜关键字，返回 文件:行号:内容，最多 30 条。"""
    out = []
    for p in glob.glob(os.path.join(WORKSPACE, pattern), recursive=True):
        if not os.path.isfile(p) or os.path.getsize(p) > 2_000_000:
            continue
        try:
            with open(p, "r", encoding="utf-8", errors="ignore") as f:
                for i, line in enumerate(f, 1):
                    if keyword in line:
                        out.append(f"{os.path.relpath(p, WORKSPACE)}:{i}: "
                                   + line.strip()[:160])
                        if len(out) >= 30:
                            return "\n".join(out)
        except OSError:
            continue
    return "\n".join(out) or f"没找到 {keyword!r}"


@tool
def file_info(path: str) -> str:
    """查看一个文件/目录的大小、类型、修改时间。"""
    import stat as _st

    full = _safe_path(path)
    if not os.path.exists(full):
        raise FileNotFoundError(f"{path} 不存在")
    st = os.stat(full)
    kind = "目录" if _st.S_ISDIR(st.st_mode) else "文件"
    import time as _t
    return (f"{kind} {path}：{st.st_size} 字节，"
            f"修改于 {_t.strftime('%Y-%m-%d %H:%M', _t.localtime(st.st_mtime))}")


@tool(mutating=True)
def mkdir(path: str) -> str:
    """在工作区里新建目录（自动建父目录）。"""
    full = _safe_path(path)
    os.makedirs(full, exist_ok=True)
    return f"目录 {path} 已就绪"


@tool(mutating=True)
def move_path(src: str, dst: str) -> str:
    """移动或重命名工作区内的文件/目录。"""
    import shutil

    a, b = _safe_path(src), _safe_path(dst)
    os.makedirs(os.path.dirname(b) or WORKSPACE, exist_ok=True)
    shutil.move(a, b)
    return f"已把 {src} 移到 {dst}"


@tool(mutating=True)
def delete_path(path: str) -> str:
    """删除工作区内的文件或空目录，慎用。"""
    import shutil

    full = _safe_path(path)
    if full == os.path.abspath(WORKSPACE):
        raise PermissionError("不能删工作区本身")
    if os.path.isdir(full):
        shutil.rmtree(full)
    else:
        os.remove(full)
    return f"已删除 {path}"


@tool(mutating=True)
def run_python(code: str) -> str:
    """执行一段 Python 代码（工作区目录里跑，30 秒超时），返回 stdout。"""
    import subprocess, sys, tempfile

    fd, tmp = tempfile.mkstemp(suffix=".py", dir=WORKSPACE)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(code)
        r = subprocess.run(
            [sys.executable, tmp], cwd=WORKSPACE, timeout=30,
            capture_output=True, text=True)
        out = (r.stdout or "").strip()
        err = (r.stderr or "").strip()
        head = out[:3000] if out else "（没有输出）"
        return head + (f"\n[stderr] {err[:800]}" if err else "")
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


@tool(mutating=True)
def append_file(path: str, content: str) -> str:
    """在文件末尾追加文本，文件不存在就新建。"""
    full = _safe_path(path)
    os.makedirs(os.path.dirname(full) or WORKSPACE, exist_ok=True)
    with open(full, "a", encoding="utf-8") as f:
        f.write(content)
    return f"已追加到 {path}（{len(content)} 字符）"


@tool(mutating=True)
def run_shell(command: str) -> str:
    """在工作区里执行一条 shell 命令（20 秒超时），返回 stdout 与 stderr。"""
    import subprocess

    r = subprocess.run(
        command, shell=True, cwd=WORKSPACE, timeout=20,
        capture_output=True, text=True)
    out = (r.stdout or "").strip()
    err = (r.stderr or "").strip()
    body = (out[:2500] if out else "（没有输出）")
    if err:
        body += f"\n[stderr] {err[:1000]}"
    return f"[exit {r.returncode}] {body}"


_TAG_RE = None


def _html_to_text(html: str) -> str:
    """粗暴但够用的正文提取：去脚本/样式/标签，压空行。"""
    global _TAG_RE
    import re

    html = re.sub(r"(?is)<(script|style|noscript|svg)[^>]*>.*?</\1>", " ", html)
    html = re.sub(r"(?is)<!--.*?-->", " ", html)
    html = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</li>|</h[1-6]>", "\n", html)
    text = re.sub(r"(?s)<[^>]+>", "", html)
    import html as _h

    text = _h.unescape(text)
    lines = [ln.strip() for ln in text.splitlines()]
    text = "\n".join(ln for ln in lines if ln)
    return re.sub(r"\n{3,}", "\n\n", text)


@tool(mutating=True)
def http_get(url: str) -> str:
    """抓取网页并返回可读正文（自动去掉 HTML 标签），仅支持 http/https。"""
    import httpx

    if not url.startswith(("http://", "https://")):
        raise ValueError("只支持 http/https 地址")
    with httpx.Client(timeout=20, follow_redirects=True) as c:
        r = c.get(url, headers={"User-Agent": "Mozilla/5.0 (tautaBot)"})
    r.raise_for_status()
    ctype = r.headers.get("content-type", "")
    body = r.text
    if "html" in ctype or body.lstrip().lower().startswith(("<!doctype", "<html")):
        body = _html_to_text(body)
    return f"[{r.status_code}] {ctype}\n{body[:4000]}"


@tool(mutating=True)
def http_post_json(url: str, body: str = "{}") -> str:
    """向一个地址 POST 一段 JSON 文本，返回状态码与响应前 3000 字。"""
    import httpx

    if not url.startswith(("http://", "https://")):
        raise ValueError("只支持 http/https 地址")
    try:
        payload = json.loads(body or "{}")
    except json.JSONDecodeError as e:
        raise ValueError(f"body 不是合法 JSON：{e}") from e
    with httpx.Client(timeout=20, follow_redirects=True) as c:
        r = c.post(url, json=payload,
                   headers={"User-Agent": "Mozilla/5.0 (tautaBot)"})
    return f"[{r.status_code}] {r.text[:3000]}"
