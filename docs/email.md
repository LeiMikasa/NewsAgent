# QQ 邮箱推送

定时任务生成并保存简报后，通过 QQ 邮箱 SMTP over SSL 发送正文与 Markdown 附件。发送账号可以和收件账号相同；`EMAIL_TO` 留空时默认发到自己的邮箱。

## 配置

先在 QQ 邮箱的账号设置中开启 POP3/SMTP 或 IMAP/SMTP 服务，并取得邮箱授权码。`SMTP_PASSWORD` 填授权码，不填网页登录密码。QQ SMTP 地址与端口使用 `smtp.qq.com:465`（SSL）；可以参考 [腾讯邮箱连接器配置说明](https://main.qcloudimg.com/raw/document/product/pdf/1270_46586_cn.pdf)。本实现使用 SSL 直连，不支持把端口改为 587 后直接当作 STARTTLS 使用。

在本地 `.env` 中添加：

```dotenv
EMAIL_ENABLED=true
SMTP_HOST=smtp.qq.com
SMTP_PORT=465
SMTP_USERNAME=你的QQ邮箱地址
SMTP_PASSWORD=你的邮箱授权码
# 可选：不填时使用 SMTP_USERNAME
EMAIL_TO=你的收件QQ邮箱地址
SMTP_TIMEOUT=30
```

地址填写完整邮箱，例如 `123456@qq.com`。授权码只在本地填写，不需要发到聊天里或前端。真实 `.env` 已被 Git 忽略，SMTP 授权码不写入 SQLite、不由 API 返回，配置对象的 repr 也不包含授权码。错误记录会隐藏配置中的授权码。

修改配置后重启后端：

```powershell
cd D:\NewsAgent
.\.venv\Scripts\python.exe -m backend
```

访问 `http://127.0.0.1:8000/docs`：

1. `GET /api/email` 检查 `enabled` 和 `configured` 是否为 true。
2. 使用资料接口填写身份和新闻偏好，并设置好真实模型与 Tavily 配置。
3. `PUT /api/subscription` 开启每日定时生成。
4. 定时生成成功后，查询运行记录中的 `email_status`。

邮件发送开关与定时订阅开关分别控制：开启 `EMAIL_ENABLED` 不会自动开启每日订阅。

## 发送流程

```text
定时触发 → Agent 生成 → 保存简报成功 → 记录生成成功和待发送状态 → SMTP 发送
                                                              ↓
                                                      单独更新邮件状态
```

- 只有定时生成且归档成功时自动发送。
- 手动 `POST /api/generate` 默认只生成；如需要邮件，另调用发送接口。
- 邮件标题为 `AI 新闻简报｜<简报标题>`，正文为 UTF-8 文字，附 `.md` 正文文件。
- 邮件失败保留已保存的简报，运行记录的 `status` 仍然为 `succeeded`；失败原因看 `email_error`。
- 邮件发送也使用同一个工作线程和占用标记，期间再次生成或重复提交邮件会返回 409；查询状态与历史仍可用。
- 发送使用标准库 `smtplib` 和默认验证服务器证书的 SSL 上下文，无需额外依赖。

## 邮件状态与重发

运行列表和运行详情新增：`email_status`、`email_attempts`、`email_error`、`email_sent_at`、`email_message_id`、`email_recipient`。

| 状态 | 含义 |
| --- | --- |
| `not_requested` | 未请求邮件发送，例如普通手动生成或邮件开关关闭 |
| `pending` | 已提交发送任务 |
| `sending` | 正在尝试发送 |
| `sent` | SMTP 服务器已接受邮件；不代表已经阅读或必然进入收件箱 |
| `failed` | 配置、认证或明确拒绝等导致发送失败 |
| `unknown` | 提交中断或后端异常退出，不能确定 SMTP 是否已经接受 |

`email_attempts` 统计进入发送尝试的次数，也包含配置检查失败。第一版不自动重试整封邮件：提交邮件后若连接中断，服务器可能已经收到了邮件，自动重发可能产生重复。

重发接口：

```http
POST /api/runs/{run_id}/send-email
```

无请求体，返回 HTTP 202 和更新后的运行记录。随后查询同一个运行编号的 `email_status`。接口直接读取对应的已归档简报，不创建 Runtime、不调用模型、不再次生成正文；`brief_id` 保持原值，重试沿用记录的 Message-ID。

它也可以用于首次发送手动生成的简报。已经 `sent` 的记录会拒绝重复发送；失败的生成任务无法发送邮件。对于 `unknown`，请先检查收件箱再决定是否调用重发接口。沿用 Message-ID 有助于标识同一封邮件，但 SMTP 不保证恰好投递一次。

重启时，未完成的 `pending`/`sending` 状态改为 `unknown`，不会自动重放邮件。原有数据库从版本 1 自动升级为版本 2，订阅、简报和历史运行记录保留；旧记录的邮件状态为 `not_requested`，不会批量发送旧简报。

SMTP 配置缺失不会阻止查询或简报生成；定时生成成功后，邮件状态会记录失败原因。修改授权码后，需要重启后端，再重试发送。邮件失败也不会触发当天定时生成再次调用模型。

## 离线测试

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -p test_email_delivery.py -v
```

测试模拟 SMTP，覆盖认证失败、明确拒绝、连接中断、退出连接失败、UTF-8 正文与附件、默认/指定收件人、保存失败不发送、发送失败保留简报、显式重发、并发控制和数据库升级，不发送真实邮件。
