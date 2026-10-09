"""确定性离线闭环，覆盖 5 个工具，不调用真实 API。"""

DEMO_PROMPT = "在 .agent/demo 中写一条示例 AI 新闻，列出目录、读取、搜索，并运行 echo 检查 shell。"
DEMO_SCRIPT = [
    {"tool": "write_file", "input": {"path": ".agent/demo/news.txt", "content": "AI news demo: agent loop is ready.\n"}},
    [
        {"tool": "list_dir", "input": {"path": ".agent/demo"}},
        {"tool": "read_file", "input": {"path": ".agent/demo/news.txt"}},
        {"tool": "search_content", "input": {"keyword": "AI", "dir": ".agent/demo"}},
        {"tool": "bash", "input": {"command": "echo newsagent-shell-ok"}},
    ],
    "[mock demo] 示例文件已写入 .agent/demo/news.txt，5 个基础工具调用已返回。",
]
