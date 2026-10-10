import { useEffect, useState } from "react";
import {
  ArrowClockwise,
  ArrowSquareOut,
  DownloadSimple,
} from "@phosphor-icons/react";
import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { api, formatDate } from "./api";
import type { BriefDetail } from "./types";

export default function BriefReader({ id }: { id: string }) {
  const [brief, setBrief] = useState<BriefDetail | null>(null);
  const [error, setError] = useState("");
  const [retry, setRetry] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    setBrief(null);
    setError("");
    api
      .brief(id, controller.signal)
      .then((value) => {
        if (!controller.signal.aborted) setBrief(value);
      })
      .catch((exc) => {
        if (!controller.signal.aborted) setError((exc as Error).message);
      });
    return () => controller.abort();
  }, [id, retry]);

  if (error)
    return (
      <section className="panel reader reader-message" role="alert">
        <h2>这份简报暂时无法读取</h2>
        <p>{error}</p>
        <button
          className="button secondary"
          onClick={() => setRetry((value) => value + 1)}
        >
          <ArrowClockwise size={17} />
          重新加载
        </button>
      </section>
    );
  if (!brief)
    return (
      <section
        className="panel reader"
        aria-busy="true"
        aria-label="正在加载简报"
      >
        <div className="skeleton wide" />
        <div className="skeleton" />
        <div className="skeleton" />
        <p className="muted">正在打开简报…</p>
      </section>
    );

  const heading = brief.content.match(/^\s*#\s+([^\r\n]+)\r?\n/);
  const content =
    heading?.[1].trim() === brief.title.trim()
      ? brief.content.slice(heading[0].length)
      : brief.content;

  function download() {
    if (!brief) return;
    const url = URL.createObjectURL(
      new Blob([brief.content], { type: "text/markdown;charset=utf-8" }),
    );
    const link = document.createElement("a");
    link.href = url;
    link.download = `${brief.id}.md`;
    link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  return (
    <article className="panel reader">
      <div className="reader-top">
        <span className="eyebrow">PERSONAL BRIEFING</span>
        <button className="button text-button" onClick={download}>
          <DownloadSimple size={17} />
          下载 Markdown
        </button>
      </div>
      <h2 className="brief-title">{brief.title}</h2>
      <div className="brief-meta">
        <time dateTime={brief.created_at}>{formatDate(brief.created_at)}</time>
        <span>{brief.model}</span>
        <span>{brief.tool_calls} 次工具调用</span>
      </div>
      <div className="markdown">
        <Markdown
          remarkPlugins={[remarkGfm]}
          skipHtml
          components={{
            a: ({ children, href }) => (
              <a href={href} target="_blank" rel="noopener noreferrer">
                {children}
                <ArrowSquareOut size={12} aria-hidden="true" />
              </a>
            ),
            // Remote images are rendered as links so opening a brief doesn't load third-party trackers.
            img: ({ src, alt }) => (
              <a href={src} target="_blank" rel="noopener noreferrer">
                {alt || "查看原文图片"}
                <ArrowSquareOut size={12} aria-hidden="true" />
              </a>
            ),
          }}
        >
          {content}
        </Markdown>
      </div>
      <footer className="reader-footer">
        由 NewsAgent 根据你的偏好整理 · 请结合原文判断新闻内容
      </footer>
    </article>
  );
}
