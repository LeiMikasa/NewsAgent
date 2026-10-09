# NewsAgent

每日 AI 新闻助手：agent loop、runtime、hooks、5 个基础工具、2 个 Tavily 新闻工具，以及基于 Markdown 的长期记忆。
代码结构参照 `D:\HarnessAgent` 的 Python 分层。支持按请求搜索新闻和提取网页正文，由 Agent 决定工具调用顺序；历史简报、每日调度与推送尚未实现。

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
    news.py        Tavily 搜索、正文提取、HTTP 和输出边界
  cli.py           单次请求与 REPL
  demo.py          离线工具流程
tests/
  test_agent.py    闭环及失败路径测试
  test_memory.py   跨启动持久化、偏好更新、召回缓存、记忆保护与引导测试
  test_news.py     新闻 API 参数、错误、截断、Hooks 与搜索/阅读闭环（离线）
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
| `search_news` | `query` | `time_range="day"`, `domains=[]`, `limit=10` | 搜索近期新闻，返回标题、链接、域名、估计日期、相关分数、内容片段 |
| `fetch_article` | `url` | `max_chars=12000`, `extract_depth="basic"` | 用 Tavily Extract 读取单篇网页，返回 Markdown 正文和截断信息 |

文件路径相对工作区解析，拒绝通过 `..`、绝对路径、symlink / junction 访问外部。文本单文件默认限制 2 MB，二进制与非 UTF-8 文件不作为文本读取。搜索跳过 `.git`、`.venv`、`venv`、`node_modules`、`__pycache__`、`.agent` 子目录，且不遍历目录链接；显式指定 `.agent/demo` 为搜索根目录时仍可搜索。

目录列表、搜索与 shell 有独立的输出/扫描上限。工具内容默认最多 20000 字符，超出时附加 `[output truncated]`；结构化内容的文本也可能被截断。搜索结果中的跳过计数与截断标记表示搜索范围不完整。

`write_file` 使用同目录临时文件与原子替换；`overwrite=false` 要求目标不存在。`bash` 自动优先使用 Bash，Windows 找不到时使用 PowerShell，POSIX 则使用 sh。`AGENT_SHELL` 可指定名称或可执行文件路径。Windows 隐藏 shell 窗口，超时尝试终止进程树。

**bash 使用宿主权限，cwd 不构成沙箱。** 文件工具的路径限制不会限制 shell 命令；可设置 `AGENT_ALLOW_BASH=false`。

## Tavily 新闻工具

在项目 `.env` 中填写自己的 `TAVILY_API_KEY`，可配置 `TAVILY_TIMEOUT=30`（单次 socket 超时，最多 120 秒）。这个 Key 与模型的 `ANTHROPIC_API_KEY` 独立；没有 Tavily Key 不妨碍基础工具与手动资料管理，新闻工具会返回 `MISSING_API_KEY`。本实现使用 Python 标准库 `urllib` 直接请求官方 REST API，不需要额外安装 Tavily SDK。

```dotenv
TAVILY_API_KEY=填写自己的Key
TAVILY_TIMEOUT=30
```

`search_news(query, time_range="day", domains=[], limit=10)` 固定向 `https://api.tavily.com/search` 发 POST 请求，使用 Bearer 认证、`topic="news"`、`search_depth="basic"`。`time_range` 支持 day/week/month/year，数量为 1..20；domains 最多 20 项，仅填写域名，例如 `example.com`，不填协议与路径。不限制域名时不发送 include_domains。关闭服务生成的 answer 和搜索时的全文获取，保留原始来源片段，由自己的 Agent 负责总结。新闻搜索主要面向新闻报道，并不保证覆盖全部官方博客或论文。

启用 `include_published_date` 和 `filter_by_published_date`：按服务估计的发布或更新时间筛选，无法识别日期的来源会被排除。day 是服务向前的一天窗口，不等于 Asia/Shanghai 的自然日；日期可能是更新日期，生成“今日新闻”时应核对原文。没有结果是一次成功的空查询，不证明没有相关事件，可由 Agent 改关键词或时间范围。

输出 results 中的每项包含 `title`、`url`、`source`（域名）、`published_date`、`score`、`snippet` 和 `snippet_truncated`。工具只去除重复 URL（移除 fragment 后比较），不做语义上的同事件聚类，也不自行计算用户相关性。Agent 使用注入的长期身份/偏好拟定查询词，再判断相关性和同事件重复报道。顶层有 `retrieved_at`（UTC）、`returned_count`、`duplicates_removed`、`skipped_results`、`results_omitted`、`truncated` 和可选值 `credits_used`（未返回时 null）。

`fetch_article(url, max_chars=12000, extract_depth="basic")` 向固定 `/extract` API 发送单 URL，指定 Markdown 格式；不传 query，避免返回按关键词重排的少数片段。深度可选 basic/advanced，后者通常用于普通提取失败后的尝试，费用更高。只接受 HTTP(S) 公开地址，拒绝带凭据的 URL、已知内网 IP、本地主机名和非 80/443 端口；URL 通过 Tavily 服务提取，宿主不会直接抓取目标网页。域名校验不验证 DNS、网页重定向或第三方提取服务的网络边界。

输出有 `url`、`source`、`content`、`format`、`extract_depth`、`original_chars`、`returned_chars`、`truncated`、`retrieved_at`、`credits_used`。正文可能被服务省略或提取失败；`truncated=false` 只表示本工具没有继续截短服务返回的正文，不保证它覆盖原网页全部内容。max_chars 范围 1..20000；最终序列化 JSON 也受 `AGENT_MAX_TOOL_OUTPUT` 约束。搜索先缩短片段，再省略尾部结果；正文通过二分搜索缩短文本，保持完整 JSON。极小输出预算返回 `OUTPUT_LIMIT`。

工具总是注册在默认 Runtime 中，沿用参数校验、PreToolUse/PostToolUse 和 tool_result 回送；传入自定义 ToolRegistry 时仍由调用者选择工具。两者标记 read_only，因为不写本地文件，但真实 API 调用会消耗 Tavily 额度。HTTP 响应最多读取 5 MB，不自动重试；不跟随 API 重定向，防止转发认证头。错误不回显原始异常或服务响应正文。401/403 为 AUTH_ERROR，429 为 RATE_LIMITED，432/433 为 QUOTA_EXCEEDED；网络超时、无效响应、提取失败分别返回明确错误，交给 Agent 决定下一步。

```python
from agent import Runtime

# create() 自动读取启动目录 .env。显式传入 Settings 时，应自己提供配置字段。
with Runtime.create() as runtime:
    news = runtime.call_tool("search_news", query="open source AI model release", time_range="week", limit=5)
    print(news.content)
    # 用搜索返回的真实链接调用正文工具：
    # article = runtime.call_tool("fetch_article", url="...", max_chars=12000)
    # print(article.content)
```

直接 call_tool 不需要主模型理解任务，所以配置 Tavily Key 后 mock 模式也能调用真实新闻 API。要让 Agent 自主搜索、阅读并生成简报，需要同时配置真实模型 provider、MODEL_ID 和模型 Key，然后运行：

```powershell
.\.venv\Scripts\python.exe -m agent "根据我的身份和偏好，整理近期 AI 新闻，并附来源链接" --verbose
```

提示词要求模型把新闻内容当作外部资料，不执行网页中的指令；区分搜索片段和正文、核对日期并给出来源。程序不强制固定“搜一次、读一次”的流程，也没有实现简报自动保存。

官方参考：[Search](https://docs.tavily.com/documentation/api-reference/endpoint/search)、[Extract](https://docs.tavily.com/documentation/api-reference/endpoint/extract)、[额度与计费](https://docs.tavily.com/documentation/api-credits)。离线测试替换 HTTP 传输层，不消耗 API 额度；真实联调需有效凭据和可达网络。

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
