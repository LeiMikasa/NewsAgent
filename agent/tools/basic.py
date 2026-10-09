"""五个基础工具。文件访问限于 workdir；shell 使用宿主权限。"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import tempfile
from pathlib import Path

from .registry import Tool, ToolContext, ToolRegistry
from .result import ToolResult

SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", ".agent"}


def safe_path(ctx: ToolContext, raw: str) -> Path:
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("path must be nonempty text")
    root = Path(ctx.workdir).resolve()
    candidate = Path(raw).expanduser()
    candidate = (candidate if candidate.is_absolute() else root / candidate).resolve()
    # resolve() 同时检查 ../、symlink 和 Windows junction 的实际目标。
    if not candidate.is_relative_to(root):
        raise PermissionError(f"path escapes workspace: {raw}")
    return candidate


def relative_path(ctx: ToolContext, path: Path) -> str:
    return path.relative_to(Path(ctx.workdir).resolve()).as_posix()

# 查看目录内容  用于了解一个目录有哪些文件和子目录，只列出当前层，不递归展开
def run_list_dir(args: dict, ctx: ToolContext) -> ToolResult:
    path = safe_path(ctx, args["path"])
    if path.exists() and not path.is_dir():
        raise NotADirectoryError(str(path))
    entries = []
    truncated = False
    limit = args.get("limit", 200)
    # 只枚举直接子项；避免一次性把大目录加载进内存。
    with os.scandir(path) as children:
        for child in children:
            if len(entries) == limit:
                truncated = True
                break
            target = Path(child.path)
            try:
                resolved = safe_path(ctx, str(target))
                is_dir = resolved.is_dir()
                entry = {"name": child.name, "type": "directory" if is_dir else "file",
                         "size_bytes": None if is_dir else resolved.stat().st_size}
            except (OSError, RuntimeError):
                entry = {"name": child.name, "type": "unavailable", "size_bytes": None}
            entries.append(entry)
    entries.sort(key=lambda item: (item["type"] != "directory", item["name"].casefold()))
    return ToolResult.success({"path": relative_path(ctx, path), "entries": entries, "truncated": truncated})

# 读取文件内容
def _read_text(path: Path, max_bytes: int) -> str | None:
    # 有界读取，也能防止 stat 与读取之间文件增长造成大内存消耗。
    with path.open("rb") as handle:
        raw = handle.read(max_bytes + 1)
    if len(raw) > max_bytes:
        return None
    if b"\x00" in raw:
        raise UnicodeError(f"binary file is not supported: {path.name}")
    return raw.decode("utf-8-sig")


def run_read_file(args: dict, ctx: ToolContext) -> ToolResult:
    path = safe_path(ctx, args["path"])
    if path.is_dir():
        raise IsADirectoryError(str(path))
    text = _read_text(path, ctx.settings.max_file_bytes)
    if text is None:
        return ToolResult.failure("FILE_TOO_LARGE", f"file exceeds {ctx.settings.max_file_bytes} bytes")
    # 默认保持完整原文（包括末尾换行）；指定 offset/limit 后返回行窗口。
    if "offset" not in args and "limit" not in args:
        return ToolResult.success(text)
    lines = text.splitlines(keepends=True)
    offset = args.get("offset", 1)
    end = min(len(lines), offset - 1 + args.get("limit", len(lines)))
    content = "".join(lines[offset - 1:end])
    return ToolResult.success({"path": relative_path(ctx, path), "offset": offset,
                               "total_lines": len(lines), "content": content,
                               "truncated": end < len(lines)})


def run_search_content(args: dict, ctx: ToolContext) -> ToolResult:
    directory = safe_path(ctx, args["dir"])
    if not directory.is_dir():
        raise NotADirectoryError(str(directory))
    keyword = args["keyword"]
    case_sensitive = args.get("case_sensitive", False)
    needle = keyword if case_sensitive else keyword.casefold()
    matches, skipped, scanned = [], 0, 0
    truncated = False
    limit = args.get("max_results", 100)
    scan_limit = args.get("max_files", 5000)
    # 无权限目录也显式计入 skipped，不能悄悄把不完整搜索当作完整结果。
    def on_walk_error(_error):
        nonlocal skipped
        skipped += 1

    for root, dirs, files in os.walk(directory, followlinks=False, onerror=on_walk_error):
        kept = []
        for name in sorted(dirs):
            child = Path(root) / name
            if name in SKIP_DIRS or child.is_symlink() or child.is_junction():
                skipped += 1
                continue
            try:
                safe_path(ctx, str(child))
                kept.append(name)
            except (OSError, RuntimeError):
                skipped += 1
        dirs[:] = kept
        for name in sorted(files):
            if scanned >= scan_limit:
                truncated = True
                break
            scanned += 1
            try:
                path = safe_path(ctx, str(Path(root) / name))
                text = _read_text(path, ctx.settings.max_file_bytes)
                if text is None:
                    skipped += 1
                    continue
            except (OSError, UnicodeError, RuntimeError):
                skipped += 1
                continue
            for number, line in enumerate(text.splitlines(), 1):
                if needle in (line if case_sensitive else line.casefold()):
                    if len(matches) >= limit:
                        truncated = True
                        break
                    matches.append({"path": relative_path(ctx, path), "line": number,
                                    "text": line[:500], "line_truncated": len(line) > 500})
            if truncated:
                break
        if truncated:
            break
    return ToolResult.success({"keyword": keyword, "matches": matches, "scanned_files": scanned,
                               "skipped_entries": skipped, "truncated": truncated,
                               "excluded_dirs": sorted(SKIP_DIRS)})


def run_write_file(args: dict, ctx: ToolContext) -> ToolResult:
    path = safe_path(ctx, args["path"])
    if path.exists() and path.is_dir():
        raise IsADirectoryError(str(path))
    if not args.get("overwrite", True) and path.exists():
        return ToolResult.failure("ALREADY_EXISTS", f"file already exists: {args['path']}")
    content = args["content"].encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    # 同目录临时文件 + replace，避免普通写入失败留下半个文件。
    fd, temporary = tempfile.mkstemp(prefix=".newsagent-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        if args.get("overwrite", True):
            os.replace(temporary, path)
        else:
            # link 的目标必须不存在，避免检查之后出现同名文件时覆盖它。
            os.link(temporary, path)
    except FileExistsError:
        return ToolResult.failure("ALREADY_EXISTS", f"file already exists: {args['path']}")
    finally:
        Path(temporary).unlink(missing_ok=True)
    return ToolResult.success({"path": relative_path(ctx, path), "bytes_written": len(content)})


def shell_command(shell: str) -> list[str]:
    if shell == "auto":
        executable = shutil.which("bash")
        if executable is None:
            executable = (shutil.which("pwsh") or shutil.which("powershell")) if os.name == "nt" else shutil.which("sh")
    else:
        executable = shutil.which(shell)
    if executable is None:
        raise FileNotFoundError(f"shell not found: {shell}; set AGENT_SHELL")
    name = Path(executable).stem.lower()
    if name in {"powershell", "pwsh"}:
        return [executable, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command"]
    if name == "cmd":
        return [executable, "/d", "/s", "/c"]
    return [executable, "-c"]


def _kill_process_tree(process: subprocess.Popen) -> None:
    if os.name == "nt":
        # cmd 未参与路径操作；taskkill 仅针对本次子进程及其子树。
        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       creationflags=subprocess.CREATE_NO_WINDOW, timeout=10, check=False)
        if process.poll() is None:
            process.kill()
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    process.wait(timeout=10)


def run_bash(args: dict, ctx: ToolContext) -> ToolResult:
    if not ctx.settings.allow_bash:
        return ToolResult.failure("POLICY_BLOCKED", "bash is disabled by AGENT_ALLOW_BASH")
    argv = shell_command(ctx.settings.shell)
    timeout = args.get("timeout", ctx.settings.bash_timeout)
    timed_out = False
    options = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW}
               if os.name == "nt" else {"start_new_session": True})
    # 输出落入临时文件，避免 capture_output 将所有输出一次装入内存。
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        process = subprocess.Popen([*argv, args["command"]], cwd=ctx.workdir,
                                   stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr, **options)
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            _kill_process_tree(process)
        except BaseException:
            _kill_process_tree(process)
            raise
        cap = max(1, ctx.settings.max_tool_output // 3)
        def read_output(handle):
            handle.seek(0)
            raw = handle.read(cap + 1)
            return raw[:cap].decode("utf-8", errors="replace"), len(raw) > cap
        out, out_cut = read_output(stdout)
        err, err_cut = read_output(stderr)
    result = {"shell": Path(argv[0]).name, "exit_code": process.returncode,
              "stdout": out, "stderr": err, "truncated": out_cut or err_cut, "timed_out": timed_out}
    if timed_out or process.returncode != 0:
        result["message"] = (f"timeout after {timeout}s" if timed_out else "command exited with a nonzero status")
        result["next_action"] = "inspect actual state before retrying; side effects may have occurred"
        data = ToolResult.success(result)
        return ToolResult.failure("TIMEOUT" if timed_out else "COMMAND_FAILED", data.content)
    return ToolResult.success(result)


def register_basic_tools(registry: ToolRegistry) -> None:
    string = {"type": "string", "minLength": 1, "pattern": "\\S"}
    def schema(properties, required):
        return {"type": "object", "properties": properties, "required": required,
                "additionalProperties": False}
    definitions = [
        Tool("list_dir", "List immediate directory entries relative to the workspace.", schema(
            {"path": string, "limit": {"type": "integer", "minimum": 1, "maximum": 1000}}, ["path"]),
             run_list_dir, read_only=True),
        Tool("read_file", "Read a UTF-8 text file; offset is a 1-based line number. Oversized/binary files are rejected.", schema(
            {"path": string, "offset": {"type": "integer", "minimum": 1},
             "limit": {"type": "integer", "minimum": 1, "maximum": 5000}}, ["path"]), run_read_file, read_only=True),
        Tool("search_content", "Recursively search UTF-8 file content for a literal keyword (case-insensitive by default). Skips binary/oversized files and dependency/state directories.", schema(
            {"keyword": string, "dir": string, "case_sensitive": {"type": "boolean"},
             "max_results": {"type": "integer", "minimum": 1, "maximum": 1000},
             "max_files": {"type": "integer", "minimum": 1, "maximum": 50000}}, ["keyword", "dir"]),
             run_search_content, read_only=True),
        Tool("write_file", "Write UTF-8 content, creating parent directories; overwrites by default. Set overwrite=false to require a new file.", schema(
            {"path": string, "content": {"type": "string"}, "overwrite": {"type": "boolean"}}, ["path", "content"]), run_write_file),
        Tool("bash", "Execute a host shell command with the workspace as cwd. This is not a filesystem sandbox. Returns stdout, stderr and exit_code; timeout may leave side effects.", schema(
            {"command": string, "timeout": {"type": "number", "exclusiveMinimum": 0, "maximum": 300}}, ["command"]), run_bash),
    ]
    for tool in definitions:
        registry.register(tool)
