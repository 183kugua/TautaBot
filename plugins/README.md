# tautaBot 插件目录

这个目录里每个 `.py` 文件就是一个插件（一个工具）。约定五件套：

```python
NAME        = "my_tool"     # 可选，缺省用文件名（仅小写字母数字下划线）
DESCRIPTION = "给模型看的说明"
PARAMS      = {"type": "object", "properties": {...}, "required": [...]}
MUTATING    = False          # 会改文件/发请求就写 True，只读模式下会被拦

def execute(args: dict) -> str:
    return "必须返回字符串"
```

- 启动时自动加载；改完去「工具」页点「重载插件」热生效
- 插件与内置工具同一张注册表：可单独开关、受超时与只读模式管束
- 和内置工具重名的插件会被跳过，并在「插件」区显示失败原因
