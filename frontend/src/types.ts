export interface Profile {
  identity: string;
  preferences: string;
  missing_fields: string[];
  memory_enabled: boolean;
}

export interface Subscription {
  enabled: boolean;
  time: string;
  timezone: "Asia/Shanghai";
  updated_at: string;
  next_run_at: string | null;
}

export interface EmailConfig {
  enabled: boolean;
  configured: boolean;
  sender: string | null;
  recipient: string | null;
  smtp_host: string;
  smtp_port: number;
  missing_fields: string[];
}

export interface Run {
  id: string;
  source: "manual" | "scheduled";
  status:
    "queued" | "running" | "succeeded" | "failed" | "interrupted" | "skipped";
  created_at: string;
  progress: string;
  brief_id: string | null;
  error: string;
  email_status:
    "not_requested" | "pending" | "sending" | "sent" | "failed" | "unknown";
  email_error: string;
  email_recipient: string | null;
  result: { unsaved_content?: string; save_error?: string } | null;
}

export interface Brief {
  id: string;
  title: string;
  created_at: string;
  provider: string;
  model: string;
  turns: number;
  tool_calls: number;
  content_bytes: number;
}

export interface BriefDetail extends Brief {
  content: string;
}
export interface Dashboard {
  profile: Profile;
  subscription: Subscription;
  email: EmailConfig;
  status: { busy: boolean; active_run: Run | null };
  runs: Run[];
  briefs: Brief[];
}
