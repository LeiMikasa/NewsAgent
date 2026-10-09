"""一次请求 / 交互 REPL / 离线 demo。"""

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

from .config import load_settings
from .demo import DEMO_PROMPT, DEMO_SCRIPT
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
    parser.add_argument("--verbose", action="store_true", help="在 stderr 输出模型/工具进度")
    parser.add_argument("--json", action="store_true", help="输出结构化 LoopResult")
    args = parser.parse_args(argv)
    if args.demo and args.prompt:
        parser.error("--demo cannot be combined with a prompt")
    if args.demo and args.provider and args.provider != "mock":
        parser.error("--demo requires mock provider")
    try:
        settings = load_settings(args.env_file, provider="mock" if args.demo else args.provider,
                                 model="mock-1" if args.demo else args.model,
                                 workdir=args.workdir, max_turns=args.max_turns,
                                 allow_bash=False if args.no_bash else None)
        trace = (lambda text: print(text, file=sys.stderr)) if args.verbose else None
        with Runtime.create(settings, script=DEMO_SCRIPT if args.demo else None, on_event=trace) as runtime:
            def submit(prompt):
                result = runtime.submit(prompt)
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
            print(f"NewsAgent ({settings.provider}, {settings.model}) · /reset 清空会话 · /exit 退出")
            while True:
                try:
                    prompt = input("你> ").strip()
                except EOFError:
                    break
                if prompt == "/exit":
                    break
                if prompt == "/reset":
                    runtime.reset()
                    print("会话已清空。")
                elif prompt:
                    submit(prompt)
        return 0
    except KeyboardInterrupt:
        return 130
    except (ValueError, RuntimeError, OSError) as exc:
        print(f"configuration/runtime error: {exc}", file=sys.stderr)
        return 1
