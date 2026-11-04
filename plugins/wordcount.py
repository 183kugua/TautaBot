"""字数统计：统计一段文本的字符数、词数与行数。"""
DESCRIPTION = "统计一段文本的字符数、词数（按空白切）与行数。"
PARAMS = {
    "type": "object",
    "properties": {"text": {"type": "string", "description": "要统计的文本"}},
    "required": ["text"],
}


def execute(args):
    text = str(args.get("text") or "")
    return (f"字符 {len(text)} 个 / 词 {len(text.split())} 个 / "
            f"行 {len(text.splitlines()) or (1 if text else 0)} 行")
