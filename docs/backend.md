# 单用户后端与定时生成

启动一个 FastAPI 进程，包含 APScheduler 和一个生成任务工作线程。前端关闭不影响任务；后端进程退出或电脑关机期间不能执行。新闻的搜索、阅读与整理仍由 Agent 决定，调度器只负责触发。

## 启动与试用

```powershell
cd D:\NewsAgent
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m backend
```

访问 `http://127.0.0.1:8000/docs`，用 FastAPI 自带的接口页面测试：

1. `GET /api/profile` 查看是否缺少身份和偏好。
2. `PUT /api/profile` 保存资料。
3. `POST /api/generate` 提交 `{}`，使用 `.env` 中的真实模型和新闻凭据生成简报。
4. 使用返回的 `id` 查询 `GET /api/runs/{id}`；成功后用 `brief_id` 读取正文。
5. `PUT /api/subscription` 设置时间并开启订阅。

后端沿用 Agent 的配置加载规则，默认读取启动目录的 `.env`。启动和资料/历史查询不创建真实模型客户端，缺少密钥时也能修改资料；生成时才创建 Runtime 和模型客户端。默认 mock 无法生成真实简报，会被记录为失败，不会归档框架状态文字。

指定路径、端口或前端来源：

```powershell
.\.venv\Scripts\python.exe -m backend --workdir D:\NewsAgent --env-file D:\NewsAgent\.env --port 8000
# 自定义 CORS 来源，可重复传参；传入后替代默认来源
.\.venv\Scripts\python.exe -m backend --cors-origin http://localhost:5173
```

默认绑定 `127.0.0.1`，默认允许 localhost/127.0.0.1 的 5173、3000 端口作为前端来源。第一版为本地单用户接口，没有账号系统和前端页面。请用一个进程运行；同一工作区的第二个后端会被操作系统文件锁拦住。这个锁只约束新后端，CLI 不参与此锁，不要在后端生成时用另一个 CLI 进程改写长期资料。

## 文件职责

| 文件 | 职责 |
| --- | --- |
| `backend/app.py` | 请求校验、HTTP 接口、CORS、生命周期与错误映射 |
| `backend/service.py` | 手动/定时统一生成入口、资料操作、并发控制、失败记录 |
| `backend/scheduling.py` | 恢复每日任务、修改/移除任务、下一次执行时间 |
| `backend/storage.py` | SQLite 订阅与运行记录、进程锁、重启中断恢复 |
| `backend/__main__.py` | `python -m backend` 启动入口 |

FastAPI 的 `lifespan` 在启动时初始化服务、读取订阅、注册定时任务；关闭时先停止调度，再等待正在生成的任务完成并关闭 Runtime。每次生成均创建新 Runtime，会话和工具恢复预算不跨生成任务共享。资料与偏好从磁盘长期记忆重新读取。

```text
前端手动生成 ──┐
               ├─ BackendService → 单个工作线程 → 新 Runtime → generate_brief → Markdown 归档
每天定时触发 ──┘                                ↓
                                             SQLite 运行状态
```

目前 Runtime 为同步代码，因此放到独立工作线程；HTTP 请求只提交任务，不在事件循环里等待新闻搜索与模型调用。

## 存储

```text
.agent/
  memory/                  原有身份、偏好及其他长期记忆 Markdown
  briefs/                  原有简报 Markdown，带 YAML 元信息
  backend.sqlite3          订阅设置和任务运行记录
  backend.lock             操作系统锁；文件保留不表示进程仍在运行
```

SQLite 属于 Python 标准库，不需要独立数据库服务。订阅时间是精确的业务配置，不通过模型从记忆文本里解释。资料接口复用 `MemoryStore.set_profile()` 的保存与验证规则，不调用模型。运行期间拒绝资料修改，以避免与 Runtime 的自动记忆提取互相覆盖。订阅时间和启用状态可以在生成期间修改，新设置影响后续触发，暂停订阅不取消正在生成的任务。

所有 `.agent` 内容均已被 Git 忽略，后端不会覆盖现有 `.env`、记忆或历史简报。

## HTTP 接口

| 方法和路径 | 用途 |
| --- | --- |
| `GET /api/profile` | 身份、偏好、缺失字段和记忆启用状态 |
| `PUT /api/profile` | 修改一个或两个资料字段，未提供字段保留原值 |
| `GET /api/subscription` | 启用状态、每日时间、时区、下一次执行时间 |
| `PUT /api/subscription` | 保存并立即更新调度任务；关闭时移除任务 |
| `POST /api/generate` | 提交手动生成，返回 HTTP 202 和运行记录 |
| `GET /api/status` | 是否正在生成及当前任务 |
| `GET /api/runs?limit=20` | 最新任务记录，最多 100 条 |
| `GET /api/runs/{id}` | 单个任务状态、进度、错误、简报编号和邮件发送状态 |
| `GET /api/email` | 邮件开关、收发件地址和配置是否就绪，不返回授权码 |
| `POST /api/runs/{id}/send-email` | 发送或重试发送已归档简报，不重新调用模型 |
| `GET /api/briefs?limit=20&before=...` | 分页查询历史元信息，规则沿用 BriefStore |
| `GET /api/briefs/{id}` | 简报元信息和完整 Markdown 正文 |

保存资料示例：

```json
{
  "identity": "AI 产品经理，了解基础机器学习",
  "preferences": "关注开源模型和 AI 应用，中文，每天五条，附来源"
}
```

开启或修改订阅：

```json
{
  "enabled": true,
  "time": "08:30",
  "timezone": "Asia/Shanghai"
}
```

第一版只支持北京时间和每日触发；时间采用严格的 `HH:MM`。订阅初始为关闭、08:00。记忆开启时，资料缺失会阻止开启订阅与手动生成，返回 HTTP 409 `PROFILE_REQUIRED`。设置关闭只停止后续定时触发。

手动生成提交 `{}` 使用默认的今日简报请求；也可以传 `prompt` 和 `title`。前端收到 HTTP 202 后，可每隔约两秒查询运行状态，直到终态，再读取简报。提交任务不代表已经生成成功。

```json
{
  "prompt": "生成今日 AI 新闻简报，重点关注开源模型",
  "title": "今日开源模型简报"
}
```

状态依次为 `queued → running → succeeded/failed`。其他记录状态包括：

- `interrupted`：上次后端进程退出时未完成；重启时标记，并释放运行占用。
- `skipped`：定时触发时已有任务执行，或资料缺失，记录跳过原因。

成功的运行记录包含 `brief_id`；正文保存在 BriefStore，运行记录只存结果统计。生成成功但归档失败时，任务状态为 `failed`，记录中保留 `unsaved_content` 与 `save_error`，不会为归档失败再次调用模型。运行列表返回状态摘要，`result` 为 null；结果统计或未保存正文通过单条运行详情读取，避免一次加载大量失败正文。

资料参数、订阅参数和请求内容不合法返回 422；未知运行/简报返回 404；正在生成或发送邮件时，再次手动提交或修改资料返回 409 `BUSY`。

定时简报归档成功后可以推送到 QQ 邮箱。使用本地 `.env` 配置 SMTP 和授权码，邮件默认关闭。生成状态与邮件状态分别记录，邮件失败可以直接重发已保存简报。配置、状态和重试说明见 [QQ 邮箱推送](email.md)。

## 调度与恢复规则

- 每天按订阅时间触发，时区固定为 `Asia/Shanghai`。
- 保存订阅后刷新同一个固定任务编号，不新增重复任务；修改成已经过去的时间，正常执行从下一天开始。
- 订阅配置持久化到 SQLite，APScheduler 任务在每次启动时重建，无需另外持久化调度器的任务对象。
- 重启时只补跑当天、距订阅时间不超过一小时的任务；不补跑往日简报。进程内错过触发也只允许一小时延迟，多个错过时点合并。
- 每个北京时间日期最多尝试一次定时生成，包括失败和中断的尝试。重复触发或重启不会反复调用模型。失败后可查看历史，再手动生成；手动生成不限每天次数。
- 定时触发碰到正在生成的任务时记录 `skipped`，不自动排队；用户可以稍后手动补生成。
- 工具重试和不完整模型响应恢复仍由原 Agent 处理。调度层不自动重跑整项任务，避免重复成本和文件副作用。
- 关闭时等待当前任务完成，不提供强制取消。若强制终止进程，已生成的简报文件可能存在而任务状态尚未更新；重启后检查历史再决定是否手动重试。

运行记录可持续增长，目前没有自动清理。生成任务的并发为 1，前端资料、状态和历史查询可以继续响应；后端执行服务的任务不会因刷新前端而丢失。

## 验证

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -p test_backend.py -v
.\.venv\Scripts\python.exe -m unittest discover -s tests -q
```

测试使用离线 Runtime 和临时工作区，覆盖 HTTP 参数、资料持久化、真实调度器触发、前后台并发、每日去重、补跑窗口、重启恢复、第二个后端拦截、失败正文保留、密钥脱敏和优雅关闭。不请求真实模型或新闻 API。
