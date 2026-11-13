"""自动点击的执行器：把「驱动鼠标」从 OCR 主循环里挪出去。

为什么要单独一层：滑杆闭环要 2–6s（长按 → 拖 → 读气泡 → 确定）。如果直接在
`drain_advice` 里同步跑，主循环这段时间既不采帧、也不打建议 —— 2026-11-12
21:23 实测 12.7s 没有一条状态记录，而且建议日志要等点击结束才打出来，
用户看到的就是「建议迟迟不出来、出来了动作又慢」。

**安全闸不在这里**：点不点、点哪条、用哪个坐标，全部由 `LiveProcessor` 在
主线程判断完再 `submit`。执行器只负责「`clicker.run` 在哪个线程跑」。

两个实现：

  · `InlineClickExecutor`（默认）：原地执行。单测、离线回放、dry-run 语义
    与改造前**逐字节等价** —— 18 个 `test_auto_click_*` 一个都不用改。
  · `ThreadClickExecutor`：live 启动时装配。单线程 + 队列；主循环用 `busy`
    判断「点击在途」，期间跳过 OCR（滑杆会盖住公共牌第三张，继续识别只会
    造出 `board 2 张` 的假警报）。

主循环不吃异常：`clicker.run` 抛出来的错误由工作线程兜住、封成
`ClickResult("error")` 交回主线程，绝不把工作线程打死。
"""

from __future__ import annotations

import queue
import threading
from typing import Callable, Protocol, runtime_checkable


@runtime_checkable
class ClickExecutor(Protocol):
    """`clicker.run` 的执行位置。`inline=True` 表示同步执行（测试/离线）。"""

    inline: bool

    @property
    def busy(self) -> bool:
        """鼠标正在被驱动（滑杆打开着）。主循环据此暂停识别。"""
        ...

    def submit(self, fn: Callable[[], None]) -> None:
        ...

    def close(self) -> None:
        ...


class InlineClickExecutor:
    """原地执行：`submit` 立刻跑完，`busy` 永远是 False。

    这是默认实现，所以「不装配执行器」= 改造前的同步行为。
    """

    inline = True

    @property
    def busy(self) -> bool:
        return False

    def submit(self, fn: Callable[[], None]) -> None:
        fn()

    def close(self) -> None:
        return None


class ThreadClickExecutor:
    """单线程 + 队列：一次只跑一个点击，主循环用 `busy` 让路。

    `busy` 从 `submit` 起为真，到任务跑完为止 —— 语义就是「鼠标正被驱动」。
    任务里抛异常不会杀死线程（错误由调用方在任务体内封成结果）。
    """

    inline = False

    def __init__(self, name: str = "live-click") -> None:
        self._queue: "queue.Queue[Callable[[], None] | None]" = queue.Queue()
        self._busy = threading.Event()
        self._thread = threading.Thread(target=self._loop, name=name,
                                        daemon=True)
        self._thread.start()

    @property
    def busy(self) -> bool:
        return self._busy.is_set()

    def submit(self, fn: Callable[[], None]) -> None:
        self._busy.set()
        self._queue.put(fn)

    def _loop(self) -> None:
        while True:
            fn = self._queue.get()
            if fn is None:
                self._busy.clear()
                return
            try:
                fn()
            except Exception:        # noqa: BLE001 — 任务自己负责记错误
                pass
            finally:
                if self._queue.empty():
                    self._busy.clear()

    def close(self) -> None:
        self._busy.clear()
        self._queue.put(None)
