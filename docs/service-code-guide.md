# service.py 全代码与调用跳转说明

本文对应当前 `D:/NewsAgent/backend/service.py`。覆盖原文件全部 256 行，逐条解释有内容的行，并给出它调用到其他文件的路径。原业务文件没有因本文被修改。行号对应本次读取的版本，后续增删代码后可能变化。

## 先理解“跳转”

| 代码形式 | 什么时候执行 | 执行结束之后 |
| --- | --- | --- |
| `self.store.create_run(...)` | 当前线程立即调用 | 返回字典，继续下一行 |
| `self._executor.submit(self._generate, ...)` | 由工作线程执行传入函数 | 提交方先继续；业务结果另存数据库 |
| `Runtime.create(..., on_event=progress)` | 创建时只传入回调 | Agent 运行时调用 `progress(message)` 更新进度 |
| `apply_schedule(saved)` | 当前线程调用传入的函数 | 本项目执行 `scheduler.apply(saved)`，返回后继续 |
| `with ... as runtime` | 进入时调用 `__enter__()` | 离开时调用 `__exit__()`，这里会关闭 Runtime |
| `raise BusyError(...)` | 中断正常执行，向外抛异常 | 接口层匹配异常处理器并生成 HTTP 错误 |

普通函数调用会执行完再回到调用位置；它不是跳过去之后就不回来。函数作为参数传入时，没有括号的 `self._generate` 或 `progress` 表示函数对象；真正加括号调用时才执行函数体。

## 对象和数据分别在哪里

| 对象/数据 | 所在位置 | 生命周期 |
| --- | --- | --- |
| `BackendService`、线程池、`_active_id` | 后端进程内存 | 随后端进程启动/停止 |
| `Runtime`、本次 messages、ToolRecovery | 每次生成自己的对象 | 一次生成结束后关闭 Runtime |
| 订阅和任务/邮件状态 | `.agent/backend.sqlite3` | 后端重启后仍能读取 |
| 用户身份和偏好 | `.agent/memory/*.md` | CLI 和后端复用核心记忆文件 |
| 简报正文 | `.agent/briefs/*.md` | 历史页和邮件按需读取 |
| 模型密钥与 SMTP 授权码 | 后端配置 | 前端不接收配置中的秘密值 |

`StateStore` 负责数据库操作，`MemoryStore` 负责记忆文件，`BriefStore` 负责简报文件。service.py 把这些对象组织起来。服务层的 MockLLM 只用于直接资料管理；生成 Runtime 仍按 Settings 选择模型。

## 原文件逐段解释

每段先保留原代码，再用表格解释每一条有内容的行。表格中的链接可打开原代码对应行。

### 1. 导入、常量与自定义异常

[打开原文件第 1 行](D:/NewsAgent/backend/service.py:1)

```python
"""Manual and scheduled requests share one worker and one control lock."""

from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import datetime, timedelta
from typing import Callable

from agent import BriefStore, Runtime, Settings
from agent.briefs import SHANGHAI
from agent.llm import MockLLM
from agent.memory import MemoryStore

from .storage import InstanceLock, StateStore
from .email_delivery import EmailSender

logger = logging.getLogger(__name__)
CATCH_UP_WINDOW = timedelta(hours=1)


class BusyError(RuntimeError):
    pass


class ProfileRequired(ValueError):
    pass


```

| 原行号 | 解释 |
| --- | --- |
| [1](D:/NewsAgent/backend/service.py:1) | 模块说明字符串：手动生成与定时生成共用同一个工作线程池和控制锁。 |
| [3](D:/NewsAgent/backend/service.py:3) | 让类型注解延后求值；这些注解用于描述参数和返回值，不等同于运行时校验。 |
| [5](D:/NewsAgent/backend/service.py:5) | 导入日志模块，后面使用 logger.error() 记录执行异常。 |
| [6](D:/NewsAgent/backend/service.py:6) | 导入线程模块，使用 threading.RLock() 保护多个线程共享的服务状态。 |
| [7](D:/NewsAgent/backend/service.py:7) | 导入线程池。submit() 会安排函数在工作线程执行，并立即返回 Future。 |
| [8](D:/NewsAgent/backend/service.py:8) | 导入 asdict()，将生成结果 dataclass 转成可保存为 JSON 的字典。 |
| [9](D:/NewsAgent/backend/service.py:9) | datetime 表示日期时间，timedelta 表示时间差；用于定时任务的当天日期和一小时窗口。 |
| [10](D:/NewsAgent/backend/service.py:10) | Callable 是类型提示，说明参数是可以调用的对象，例如函数。 |
| [12](D:/NewsAgent/backend/service.py:12) | 导入简报存储、Agent Runtime 和配置类；agent 包在 __init__.py 中导出这些类。 |
| [13](D:/NewsAgent/backend/service.py:13) | 导入项目定义的北京时间时区 SHANGHAI。 |
| [14](D:/NewsAgent/backend/service.py:14) | 导入模拟模型对象，供服务层直接维护用户资料使用。 |
| [15](D:/NewsAgent/backend/service.py:15) | 导入 Markdown 长期记忆存储类。 |
| [17](D:/NewsAgent/backend/service.py:17) | 点号代表当前 backend 包：从同包 storage.py 导入实例锁和 SQLite 状态存储。 |
| [18](D:/NewsAgent/backend/service.py:18) | 从同包 email_delivery.py 导入邮件发送器。 |
| [20](D:/NewsAgent/backend/service.py:20) | __name__ 是当前模块名称 backend.service；创建对应的日志记录器，不是输出日志本身。 |
| [21](D:/NewsAgent/backend/service.py:21) | 定义一小时补跑窗口；hours=1 是关键字参数。 |
| [24](D:/NewsAgent/backend/service.py:24) | 定义后台忙碌异常，继承 RuntimeError，便于 app.py 按类型返回 HTTP 409。 |
| [25](D:/NewsAgent/backend/service.py:25) | pass 表示类体不增加其他内容；这个类仍有自己的异常类型。 |
| [28](D:/NewsAgent/backend/service.py:28) | 定义用户资料不完整异常，继承 ValueError；app.py 单独把它映射为 HTTP 409。 |
| [29](D:/NewsAgent/backend/service.py:29) | 无需添加方法，异常信息沿用父类构造方式。 |

### 2. __init__：构造服务对象

[打开原文件第 32 行](D:/NewsAgent/backend/service.py:32)

```python
class BackendService:
    def __init__(self, settings: Settings, *, runtime_factory: Callable | None = None,
                 email_sender: EmailSender | None = None):
        self.settings = settings
        self.store = StateStore(settings.workdir) # 保存订阅设置，任务状态，错误和邮件结果
        # Operational state must not be mistaken for long-term memory or a brief.
        if any(self.store.path.resolve().is_relative_to(directory) for directory in (settings.memory_dir, settings.briefs_dir)):
            raise ValueError("backend state directory overlaps memory_dir or briefs_dir")
        self.instance_lock = InstanceLock(self.store.path.with_suffix(".lock"))
        # Profile CRUD is deterministic and must work without API credentials or model calls.
        self.memory = MemoryStore(MockLLM(script=[]), settings.model, settings.memory_dir,
                                  settings.workdir, enabled=settings.memory_enabled)
        self.briefs = BriefStore(settings.workdir, settings.briefs_dir, max_content_bytes=settings.max_file_bytes)
        self.runtime_factory = runtime_factory or (lambda on_event: Runtime.create(settings, on_event=on_event))
        self.email_sender = email_sender if email_sender is not None else EmailSender(settings)
        self._lock = threading.RLock()
        self._executor = None
        self._active_id = None
        self._started = False
        self._closing = False
```

| 原行号 | 解释 |
| --- | --- |
| [32](D:/NewsAgent/backend/service.py:32) | 定义服务类。self 表示当前服务实例，实例成员可在不同方法中共同访问。 |
| [33](D:/NewsAgent/backend/service.py:33) | 构造函数接受 Settings；* 后的参数必须按名称传入。runtime_factory 是函数或 None，默认不提供。 |
| [34](D:/NewsAgent/backend/service.py:34) | 允许提供邮件发送器；用于测试替换真实发送行为。-> None 描述构造函数不返回业务结果。 |
| [35](D:/NewsAgent/backend/service.py:35) | 保留配置对象，后续方法读取同一份配置。 |
| [36](D:/NewsAgent/backend/service.py:36) | 创建状态存储对象，数据库路径是工作区/.agent/backend.sqlite3；此处尚未初始化表。 |
| [37](D:/NewsAgent/backend/service.py:37) | 原注释说明：运行状态不能混入用户长期记忆或简报目录。 |
| [38](D:/NewsAgent/backend/service.py:38) | resolve() 解析实际绝对路径；is_relative_to() 判断数据库是否落在指定目录内。生成器逐个检查记忆目录、简报目录，any() 遇到一个 True 就成立。 |
| [39](D:/NewsAgent/backend/service.py:39) | 目录冲突时终止构造，避免后台数据库进入记忆或简报存储范围。 |
| [40](D:/NewsAgent/backend/service.py:40) | with_suffix(".lock") 把 backend.sqlite3 的扩展名替换为 .lock，得到 backend.lock；建立跨进程实例锁对象。 |
| [41](D:/NewsAgent/backend/service.py:41) | 原注释说明：查看、修改用户资料应直接操作文件，不要求模型凭据或模型请求。 |
| [42](D:/NewsAgent/backend/service.py:42) | 构造服务层 MemoryStore，传入 MockLLM(script=[])、模型名称和记忆目录；这不会把实际生成模型切换为 mock。 |
| [43](D:/NewsAgent/backend/service.py:43) | 继续传入工作区和记忆开关；enabled= 是关键字参数。与上一行共同构成一个调用。 |
| [44](D:/NewsAgent/backend/service.py:44) | 创建服务层简报存储，用于查阅归档、读取邮件正文；正文大小上限沿用配置。 |
| [45](D:/NewsAgent/backend/service.py:45) | 有外部工厂时使用外部工厂，否则保留 lambda 函数。lambda 在后续被调用时才创建 Runtime，并把执行事件回调 on_event 交给它。 |
| [46](D:/NewsAgent/backend/service.py:46) | 有外部邮件发送器时使用它；否则创建 EmailSender(settings)。这里的 if ... else 是条件表达式。 |
| [47](D:/NewsAgent/backend/service.py:47) | 创建可重入线程锁，同一线程可以嵌套获取；其他线程需要等待。它只协调当前进程中的这个服务实例。 |
| [48](D:/NewsAgent/backend/service.py:48) | 先没有线程池，start() 中才初始化。 |
| [49](D:/NewsAgent/backend/service.py:49) | 当前执行位置空闲。None 表示尚无活动任务编号。 |
| [50](D:/NewsAgent/backend/service.py:50) | 服务尚未启动；创建对象和启动服务是两个阶段。 |
| [51](D:/NewsAgent/backend/service.py:51) | 服务尚未处于关闭过程。 |

调用去向与入口：

| 调用/入口 | 作用 |
| --- | --- |
| [StateStore.__init__](D:/NewsAgent/backend/storage.py:64) | 建立数据库路径；表尚未创建。 |
| [InstanceLock.__init__](D:/NewsAgent/backend/storage.py:23) | 建立操作系统锁对象。 |
| [MemoryStore.__init__](D:/NewsAgent/agent/memory.py:202) | 服务层资料读写组件。 |
| [BriefStore.__init__](D:/NewsAgent/agent/briefs.py:25) | 服务层归档读取组件。 |
| [Runtime.create](D:/NewsAgent/agent/runtime.py:72) | 这里只保留工厂，实际生成时才调用。 |
| [EmailSender.__init__](D:/NewsAgent/backend/email_delivery.py:21) | 默认邮件发送器。 |

### 3. start：初始化数据库、实例锁与线程池

[打开原文件第 53 行](D:/NewsAgent/backend/service.py:53)

```python
    def start(self) -> None:
        with self._lock:
            if self._started:
                raise RuntimeError("backend already started")
            self.store.prepare()
            self.instance_lock.acquire()
            try:
                self.store.initialize()
                self.store.recover_interrupted()
                self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="newsagent")
                self._started = True
                self._closing = False
            except Exception:
                self.instance_lock.release()
                raise
```

| 原行号 | 解释 |
| --- | --- |
| [53](D:/NewsAgent/backend/service.py:53) | 定义服务启动方法；由 app.py 的 lifespan 调用。 |
| [54](D:/NewsAgent/backend/service.py:54) | 进入控制锁，退出 with 时自动释放。 |
| [55](D:/NewsAgent/backend/service.py:55) | 检查是否已经启动。 |
| [56](D:/NewsAgent/backend/service.py:56) | 重复启动属于错误，抛出异常而非再创建一套工作线程池。 |
| [57](D:/NewsAgent/backend/service.py:57) | 调用 StateStore.prepare()：检查路径并创建数据库父目录。 |
| [58](D:/NewsAgent/backend/service.py:58) | 调用 InstanceLock.acquire() 获取操作系统文件锁，阻止另一后端使用同一工作区。 |
| [59](D:/NewsAgent/backend/service.py:59) | 锁取得之后开始可能失败的初始化操作。 |
| [60](D:/NewsAgent/backend/service.py:60) | 调用 StateStore.initialize() 创建或迁移表、索引及默认订阅记录。 |
| [61](D:/NewsAgent/backend/service.py:61) | 调用 recover_interrupted()：旧 queued/running 生成任务改成 interrupted；旧 pending/sending 邮件改成 unknown。 |
| [62](D:/NewsAgent/backend/service.py:62) | 准备最多一个工作线程的线程池。线程按需启动，thread_name_prefix 只设置线程名字前缀。 |
| [63](D:/NewsAgent/backend/service.py:63) | 初始化成功，标记已启动。 |
| [64](D:/NewsAgent/backend/service.py:64) | 清除正在关闭标记。 |
| [65](D:/NewsAgent/backend/service.py:65) | 捕获初始化阶段的常规异常；不会捕获所有 BaseException，例如 KeyboardInterrupt。 |
| [66](D:/NewsAgent/backend/service.py:66) | 初始化失败时释放实例锁，避免失败实例继续占用工作区。 |
| [67](D:/NewsAgent/backend/service.py:67) | 裸 raise 把刚刚捕获的原异常继续向外抛，保留原来的异常信息。 |

调用去向与入口：

| 调用/入口 | 作用 |
| --- | --- |
| [app.lifespan 调用这里](D:/NewsAgent/backend/app.py:108) | 应用启动先 start 服务，再启动定时器。 |
| [StateStore.prepare](D:/NewsAgent/backend/storage.py:68) | 检查数据库路径，创建父目录。 |
| [InstanceLock.acquire](D:/NewsAgent/backend/storage.py:27) | 获取 OS 文件锁。 |
| [StateStore.initialize](D:/NewsAgent/backend/storage.py:89) | 创建/迁移表及去重索引。 |
| [StateStore.recover_interrupted](D:/NewsAgent/backend/storage.py:121) | 将旧未完成记录标记为中断/未知。 |

### 4. close：正常关闭服务

[打开原文件第 69 行](D:/NewsAgent/backend/service.py:69)

```python
    def close(self) -> None:
        with self._lock:
            if not self._started:
                return
            self._closing = True
            executor = self._executor
        # Do not hold the control lock while the worker finishes its current request.
        try:
            executor.shutdown(wait=True)
        finally:
            with self._lock:
                self._started = False
                self._executor = None
                self.instance_lock.release()
```

| 原行号 | 解释 |
| --- | --- |
| [69](D:/NewsAgent/backend/service.py:69) | 定义正常关闭方法。 |
| [70](D:/NewsAgent/backend/service.py:70) | 短暂获取控制锁，保护下面的状态变更。 |
| [71](D:/NewsAgent/backend/service.py:71) | 服务没有启动就无需关闭。 |
| [72](D:/NewsAgent/backend/service.py:72) | 提前返回；退出 with 时锁仍会自动释放。 |
| [73](D:/NewsAgent/backend/service.py:73) | 先标记正在关闭，后续 generate()/send_email() 就不能接收新任务。 |
| [74](D:/NewsAgent/backend/service.py:74) | 将线程池引用保存到局部变量，供退出控制锁后关闭。 |
| [75](D:/NewsAgent/backend/service.py:75) | 原注释说明：等待工作线程结束时不能一直握着控制锁。 |
| [76](D:/NewsAgent/backend/service.py:76) | 退出上面的锁后，开始等待线程池关闭。 |
| [77](D:/NewsAgent/backend/service.py:77) | shutdown(wait=True) 停止继续提交，并等待已提交工作结束；它不会把当前模型请求强制取消。 |
| [78](D:/NewsAgent/backend/service.py:78) | 无论等待阶段如何结束，都执行清理。 |
| [79](D:/NewsAgent/backend/service.py:79) | 重新取得控制锁。 |
| [80](D:/NewsAgent/backend/service.py:80) | 标记服务停止。 |
| [81](D:/NewsAgent/backend/service.py:81) | 清除服务保留的线程池引用。 |
| [82](D:/NewsAgent/backend/service.py:82) | 释放跨进程实例锁；内部关闭锁文件句柄。 |

调用去向与入口：

| 调用/入口 | 作用 |
| --- | --- |
| [app.lifespan 关闭服务](D:/NewsAgent/backend/app.py:117) | 通过 asyncio.to_thread 等待关闭，避免直接阻塞事件循环。 |
| [InstanceLock.release](D:/NewsAgent/backend/storage.py:47) | 解除操作系统锁并关闭句柄。 |

### 5. clean_error：处理错误文字

[打开原文件第 84 行](D:/NewsAgent/backend/service.py:84)

```python
    def clean_error(self, message: str) -> str:
        for secret in (self.settings.api_key, self.settings.tavily_api_key, self.settings.smtp_password):
            if secret:
                message = message.replace(secret, "[redacted]")
        return message[:16_000]
```

| 原行号 | 解释 |
| --- | --- |
| [84](D:/NewsAgent/backend/service.py:84) | 定义错误/进度文字处理函数，输入输出均为字符串。 |
| [85](D:/NewsAgent/backend/service.py:85) | 依次取当前配置里的模型密钥、Tavily 密钥、SMTP 授权码。 |
| [86](D:/NewsAgent/backend/service.py:86) | 只处理有值的密钥；避免用空字符串做替换。 |
| [87](D:/NewsAgent/backend/service.py:87) | 替换文字中出现的已知密钥文本，返回新的字符串并重新赋给 message。 |
| [88](D:/NewsAgent/backend/service.py:88) | 切片保留前 16000 个字符；16_000 中的下划线只帮助阅读。 |

### 6. profile：查看身份和偏好

[打开原文件第 90 行](D:/NewsAgent/backend/service.py:90)

```python
    def profile(self) -> dict:
        with self._lock:
            return {**self.memory.profile(), "missing_fields": self.memory.missing_profile_fields(),
                    "memory_enabled": self.memory.enabled}
```

| 原行号 | 解释 |
| --- | --- |
| [90](D:/NewsAgent/backend/service.py:90) | 定义查询用户资料方法。 |
| [91](D:/NewsAgent/backend/service.py:91) | 使用控制锁，让资料读取和服务层资料修改保持协调。 |
| [92](D:/NewsAgent/backend/service.py:92) | ** 展开 MemoryStore.profile() 的字典，再添加 missing_fields；字段内容由 Markdown 核心记忆读出。 |
| [93](D:/NewsAgent/backend/service.py:93) | 补充 memory_enabled 开关，前端据此判断是否要求填写资料。 |

调用去向与入口：

| 调用/入口 | 作用 |
| --- | --- |
| [GET /api/profile](D:/NewsAgent/backend/app.py:157) | HTTP 接口调用服务查询资料。 |
| [MemoryStore.profile](D:/NewsAgent/agent/memory.py:254) | 直接读取核心记录正文。 |
| [MemoryStore.missing_profile_fields](D:/NewsAgent/agent/memory.py:262) | 生成缺失字段名称列表。 |

### 7. update_profile：修改身份和偏好

[打开原文件第 95 行](D:/NewsAgent/backend/service.py:95)

```python
    def update_profile(self, *, identity: str | None = None, preferences: str | None = None) -> dict:
        with self._lock:
            if self._active_id is not None:
                raise BusyError("后台任务正在执行，请完成后再修改资料。")
            self.memory.set_profile(identity=identity, preferences=preferences)
            return self.profile()
```

| 原行号 | 解释 |
| --- | --- |
| [95](D:/NewsAgent/backend/service.py:95) | 定义资料修改方法；identity/preferences 都可不传，None 表示这一项不修改。 |
| [96](D:/NewsAgent/backend/service.py:96) | 让检查活动任务和写资料处在同一个锁保护区间。 |
| [97](D:/NewsAgent/backend/service.py:97) | 只要 _active_id 有编号，就有生成或邮件任务占用执行位置。 |
| [98](D:/NewsAgent/backend/service.py:98) | 任务执行期间拒绝改资料，避免同一次生成前后读到不同的用户信息。 |
| [99](D:/NewsAgent/backend/service.py:99) | 调用 MemoryStore.set_profile()，验证输入、写核心 Markdown 记录并更新记忆索引。 |
| [100](D:/NewsAgent/backend/service.py:100) | 再调用 self.profile() 返回最新资料；它也会拿锁，所以使用可重入 RLock。 |

调用去向与入口：

| 调用/入口 | 作用 |
| --- | --- |
| [PUT /api/profile](D:/NewsAgent/backend/app.py:161) | 校验请求后调用服务；exclude_none=True 不覆盖未提供字段。 |
| [MemoryStore.set_profile](D:/NewsAgent/agent/memory.py:267) | 先校验所有字段，再写固定记录。 |
| [MemoryStore.write_record](D:/NewsAgent/agent/memory.py:403) | 写 Markdown 并重建索引。 |
| [MemoryStore._write_document](D:/NewsAgent/agent/memory.py:283) | 临时文件、fsync、os.replace 实现单文件原子替换。 |

### 8. require_profile：检查资料完整性

[打开原文件第 102 行](D:/NewsAgent/backend/service.py:102)

```python
    def require_profile(self) -> None:
        if self.memory.enabled and self.memory.missing_profile_fields():
            raise ProfileRequired("请先填写身份和新闻偏好。")
```

| 原行号 | 解释 |
| --- | --- |
| [102](D:/NewsAgent/backend/service.py:102) | 定义资料完整性检查方法。 |
| [103](D:/NewsAgent/backend/service.py:103) | 记忆启用且缺失字段列表非空时，条件为 True；关闭记忆时不要求完整资料。 |
| [104](D:/NewsAgent/backend/service.py:104) | 抛出专门的资料缺失异常，正常接口会转换为 HTTP 409。 |

调用去向与入口：

| 调用/入口 | 作用 |
| --- | --- |
| [MemoryStore.missing_profile_fields](D:/NewsAgent/agent/memory.py:262) | 关闭记忆时返回空列表。 |
| [ProfileRequired 异常处理器](D:/NewsAgent/backend/app.py:136) | 异常映射为 HTTP 409。 |

### 9. update_subscription：保存订阅并更新定时器

[打开原文件第 106 行](D:/NewsAgent/backend/service.py:106)

```python
    def update_subscription(self, values: dict, apply_schedule: Callable) -> dict:
        with self._lock:
            if values["enabled"]:
                self.require_profile()
            old = self.store.subscription()
            saved = self.store.set_subscription(**values)
            try:
                apply_schedule(saved)
            except Exception:
                self.store.set_subscription(**{key: old[key] for key in ("enabled", "time", "timezone")})
                apply_schedule(old)
                raise
            return saved
```

| 原行号 | 解释 |
| --- | --- |
| [106](D:/NewsAgent/backend/service.py:106) | 定义订阅更新方法。values 是设置字典，apply_schedule 是把设置应用到定时器的函数。 |
| [107](D:/NewsAgent/backend/service.py:107) | 获取控制锁，串行完成订阅更新和调度规则更新。 |
| [108](D:/NewsAgent/backend/service.py:108) | 只有准备开启订阅时检查资料；暂停订阅不要求资料完整。 |
| [109](D:/NewsAgent/backend/service.py:109) | 普通方法调用：立即运行 require_profile()，成功则返回此处继续，失败则抛异常。 |
| [110](D:/NewsAgent/backend/service.py:110) | 先读出旧设置，供失败回滚使用。 |
| [111](D:/NewsAgent/backend/service.py:111) | **values 将 enabled/time/timezone 字典展开为命名参数，写数据库并取得保存后的设置。 |
| [112](D:/NewsAgent/backend/service.py:112) | 开始尝试更新内存中的定时规则。 |
| [113](D:/NewsAgent/backend/service.py:113) | 调用外部传进来的函数。app.py 传入的是 scheduler.apply，所以执行会进入 scheduling.py。 |
| [114](D:/NewsAgent/backend/service.py:114) | 定时规则更新失败时尝试恢复。 |
| [115](D:/NewsAgent/backend/service.py:115) | 字典推导式仅取旧 enabled/time/timezone；不把 updated_at 等额外字段传入 set_subscription()。随后 ** 展开并写回。 |
| [116](D:/NewsAgent/backend/service.py:116) | 把旧设置再次应用到定时器；若恢复本身失败，该异常也会向外传播。 |
| [117](D:/NewsAgent/backend/service.py:117) | 恢复顺利时，继续抛出最初的调度更新异常。这里并非跨 SQLite 与定时器的单一数据库事务。 |
| [118](D:/NewsAgent/backend/service.py:118) | 两边更新完成后返回已保存设置。 |

调用去向与入口：

| 调用/入口 | 作用 |
| --- | --- |
| [PUT /api/subscription](D:/NewsAgent/backend/app.py:169) | 传入 body 字典和 scheduler.apply 函数。 |
| [StateStore.subscription](D:/NewsAgent/backend/storage.py:130) | 读取旧订阅。 |
| [StateStore.set_subscription](D:/NewsAgent/backend/storage.py:136) | 保存 enabled/time/timezone。 |
| [DailyScheduler.apply](D:/NewsAgent/backend/scheduling.py:29) | 启用时替换 Cron job，暂停时删除 job。 |

### 10. status：查询是否忙碌

[打开原文件第 120 行](D:/NewsAgent/backend/service.py:120)

```python
    def status(self) -> dict:
        with self._lock:
            active = self.store.get_run(self._active_id) if self._active_id is not None else None
            return {"busy": self._active_id is not None, "active_run": active}
```

| 原行号 | 解释 |
| --- | --- |
| [120](D:/NewsAgent/backend/service.py:120) | 定义后台状态查询方法。 |
| [121](D:/NewsAgent/backend/service.py:121) | 读取活动编号和对应记录时取得控制锁。 |
| [122](D:/NewsAgent/backend/service.py:122) | 有活动编号时从数据库查询该任务；否则 active=None。条件表达式只执行选中的分支。 |
| [123](D:/NewsAgent/backend/service.py:123) | 返回 busy 布尔值及活动任务记录。生成已成功但邮件仍发送时，busy 仍可能是 True。 |

调用去向与入口：

| 调用/入口 | 作用 |
| --- | --- |
| [GET /api/status](D:/NewsAgent/backend/app.py:179) | 接口返回 busy 和 active_run。 |
| [StateStore.get_run](D:/NewsAgent/backend/storage.py:192) | 查询任务，JSON result 转回字典。 |
| [useDashboard](D:/NewsAgent/frontend/src/useDashboard.ts:6) | 前端定期查询并更新 React 状态。 |

### 11. generate：接受并提交生成任务

[打开原文件第 125 行](D:/NewsAgent/backend/service.py:125)

```python
    def generate(self, *, source: str = "manual", prompt: str | None = None, title: str | None = None,
                 day: str | None = None) -> dict:
        with self._lock:
            if not self._started or self._closing:
                raise RuntimeError("backend is stopping or not started")
            if source == "scheduled" and self.store.has_scheduled_run(day):
                return {"status": "already_attempted", "scheduled_date": day}
            if self._active_id is not None:
                raise BusyError("已有生成或邮件任务正在执行，请等待完成。")
            self.require_profile()
            run = self.store.create_run(source, day=day)
            self._active_id = run["id"]
            try:
                self._executor.submit(self._generate, run["id"], prompt, title)
            except Exception as exc:
                self._active_id = None
                self.store.finish_run(run["id"], ok=False, brief_id=None,
                                      error=self.clean_error(str(exc)), result=None)
                raise
            return run
```

| 原行号 | 解释 |
| --- | --- |
| [125](D:/NewsAgent/backend/service.py:125) | 定义任务提交入口；source 默认 manual，prompt/title 可省略，* 要求这些参数按名称传入。 |
| [126](D:/NewsAgent/backend/service.py:126) | day 用来标识定时任务所属日期，手动生成通常不用；返回的是任务字典。 |
| [127](D:/NewsAgent/backend/service.py:127) | 用锁保护完整的检查、创建任务和预留执行位置过程。 |
| [128](D:/NewsAgent/backend/service.py:128) | 服务未启动，或者正在关闭，都不能提交工作。 |
| [129](D:/NewsAgent/backend/service.py:129) | 这类内部生命周期错误抛 RuntimeError。 |
| [130](D:/NewsAgent/backend/service.py:130) | 仅定时任务检查当天是否已经尝试；失败和中断也占用当天尝试机会，skipped 不占用。 |
| [131](D:/NewsAgent/backend/service.py:131) | 已有当天尝试则返回 already_attempted，不创建新任务。这个返回主要供定时器内部使用。 |
| [132](D:/NewsAgent/backend/service.py:132) | 检查是否已经有生成/邮件任务占用执行位置。 |
| [133](D:/NewsAgent/backend/service.py:133) | 忙碌时拒绝新请求；当前服务不会把多个业务请求排成等待队列。 |
| [134](D:/NewsAgent/backend/service.py:134) | 执行资料完整性检查。 |
| [135](D:/NewsAgent/backend/service.py:135) | 调用 StateStore.create_run()：生成 UUID、插入 queued 记录，并读回任务字典。 |
| [136](D:/NewsAgent/backend/service.py:136) | 立即预留执行位置，避免工作线程尚未开始时另一请求误认为空闲。 |
| [137](D:/NewsAgent/backend/service.py:137) | 捕获“提交给线程池”阶段的错误。 |
| [138](D:/NewsAgent/backend/service.py:138) | 传函数 self._generate 及其三个参数给 submit()；由工作线程稍后执行。此处不等待生成完成，返回的 Future 没有保存在服务中。 |
| [139](D:/NewsAgent/backend/service.py:139) | 这个 except 处理提交失败，工作线程内发生的生成异常由 _generate() 自己处理。 |
| [140](D:/NewsAgent/backend/service.py:140) | 提交失败就归还执行位置。 |
| [141](D:/NewsAgent/backend/service.py:141) | 把已创建的任务标记失败，且没有关联简报编号。 |
| [142](D:/NewsAgent/backend/service.py:142) | 继续提供经过处理的错误和空结果；与上一行是同一个 finish_run 调用。 |
| [143](D:/NewsAgent/backend/service.py:143) | 抛回接口层，让提交失败得到错误响应。 |
| [144](D:/NewsAgent/backend/service.py:144) | 返回创建时的 queued 任务快照；工作线程可能已将数据库状态改成 running，但不会自动改写这个旧字典。 |

调用去向与入口：

| 调用/入口 | 作用 |
| --- | --- |
| [POST /api/generate](D:/NewsAgent/backend/app.py:175) | 接口只传 prompt/title；默认手动来源。 |
| [StateStore.has_scheduled_run](D:/NewsAgent/backend/storage.py:142) | 查询当天是否有非 skipped 定时记录。 |
| [StateStore.create_run](D:/NewsAgent/backend/storage.py:147) | 生成 UUID，插入 queued，读回字典。 |
| [BackendService._generate](D:/NewsAgent/backend/service.py:146) | 由工作线程执行；不会直接返回正文给提交接口。 |
| [BusyError 异常处理器](D:/NewsAgent/backend/app.py:132) | 忙碌映射为 HTTP 409。 |

### 12. _generate：工作线程实际生成、归档、记录结果

[打开原文件第 146 行](D:/NewsAgent/backend/service.py:146)

```python
    def _generate(self, run_id: str, prompt: str | None, title: str | None) -> None:
        try:
            run = self.store.get_run(run_id)
            self.store.start_run(run_id)
            def progress(message):
                self.store.progress(run_id, self.clean_error(str(message))[:1000])
            with self.runtime_factory(progress) as runtime:
                generated = runtime.generate_brief(prompt, title=title)
            result = asdict(generated.result)
            # Successful body lives in BriefStore; retain failed-save text for recovery without new API calls.
            text = result.pop("text")
            result["error"] = self.clean_error(result["error"])
            result["save_error"] = self.clean_error(generated.save_error)
            if generated.result.ok and generated.save_error:
                result["unsaved_content"] = text
            error = self.clean_error(generated.save_error or generated.result.error or
                                     ("任务未完成：" + generated.result.stop_reason if not generated.ok else ""))
            email_pending = generated.ok and run["source"] == "scheduled" and self.settings.email_enabled
            self.store.finish_run(run_id, ok=generated.ok,
                                  brief_id=generated.brief["id"] if generated.brief else None,
                                  error=error, result=result, email_pending=email_pending)
            if email_pending:
                self._send_email(run_id)
        except Exception as exc:
            error = self.clean_error(f"{type(exc).__name__}: {exc}")
            logger.error("brief run %s failed: %s", run_id, error)
            self.store.finish_run(run_id, ok=False, brief_id=None, error=error, result=None)
        finally:
            with self._lock:
                self._active_id = None
```

| 原行号 | 解释 |
| --- | --- |
| [146](D:/NewsAgent/backend/service.py:146) | 定义工作线程实际执行的函数；它的返回值不作为 HTTP 响应，结果通过数据库供前端查询。 |
| [147](D:/NewsAgent/backend/service.py:147) | 包住生成流程，记录常规异常并最终归还执行位置。 |
| [148](D:/NewsAgent/backend/service.py:148) | 读数据库任务，取得来源 manual/scheduled 等信息。 |
| [149](D:/NewsAgent/backend/service.py:149) | 更新生成状态为 running，同时记录开始时间。 |
| [150](D:/NewsAgent/backend/service.py:150) | 定义内部回调函数，闭包能记住本次 run_id 和外层 self；定义函数时不会立即运行它。 |
| [151](D:/NewsAgent/backend/service.py:151) | 回调真正被调用时，转为字符串、替换已知密钥、限长 1000 字符，再写入这条任务的最新 progress。 |
| [152](D:/NewsAgent/backend/service.py:152) | 调用工厂创建 Runtime，把 progress 函数作为回调传入。with 调用 Runtime.__enter__()，退出时调用 __exit__()。 |
| [153](D:/NewsAgent/backend/service.py:153) | 在同一个工作线程中同步调用 Runtime.generate_brief()，直到 Agent 运行并归档结束，返回 BriefResult。 |
| [154](D:/NewsAgent/backend/service.py:154) | 把 generated.result（LoopResult dataclass）转换成字典 result；保留轮数、工具次数、停止原因等字段。 |
| [155](D:/NewsAgent/backend/service.py:155) | 原注释说明：成功正文放在简报文件；保存失败时才在任务详情保留完整正文供恢复。 |
| [156](D:/NewsAgent/backend/service.py:156) | pop("text") 同时取出正文并从 result 删除 text 键；不会删除生成对象或归档文件里的正文。 |
| [157](D:/NewsAgent/backend/service.py:157) | 处理 LoopResult 的错误文字。 |
| [158](D:/NewsAgent/backend/service.py:158) | 把归档错误作为 save_error 加入任务结果字典。 |
| [159](D:/NewsAgent/backend/service.py:159) | 判断 Agent 已正常结束，但归档出现错误；generated.result.ok 与 generated.ok 的含义不同。 |
| [160](D:/NewsAgent/backend/service.py:160) | 这时把已生成但未保存的正文留在 unsaved_content；失败的未完成草稿不会通过这条分支保留。 |
| [161](D:/NewsAgent/backend/service.py:161) | or 按顺序选择第一个非空值：优先保存错误，其次 Agent 错误。选中后不继续计算后面的分支。 |
| [162](D:/NewsAgent/backend/service.py:162) | 没有上述错误但总体失败时，用 stop_reason 构造失败说明；总体成功时为空字符串。最后用 clean_error 处理。 |
| [163](D:/NewsAgent/backend/service.py:163) | 只有总体成功、来源 scheduled、邮件开关启用，才置 email_pending=True。配置是否完整会在邮件发送器内检查。 |
| [164](D:/NewsAgent/backend/service.py:164) | 把生成状态保存为 succeeded/failed；generated.ok 要求 Agent 成功且实际归档成功。 |
| [165](D:/NewsAgent/backend/service.py:165) | 有简报元信息时写入其编号，没有则写 None。 |
| [166](D:/NewsAgent/backend/service.py:166) | 同时保存错误、统计结果，并按 email_pending 设置邮件为 pending 或 not_requested。 |
| [167](D:/NewsAgent/backend/service.py:167) | 检查是否要自动发邮件。 |
| [168](D:/NewsAgent/backend/service.py:168) | 普通同步调用 _send_email()，仍在当前工作线程执行；不是再创建一个生成任务。 |
| [169](D:/NewsAgent/backend/service.py:169) | 捕获生成流程中抛出的常规异常。 |
| [170](D:/NewsAgent/backend/service.py:170) | type(exc).__name__ 取得异常类型名称，f-string 拼接类型和消息，再处理已知密钥。 |
| [171](D:/NewsAgent/backend/service.py:171) | 使用日志记录器写出任务编号与错误；占位符 %s 由 logging 处理。 |
| [172](D:/NewsAgent/backend/service.py:172) | 尝试把生成任务标记失败；若数据库本身不可写，这次记录也可能失败，finally 仍会运行。 |
| [173](D:/NewsAgent/backend/service.py:173) | 无论正常结束、提前失败还是捕获后再次抛错，都执行这个清理块。 |
| [174](D:/NewsAgent/backend/service.py:174) | 工作线程再次取得控制锁。 |
| [175](D:/NewsAgent/backend/service.py:175) | 清空活动编号，服务重新可以接受下一次生成或邮件请求。 |

调用去向与入口：

| 调用/入口 | 作用 |
| --- | --- |
| [StateStore.start_run](D:/NewsAgent/backend/storage.py:155) | 状态置 running，写开始时间。 |
| [StateStore.progress](D:/NewsAgent/backend/storage.py:159) | 更新最新事件，只在 running 状态下更新。 |
| [Runtime.create](D:/NewsAgent/agent/runtime.py:72) | 创建模型客户端和本次 Runtime。 |
| [Runtime.__enter__](D:/NewsAgent/agent/runtime.py:260) | 返回 Runtime 本身。 |
| [Runtime.generate_brief](D:/NewsAgent/agent/runtime.py:186) | 生成成功后调用 BriefStore.save。 |
| [Runtime.submit](D:/NewsAgent/agent/runtime.py:152) | 准备消息、恢复预算、记忆回调，进入 run_loop。 |
| [run_loop](D:/NewsAgent/agent/loop.py:113) | 多轮调用模型，选择和执行工具，触发进度回调。 |
| [BriefStore.save](D:/NewsAgent/agent/briefs.py:64) | 校验、生成元信息、发布完整 Markdown 文件。 |
| [BriefResult.ok](D:/NewsAgent/agent/runtime.py:34) | 要求 LoopResult 成功、brief 存在、save_error 为空。 |
| [Runtime.__exit__](D:/NewsAgent/agent/runtime.py:263) | 离开 with 时调用 close。 |
| [StateStore.finish_run](D:/NewsAgent/backend/storage.py:163) | 保存生成结果和邮件 pending 状态。 |

### 13. send_email：接受独立邮件任务

[打开原文件第 177 行](D:/NewsAgent/backend/service.py:177)

```python
    def send_email(self, run_id: str) -> dict:
        """Explicit send/retry uses the archived brief and never invokes the model."""
        with self._lock:
            if not self._started or self._closing:
                raise RuntimeError("backend is stopping or not started")
            if self._active_id is not None:
                raise BusyError("已有生成或邮件任务正在执行，请等待完成。")
            run = self.store.get_run(run_id)
            if run["status"] != "succeeded" or not run["brief_id"]:
                raise ValueError("这条运行记录没有成功保存的简报，无法发送邮件。")
            if run["email_status"] == "sent":
                raise ValueError("该简报已发送成功，无需重复发送。")
            self.email_sender.require_ready()
            self.briefs.get(run["brief_id"])
            run = self.store.queue_email(run_id)
            self._active_id = run_id
            try:
                self._executor.submit(self._send_email_task, run_id)
            except Exception as exc:
                self._active_id = None
                self.store.finish_email(run_id, "failed", self.clean_error(str(exc)))
                raise
            return run
```

| 原行号 | 解释 |
| --- | --- |
| [177](D:/NewsAgent/backend/service.py:177) | 定义手动发送/重发已有简报的提交入口；返回的是任务状态字典。 |
| [178](D:/NewsAgent/backend/service.py:178) | 方法说明：读取归档正文发送邮件，不调用模型重新生成。 |
| [179](D:/NewsAgent/backend/service.py:179) | 用同一把锁保护邮件提交和执行位置预留。 |
| [180](D:/NewsAgent/backend/service.py:180) | 检查服务已启动且未关闭。 |
| [181](D:/NewsAgent/backend/service.py:181) | 生命周期不允许提交时抛异常。 |
| [182](D:/NewsAgent/backend/service.py:182) | 检查生成或其他邮件是否占用执行位置。 |
| [183](D:/NewsAgent/backend/service.py:183) | 后台忙碌则拒绝邮件请求。 |
| [184](D:/NewsAgent/backend/service.py:184) | 从数据库读取要发送的原任务。 |
| [185](D:/NewsAgent/backend/service.py:185) | 要求生成已成功且存在 brief_id。 |
| [186](D:/NewsAgent/backend/service.py:186) | 没有成功归档的正文就不能发送。 |
| [187](D:/NewsAgent/backend/service.py:187) | 当前记录已经确认 SMTP 接收成功时，不允许重复发送。 |
| [188](D:/NewsAgent/backend/service.py:188) | 抛 ValueError，接口层转换为 HTTP 422。 |
| [189](D:/NewsAgent/backend/service.py:189) | 调用 EmailSender.require_ready() 检查邮件开关、账号、授权码等必要配置。 |
| [190](D:/NewsAgent/backend/service.py:190) | 先实际读取归档，确认文件存在且可以解析；本次结果不保存，工作线程会再次读取。 |
| [191](D:/NewsAgent/backend/service.py:191) | 把原任务的邮件状态改为 pending，并清空上次邮件错误；生成状态保持 succeeded。 |
| [192](D:/NewsAgent/backend/service.py:192) | 预留同一个后台执行位置。 |
| [193](D:/NewsAgent/backend/service.py:193) | 开始提交邮件工作。 |
| [194](D:/NewsAgent/backend/service.py:194) | 把 _send_email_task 函数和 run_id 交给同一个线程池，不等待 SMTP 完成。 |
| [195](D:/NewsAgent/backend/service.py:195) | 提交失败时执行清理。 |
| [196](D:/NewsAgent/backend/service.py:196) | 归还执行位置。 |
| [197](D:/NewsAgent/backend/service.py:197) | 记录邮件失败，保留简报生成成功状态。 |
| [198](D:/NewsAgent/backend/service.py:198) | 继续向接口层抛出提交异常。 |
| [199](D:/NewsAgent/backend/service.py:199) | 返回 pending 邮件状态的任务快照。 |

调用去向与入口：

| 调用/入口 | 作用 |
| --- | --- |
| [POST /api/runs/{run_id}/send-email](D:/NewsAgent/backend/app.py:196) | 手动发送/重试接口。 |
| [EmailSender.require_ready](D:/NewsAgent/backend/email_delivery.py:39) | 检查开关与必要配置。 |
| [BriefStore.get](D:/NewsAgent/agent/briefs.py:161) | 读取正文与元信息。 |
| [StateStore.queue_email](D:/NewsAgent/backend/storage.py:171) | 仅更新原任务的邮件字段。 |
| [BackendService._send_email_task](D:/NewsAgent/backend/service.py:201) | 邮件工作线程包装函数。 |

### 14. _send_email_task：独立邮件任务的清理包装

[打开原文件第 201 行](D:/NewsAgent/backend/service.py:201)

```python
    def _send_email_task(self, run_id: str) -> None:
        try:
            self._send_email(run_id)
        finally:
            with self._lock:
                self._active_id = None
```

| 原行号 | 解释 |
| --- | --- |
| [201](D:/NewsAgent/backend/service.py:201) | 定义独立邮件任务的工作线程包装函数。 |
| [202](D:/NewsAgent/backend/service.py:202) | 开始执行实际邮件工作。 |
| [203](D:/NewsAgent/backend/service.py:203) | 普通调用 _send_email()；这也是定时生成完成后使用的发送函数。 |
| [204](D:/NewsAgent/backend/service.py:204) | 无论发送结果如何，都执行执行位置清理。 |
| [205](D:/NewsAgent/backend/service.py:205) | 获取控制锁。 |
| [206](D:/NewsAgent/backend/service.py:206) | 归还活动任务位置；自动邮件的这一清理由 _generate() 的 finally 完成。 |

调用去向与入口：

| 调用/入口 | 作用 |
| --- | --- |
| [BackendService._send_email](D:/NewsAgent/backend/service.py:208) | 共用实际发送实现，结束时包装函数归还执行位置。 |

### 15. _send_email：实际读取归档、发送并记录邮件结果

[打开原文件第 208 行](D:/NewsAgent/backend/service.py:208)

```python
    def _send_email(self, run_id: str) -> None:
        # Notification errors must not overwrite the successful generation record.
        result = None
        submitting = False
        try:
            run = self.store.get_run(run_id)
            message_id = run["email_message_id"] or self.email_sender.message_id(run_id)
            self.store.start_email(run_id, message_id, self.email_sender.recipient)
            brief = self.briefs.get(run["brief_id"])
            submitting = True
            result = self.email_sender.send(brief, message_id)
            self.store.finish_email(run_id, result.status, self.clean_error(result.error))
        except Exception as exc:
            error = self.clean_error(f"{type(exc).__name__}: {exc}")
            logger.error("email for run %s failed: %s", run_id, error)
            status = result.status if result is not None else ("unknown" if submitting else "failed")
            try:
                self.store.finish_email(run_id, status, error)
            except Exception as storage_exc:
                logger.error("email status could not be saved: %s", self.clean_error(str(storage_exc)))
```

| 原行号 | 解释 |
| --- | --- |
| [208](D:/NewsAgent/backend/service.py:208) | 定义真正读取归档、调用发送器并记录邮件结果的方法。 |
| [209](D:/NewsAgent/backend/service.py:209) | 原注释说明：邮件错误不能覆盖已经成功的生成记录。 |
| [210](D:/NewsAgent/backend/service.py:210) | 初始还没有发送结果，None 用来和已经返回 DeliveryResult 的情况区分。 |
| [211](D:/NewsAgent/backend/service.py:211) | 初始尚未调用发送器；这个标志用于意外抛错时保守判断结果。 |
| [212](D:/NewsAgent/backend/service.py:212) | 包住准备、发送和状态记录。 |
| [213](D:/NewsAgent/backend/service.py:213) | 读取原任务及它的 brief_id、旧 message_id。 |
| [214](D:/NewsAgent/backend/service.py:214) | 有旧 Message-ID 就沿用，没有就通过 EmailSender.message_id() 创建；同一标识不保证邮箱严格去重。 |
| [215](D:/NewsAgent/backend/service.py:215) | 设置邮件为 sending，保存 Message-ID/收件人，并增加 email_attempts。recipient 是属性，访问时执行它的 getter。 |
| [216](D:/NewsAgent/backend/service.py:216) | 调用 BriefStore.get() 读取原来保存的 Markdown 正文和元信息。 |
| [217](D:/NewsAgent/backend/service.py:217) | 准备进入 email_sender.send()，先标记已进入发送阶段；不表示 SMTP 已接收。 |
| [218](D:/NewsAgent/backend/service.py:218) | 调用邮件发送器，它在内部连接 SMTP、登录和提交邮件，返回 DeliveryResult。 |
| [219](D:/NewsAgent/backend/service.py:219) | 保存发送器返回的 sent/failed/unknown 及错误；clean_error 处理错误文字。 |
| [220](D:/NewsAgent/backend/service.py:220) | 处理本方法中意外抛出的异常，例如读取文件、状态写入或发送器未正常返回。 |
| [221](D:/NewsAgent/backend/service.py:221) | 形成可保存的异常说明。 |
| [222](D:/NewsAgent/backend/service.py:222) | 记录邮件异常日志；不会调用 finish_run() 覆盖生成结果。 |
| [223](D:/NewsAgent/backend/service.py:223) | 如果已经拿到结果，就优先沿用实际结果；否则在发送阶段后为 unknown，发送阶段前为 failed。 |
| [224](D:/NewsAgent/backend/service.py:224) | 再尝试把邮件异常保存进数据库。 |
| [225](D:/NewsAgent/backend/service.py:225) | 仅更新邮件状态和邮件错误。 |
| [226](D:/NewsAgent/backend/service.py:226) | 若保存异常本身又失败，则捕获这个第二层异常。 |
| [227](D:/NewsAgent/backend/service.py:227) | 记录无法持久化邮件状态的错误；方法不再继续向外抛该异常。数据库不可用时，页面可能仍看到旧状态。 |

调用去向与入口：

| 调用/入口 | 作用 |
| --- | --- |
| [EmailSender.message_id](D:/NewsAgent/backend/email_delivery.py:46) | 新任务生成 RFC Message-ID；重试沿用已有值。 |
| [EmailSender.recipient 属性](D:/NewsAgent/backend/email_delivery.py:25) | EMAIL_TO 没有配置时使用发件邮箱。 |
| [StateStore.start_email](D:/NewsAgent/backend/storage.py:176) | sending、编号、收件人、email_attempts+1。 |
| [BriefStore.get](D:/NewsAgent/agent/briefs.py:161) | 读取原归档正文。 |
| [EmailSender.send](D:/NewsAgent/backend/email_delivery.py:69) | TLS SMTP 连接、登录、发送、处理网络/认证错误。 |
| [EmailSender.message](D:/NewsAgent/backend/email_delivery.py:50) | 生成主题、正文、Message-ID 和 Markdown 附件。 |
| [StateStore.finish_email](D:/NewsAgent/backend/storage.py:181) | 只更新邮件状态和错误。 |

### 16. run_scheduled：定时触发入口

[打开原文件第 229 行](D:/NewsAgent/backend/service.py:229)

```python
    def run_scheduled(self, *, now: datetime | None = None) -> dict:
        # Lock subscription check and reservation together, so disable cannot race a new run.
        with self._lock:
            now = (now or datetime.now(SHANGHAI)).astimezone(SHANGHAI)
            day = now.strftime("%Y-%m-%d")
            subscription = self.store.subscription()
            if not subscription["enabled"]:
                return {"status": "disabled"}
            hour, minute = map(int, subscription["time"].split(":"))
            due = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if not timedelta(0) <= now - due <= CATCH_UP_WINDOW:
                return {"status": "outside_window"}
            try:
                return self.generate(source="scheduled", day=day)
            except (BusyError, ProfileRequired) as exc:
                return self.store.create_run("scheduled", day=day, status="skipped", error=str(exc))
```

| 原行号 | 解释 |
| --- | --- |
| [229](D:/NewsAgent/backend/service.py:229) | 定义定时触发入口；可按名称传入 now，方便离线测试指定时间。 |
| [230](D:/NewsAgent/backend/service.py:230) | 原注释说明：检查订阅与预留任务共用锁，防止暂停订阅和触发生成互相穿插。 |
| [231](D:/NewsAgent/backend/service.py:231) | 取得可重入控制锁。 |
| [232](D:/NewsAgent/backend/service.py:232) | 没有传 now 就取当前北京时间，再转换到项目统一时区；测试适合传带时区的 datetime。 |
| [233](D:/NewsAgent/backend/service.py:233) | 格式化为 YYYY-MM-DD，用于当天定时任务的去重键。 |
| [234](D:/NewsAgent/backend/service.py:234) | 读取持久化订阅设置。 |
| [235](D:/NewsAgent/backend/service.py:235) | 检查订阅开关。 |
| [236](D:/NewsAgent/backend/service.py:236) | 关闭订阅时直接返回 disabled，不创建生成任务记录。 |
| [237](D:/NewsAgent/backend/service.py:237) | 例如 "08:30" 先 split 为 ["08", "30"]，再用 map(int, ...) 转成整数，拆给 hour/minute。 |
| [238](D:/NewsAgent/backend/service.py:238) | 保留 now 的年月日和时区，把时分设置为订阅时间、秒和微秒归零，得到今天的 due。 |
| [239](D:/NewsAgent/backend/service.py:239) | 链式比较检查 now-due 在 0 到 1 小时之间；早于时间或迟到超过一小时都不符合。边界一小时也允许。 |
| [240](D:/NewsAgent/backend/service.py:240) | 不在窗口则返回 outside_window，不创建新生成任务。 |
| [241](D:/NewsAgent/backend/service.py:241) | 尝试提交定时生成。 |
| [242](D:/NewsAgent/backend/service.py:242) | 普通调用本类 generate()，传 scheduled 来源与当天日期。generate() 内部才将实际生成交给工作线程。 |
| [243](D:/NewsAgent/backend/service.py:243) | 只捕获忙碌和资料缺失两类异常，不把所有内部错误都当作 skipped。 |
| [244](D:/NewsAgent/backend/service.py:244) | 记录一条 source=scheduled、status=skipped 的任务，保存跳过原因，不提交工作线程。 |

调用去向与入口：

| 调用/入口 | 作用 |
| --- | --- |
| [DailyScheduler.apply 注册入口](D:/NewsAgent/backend/scheduling.py:35) | APScheduler 到点调用 run_scheduled。 |
| [StateStore.subscription](D:/NewsAgent/backend/storage.py:130) | 重新读取最新订阅，避免暂停后继续提交。 |
| [BackendService.generate](D:/NewsAgent/backend/service.py:125) | 把定时来源与当天日期交给公共生成入口。 |
| [StateStore.create_run](D:/NewsAgent/backend/storage.py:147) | 忙碌或资料缺失时只写 skipped 记录。 |

### 17. catch_up：启动时检查当天补跑

[打开原文件第 246 行](D:/NewsAgent/backend/service.py:246)

```python
    def catch_up(self, *, now: datetime | None = None) -> dict | None:
        with self._lock:
            subscription = self.store.subscription()
            if not subscription["enabled"]:
                return None
            now = (now or datetime.now(SHANGHAI)).astimezone(SHANGHAI)
            hour, minute = map(int, subscription["time"].split(":"))
            due = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if timedelta(0) <= now - due <= CATCH_UP_WINDOW:
                return self.run_scheduled(now=now)
            return None
```

| 原行号 | 解释 |
| --- | --- |
| [246](D:/NewsAgent/backend/service.py:246) | 定义后端启动时的补跑检查；返回任务字典或 None。 |
| [247](D:/NewsAgent/backend/service.py:247) | 获取控制锁；后续嵌套 run_scheduled()、generate() 仍由同一线程取得同一把 RLock。 |
| [248](D:/NewsAgent/backend/service.py:248) | 读取当前订阅。 |
| [249](D:/NewsAgent/backend/service.py:249) | 订阅关闭则无需检查补跑。 |
| [250](D:/NewsAgent/backend/service.py:250) | 返回 None 表示没有提交补跑任务。 |
| [251](D:/NewsAgent/backend/service.py:251) | 取或转换当前北京时间。 |
| [252](D:/NewsAgent/backend/service.py:252) | 将订阅时分转成整数。 |
| [253](D:/NewsAgent/backend/service.py:253) | 计算今天的预定执行时间。 |
| [254](D:/NewsAgent/backend/service.py:254) | 只有已经到点且最多迟到一小时，才有资格补跑。 |
| [255](D:/NewsAgent/backend/service.py:255) | 调用 run_scheduled()，再次检查开关、窗口、当天去重和是否忙碌，返回它的处理结果。 |
| [256](D:/NewsAgent/backend/service.py:256) | 还没到点或已超过窗口，返回 None；没有补跑之前几天的逻辑。 |

调用去向与入口：

| 调用/入口 | 作用 |
| --- | --- |
| [DailyScheduler.start](D:/NewsAgent/backend/scheduling.py:19) | 恢复订阅规则之后调用 catch_up。 |
| [BackendService.run_scheduled](D:/NewsAgent/backend/service.py:229) | 复用时间检查、去重和任务提交逻辑。 |

## 按真实执行顺序追一次手动生成

### A. 提交请求这一侧

```text
App.tsx 点击生成按钮
  → api.ts 的 api.generate() 发 POST /api/generate
  → app.py 的 generate(body)
  → service.generate(prompt=..., title=...)
  → require_profile()
  → StateStore.create_run() 保存 queued
  → _active_id = run["id"] 预留位置
  → executor.submit(_generate, run_id, prompt, title)
  → service.generate() 返回任务字典
  → FastAPI 按 RunResponse 输出 HTTP 202
```

HTTP 202 只说明任务已接受。返回体是创建时的记录快照；查询接口拿到的数据库状态可能已经更新。

### B. 工作线程这一侧

```text
线程池调用 _generate(run_id, prompt, title)
  → StateStore.start_run() 设置 running
  → 定义 progress 回调
  → runtime_factory(progress)
     → Runtime.create(settings, on_event=progress)
     → build_client(settings) 创建对应模型客户端
     → Runtime.__init__() 组装工具、Hooks、记忆、ToolRecovery
  → Runtime.__enter__() 返回实例
  → runtime.generate_brief()
     → runtime.submit()
        → SessionStart / UserPromptSubmit hooks
        → 新的工具恢复预算与消息
        → run_loop(..., system=self.build_system, on_event=progress)
           → 调用 build_system() 注入固定身份/偏好和相关记忆
           → 模型生成下一步
           → 若有工具调用，execute_tool() 执行并回填结果
           → 若最终回复，Stop hook 决定是否结束
        → 符合条件时提取长期记忆
        → 返回 LoopResult
     → 正常结束后 BriefStore.save() 发布 Markdown
     → 返回 BriefResult
  → Runtime.__exit__() 关闭模型客户端等资源
  → 转换统计结果、保留必要错误信息
  → StateStore.finish_run() 保存成功/失败与 brief_id
  → 手动任务默认不自动发邮件
  → finally 清空 _active_id
```

[模型客户端选择](D:/NewsAgent/agent/llm.py:116) · [提示词与记忆组装](D:/NewsAgent/agent/runtime.py:112) · [Agent loop](D:/NewsAgent/agent/loop.py:113) · [工具执行](D:/NewsAgent/agent/loop.py:41) · [关闭 Runtime](D:/NewsAgent/agent/runtime.py:240)

### C. 进度回调如何“调用回来”

```python
def progress(message):
    self.store.progress(run_id, self.clean_error(str(message))[:1000])
```

这里的函数定义不会写数据库。它被保存在 Runtime.on_event 中，再传入 run_loop。循环执行：

```python
on_event(f"[model] round {turn}")
```

此时 on_event 指向的就是服务层 progress 函数，所以相当于：

```python
progress("[model] round 1")
```

函数通过闭包记住正确的 run_id，写完进度后返回 loop，loop 继续执行。这个回调没有独立线程，它运行在当前生成工作线程里。

前端从 [useDashboard](D:/NewsAgent/frontend/src/useDashboard.ts:6) 发起查询，经 [api.dashboard](D:/NewsAgent/frontend/src/api.ts:67) 读取 `/api/status`，到 [app.get_status](D:/NewsAgent/backend/app.py:179)，再到 [service.status](D:/NewsAgent/backend/service.py:120) 和 [StateStore.get_run](D:/NewsAgent/backend/storage.py:192)。响应返回浏览器后，React 更新界面。

## 保存资料与修改订阅的调用往返

### 保存身份和偏好

```text
PUT /api/profile
  → app.update_profile(body)
  → body.model_dump(exclude_none=True) 转成只含提供字段的字典
  → service.update_profile(**字典)
  → MemoryStore.set_profile()
     → 先验证所有待改字段
     → write_record() 写固定记忆文件
     → _write_document() 临时文件写完并替换
     → rebuild_index() 重建索引
  → service.profile() 重新读固定记忆
  → app 返回 JSON
```

没有传入的字段保留原值，None 不表示清空字段。当前 MemoryStore.set_profile() 在记忆关闭时会拒绝修改，所以关闭记忆并不意味着还能保存资料等待以后使用。

### 修改订阅

```text
PUT /api/subscription
  → app.update_subscription(body)
  → service.update_subscription(body.model_dump(), scheduler.apply)
     → StateStore.set_subscription() 写 SQLite
     → apply_schedule(saved) 实际调用 DailyScheduler.apply(saved)
        → 关闭：remove_job()
        → 开启：add_job(run_scheduled, CronTrigger(...))
     → 返回 saved
  → app 加上 scheduler.next_run_at()
  → 返回 JSON
```

`scheduler.apply` 不带括号时是传入一个绑定方法；服务层执行 `apply_schedule(saved)` 才真正进入这个方法。更新规则本身不会立即生成简报；到点后 APScheduler 才调用已注册的 `run_scheduled`。

## 定时生成与启动补跑

```text
后端启动
  → service.start()
  → scheduler.start()
     → scheduler.apply(数据库订阅)
     → scheduler.resume()
     → service.catch_up()
        → 若处在今天 due 后 0～1 小时：run_scheduled()

每天到点
  → APScheduler 调用 service.run_scheduled()
  → service.generate(source="scheduled", day="YYYY-MM-DD")
  → 同一个 _generate()
  → 成功归档且邮件开关开启：_send_email()
  → 最后清空 _active_id
```

同一天非 skipped 的定时记录会阻止再次定时提交，包含失败和中断记录。skipped 不算已尝试生成，后续启动时如果还在窗口中可以重新检查；没有定时器自动反复重试 skipped 的逻辑。手动生成不受当天定时去重限制，但仍要求后台空闲。

例如订阅 08:30：08:10 启动等到点；08:50 启动检查补跑；09:30 仍在一小时边界；09:31 启动不补跑。它只检查今天，不回补前几天。

## 邮件发送的两条入口

```text
定时生成成功
  → _generate() 先 finish_run(succeeded, email_pending=True)
  → _send_email()
  → _generate() 的 finally 释放执行位置

用户点击发送/重试邮件
  → POST /api/runs/{run_id}/send-email
  → service.send_email()
  → queue_email() 保留原生成结果
  → submit(_send_email_task, run_id)
  → _send_email_task() 调用 _send_email()
  → _send_email_task() 的 finally 释放执行位置
```

两条路径都读取已有 Markdown。EmailSender.send() 构造邮件、连接 SMTP_SSL、登录、调用 send_message()，最终返回 DeliveryResult；已知认证/拒收失败返回 failed，提交过程中断可能返回 unknown，正常被 SMTP 接收返回 sent。sent 不等于收件箱已收到或用户已读。

发送器内部也有 submitting 标记，能区分连接登录阶段与提交邮件阶段。服务层的 submitting 是在调用发送器前设置，用于处理发送器意外抛异常的保守判断，不能把两者当成完全相同的时点。

邮件的 sent/failed/unknown 与生成的 succeeded/failed 是独立字段。生成已 succeeded、邮件 sending 时，service.status() 仍返回 busy=True。相同 Message-ID 有助于标识重发，不能提供严格去重保证；结果 unknown 时应先检查邮箱。

## 锁为什么这样放

RLock 保护共享状态的短操作，例如“检查空闲 → 写 queued → 设置 _active_id → 提交工作”。_generate() 的模型调用与 SMTP 工作不一直持有控制锁，所以查询状态仍可以正常完成。

同一线程的嵌套调用包括 `update_profile() → profile()`、`catch_up() → run_scheduled() → generate()`。使用 RLock 允许这些嵌套获取。不同线程仍然互斥。

InstanceLock 是另一个层次：操作系统文件锁限制同工作区的后端实例，不等同于 RLock，也没有覆盖单独运行的 CLI。

close() 先设置 _closing，再退出锁等待 executor.shutdown(wait=True)。工作线程结束时可以取得锁清空 _active_id；如果关闭方一直握着锁等待，就可能互相等待。正常关闭等待当前工作；进程被强制终止时，下一次启动仅标记旧任务中断，不能继续原来内存中的 Agent loop。

## Python 写法对照

| 写法 | 这里的具体意思 |
| --- | --- |
| `self.x` | 当前服务实例的成员，不同方法访问同一份状态 |
| `x: str` / `-> dict` | 类型注解；运行时输入校验另外由接口模型等代码完成 |
| `str \| None` | 字符串或 None |
| 参数中的 `*` | 后面的参数必须按名称传入 |
| `**values` | 字典展开成关键字参数 |
| `{**old, "key": value}` | 合并字典并添加/覆盖键 |
| `a if condition else b` | 条件成立用 a，否则用 b，只计算选中的分支 |
| `a or b` | 取第一个真值，可能返回对象本身，不一定是 bool |
| `a and b` | 前项为假则短路，否则继续；本文件常用于条件判断 |
| `lambda x: expression` | 创建一个小函数，此时不立即执行 expression |
| `with lock:` | 进入时获取锁，离开时释放锁 |
| `try / except / finally` | 正常尝试、匹配异常处理、离开时清理 |
| 裸 `raise` | 重新抛出当前捕获的异常 |
| `return` | 返回调用方，并结束当前函数 |
| `pop("text")` | 取出对应值，同时移除字典中的这个键 |
| `[:1000]` | 字符串前 1000 个字符，不是 1000 字节 |
| `@property` 对应的 `.ok` / `.recipient` | 不写括号，但访问时会运行属性 getter |
| `now.replace(...)` | 返回新的日期时间对象，原 now 不被就地修改 |

## 调用位置速查

| 想追的行为 | 入口 | 实际下一步 |
| --- | --- | --- |
| 服务启动 | [lifespan](D:/NewsAgent/backend/app.py:108) | [service.start](D:/NewsAgent/backend/service.py:53) |
| 修改资料 | [接口](D:/NewsAgent/backend/app.py:161) | [service.update_profile](D:/NewsAgent/backend/service.py:95) → [MemoryStore.set_profile](D:/NewsAgent/agent/memory.py:267) |
| 修改订阅 | [接口](D:/NewsAgent/backend/app.py:169) | [service.update_subscription](D:/NewsAgent/backend/service.py:106) → [scheduler.apply](D:/NewsAgent/backend/scheduling.py:29) |
| 提交生成 | [接口](D:/NewsAgent/backend/app.py:175) | [service.generate](D:/NewsAgent/backend/service.py:125) → 工作线程执行 [_generate](D:/NewsAgent/backend/service.py:146) |
| 真正运行 Agent | [generate_brief](D:/NewsAgent/agent/runtime.py:186) | [submit](D:/NewsAgent/agent/runtime.py:152) → [run_loop](D:/NewsAgent/agent/loop.py:113) |
| 保存简报 | [generate_brief](D:/NewsAgent/agent/runtime.py:186) | [BriefStore.save](D:/NewsAgent/agent/briefs.py:64) |
| 更新进度 | [_generate 的 progress](D:/NewsAgent/backend/service.py:150) | [StateStore.progress](D:/NewsAgent/backend/storage.py:159) |
| 查询状态 | [接口](D:/NewsAgent/backend/app.py:179) | [service.status](D:/NewsAgent/backend/service.py:120) → [StateStore.get_run](D:/NewsAgent/backend/storage.py:192) |
| 邮件重发 | [接口](D:/NewsAgent/backend/app.py:196) | [service.send_email](D:/NewsAgent/backend/service.py:177) → [_send_email_task](D:/NewsAgent/backend/service.py:201) → [_send_email](D:/NewsAgent/backend/service.py:208) |
| SMTP 提交 | [_send_email](D:/NewsAgent/backend/service.py:208) | [EmailSender.send](D:/NewsAgent/backend/email_delivery.py:69) |
| 定时触发 | [注册 Cron job](D:/NewsAgent/backend/scheduling.py:35) | [run_scheduled](D:/NewsAgent/backend/service.py:229) → [generate](D:/NewsAgent/backend/service.py:125) |
| 启动补跑 | [scheduler.start](D:/NewsAgent/backend/scheduling.py:19) | [catch_up](D:/NewsAgent/backend/service.py:246) → [run_scheduled](D:/NewsAgent/backend/service.py:229) |
