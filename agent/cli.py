"""一次请求 / 交互 REPL / 离线 demo。"""

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

from .config import load_settings
from .demo import DEMO_PROMPT, DEMO_SCRIPT
from .onboarding import ensure_profile, show_profile
from .runtime import Runtime


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="NewsAgent 基础执行框架")
    parser.add_argument("prompt", nargs="?", help="单次用户请求；省略时进入交互模式")
    parser.add_argument("--demo", action="store_true", help="离线演示全部 5 个工具")
    parser.add_argument("--provider", choices=["mock", "anthropic", "deepseek"])
    parser.add_argument("--model")
    parser.add_argument("--workdir", type=Path)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--max-turns", type=int)
    parser.add_argument("--no-bash", action="store_true", help="禁用 shell 工具")
    parser.add_argument("--no-memory", action="store_true", help="关闭长期记忆与首次资料引导")
    parser.add_argument("--no-auto-memory", action="store_true", help="保留资料加载，关闭普通对话的自动记忆提取")
    parser.add_argument("--setup-profile", action="store_true", help="交互建立或修改长期身份和新闻偏好后退出")
    parser.add_argument("--verbose", action="store_true", help="在 stderr 输出模型/工具进度")
    parser.add_argument("--json", action="store_true", help="输出结构化 LoopResult")
    args = parser.parse_args(argv)
    if args.demo and args.prompt:
        parser.error("--demo cannot be combined with a prompt")
    if args.demo and args.provider and args.provider != "mock":
        parser.error("--demo requires mock provider")
    if args.setup_profile and (args.demo or args.prompt or args.no_memory or args.json):
        parser.error("--setup-profile cannot be combined with --demo, prompt, --no-memory or --json")
    if args.setup_profile and not sys.stdin.isatty():
        parser.error("--setup-profile requires an interactive terminal")
    try:
        settings = load_settings(args.env_file, provider="mock" if args.demo else args.provider,
                                 model="mock-1" if args.demo else args.model,
                                 workdir=args.workdir, max_turns=args.max_turns,
                                 allow_bash=False if args.no_bash else None,
                                 memory_enabled=False if args.no_memory or args.demo else None,
                                 memory_auto_extract=False if args.no_auto_memory else None)
        trace = (lambda text: print(text, file=sys.stderr)) if args.verbose else None
        with Runtime.create(settings, script=DEMO_SCRIPT if args.demo else None, on_event=trace) as runtime:
            if args.setup_profile:
                if not runtime.memory.enabled:
                    raise ValueError("--setup-profile requires memory to be enabled")
                ensure_profile(runtime, edit=True)
                return 0
            if not args.demo and not args.json and sys.stdin.isatty():
                ensure_profile(runtime)
            def submit(prompt):
                result = runtime.submit(prompt)
                notes = runtime.memory.drain_notes()
                if args.verbose:
                    for note in notes:
                        print("[memory] " + note, file=sys.stderr)
                if args.json:
                    print(json.dumps(asdict(result), ensure_ascii=False))
                else:
                    if result.text:
                        print(result.text)
                    if not result.ok:
                        print(f"[{result.stop_reason}] {result.error}", file=sys.stderr)
                return result
            if args.demo or args.prompt:
                result = submit(DEMO_PROMPT if args.demo else args.prompt)
                # demo 不依赖固定终稿断言成功；检查所有工具结果。
                if args.demo:
                    errors = [block for message in runtime.messages if isinstance(message["content"], list)
                              for block in message["content"]
                              if block.get("type") == "tool_result" and block.get("is_error")]
                    if errors:
                        print("demo tool failures: " + json.dumps(errors, ensure_ascii=False), file=sys.stderr)
                        return 1
                return 0 if result.ok else 1
            if not sys.stdin.isatty():
                parser.error("provide a prompt or --demo when stdin is not interactive")
            print(f"NewsAgent ({settings.provider}, {settings.model}) · /profile 查看资料 · /memory 查看记忆 · /reset 清空会话 · /exit 退出")
            while True:
                try:
                    prompt = input("你> ").strip()
                except EOFError:
                    break
                if prompt == "/exit":
                    break
                if prompt == "/reset":
                    runtime.reset()
                    print("当前对话已清空，长期记忆保留。")
                elif prompt == "/profile":
                    show_profile(runtime)
                elif prompt == "/profile edit":
                    ensure_profile(runtime, edit=True)
                elif prompt == "/memory":
                    if not runtime.memory.enabled:
                        print("长期记忆已关闭。")
                    else:
                        records = runtime.memory.list_records()
                        print(f"长期记忆共 {len(records)} 条：")
                        for record in records:
                            print(f"- {record['name']}: {record['description']}")
                elif prompt:
                    submit(prompt)
        return 0
    except KeyboardInterrupt:
        return 130
    except (ValueError, RuntimeError, OSError) as exc:
        print(f"configuration/runtime error: {exc}", file=sys.stderr)
        return 1
