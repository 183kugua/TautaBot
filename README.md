# tautaBot · 单智能体框架

> **v0.9.1 · 细浪** · The AI under the aurora


一个能自己调工具的云端智能体，带网页控制台。单用户、无登录、靠令牌鉴权。

```
浏览器 ──SSE──> FastAPI ──> 代理循环 ──> 模型网关(OpenAI 兼容)
                                │
                                └──> 工具注册表（装饰器注册，带越界防护）
```

## 快速开始

```bash
cp .env.example .env      # 填 AGENT_API_KEY 和 AGENT_ACCESS_TOKEN
.venv/bin/python -m uvicorn api.server:app --host 0.0.0.0 --port 8090
```

打开 `http://<你的地址>:8090/?token=<AGENT_ACCESS_TOKEN>`，令牌会存进浏览器。

## 已实现

**对话**
- 推理模型思考过程流式显示（可折叠回看，不进历史）
- 逐字流式输出（真 SSE，不是假打字机），15 秒心跳保活
- 同一轮多个工具调用并行执行（默认 4 并发），每个结果带耗时
- 服务端停止：断开前端的同时让后端也停下，不再白烧 token
- 工具调用可视化：调用名、参数、返回值、耗时、失败原因
- 回答结束自动排 Markdown（代码块、表格、列表、引用、粗体、链接）
- 中途可停止生成
- 消息一键复制（含代码块独立复制）、消息时间戳、气泡 token 用量
- 会话内查找高亮；最后一条可重发
- 会话列表、搜索、删除、重命名、自动命名、条数徽标、多会话切换
- 一键清空当前会话；导出 Markdown / JSON / HTML

**模型**
- 多服务商管理：各自独立地址和密钥，一键切换，各自记住上次用的模型
- 常见服务商地址模板（OpenAI / DeepSeek / Moonshot / 智谱 / 通义 / 硅基流动 / 本地 Ollama）
- 拉取模型列表、连通性测试（返回延迟与用量）
- 模型页直接切换，不用改配置文件

**插件**
- 目录包形态：metadata.yaml（信息）/ main.py（代码）/ config.yaml（配置）/ requirements.txt（依赖）分门别类，旧单文件兼容
- AstrBot 级能力：TOOLS 多工具、COMMANDS 斜杠命令（/name 直达不调模型）、ROUTES 程序调用口、on_load/on_unload/on_message/on_response 钩子、INTERVAL+tick 定时、CONFIG_SCHEMA 配置提示、ctx（config/data_dir/log）
- 插件管理页：清单、启停、删除、粘贴代码安装、zip 上传安装、模板一键安装、配置可视化编辑、失败回显
- 热重载、失败隔离、只读拦截、超时与单工具开关全适用

**工具**
- `@tool` 装饰器注册，自动导出 JSON Schema
- 内置 15 个：`calc` `current_time` `read_file` `write_file` `append_file`
  `list_files` `search_text` `file_info` `mkdir` `move_path` `delete_path`
  `run_python` `run_shell` `http_get` `http_post_json`
- 超长工具输出自动落盘 `data/tool_outputs/`，只把头部喂回模型
- 模型网关 429 / 5xx / 网络抖动自动退避重试
- 工作区路径越界防护
- 工具超时（子线程执行）
- 只读模式（`AGENT_READONLY=1` 时拦截写操作）
- 工具出错把错误喂回模型，允许重试（`max_tool_retries`）
- 工具页可直接填参数在线调试

**记忆**
- 短期窗口，落盘 JSON，重启不丢
- 长期摘要：把早前对话压成 200 字要点，省上下文
- 窗口快满时主动提示
- token 预算裁剪：超预算先丢最旧消息（`memory.context_budget`）

**运维**
- 实时日志：网页「日志」视图 SSE 推送，ERROR/WARNING 分色，断线自动重连
- 启动自动备份会话数据到 `data/backups/`，保留 5 份；设置页一键恢复
- 长会话分页渲染（默认最近 50 条，按需加载更早）
- 按天统计调用次数与 token（`/api/stats`）
- 运行状态：已运行时长、内存、会话数（`/api/status`）
- 会话导出 Markdown / JSON / HTML，压缩前原文自动归档
- 新会话自动起名；工具调用按天落审计日志，工具页可看最近记录
- 令牌鉴权、systemd 常驻
- PWA：可「添加到主屏幕」，全平台图标一套齐

## 配置

`config.yaml`：

```yaml
name: tautaBot
model:
  name: gpt-4o-mini
  temperature: 0.7
  top_p: 1.0
  max_tokens: 2048
  timeout: 300
  max_steps: 8            # 单轮最多几跳工具调用
  max_tool_retries: 3
memory:
  window_limit: 20
  context_budget: 24000       # 估算超预算就丢最旧的消息
server:
  host: 0.0.0.0
  port: 8090
providers:                # 多服务商，密钥只存在服务器上
  - id: p_default
    label: 默认服务商
    base_url: https://api.openai.com/v1
    api_key: ''
active_provider: p_default
```

`.env`（权限 600，不进版本库）：

```
AGENT_API_KEY=            # 环境密钥优先于配置文件
AGENT_ACCESS_TOKEN=       # 访问令牌，必填
AGENT_WORKSPACE=/opt/agent/workspace
AGENT_READONLY=0
AGENT_TOOL_TIMEOUT=30
```

## 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/chat` | SSE 对话流 |
| GET | `/api/history` | 会话历史与摘要 |
| GET/DELETE | `/api/sessions` `/api/session/{id}` | 会话列表 / 删除 |
| POST | `/api/compact` | 压缩历史为摘要 |
| GET | `/api/tools` | 工具清单 |
| POST | `/api/tool/run` | 单次执行工具（调试用） |
| GET/POST/DELETE | `/api/providers` | 服务商管理 |
| POST | `/api/providers/active` | 切换服务商 |
| GET | `/api/models` | 模型列表 |
| POST | `/api/model/test` `/api/model/select` | 测试 / 切换模型 |
| GET | `/api/stats` `/api/status` | 用量 / 运行状态 |
| GET/POST | `/api/config` | 读写配置 |
| GET | `/api/export?fmt=markdown\|json\|html` | 导出会话 |
| POST | `/api/session/{id}/undo` | 撤掉最后一轮问答 |
| GET | `/api/archive/{id}` | 压缩归档列表 / 原文 |
| GET | `/api/logs` `/api/logs/stream` | 日志尾部 / 实时日志（SSE） |
| POST | `/api/reset` | 清空会话 |
| POST | `/api/session/{id}/undo` | 撤销上一轮 |
| GET | `/api/tool_logs` `/api/tool_logs/summary` | 工具审计日志 / 聚合 |
| GET | `/api/provider-templates` | 常见服务商地址模板 |
| GET | `/api/health` | 健康检查 |

除 `/` 与 `/api/health` 外，全部需要 `?token=` 或 `Authorization: Bearer`。

## 部署

```bash
sudo cp /opt/agent/deploy/agent.service /etc/systemd/system/
sudo systemctl enable --now agent
```

unit 要点：`WorkingDirectory=/opt`、`PYTHONPATH=/opt`、服务名放进包父目录才不会被同名包遮住。

## 结构

```
core/llm.py      模型网关：流式、工具调用聚合、重试、用量
core/tools.py    工具注册表与内置工具
core/loop.py     代理循环：多跳工具、超时、只读拦截
core/stats.py    按天用量统计
tests/smoke.py   零网络冒烟测试
memory/window.py 窗口 + 摘要 + 落盘
api/server.py    FastAPI 与全部接口
web/index.html   单文件控制台
```
