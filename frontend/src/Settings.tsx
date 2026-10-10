import { useEffect, useState } from "react";
import {
  Check,
  Clock,
  EnvelopeSimple,
  FloppyDisk,
  UserCircle,
} from "@phosphor-icons/react";
import { api, formatDate } from "./api";
import type { Dashboard } from "./types";

interface Props {
  data: Dashboard;
  disabled: boolean;
  act: (work: () => Promise<unknown>, success: string) => Promise<boolean>;
}

export default function Settings({ data, disabled, act }: Props) {
  const [identity, setIdentity] = useState(data.profile.identity);
  const [preferences, setPreferences] = useState(data.profile.preferences);
  const [profileDirty, setProfileDirty] = useState(false);
  const [enabled, setEnabled] = useState(data.subscription.enabled);
  const [time, setTime] = useState(data.subscription.time);
  const [scheduleDirty, setScheduleDirty] = useState(false);
  useEffect(() => {
    if (!profileDirty) {
      setIdentity(data.profile.identity);
      setPreferences(data.profile.preferences);
    }
  }, [data.profile.identity, data.profile.preferences, profileDirty]);
  useEffect(() => {
    if (!scheduleDirty) {
      setEnabled(data.subscription.enabled);
      setTime(data.subscription.time);
    }
  }, [data.subscription.enabled, data.subscription.time, scheduleDirty]);

  return (
    <div className="settings-layout">
      <section className="panel settings-panel">
        <div className="section-heading">
          <UserCircle size={24} />
          <div>
            <h2>让简报更懂你</h2>
            <p>你的身份与长期偏好，会在生成简报时加载。</p>
          </div>
        </div>
        {!data.profile.memory_enabled && (
          <p className="notice">
            后端已关闭长期记忆。保存的资料将在启用记忆后用于生成。
          </p>
        )}
        <form
          onSubmit={async (event) => {
            event.preventDefault();
            if (
              await act(
                () => api.profile(identity.trim(), preferences.trim()),
                "身份和偏好已保存到长期记忆。",
              )
            )
              setProfileDirty(false);
          }}
        >
          <fieldset disabled={disabled}>
            <label htmlFor="identity">
              你的身份 <span>职业、背景或学习方向</span>
            </label>
            <textarea
              id="identity"
              required
              maxLength={4000}
              rows={3}
              value={identity}
              placeholder="例如：计算机专业学生，正在学习 Agent 开发，准备找实习。"
              onChange={(event) => {
                setIdentity(event.target.value);
                setProfileDirty(true);
              }}
            />
            <label htmlFor="preferences">
              新闻偏好 <span>主题、语言、篇幅与深度</span>
            </label>
            <textarea
              id="preferences"
              required
              maxLength={4000}
              rows={5}
              value={preferences}
              placeholder="例如：关注开源模型、Agent 工具和工程实践；中文，每天 5 条，附原文链接与简短解读。"
              onChange={(event) => {
                setPreferences(event.target.value);
                setProfileDirty(true);
              }}
            />
            <div className="form-footer">
              <span>以后可以随时修改</span>
              <button
                className="button primary"
                disabled={!identity.trim() || !preferences.trim()}
              >
                <FloppyDisk size={17} />
                保存资料
              </button>
            </div>
          </fieldset>
        </form>
      </section>
      <div className="settings-side">
        <section className="panel settings-panel">
          <div className="section-heading">
            <Clock size={24} />
            <div>
              <h2>每天，按时见面</h2>
              <p>北京时间 · Asia/Shanghai</p>
            </div>
          </div>
          <form
            onSubmit={async (event) => {
              event.preventDefault();
              if (
                await act(
                  () => api.subscription(enabled, time),
                  "订阅设置已保存。",
                )
              )
                setScheduleDirty(false);
            }}
          >
            <fieldset disabled={disabled}>
              <label className="switch-label" htmlFor="subscription-enabled">
                <span>每日自动生成</span>
                <input
                  id="subscription-enabled"
                  type="checkbox"
                  role="switch"
                  checked={enabled}
                  onChange={(event) => {
                    setEnabled(event.target.checked);
                    setScheduleDirty(true);
                  }}
                />
              </label>
              <label htmlFor="subscription-time">生成时间</label>
              <input
                id="subscription-time"
                type="time"
                required
                value={time}
                onChange={(event) => {
                  setTime(event.target.value);
                  setScheduleDirty(true);
                }}
              />
              <p className="field-help">
                下一次：
                {data.subscription.next_run_at
                  ? formatDate(data.subscription.next_run_at)
                  : "订阅尚未开启"}
              </p>
              <button className="button secondary full">
                <Check size={17} />
                保存订阅
              </button>
            </fieldset>
          </form>
          <p className="footnote">
            自动生成需要后端持续运行。电脑休眠或服务关闭时，无法按时执行。
          </p>
        </section>
        <section className="panel settings-panel">
          <div className="section-heading">
            <EnvelopeSimple size={24} />
            <div>
              <h2>送到你的邮箱</h2>
              <p>定时简报生成后，自动推送。</p>
            </div>
          </div>
          <span
            className={`badge ${data.email.enabled && data.email.configured ? "good" : ""}`}
          >
            {data.email.enabled
              ? data.email.configured
                ? "推送已就绪"
                : "配置未完成"
              : "邮件推送未开启"}
          </span>
          <dl className="detail-list">
            <div>
              <dt>收件邮箱</dt>
              <dd>{data.email.recipient || "尚未配置"}</dd>
            </div>
            <div>
              <dt>发件邮箱</dt>
              <dd>{data.email.sender || "尚未配置"}</dd>
            </div>
          </dl>
          <details className="email-help">
            <summary>如何开启 QQ 邮箱推送</summary>
            <p>
              在 QQ 邮箱中开启 SMTP 服务并获取授权码，然后在项目根目录的 .env
              中填写以下配置，再重启后端。授权码只保存在后端。
            </p>
            <pre>
              EMAIL_ENABLED=true{"\n"}SMTP_USERNAME=你的QQ邮箱{"\n"}
              SMTP_PASSWORD=邮箱授权码{"\n"}EMAIL_TO=收件邮箱
            </pre>
            <p>
              手动生成的简报需要在任务记录中点击发送。邮件「已提交」表示 SMTP
              服务接收了邮件，请以邮箱实际收件为准。
            </p>
          </details>
        </section>
      </div>
    </div>
  );
}
