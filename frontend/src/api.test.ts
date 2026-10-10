import { afterEach, describe, expect, it, vi } from "vitest";
import { api, errorMessage, mergeBriefs, request } from "./api";
import type { Brief } from "./types";

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe("backend requests", () => {
  it("submits a one-off prompt without empty optional fields", async () => {
    const fetch = vi
      .fn()
      .mockImplementation(
        async () =>
          new Response(JSON.stringify({ id: "run" }), { status: 202 }),
      );
    vi.stubGlobal("fetch", fetch);
    await api.generate("   ");
    expect(fetch.mock.calls[0][1].body).toBe("{}");
    await api.generate("  关注 Agent 进展  ");
    expect(JSON.parse(fetch.mock.calls[1][1].body)).toEqual({
      prompt: "关注 Agent 进展",
    });
  });

  it("surfaces the backend BUSY reason and does not retry a mutation", async () => {
    const fetch = vi
      .fn()
      .mockResolvedValue(
        new Response(
          JSON.stringify({ detail: { code: "BUSY", message: "任务正在执行" } }),
          { status: 409 },
        ),
      );
    vi.stubGlobal("fetch", fetch);
    await expect(api.generate()).rejects.toThrow("任务正在执行");
    expect(fetch).toHaveBeenCalledTimes(1);
  });

  it("shows validation errors rather than an object string", () => {
    expect(errorMessage([{ loc: ["body", "time"], msg: "时间格式无效" }])).toBe(
      "time：时间格式无效",
    );
  });

  it("handles a non-JSON proxy failure", async () => {
    vi.stubGlobal(
      "fetch",
      vi
        .fn()
        .mockResolvedValue(
          new Response("<html>Bad Gateway</html>", { status: 502 }),
        ),
    );
    await expect(request("/status")).rejects.toThrow("502");
  });

  it("retries mail through the saved run rather than generating a brief", async () => {
    const fetch = vi
      .fn()
      .mockResolvedValue(new Response("{}", { status: 202 }));
    vi.stubGlobal("fetch", fetch);
    await api.sendEmail("task-id");
    expect(fetch.mock.calls[0][0]).toBe("/api/runs/task-id/send-email");
    expect(fetch.mock.calls[0][1].method).toBe("POST");
  });

  it("aborts a hung request and asks to refresh rather than resubmitting", async () => {
    vi.useFakeTimers();
    const fetch = vi.fn().mockImplementation(
      (_url, options) =>
        new Promise((_resolve, reject) => {
          options.signal.addEventListener("abort", () =>
            reject(new DOMException("Aborted", "AbortError")),
          );
        }),
    );
    vi.stubGlobal("fetch", fetch);
    const result = expect(api.generate()).rejects.toThrow(
      "请求超时，请刷新任务状态后再试。",
    );
    await vi.advanceTimersByTimeAsync(15000);
    await result;
    expect(fetch).toHaveBeenCalledTimes(1);
  });
});

it("keeps older pages and updates duplicate metadata when polling latest briefs", () => {
  const brief = (id: string, title: string) => ({ id, title }) as Brief;
  expect(
    mergeBriefs(
      [brief("03", "最新"), brief("02", "新标题")],
      [brief("02", "旧标题"), brief("01", "历史")],
    ),
  ).toEqual([brief("03", "最新"), brief("02", "新标题"), brief("01", "历史")]);
});
