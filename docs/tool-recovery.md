# 工具恢复代码说明

本次将 `D:/HarnessAgent/agent/tools/recovery.py` 的两个独立预算适配到 NewsAgent。保留 dataclass ToolResult、现有 Hooks 的异常处理和工作区边界，没有导入 HarnessAgent 的上下文压缩、权限审批、MCP 或团队模块。

## 1. 两个数字怎样计数

`max_tool_retries=2`：程序第一次执行之后最多重试两次，一次模型工具调用最多执行三次 handler。只有明确允许重试的只读工具，且失败属于临时故障，才会重试；参数验证失败执行次数为零。

`max_tool_repairs=2`：同一用户请求中，同一工具/目标的第一次可修正失败之后，最多两次修正机会。三次最终失败后，下一次提出的调用被拦截。修正后的参数是否正确由模型负责；程序只计算这些最终失败并限制次数。这个数字不是模型 API 总调用次数，也不计算所有错误。

程序中间失败不直接进入模型修正计数。程序依次执行 `失败 → 失败 → 成功` 时，结果 attempts=3，目标失败计数清零；执行 `临时失败 → 临时失败 → NOT_FOUND` 时，结果 attempts=3，但可修正失败计数只增加一次。

TIMEOUT 不属于模型修正集合，单次程序三次尝试全部超时后，模型若再次提出调用，会获得新的程序重试预算；这时仍由 max_turns 限制总模型轮次。没有全局无限次数重试机制，也没有全错误类型的熔断；不能将两个默认数字简单乘成九次。

## 2. result.py：给恢复器可靠的状态

ToolResult 仍为冻结 dataclass，原来的 ok/content/error_code 保留。新增：

| 字段 | 作用 |
|---|---|
| field | 具体出错的参数 |
| action | 给模型的下一步建议 |
| transient | 是否明确属于临时故障 |
| execution_status | succeeded/failed/not_executed/unknown |
| inspected_path | 已完整检查的文件路径，只供程序状态恢复 |
| retry_after | 服务建议的等待秒数 |
| attempts | 本次工具调用实际执行次数 |
| repair_failures | 当前工具/目标累计的可修正最终失败数 |

`success()` 保持原来的文字或 JSON content，并允许携带完整文件检查证据。`failure()` 可以标记错误的性质，默认 not_executed；已请求服务但失败时应显式设 failed，连接结果不明或文件修改结果未知时设 unknown。

`from_exception()` 复用 HarnessAgent 的保守分类。权限错误、路径不存在、文本解码失败、目录前提错误有各自错误码；TimeoutError/ConnectionError/InterruptedError 和指定临时 errno 标记 transient。读操作失败设 failed，写操作异常保守设 unknown。识别为临时错误仍不等于允许重试，需要工具满足两个宿主标记。

`model_content()` 成功时仍返回原 content；失败时序列化状态、次数和 recovery_hint。auto_retry=false 表示这是回送模型的最终结果，并不表示之前没有内部重试。完整读取路径不发给模型。正文与错误截断使用 dataclasses.replace，保留这些元数据。

## 3. registry.py：声明能力和分类异常

Tool 增加 `retry_safe=False`。`read_only` 与 `retry_safe` 都由注册工具的 Python 代码设置，不放进模型参数 Schema。避免模型通过参数开启重试。

`validate()` 保持执行前的 JSON Schema 校验，并附加 correct_arguments 和 field。验证失败不触发 PreToolUse，也不执行 handler。

`dispatch()` 只做一次尝试，每次向 handler 传递参数深拷贝。某次 handler 修改自己的参数字典，不影响后续重试的原参数。异常统一交给 `ToolResult.from_exception()`。它不自行重试，避免和 loop 形成两套重复重试逻辑。

## 4. recovery.py：恢复状态与判断

`ToolRecovery.__init__()` 持有 failures 字典、uncertain_files 集合、events 列表、executions 计数。sleep 和 random_value 可注入，因此测试不需要等待。两个预算均校验类型及范围。

`arguments_hash()` 对参数的确定性序列化计算 SHA-256，日志只保存哈希。失败的异常目标归一化保守回退，不执行文件或网络操作。

`normalized_path()` 解析绝对路径、相对路径、用户目录与实际链接目标，并做 Windows 路径大小写归一化。

`normalized_url()` 统一域名大小写和末尾点、移除默认端口、补根路径、删除 fragment。保留查询参数和路径大小写，不随意把不同资源合并。该函数只为计数，不代替新闻工具的 URL 安全校验。

`key()` 得到 `(workdir, tool_name, target)`。文件目标是实际路径。search_news 的 target 是归一化 query/time_range/domains 的哈希，忽略 limit；fetch_article 是 URL 的哈希，忽略正文长度和提取深度。其他工具沿用路径/目录/ID 等目标字段，没有目标时使用 default。

`exhausted()` 先检查文件写入结果是否未知，然后检查累计可修正失败数是否达到 `1 + max_repairs`。返回 STATE_CHECK_REQUIRED 或 RECOVERY_EXHAUSTED，不执行 handler。

`observe()` 只接收一次模型工具调用的最终结果。指定可修正错误增加计数，成功删除该目标的失败计数；写入 unknown 记录目标，带完整检查证据的 read_file 清除目标。返回带更新 repair_failures 的结果。

REPAIR_CODES 保留 INVALID_ARGUMENT/UNKNOWN_TOOL/NOT_FOUND/PRECONDITION_FAILED，补充新闻的 INVALID_REQUEST/EXTRACTION_FAILED/EMPTY_CONTENT。认证、限流和超时未加入这份修正集合。换成新目标可以开始新的修正计数，max_turns 仍限制模型总轮次。

`can_retry()` 要求工具存在、read_only、retry_safe、结果失败且 transient、程序重试预算未耗尽，并排除权限/认证/参数等错误。Retry-After 超过等待上限时也拒绝自动重试。

`record()` 记录每次尝试或拦截的 phase/attempt/ok/error_code/execution_status/repair_failures/elapsed_seconds/auto_retry。只有 execution 阶段增加实际执行总数；其余阶段 attempt=0。最多保留最近 200 条，dropped_events 统计被省略的记录，不影响预算。

`wait()` 使用 HarnessAgent 的指数退避加随机抖动，增加有效 Retry-After 等待提示；自动等待最多 30 秒。大于上限的服务提示在 can_retry 阶段被拒绝，不会压缩成更短等待后提前请求。

## 5. loop.py：把恢复包在每次工具调用外面

execute_tool 的执行顺序：

```text
确定计数目标
  → 检查恢复状态与模型修正预算
  → Schema 校验
  → PreToolUse
  → dispatch 一次
  → 是否还有安全自动重试机会？
      有：PostToolUse、记录、等待，再经过 PreToolUse 和 dispatch
      无：observe 最终结果、PostToolUse、记录、返回
```

参数错误、Hook 拦截等没有执行 handler 的结果也回送模型，并保持现有 PostToolUse 通知行为。PreToolUse 异常阻止调用；PostToolUse 任意观察者失败均不改写工具结果，其他观察者继续运行。每次自动尝试都重查 PreToolUse。

工具输出截断会去掉 inspected_path，避免“只看到了部分文件”解除写入状态保护。其他元数据不丢失。内部重试只产生程序记录，不产生新的 tool_use/tool_result；模型最后仍收到与原 tool_use_id 配对的一份结果。

run_loop 使用整个请求共享的恢复对象。tool_calls 表示模型提出的调用数量，包括被拦截的调用；tool_attempts 表示 handler 实际执行总数，包括内部重试。仍然保留原有 stop_reason 和 ok 语义：final 是正常给出最终回答，不是所有工具成功的证明。

verbose 增加 `[retry] 工具名: 错误码; next attempt ...` 和 `[recovery] 工具名: 拦截码`。不打印参数或 API Key。

## 6. runtime.py：控制恢复作用域

Runtime 持有 recovery。每次通过 UserPromptSubmit 的 submit 新建恢复对象，注入 run_loop；自动重试和模型修正共享这一对象，不跨用户请求累积。reset 也重建恢复对象，并保持长期记忆文件。

连续直接 call_tool 使用当前对象，便于 Python 调用者逐步修复同一目标。下一次 submit 或 reset 建立新的作用域。不同 Runtime 对象的状态互相独立。

RuntimeStats 增加 tool_attempts。请求统计由 LoopResult 汇总，直接调用统计使用 recovery.executions 的前后差值。ctx.extra['tool_attempts'] 引用同一份有上限的 events，不再复制正文或参数历史。

文件未知状态也仅保存在这个作用域，不跨请求/进程持久化。这里只有文件工具的写入目标检查，不会解析所有 bash 副作用，也不构成完整的权限沙箱。

## 7. basic.py：只读重试与完整文件检查

list_dir/read_file/search_content 注册 retry_safe=True；write_file/bash 保持不允许自动重试。

read_file 完整且未截断时返回 inspected_path。行窗口只有从第一行开始且覆盖全文、最终工具输出未截断，才能算完整检查。文件不存在时返回同一规范路径的不存在证据，也能确认之前未知写入没有留下该文件。

超长文件、二进制文件、读取权限错误不提供证据。write_file 的异常由 registry 保守分类为 unknown，恢复器阻止同目标再次写入，直到明确检查。

bash 超时或非零退出使用 inspect_state/unknown，超时可标记 transient 但仍不能自动重试，因为工具不是 read_only。要求模型检查实际状态；当前不会由程序推断具体被修改的文件。

## 8. news.py：给 HTTP 故障打标签

请求函数仍只执行一次，由外层恢复器控制重复执行。明确的临时网络故障、HTTP 408/425/429/500/502/503/504 标记 transient；认证、额度、参数、证书错误不自动重试。其他不能确定是否临时的错误保守返回。

`_retry_after()` 解析秒数或 HTTP 日期，拒绝非有限数字和无效值。`_network_transient()` 判断实际异常类型/errno；不通过异常文字猜测，证书验证错误直接视为非临时。

两个新闻工具显式 retry_safe=True。服务器参数错误进入模型修正预算；正文提取失败和空内容不盲目重复，而是允许模型改变提取深度或选择其他来源。改变深度和长度仍算同一个 URL 目标。

## 9. 配置与测试

```dotenv
AGENT_MAX_TOOL_RETRIES=2
AGENT_MAX_TOOL_REPAIRS=2
```

retries 支持 0..5，0 关闭自动重试；repairs 支持 0..10，0 表示首次可修正失败后不再允许修正。Settings 和 ToolRecovery 都验证范围。没有改动用户实际 .env，未填写这两项时使用默认值。

test_tool_recovery.py 用故障注入验证两个预算、成功清零、目标区分、状态未知写入、截断读取、每次权限检查、观察者异常、参数隔离、HTTP 重试、Retry-After、统计和日志上限。HTTP 与 sleep 均替换为测试实现，不发送真实请求、不消耗 API 额度。原新闻错误分类测试显式关闭重试，单独验证单次响应；恢复行为由专项测试验证。

运行：

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m agent --demo --verbose --json
```
