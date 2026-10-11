# NewsAgent

每日 AI 新闻助手，根据用户身份、订阅偏好和本次要求搜索新闻、阅读文章并生成个性化简报。新闻主题由用户选择，可关注财经、体育、教育、科技等领域。

支持浏览器阅读、历史简报、长期记忆、每日定时生成和 QQ 邮箱推送，也提供命令行交互入口。

## 功能

- **个性化简报**：根据身份和偏好检索、筛选新闻，输出正文、来源链接和日期。
- **新闻工具**：使用 Tavily 搜索新闻、提取文章正文，工具调用顺序由 Agent 决定。
- **长期记忆**：身份和偏好保存为 Markdown；核心资料固定加载，其他记忆按请求召回。
- **历史简报**：生成成功后归档，支持浏览、读取和下载 Markdown。
- **每日订阅**：在前端设置北京时间的生成时间，支持启用和暂停。
- **邮件推送**：定时简报成功保存后，可通过 QQ 邮箱发送正文和 Markdown 附件。
- **Agent 基础能力**：工具调用、生命周期钩子、工具失败恢复，以及空响应和截断响应恢复。

## 快速开始

需要：

- Python **3.12 或更高版本**。
- Node.js **22.12 或更高版本**，包含 npm。
- Git。
- 一个可用的模型 API Key 和模型 ID，以及 Tavily API Key。

以下命令使用 Windows PowerShell，在你希望存放项目的目录中执行。

### 1. 下载项目并安装后端依赖

```powershell
git clone https://github.com/LeiMikasa/NewsAgent.git
cd NewsAgent

python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt

Copy-Item .env.example .env
```

如果已经有自己的 `.env`，保留现有文件，按 `.env.example` 补充需要的配置即可。

### 2. 配置模型和新闻工具

用文本编辑器打开项目根目录下的 `.env`，修改以下字段。示例中的模型 ID 和密钥需要替换为你自己的值：

```dotenv
AGENT_PROVIDER=deepseek
MODEL_ID=填写账号支持的模型ID
ANTHROPIC_API_KEY=填写模型服务的APIKey

TAVILY_API_KEY=填写Tavily的APIKey

AGENT_MEMORY_ENABLED=true
EMAIL_ENABLED=false
```

当前支持 `deepseek` 和 `anthropic` 两种真实模型配置，使用 Anthropic Messages 兼容接口：

- `deepseek` 默认使用 `https://api.deepseek.com/anthropic`，通常不需要填写 `ANTHROPIC_BASE_URL`。
- 使用 Anthropic 时，将 `AGENT_PROVIDER` 改为 `anthropic`，填写对应的模型 ID 和 API Key；需要自定义端点时设置 `ANTHROPIC_BASE_URL`。
- 两种配置都通过 `ANTHROPIC_API_KEY` 读取模型密钥。Tavily 使用独立的 `TAVILY_API_KEY`。

`.env.example` 默认使用 `mock`，适合离线验证工具执行；生成真实新闻简报需要切换到真实模型并配置 Tavily。

### 3. 构建前端并启动

```powershell
cd frontend
npm.cmd ci
npm.cmd run build
cd ..

.\.venv\Scripts\python.exe -m backend
```

打开：

- 浏览器界面：<http://127.0.0.1:8000>
- API 文档：<http://127.0.0.1:8000/docs>

首次使用，在「订阅设置」中填写身份和新闻偏好并保存，例如：

> 身份：市场营销从业者，关注消费行业。
>
> 偏好：消费、零售和品牌新闻；中文，每天 5 条，附来源链接和简短解读。

然后回到「我的简报」，点击生成。生成完成后，可以阅读正文、下载 Markdown，并在「历史简报」中查看归档。

前端构建完成后，日常只需在项目根目录运行：

```powershell
.\.venv\Scripts\python.exe -m backend
```

终端按 `Ctrl+C` 停止服务。修改 `.env` 后需要重启后端；修改前端代码后需要重新构建。

### macOS / Linux

克隆仓库后，后端安装和配置命令改为：

```bash
cd NewsAgent
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cp .env.example .env
```

按上面的说明编辑 `.env`，再构建和启动：

```bash
cd frontend
npm ci
npm run build
cd ..
.venv/bin/python -m backend
```

## 每日订阅和邮件

### 设置每日时间

在「订阅设置」中选择北京时间的生成时间，开启订阅并保存。修改时间后会更新调度，无需重启后端。

定时任务由后端进程执行，需要保持电脑开机、处于唤醒状态，并保持后端运行。CLI 不启动定时任务。

当前同一天定时生成最多尝试一次；当天已经尝试过后，修改时间不会再生成一次。生成失败可以使用手动生成入口重试。后端启动时会检查当天订阅时间，在到期后一小时内尝试补跑；历史中已存在当天非跳过的定时记录时仍会跳过。

### 启用 QQ 邮箱推送

在 QQ 邮箱中开启 SMTP 服务，获取邮箱授权码，然后修改 `.env`：

```dotenv
EMAIL_ENABLED=true
SMTP_HOST=smtp.qq.com
SMTP_PORT=465
SMTP_USERNAME=你的QQ邮箱@qq.com
SMTP_PASSWORD=你的邮箱授权码
# 可选；不填写时发送到 SMTP_USERNAME。
EMAIL_TO=收件邮箱@example.com
```

重启后端。定时简报成功归档后会自动发送；手动生成的简报需要在任务记录中点击发送邮件。邮件失败时可以重发已有简报，不需要重新调用模型生成正文。

`SMTP_PASSWORD` 填写邮箱授权码，不是邮箱登录密码。前端只显示配置状态，不填写或保存密钥。

## 命令行使用

在项目根目录运行。以下为 Windows 命令，macOS / Linux 将 Python 路径替换为 `.venv/bin/python`。

```powershell
# 离线验证：执行五个基础工具，无需模型和 Tavily API Key。
.\.venv\Scripts\python.exe -m agent --demo --verbose

# 建立或修改身份、新闻偏好。
.\.venv\Scripts\python.exe -m agent --setup-profile

# 按长期偏好生成并保存今日简报。
.\.venv\Scripts\python.exe -m agent --brief --verbose

# 指定本次主题和归档标题。
.\.venv\Scripts\python.exe -m agent "整理近期体育新闻，附来源链接" --brief --brief-title "体育简报"

# 查看历史，使用列表返回的 ID 读取简报。
.\.venv\Scripts\python.exe -m agent --list-briefs
.\.venv\Scripts\python.exe -m agent --read-brief 简报ID

# 进入交互模式。
.\.venv\Scripts\python.exe -m agent
```

交互命令：`/brief` 生成并保存，`/briefs` 查看历史，`/read-brief ID` 读取正文，`/profile` 查看资料，`/profile edit` 修改资料，`/memory` 查看记忆索引，`/reset` 清空当前对话，`/exit` 退出。

离线 demo 会写入并覆盖 `.agent/demo/news.txt`。默认 mock 不能生成真实简报。普通请求不自动归档，`--brief`、`/brief` 和后端简报入口会生成后保存。

## 工具与执行流程

| 工具 | 用途 |
| --- | --- |
| `list_dir(path)` | 列出目录内容 |
| `read_file(path)` | 读取 UTF-8 文件 |
| `search_content(keyword, dir)` | 在目录下搜索文件内容 |
| `write_file(path, content)` | 写入文件 |
| `bash(command)` | 执行 shell 命令 |
| `search_news(query)` | 搜索近期新闻 |
| `fetch_article(url)` | 提取文章正文 |

Runtime 组装配置、模型、工具和记忆。Agent Loop 将工具定义交给模型，执行模型选择的工具，再把结果回送模型，直到产生最终回答。

安全只读工具的临时故障支持有限的程序重试；模型修正工具参数有独立预算。文件写入和 shell 命令不自动重试。空响应和 `max_tokens` 截断会触发有限恢复，仍受总轮数限制。

文件工具只允许访问工作区内的路径；`bash` 使用宿主权限，可通过 `.env` 中的 `AGENT_ALLOW_BASH=false` 禁用。

## 本地数据与配置

默认数据保存在启动目录下的 `.agent/`：

```text
.agent/
  memory/
    MEMORY.md             记忆索引
    user-profile.md       身份和背景
    news-preferences.md   长期新闻偏好
    ...                   其他长期记忆
  briefs/                 Markdown 简报和元信息
  backend.sqlite3         订阅设置、任务和邮件状态
```

无需另外安装数据库。备份或迁移时，停止后端后复制 `.agent/`；换机器时重新安装依赖，并自行配置 `.env`。

身份和新闻偏好始终加载，其他记忆按请求选择并在工具循环中复用。自动记忆提取默认开启：请求成功结束后检查用户明确表达的长期信息，通过校验后保存。仅本次任务的要求不应覆盖长期偏好。设置 `AGENT_MEMORY_AUTO_EXTRACT=false` 可关闭自动提取，保留手动编辑和记忆加载。

配置优先级为：显式参数 > 进程环境变量 > `.env` > 默认值。默认读取启动目录下的 `.env`，工作区默认也是启动目录；可用 `--env-file` 和 `--workdir` 指定。

`.env` 和 `.agent/` 已加入 Git 忽略规则，密钥、身份偏好和私人简报无需提交到仓库。完整配置项见 [.env.example](.env.example)。

## 常见问题

| 现象 | 检查方式 |
| --- | --- |
| 打开页面提示前端尚未构建 | 在 `frontend` 中运行 `npm.cmd ci` 和 `npm.cmd run build` |
| 无法生成真实简报 | 检查是否仍使用 `mock`，以及模型 ID、模型 Key 和 Tavily Key 是否正确 |
| 新闻工具返回 `MISSING_API_KEY` | 在 `.env` 中填写 `TAVILY_API_KEY`，重启后端 |
| 配置修改后没有生效 | 重启后端，并检查进程环境变量是否覆盖 `.env` |
| 到订阅时间没有生成 | 检查后端是否运行、电脑是否休眠、订阅是否启用、当天是否已有定时记录；手动任务占用工作线程也可能导致定时任务被跳过 |
| 简报生成了但没有邮件 | 检查邮件是否启用、配置是否就绪、是否使用邮箱授权码，以及任务的独立邮件状态 |
| 8000 端口已占用 | 使用 `.\.venv\Scripts\python.exe -m backend --port 8001`，访问对应端口 |

## 项目结构与开发

```text
agent/       Runtime、Agent Loop、模型客户端、工具、记忆、CLI
backend/     FastAPI 接口、后台任务、定时调度、SQLite 状态、邮件发送
frontend/    React + TypeScript + Vite 浏览器界面
tests/       工具、记忆、简报、后端与失败恢复测试
docs/        接口说明和代码说明
```

前端开发时，先启动后端，再在另一个终端运行：

```powershell
cd frontend
npm.cmd run dev
```

访问 <http://127.0.0.1:5173>，开发服务器将 `/api` 代理到本机 8000 端口。后端使用其他端口时，修改 `frontend/vite.config.ts` 中的代理地址。

更多说明：

- [前端使用与开发](docs/frontend.md)
- [后端接口与调度规则](docs/backend.md)
- [QQ 邮箱配置与发送状态](docs/email.md)
- [工具失败恢复](docs/tool-recovery.md)
- [Service 代码说明](docs/service-code-guide.md)

当前面向本地单用户，使用单个后端进程和工作线程。许可证见 [LICENSE](LICENSE)。
