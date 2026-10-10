"""SMTP over TLS; one attempt, without replaying potentially accepted messages."""

from __future__ import annotations

import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

from agent import Settings


@dataclass(frozen=True)
class DeliveryResult:
    status: str  # sent | failed | unknown
    error: str = ""


class EmailSender:
    def __init__(self, settings: Settings):
        self.settings = settings

    @property
    def recipient(self) -> str | None:
        return self.settings.email_to or self.settings.smtp_username

    def config(self) -> dict:
        missing = []
        if not self.settings.smtp_username:
            missing.append("SMTP_USERNAME")
        if not self.settings.smtp_password:
            missing.append("SMTP_PASSWORD")
        return {"enabled": self.settings.email_enabled, "configured": not missing,
                "sender": self.settings.smtp_username, "recipient": self.recipient,
                "smtp_host": self.settings.smtp_host, "smtp_port": self.settings.smtp_port,
                "missing_fields": missing}

    def require_ready(self) -> None:
        if not self.settings.email_enabled:
            raise ValueError("邮件推送未启用，请在 .env 中设置 EMAIL_ENABLED=true 并重启后端。")
        missing = self.config()["missing_fields"]
        if missing:
            raise ValueError("邮件配置缺少：" + ", ".join(missing))

    def message_id(self, run_id: str) -> str:
        domain = self.settings.smtp_username.split("@")[-1] if self.settings.smtp_username else "newsagent.local"
        return make_msgid(idstring=run_id, domain=domain)

    def message(self, brief: dict, message_id: str) -> EmailMessage:
        message = EmailMessage()
        message["Subject"] = "AI 新闻简报｜" + brief["title"]
        message["From"] = self.settings.smtp_username
        message["To"] = self.recipient
        message["Date"] = formatdate(localtime=True)
        message["Message-ID"] = message_id
        message.set_content(f"{brief['title']}\n生成时间：{brief['created_at']}\n\n{brief['content']}\n"
                            f"简报编号：{brief['id']}\n", charset="utf-8")
        message.add_attachment(brief["content"].encode("utf-8"), maintype="text", subtype="markdown",
                               filename=brief["id"] + ".md")
        return message

    @staticmethod
    def _connection_failure(submitting: bool) -> DeliveryResult:
        if submitting:
            return DeliveryResult("unknown", "提交邮件时连接中断，发送结果不确定；请先检查邮箱，再决定是否重发。")
        return DeliveryResult("failed", "无法连接或登录 SMTP 服务，请检查网络、服务器地址和端口。")

    def send(self, brief: dict, message_id: str) -> DeliveryResult:
        try:
            self.require_ready()
        except ValueError as exc:
            return DeliveryResult("failed", str(exc))
        message = self.message(brief, message_id)
        client = None
        submitting = False
        try:
            client = smtplib.SMTP_SSL(self.settings.smtp_host, self.settings.smtp_port,
                timeout=self.settings.smtp_timeout, context=ssl.create_default_context())
            client.login(self.settings.smtp_username, self.settings.smtp_password)
            submitting = True
            rejected = client.send_message(message, from_addr=self.settings.smtp_username, to_addrs=[self.recipient])
            if rejected:
                return DeliveryResult("failed", "SMTP 拒绝了收件人，请检查收件地址。")
            return DeliveryResult("sent")
        except smtplib.SMTPAuthenticationError:
            return DeliveryResult("failed", "SMTP 认证失败，请检查邮箱账号、授权码和 SMTP 服务开关。")
        except (smtplib.SMTPRecipientsRefused, smtplib.SMTPSenderRefused, smtplib.SMTPDataError) as exc:
            code = getattr(exc, "smtp_code", "rejected")
            return DeliveryResult("failed", f"SMTP 拒绝邮件（{code}），请检查收件地址和邮箱服务设置。")
        except smtplib.SMTPServerDisconnected:
            return self._connection_failure(submitting)
        except smtplib.SMTPException:
            return DeliveryResult("failed", "SMTP 服务返回错误，请检查邮箱服务设置。")
        except OSError:
            return self._connection_failure(submitting)
        finally:
            if client is not None:
                # QUIT failure after successful submission must not turn an accepted message into a failed send.
                try:
                    client.quit()
                except (OSError, smtplib.SMTPException):
                    pass
                finally:
                    try:
                        client.close()
                    except (OSError, smtplib.SMTPException):
                        pass
