import { useEffect, useRef, useState } from "react";
import {
  ArrowClockwise,
  ArrowRight,
  BookOpen,
  CheckCircle,
  Clock,
  EnvelopeSimple,
  GearSix,
  List,
  Newspaper,
  Plus,
  Sparkle,
  SpinnerGap,
  Stack,
  X,
} from "@phosphor-icons/react";
import { api, formatDate, mailLabels, mergeBriefs, runLabels } from "./api";
import type { Brief } from "./types";
import { useDashboard } from "./useDashboard";
import BriefReader from "./BriefReader";
import Settings from "./Settings";

type Page = "today" | "history" | "settings";
const pages = [
  { id: "today", label: "我的简报", icon: Newspaper },
  { id: "history", label: "历史简报", icon: Stack },
  { id: "settings", label: "订阅设置", icon: GearSix },
] as const;
const pageFromHash = (): Page =>
  ["history", "settings"].includes(location.hash.slice(1))
    ? (location.hash.slice(1) as Page)
    : "today";

export default function App() {
  const { data, error, loading, refresh } = useDashboard();
  const [page, setPage] = useState<Page>(pageFromHash);
  const [menuOpen, setMenuOpen] = useState(false);
  const [working, setWorking] = useState(false);
  const actionLock = useRef(false);
  const [message, setMessage] = useState<{
    text: string;
    error: boolean;
  } | null>(null);
  const [prompt, setPrompt] = useState("");
  const [customPrompt, setCustomPrompt] = useState(false);
  const [history, setHistory] = useState<Brief[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [more, setMore] = useState(true);
  const [paging, setPaging] = useState(false);
  const [recovered, setRecovered] = useState("");
  useEffect(() => {
    const changed = () => {
      setPage(pageFromHash());
      setMenuOpen(false);
    };
    window.addEventListener("hashchange", changed);
    return () => window.removeEventListener("hashchange", changed);
  }, []);
  useEffect(() => {
    if (data) setHistory((previous) => mergeBriefs(data.briefs, previous));
  }, [data]);
  useEffect(() => {
    if (!menuOpen) return;
    const close = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        setMenuOpen(false);
        document.getElementById("menu-toggle")?.focus();
      }
    };
    window.addEventListener("keydown", close);
    return () => window.removeEventListener("keydown", close);
  }, [menuOpen]);

  function navigate(next: Page) {
    location.hash = next;
    setPage(next);
    setMenuOpen(false);
  }
  async function act(
    work: () => Promise<unknown>,
    success: string,
  ): Promise<boolean> {
    if (actionLock.current) return false;
    actionLock.current = true;
    setWorking(true);
    setMessage(null);
    try {
      await work();
      setMessage({ text: success, error: false });
      await refresh();
      return true;
    } catch (exc) {
      setMessage({
        text:
          exc instanceof TypeError
            ? "请求未完成，请检查连接并刷新任务状态后再试。"
            : (exc as Error).message,
        error: true,
      });
      await refresh();
      return false;
    } finally {
      actionLock.current = false;
      setWorking(false);
    }
  }
  async function older() {
    if (!history.length || paging) return;
    setPaging(true);
    try {
      const next = await api.older(history[history.length - 1].id);
      setHistory((previous) => mergeBriefs(next, previous));
      setMore(next.length === 20);
    } catch (exc) {
      setMessage({ text: (exc as Error).message, error: true });
    } finally {
      setPaging(false);
    }
  }
  const busy = Boolean(data?.status.busy);
  const disabled = working || busy || Boolean(error);
  const missing = Boolean(
    data?.profile.memory_enabled && data.profile.missing_fields.length,
  );
  const latest = data?.briefs[0];
  const readingId = page === "history" ? selected : latest?.id;
  const date = new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai",
    month: "long",
    day: "numeric",
    weekday: "long",
  }).format(new Date());

  return (
    <div className="app-shell">
      <a className="skip-link" href="#main">
        跳到主要内容
      </a>
      <aside className={`sidebar ${menuOpen ? "open" : ""}`}>
        <a className="brand" href="#today" onClick={() => navigate("today")}>
          <span className="brand-icon">
            <Newspaper weight="bold" size={22} />
          </span>
          <span>
            NewsAgent<small>你的 AI 新闻助手</small>
          </span>
        </a>
        <span className="nav-caption">我的工作台</span>
        <nav id="navigation" aria-label="主要导航">
          {pages.map((item) => (
            <a
              key={item.id}
              href={`#${item.id}`}
              className={page === item.id ? "active" : ""}
              aria-current={page === item.id ? "page" : undefined}
              onClick={() => navigate(item.id)}
            >
              <item.icon
                size={21}
                weight={page === item.id ? "fill" : "regular"}
              />
              {item.label}
              {page === item.id && <span className="nav-dot" />}
            </a>
          ))}
        </nav>
        <div className="sidebar-bottom">
          <div className="personal-mark">
            <span>N</span>
            <div>
              个人新闻空间<small>单用户 · 本地存储</small>
            </div>
          </div>
          <p>
            少一点信息噪音，
            <br />
            多一点值得关注的事。
          </p>
        </div>
      </aside>
      {menuOpen && (
        <button
          className="menu-overlay"
          aria-label="关闭菜单"
          onClick={() => setMenuOpen(false)}
        />
      )}
      <div className="workspace">
        <header className="topbar">
          <div>
            <button
              id="menu-toggle"
              className="mobile-menu icon-button"
              aria-label="打开导航"
              aria-controls="navigation"
              aria-expanded={menuOpen}
              onClick={() => setMenuOpen((value) => !value)}
            >
              <List size={22} />
            </button>
            <span className="breadcrumb">
              工作台 <span>/</span>{" "}
              {pages.find((item) => item.id === page)?.label}
            </span>
          </div>
          <span className={`connection ${error ? "offline" : ""}`}>
            <i />
            {loading ? "连接中" : error ? "连接异常" : "后端已连接"}
          </span>
        </header>
        <main id="main" tabIndex={-1}>
          <div className="page-heading">
            <div>
              <p className="eyebrow">
                {page === "today"
                  ? date
                  : page === "history"
                    ? "YOUR READING ARCHIVE"
                    : "MADE FOR YOU"}
              </p>
              <h1>
                {page === "today"
                  ? "每天，看见 AI 的新进展。"
                  : page === "history"
                    ? "值得再读的，都在这里。"
                    : "定义你的新闻视角。"}
              </h1>
              <p>
                {page === "today"
                  ? "根据你的身份与偏好，把新闻整理成一份清晰的简报。"
                  : page === "history"
                    ? "浏览过去的简报，沿着自己的关注方向继续探索。"
                    : "告诉 Agent 你是谁、关心什么，以及什么时候收到简报。"}
              </p>
            </div>
            {page === "today" && (
              <button
                className="button primary generate-button"
                disabled={!data || disabled || missing}
                onClick={() =>
                  void act(
                    () => api.generate(customPrompt ? prompt : undefined),
                    "任务已提交，完成后简报将自动显示。",
                  )
                }
              >
                {busy || working ? (
                  <SpinnerGap className="spin" size={18} />
                ) : (
                  <Plus size={19} />
                )}{" "}
                {busy ? "任务执行中" : "生成一份简报"}
              </button>
            )}
          </div>

          {error && (
            <div className="feedback error" role="alert">
              <span>
                {error} {data ? "当前显示上次读取的数据。" : ""}
              </span>
              <button
                onClick={() => void refresh()}
                className="button text-button"
              >
                <ArrowClockwise size={16} />
                重新连接
              </button>
            </div>
          )}
          {message && (
            <div
              className={`feedback ${message.error ? "error" : "success"}`}
              role={message.error ? "alert" : "status"}
            >
              <span>{message.text}</span>
              <button
                className="icon-button"
                aria-label="关闭提示"
                onClick={() => setMessage(null)}
              >
                <X size={18} />
              </button>
            </div>
          )}
          {busy && data?.status.active_run && (
            <div className="progress-banner" role="status">
              <SpinnerGap className="spin" size={20} />
              <div>
                <strong>
                  {data.status.active_run.email_status === "sending" ||
                  data.status.active_run.email_status === "pending"
                    ? "正在处理邮件推送"
                    : runLabels[data.status.active_run.status]}
                </strong>
                <p>
                  {data.status.active_run.progress ||
                    "后台正在处理，你可以继续浏览历史简报。"}
                </p>
              </div>
              <span>可安全刷新页面</span>
            </div>
          )}
          {loading && !data && (
            <div className="panel initial-loading">
              <SpinnerGap className="spin" size={26} />
              <p>正在读取你的新闻空间…</p>
            </div>
          )}
          {!loading && !data && (
            <div className="panel empty-state">
              <Newspaper size={44} />
              <h2>连接后，开始你的每日阅读</h2>
              <p>请先启动 NewsAgent 后端，再点击「重新连接」。</p>
            </div>
          )}

          {data && page === "settings" && (
            <Settings data={data} disabled={disabled} act={act} />
          )}
          {data && page === "today" && (
            <>
              {missing && (
                <div className="onboarding">
                  <div>
                    <span className="eyebrow">第一步 · 建立你的阅读偏好</span>
                    <h2>一份好简报，从了解你开始。</h2>
                    <p>填写身份与偏好，Agent 才能找到与你相关的 AI 新闻。</p>
                  </div>
                  <button
                    className="button secondary"
                    onClick={() => navigate("settings")}
                  >
                    设置我的偏好
                    <ArrowRight size={18} />
                  </button>
                </div>
              )}
              <div className="reading-layout">
                <div>
                  <div className="section-label">
                    <h2>最新简报</h2>
                    <button
                      className="button text-button"
                      onClick={() => navigate("history")}
                    >
                      查看历史
                      <ArrowRight size={15} />
                    </button>
                  </div>
                  {latest ? (
                    <BriefReader id={latest.id} />
                  ) : (
                    <div className="panel empty-state">
                      <div className="empty-icon">
                        <BookOpen size={38} weight="duotone" />
                      </div>
                      <span className="eyebrow">你的第一份简报</span>
                      <h2>把你关心的新闻，放在一起。</h2>
                      <p>
                        {missing
                          ? "先保存身份和偏好，然后生成第一份简报。"
                          : "点击上方「生成一份简报」，开始今天的阅读。"}
                        <br />
                        生成的内容会保存在本地，方便随时回看。
                      </p>
                      <button
                        className="button secondary"
                        disabled={disabled}
                        onClick={() =>
                          missing
                            ? navigate("settings")
                            : void act(
                                () =>
                                  api.generate(
                                    customPrompt ? prompt : undefined,
                                  ),
                                "任务已提交。",
                              )
                        }
                      >
                        {missing ? "填写阅读偏好" : "生成第一份简报"}
                        <ArrowRight size={17} />
                      </button>
                    </div>
                  )}
                  <section className="custom-request">
                    <button
                      className="button text-button"
                      onClick={() => setCustomPrompt((value) => !value)}
                      aria-expanded={customPrompt}
                    >
                      <Sparkle size={17} />
                      {customPrompt ? "收起本次要求" : "给这次简报加一点要求"}
                      <span>{customPrompt ? "−" : "+"}</span>
                    </button>
                    {customPrompt && (
                      <div>
                        <label htmlFor="custom-prompt" className="sr-only">
                          本次简报的额外要求
                        </label>
                        <textarea
                          id="custom-prompt"
                          maxLength={4000}
                          rows={3}
                          value={prompt}
                          onChange={(event) => setPrompt(event.target.value)}
                          placeholder="例如：今天重点看 Agent 框架的开源进展。"
                        />
                        <p className="field-help">
                          仅作为本次请求。若要修改长期偏好，请前往订阅设置。
                        </p>
                      </div>
                    )}
                  </section>
                </div>
                <aside className="reading-side">
                  <section className="side-section">
                    <span className="side-icon">
                      <Clock size={22} />
                    </span>
                    <h2>你的每日约定</h2>
                    <div className="schedule-time">
                      {data.subscription.time}
                      <span>北京时间</span>
                    </div>
                    <span
                      className={`badge ${data.subscription.enabled ? "good" : ""}`}
                    >
                      {data.subscription.enabled ? "订阅已开启" : "订阅已暂停"}
                    </span>
                    <p>
                      下一次生成
                      <br />
                      <strong>
                        {data.subscription.next_run_at
                          ? formatDate(data.subscription.next_run_at)
                          : "开启订阅后显示"}
                      </strong>
                    </p>
                    <button
                      className="button text-button"
                      onClick={() => navigate("settings")}
                    >
                      调整订阅
                      <ArrowRight size={15} />
                    </button>
                  </section>
                  <section className="side-section">
                    <span className="side-icon">
                      <EnvelopeSimple size={22} />
                    </span>
                    <h2>邮件推送</h2>
                    <p>
                      {data.email.enabled && data.email.configured
                        ? "定时简报将发送到"
                        : "把定时简报送到你的 QQ 邮箱。"}
                      <br />
                      <strong className="wrap-anywhere">
                        {data.email.enabled && data.email.configured
                          ? data.email.recipient
                          : "尚未就绪"}
                      </strong>
                    </p>
                    <button
                      className="button text-button"
                      onClick={() => navigate("settings")}
                    >
                      查看推送设置
                      <ArrowRight size={15} />
                    </button>
                  </section>
                  <div className="reading-note">
                    <BookOpen size={20} />
                    <p>
                      按你的节奏阅读。
                      <br />
                      所有简报都可以回看，
                      <br />
                      也可以下载为 Markdown。
                    </p>
                  </div>
                </aside>
              </div>
              <section className="activity">
                <div className="section-label">
                  <h2>最近任务</h2>
                  <span className="muted">生成与邮件分别记录</span>
                </div>
                {!data.runs.length ? (
                  <p className="activity-empty">
                    还没有任务。你的首次生成会记录在这里。
                  </p>
                ) : (
                  <div className="run-list">
                    {data.runs.slice(0, 6).map((run) => (
                      <div className="run-row" key={run.id}>
                        <div
                          className={`run-icon ${run.status === "succeeded" ? "good" : ""}`}
                        >
                          {run.status === "succeeded" ? (
                            <CheckCircle size={20} />
                          ) : (
                            <Clock size={20} />
                          )}
                        </div>
                        <div className="run-info">
                          <strong>
                            {run.source === "manual"
                              ? "手动简报"
                              : "每日定时简报"}
                            <span
                              className={`badge ${run.status === "succeeded" ? "good" : run.status === "failed" || run.status === "interrupted" ? "bad" : ""}`}
                            >
                              {runLabels[run.status]}
                            </span>
                          </strong>
                          <p>
                            {formatDate(run.created_at)} ·{" "}
                            {mailLabels[run.email_status]}
                          </p>
                          {run.error && (
                            <p className="error-text">{run.error}</p>
                          )}
                          {run.email_error && (
                            <p className="error-text">{run.email_error}</p>
                          )}
                          {run.email_status === "unknown" && (
                            <p className="error-text">
                              发送结果不确定。请先检查邮箱，确认未收到后再重发。
                            </p>
                          )}
                        </div>
                        <div className="run-actions">
                          {run.brief_id && (
                            <button
                              className="button text-button"
                              onClick={() => {
                                setSelected(run.brief_id);
                                navigate("history");
                              }}
                            >
                              阅读
                            </button>
                          )}
                          {run.status === "failed" && (
                            <button
                              className="button text-button"
                              disabled={working}
                              onClick={() =>
                                void act(async () => {
                                  const full = await api.run(run.id);
                                  setRecovered(
                                    full.result?.unsaved_content ||
                                      "该任务没有可恢复的正文。",
                                  );
                                }, "已读取任务结果。")
                              }
                            >
                              查看结果
                            </button>
                          )}
                          {run.status === "succeeded" &&
                            run.brief_id &&
                            data.email.enabled &&
                            data.email.configured &&
                            ["not_requested", "failed", "unknown"].includes(
                              run.email_status,
                            ) && (
                              <button
                                className="button secondary small"
                                disabled={disabled}
                                onClick={() =>
                                  void act(
                                    () => api.sendEmail(run.id),
                                    "邮件任务已提交，发送结果会自动更新。",
                                  )
                                }
                              >
                                <EnvelopeSimple size={15} />
                                {run.email_status === "unknown"
                                  ? "确认未收到，重发"
                                  : run.email_status === "failed"
                                    ? "重试邮件"
                                    : "发送邮件"}
                              </button>
                            )}
                        </div>
                      </div>
                    ))}
                  </div>
                )}
                {recovered && (
                  <div className="recovered-result">
                    <div>
                      <strong>任务结果 · 未归档正文</strong>
                      <button
                        className="icon-button"
                        aria-label="关闭任务结果"
                        onClick={() => setRecovered("")}
                      >
                        <X size={18} />
                      </button>
                    </div>
                    <pre>{recovered}</pre>
                  </div>
                )}
              </section>
            </>
          )}
          {data && page === "history" && (
            <div className="history-layout">
              <section className="panel archive-list">
                <div className="archive-heading">
                  <h2>简报归档</h2>
                  <span>已加载 {history.length} 份</span>
                </div>
                {!history.length && (
                  <p className="archive-empty">
                    还没有历史简报。
                    <br />
                    生成后会自动保存到这里。
                  </p>
                )}
                {history.map((brief) => (
                  <button
                    key={brief.id}
                    className={`archive-item ${selected === brief.id ? "selected" : ""}`}
                    onClick={() => setSelected(brief.id)}
                    aria-pressed={selected === brief.id}
                  >
                    <time>{formatDate(brief.created_at)}</time>
                    <strong>{brief.title}</strong>
                    <span>
                      {brief.model}
                      <ArrowRight size={15} />
                    </span>
                  </button>
                ))}
                {history.length >= 20 && more && (
                  <button
                    className="button text-button full"
                    disabled={paging}
                    onClick={() => void older()}
                  >
                    {paging ? "加载中…" : "加载更早的简报"}
                  </button>
                )}
                {!more && <p className="archive-end">已经看到最早的一份了</p>}
              </section>
              {readingId ? (
                <BriefReader id={readingId} />
              ) : (
                <div className="panel empty-state">
                  <BookOpen size={42} />
                  <h2>打开一份过去的简报</h2>
                  <p>从归档列表中选择，继续阅读。</p>
                  {!history.length && (
                    <button
                      className="button secondary"
                      onClick={() => navigate("today")}
                    >
                      回到我的简报
                      <ArrowRight size={17} />
                    </button>
                  )}
                </div>
              )}
            </div>
          )}
          <footer className="page-footer">
            <span>NewsAgent</span>
            <span>为你整理，留给你判断。</span>
          </footer>
        </main>
      </div>
    </div>
  );
}
