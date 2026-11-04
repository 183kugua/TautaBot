"""内置插件模板：插件页一键安装，装完即用。"""

WEATHER_MAIN = '''"""天气查询插件：调 wttr.in 免费接口，无需密钥。"""
import httpx

DESCRIPTION = "查一个城市的天气（数据来自 wttr.in）"
CONFIG_SCHEMA = {"type": "object",
                 "properties": {"default_city": {"type": "string"}},
                 "required": []}


def execute(args, ctx):
    city = (args or {}).get("city") or ctx["config"].get("default_city") or "Beijing"
    r = httpx.get(f"https://wttr.in/{city}?format=3", timeout=10,
                  headers={"User-Agent": "curl/8"})
    return r.text.strip() or "（接口没返回内容）"
'''

TODO_MAIN = '"""待办清单插件：数据存自己的 data_dir，重启不丢。"""\nimport json\nimport os\n\nDESCRIPTION = "待办清单：todo_add / todo_list / todo_done 三个工具 + /todo 命令"\n\n\ndef _path(ctx):\n    return os.path.join(ctx["data_dir"], "todo.json")\n\n\ndef _load(ctx):\n    try:\n        with open(_path(ctx), "r", encoding="utf-8") as f:\n            return json.load(f)\n    except (OSError, json.JSONDecodeError):\n        return []\n\n\ndef _save(ctx, lst):\n    with open(_path(ctx), "w", encoding="utf-8") as f:\n        json.dump(lst, f, ensure_ascii=False, indent=1)\n\n\ndef _render(lst):\n    if not lst:\n        return "清单空空如也。"\n    return "\\n".join(f"{\'x\' if t[\'done\'] else \' \'} {i + 1}. {t[\'text\']}"\n                      for i, t in enumerate(lst))\n\n\ndef todo_add(args, ctx):\n    lst = _load(ctx)\n    lst.append({"text": args["text"], "done": False})\n    _save(ctx, lst)\n    return f"已加：{args[\'text\']}（共 {len(lst)} 条）"\n\n\ndef todo_list(args, ctx):\n    return _render(_load(ctx))\n\n\ndef todo_done(args, ctx):\n    lst = _load(ctx)\n    i = int(args["index"]) - 1\n    if not (0 <= i < len(lst)):\n        return f"没有第 {args[\'index\']} 条"\n    lst[i]["done"] = True\n    _save(ctx, lst)\n    return f"完成：{lst[i][\'text\']}"\n\n\nTOOLS = [\n    {"name": "todo_add", "description": "加一条待办",\n     "params": {"type": "object", "properties": {"text": {"type": "string"}},\n                "required": ["text"]},\n     "execute": todo_add},\n    {"name": "todo_list", "description": "列全部待办",\n     "params": {"type": "object", "properties": {}},\n     "execute": todo_list},\n    {"name": "todo_done", "description": "把第 N 条（从 1 开始）标记完成",\n     "params": {"type": "object", "properties": {"index": {"type": "integer"}},\n                "required": ["index"]},\n     "execute": todo_done},\n]\n\nCOMMANDS = [{"name": "todo", "description": "看待办清单，/todo 直接发",\n             "execute": lambda text, ctx: _render(_load(ctx))}]\n'

TEMPLATES = {
    "weather": {
        "name": "weather", "version": "1.0", "author": "tautaBot",
        "description": "天气查询：wttr.in 免费接口，无需密钥，支持默认城市配置",
        "files": {"main.py": WEATHER_MAIN,
                  "metadata.yaml": 'name: weather\nversion: "1.0"\nauthor: tautaBot\n'
                                   'description: "天气查询（wttr.in）"\n',
                  "config.yaml": "default_city: Beijing\n",
                  "requirements.txt": ""},
    },
    "todo": {
        "name": "todo", "version": "1.0", "author": "tautaBot",
        "description": "待办清单：三个工具 + /todo 命令，数据存插件自己的目录",
        "files": {"main.py": TODO_MAIN,
                  "metadata.yaml": 'name: todo\nversion: "1.0"\nauthor: tautaBot\n'
                                   'description: "待办清单（数据目录持久化）"\n',
                  "config.yaml": "# 无需配置\n",
                  "requirements.txt": ""},
    },
}
