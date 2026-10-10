import type {
  Dashboard,
  Brief,
  BriefDetail,
  Profile,
  Run,
  Subscription,
} from "./types";

export function errorMessage(detail: unknown): string {
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    return detail
      .map((item) => {
        const field = item.loc
          ?.filter((part: unknown) => part !== "body")
          .join(".");
        return `${field ? `${field}：` : ""}${item.msg || "参数无效"}`;
      })
      .join("；");
  }
  if (detail && typeof detail === "object" && "message" in detail)
    return String(detail.message);
  return "请求失败，请稍后重试。";
}

export async function request<T>(
  path: string,
  options: RequestInit = {},
): Promise<T> {
  const controller = new AbortController();
  const abort = () => controller.abort();
  if (options.signal?.aborted) controller.abort();
  options.signal?.addEventListener("abort", abort, { once: true });
  const timeout = setTimeout(abort, 15000);
  try {
    const response = await fetch(`/api${path}`, {
      ...options,
      signal: controller.signal,
      headers: {
        ...(options.body ? { "Content-Type": "application/json" } : {}),
        ...options.headers,
      },
    });
    if (!response.ok) {
      let message = `请求失败（${response.status}）`;
      try {
        message = errorMessage((await response.json()).detail);
      } catch {
        /* non-JSON server error */
      }
      throw new Error(message);
    }
    return (await response.json()) as T;
  } catch (error) {
    if (controller.signal.aborted && !options.signal?.aborted) {
      throw new Error("请求超时，请刷新任务状态后再试。");
    }
    throw error;
  } finally {
    clearTimeout(timeout);
    options.signal?.removeEventListener("abort", abort);
  }
}

export const api = {
  async dashboard(signal?: AbortSignal): Promise<Dashboard> {
    const [profile, subscription, email, status, runs, briefs] =
      await Promise.all([
        request<Dashboard["profile"]>("/profile", { signal }),
        request<Dashboard["subscription"]>("/subscription", { signal }),
        request<Dashboard["email"]>("/email", { signal }),
        request<Dashboard["status"]>("/status", { signal }),
        request<Run[]>("/runs?limit=20", { signal }),
        request<Brief[]>("/briefs?limit=20", { signal }),
      ]);
    return { profile, subscription, email, status, runs, briefs };
  },
  brief: (id: string, signal?: AbortSignal) =>
    request<BriefDetail>(`/briefs/${encodeURIComponent(id)}`, { signal }),
  older: (before: string) =>
    request<Brief[]>(`/briefs?limit=20&before=${encodeURIComponent(before)}`),
  profile: (identity: string, preferences: string) =>
    request<Profile>("/profile", {
      method: "PUT",
      body: JSON.stringify({ identity, preferences }),
    }),
  subscription: (enabled: boolean, time: string) =>
    request<Subscription>("/subscription", {
      method: "PUT",
      body: JSON.stringify({ enabled, time, timezone: "Asia/Shanghai" }),
    }),
  generate: (prompt?: string) =>
    request<Run>("/generate", {
      method: "POST",
      body: JSON.stringify(prompt?.trim() ? { prompt: prompt.trim() } : {}),
    }),
  sendEmail: (id: string) =>
    request<Run>(`/runs/${encodeURIComponent(id)}/send-email`, {
      method: "POST",
    }),
  run: (id: string) => request<Run>(`/runs/${encodeURIComponent(id)}`),
};

export const runLabels: Record<Run["status"], string> = {
  queued: "等待开始",
  running: "正在生成",
  succeeded: "生成完成",
  failed: "生成失败",
  interrupted: "任务中断",
  skipped: "已跳过",
};
export const mailLabels: Record<Run["email_status"], string> = {
  not_requested: "未发送邮件",
  pending: "邮件待发送",
  sending: "正在发送邮件",
  sent: "邮件已提交",
  failed: "邮件发送失败",
  unknown: "邮件结果待确认",
};

export function formatDate(value: string | null, withTime = true): string {
  if (!value) return "—";
  return new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai",
    month: "long",
    day: "numeric",
    ...(withTime ? { hour: "2-digit", minute: "2-digit", hour12: false } : {}),
  }).format(new Date(value));
}

export function mergeBriefs(latest: Brief[], existing: Brief[]): Brief[] {
  const map = new Map(existing.map((brief) => [brief.id, brief]));
  latest.forEach((brief) => map.set(brief.id, brief));
  return [...map.values()].sort((a, b) => b.id.localeCompare(a.id));
}
