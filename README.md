# NewsAgent

每日 AI 新闻助手的第一阶段：搭建 agent loop、runtime、hooks 和 5 个基础工具。
代码结构参照 `D:\HarnessAgent` 的 Python 分层，后续可以在此基础上接入新闻采集、摘要、去重、每日调度与推送。

## 运行

需要 Python 3.12+。项目已建立 `.venv` 并安装依赖，直接在 PowerShell 运行：

```powershell
cd D:\NewsAgent
.\.venv\Scripts\python.exe -m agent --demo --verbose
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

首次安装或换机器时：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

demo 使用离线脚本驱动实际的 loop：写入 `.agent/demo/news.txt`，列出目录、读取、搜索，执行 `echo`，共 3 次模型响应、5 次工具调用。重复运行会覆盖该示例文件。任何工具失败都会使 demo 以非零状态退出。

默认 provider 为 `mock`，不需要 API key。普通 mock 请求只回复框架状态；任意任务需要真实模型或自行传入 mock `script` / `responder`。

```powershell
# 单次请求，结构化输出
.\.venv\Scripts\python.exe -m agent "检查当前目录" --json
# 交互模式；/reset 清空对话，/exit 退出
.\.venv\Scripts\python.exe -m agent
```

其他参数：`--workdir` 指定工作区，`--env-file` 指定配置文件，`--max-turns` 限制模型轮数，`--no-bash` 禁用 shell，`--verbose` 输出进度。

## 接入真实模型

```powershell
Copy-Item .env.example .env
```

编辑 `.env`：设置 `AGENT_PROVIDER=anthropic` 或 `deepseek`，填写服务实际支持的 `MODEL_ID`、自己的 `ANTHROPIC_API_KEY`，以及可选 `ANTHROPIC_BASE_URL`。DeepSeek 模式未指定端点时使用 `https://api.deepseek.com/anthropic`。

两种 live provider 沿用 HarnessAgent 的 Anthropic Messages 兼容接口。`.env` 已加入忽略规则；本项目未读取或复制 HarnessAgent 的密钥。真实 API 调用需要自行配置凭据，离线验证不发送模型 API 请求。

配置优先级：显式参数 > 进程环境变量 > `.env` > 默认值。默认读取启动目录下的 `.env`，工作区默认是启动目录。加载配置不会修改进程环境变量。

## 结构与执行流程

```text
agent/
  config.py        配置与校验
  llm.py           统一 LLMClient、Anthropic 兼容客户端、MockLLM
  loop.py          run_loop、execute_tool、LoopResult
  runtime.py       组件组装、会话消息、统计、生命周期
  events.py        Hooks、StopDirective
  prompt.py        系统提示词
  tools/
    registry.py    工具定义、JSON Schema 校验、dispatch
    result.py      统一成功/失败结果
    basic.py       5 个基础工具
  cli.py           单次请求与 REPL
  demo.py          离线工具流程
tests/
  test_agent.py    闭环及失败路径测试
```

流程：`submit → UserPromptSubmit → 模型 → PreToolUse → 工具 → PostToolUse → tool_result → 模型 → Stop`。

loop 支持单轮多个工具调用，顺序执行并按调用 ID 回送结果。每轮重新读取工具定义，新增工具无需修改 loop。`LoopResult` 包含 `text`、`turns`、`tool_calls`、`stop_reason` 和 `error`；只有 `stop_reason=final` 时 `ok=True`。

工具错误回送模型用于修正或解释；有副作用的工具不自动重试。模型失败返回 `error`，轮数耗尽返回 `max_turns`，响应截断返回 `incomplete`。收到 `max_tokens` / `pause_turn` 时不会执行响应中的工具。

## 工具签名

模型工具参数采用 JSON object，拒绝缺失、类型错误及未声明参数。

| 工具 | 必需参数 | 可选参数 | 行为 |
| --- | --- | --- | --- |
| `list_dir` | `path` | `limit=200` | 列出直接子项名称、类型、大小 |
| `read_file` | `path` | `offset=1`, `limit` | 读取 UTF-8 原文；窗口以 1 开始的行号计数 |
| `search_content` | `keyword`, `dir` | `case_sensitive=false`, `max_results=100`, `max_files=5000` | 递归字面量搜索，返回文件路径、行号、匹配行 |
| `write_file` | `path`, `content` | `overwrite=true` | UTF-8 写入，创建父目录；返回字节数 |
| `bash` | `command` | `timeout=30` 秒，最多 300 秒 | 返回 shell、退出码、stdout、stderr、超时与截断标记 |

文件路径相对工作区解析，拒绝通过 `..`、绝对路径、symlink / junction 访问外部。文本单文件默认限制 2 MB，二进制与非 UTF-8 文件不作为文本读取。搜索跳过 `.git`、`.venv`、`venv`、`node_modules`、`__pycache__`、`.agent` 子目录，且不遍历目录链接；显式指定 `.agent/demo` 为搜索根目录时仍可搜索。

目录列表、搜索与 shell 有独立的输出/扫描上限。工具内容默认最多 20000 字符，超出时附加 `[output truncated]`；结构化内容的文本也可能被截断。搜索结果中的跳过计数与截断标记表示搜索范围不完整。

`write_file` 使用同目录临时文件与原子替换；`overwrite=false` 要求目标不存在。`bash` 自动优先使用 Bash，Windows 找不到时使用 PowerShell，POSIX 则使用 sh。`AGENT_SHELL` 可指定名称或可执行文件路径。Windows 隐藏 shell 窗口，超时尝试终止进程树。

**bash 使用宿主权限，cwd 不构成沙箱。** 文件工具的路径限制不会限制 shell 命令；可设置 `AGENT_ALLOW_BASH=false`。

## Runtime 与 Hooks

```python
from pathlib import Path
from agent import Runtime, Settings
from agent.events import PRE_TOOL_USE, POST_TOOL_USE

with Runtime.create(Settings(workdir=Path("D:/NewsAgent"))) as runtime:
    @runtime.hooks.on(PRE_TOOL_USE)
    def guard(block, ctx):
        if block["name"] == "bash":
            return "本会话仅允许文件工具"  # 非 None 即否决

    @runtime.hooks.on(POST_TOOL_USE)
    def observe(block, result, ctx):
        print(block["name"], result.ok)

    result = runtime.call_tool("write_file", path=".agent/example.txt", content="hello AI\n")
    print(result.ok, result.content)
```

| Hook | 回调签名 | 返回值 |
| --- | --- | --- |
| `SessionStart` | `(ctx)` | 观察；首次 submit / call_tool 前触发 |
| `UserPromptSubmit` | `(prompt, ctx)` | `None` 放行；其他值拦截 |
| `PreToolUse` | `(block, ctx)` | `None` 放行；其他值拦截 |
| `PostToolUse` | `(block, result, ctx)` | 观察；对校验失败、拦截与实际执行均触发 |
| `Stop` | `(loop_result, ctx)` | `None` 结束；或返回 `StopDirective` |
| `SessionEnd` | `(ctx)` | close 时触发一次 |

Stop hook 返回 `StopDirective("continue", "请再检查文件内容")` 可以继续模型循环，仍受总轮数限制；返回 `StopDirective("return", "被策略阻止", "blocked")` 可自定义结束状态。

PreToolUse hook 异常会阻止执行。PostToolUse 异常保留实际工具结果、继续通知其他观察者，并记录在 `runtime.ctx.extra["hook_errors"]`。

`runtime.messages` 在内存中保存会话，`runtime.stats` 汇总请求数、模型轮数和工具调用次数。各 runtime 的 hooks 与状态相互独立；同一实例采用同步调用并阻止请求重入。本阶段未加入会话落盘、长期记忆、上下文压缩和调度器。

## 扩展工具

```python
from agent.tools import Tool, ToolResult

runtime.tools.register(Tool(
    name="ping",
    description="Return pong",
    input_schema={"type": "object", "additionalProperties": False},
    handler=lambda args, ctx: ToolResult.success("pong"),
    read_only=True,
))
```

测试覆盖工具闭环、中文读写、覆盖保护、参数错误、工作区越界、搜索边界、hook 拦截/异常、停止续跑、轮数耗尽、响应截断、模型失败、会话隔离、shell 错误/超时/截断及配置优先级。链接测试在宿主不允许创建 symlink 时自动跳过。
