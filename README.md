# NewsAgent

每日 AI 新闻助手：agent loop、runtime、hooks、5 个基础工具，以及基于 Markdown 的长期记忆。
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
# 交互模式；首次询问身份与订阅偏好，/reset 清空对话，/exit 退出
.\.venv\Scripts\python.exe -m agent
```

其他参数：`--workdir` 指定工作区，`--env-file` 指定配置文件，`--max-turns` 限制模型轮数，`--no-bash` 禁用 shell，`--verbose` 输出进度及记忆诊断。

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
  memory.py        从 HarnessAgent 复用的长期记忆：保存、召回、提取、整理
  onboarding.py    首次身份与偏好引导、资料查看与编辑
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
  test_memory.py   跨启动持久化、偏好更新、召回缓存、记忆保护与引导测试
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

`runtime.messages` 在内存中保存会话，`runtime.stats` 汇总用户请求的模型轮数和工具调用次数，不包含额外的记忆模型调用。各 runtime 的 hooks 与对话状态相互独立；同一实例采用同步调用并阻止请求重入。同一工作区的 Runtime 会读取相同的持久记忆目录，当前面向单用户使用。本阶段未加入对话记录落盘、上下文压缩和调度器。

## 长期记忆

实现复用自 `D:\HarnessAgent\agent\memory.py`，保留 Markdown 文件、索引、相关记忆选择、对话提取与整理机制。只复用代码，NewsAgent 的记忆保存在自己的工作区。

首次在交互终端启动时会询问职业/角色/背景，以及新闻关注主题、来源、语言、深度和长度。直接保存用户输入，不依赖模型生成，因此 mock 模式也可以建立资料。输入 `/skip` 可跳过本次设置，空白会重新询问。

```text
.agent/memory/
  MEMORY.md             生成的记忆索引
  user-profile.md       身份与背景（固定加载）
  news-preferences.md    新闻订阅偏好（固定加载）
  <其他记忆>.md          相关时召回的稳定信息
```

资料完整后，下次启动不重复询问；只缺一项时仅补问该项。已跳过的当前会话不会被提示反复询问。首次引导只在普通交互终端运行，`--json`、非交互请求与 `--demo` 不调用 input；demo 关闭记忆，不创建用户资料。

```powershell
# 只建立或修改资料，完成后退出
.\.venv\Scripts\python.exe -m agent --setup-profile
# 保留资料加载与手动编辑，关闭对话结束后的自动提取
.\.venv\Scripts\python.exe -m agent --no-auto-memory
# 完全关闭记忆和引导
.\.venv\Scripts\python.exe -m agent --no-memory
```

交互命令：`/profile` 查看身份与偏好；`/profile edit` 修改，已有字段留空时保留；`/memory` 查看记忆索引；`/reset` 清空当前对话，保留磁盘记忆。

Runtime 持有 `memory`，并使用 `build_system()` 组装基础提示词、固定身份/偏好和按请求选择的其他记忆。普通记忆每次用户请求只选择一次，工具循环复用缓存；下一次请求重新选择。核心资料不依赖模型选择，始终加载。

Stop hook 标记提取意图，Runtime 在请求实际以 `final` 结束后提取一次，避免 Stop 续跑时重复提取；失败、拒绝、轮数耗尽时不自动保存。提取只检查本次请求的对话，要求模型提供用户原文证据，并仅接收 `persistent` 信息。助手推测、工具结果与当前任务状态不作为长期事实。

用户明确变更身份或长期偏好时，更新对应固定记录，并要求模型保留未更改的内容。`今天只看…`、`这次…` 等临时要求不会覆盖长期资料。核心记录不参与自动整理，不会被记忆合并和裁剪。一般记忆达到 10 条时整理，核心两条不计入阈值。

每个 Markdown 文件采用临时文件加原子替换；整理失败时尝试恢复原始一般记忆。模型错误或不完整的记忆响应不会使正常对话失败，使用 `--verbose` 查看诊断，`/profile` 检查实际保存内容。

默认 mock 不理解任意自然语言，也不自动提取新记忆：它能保存/加载手动资料，并对一般记忆用关键词作召回后备。真实模型用于自动提取和偏好更正；离线测试通过独立 `memory_llm` 脚本验证这些行为，不消耗主 Agent 的 mock 脚本。

配置：`AGENT_MEMORY_ENABLED=true`，`AGENT_MEMORY_AUTO_EXTRACT=true`，`AGENT_MEMORY_DIR=.agent/memory`（默认，路径必须在工作区子目录）。默认目录已由 `.gitignore` 排除；使用其他目录时，应将该目录加入忽略规则。

```python
with Runtime.create(Settings(workdir=Path("D:/NewsAgent"))) as runtime:
    runtime.update_profile(identity="AI 产品经理", preferences="开源与应用，中文，每天五条")
    print(runtime.memory.profile())
    result = runtime.submit("请按我的偏好处理这次任务")
```

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
