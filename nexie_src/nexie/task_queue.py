# Nexie — AI Coding Agent
# Copyright (c) 2026. All rights reserved.
# 本软件仅供个人学习与研究使用，禁止未经授权的商业用途、分发或修改后闭源。
# Licensed for personal, educational, and non-commercial use only.
"""
Nexie — 任务队列模块
FIFO任务队列，UI线程与工作线程分离。
特性：
- FIFO排队，先到先处理
- 队列长度限制（默认5），满时拒绝新任务
- 支持取消当前任务
- 请求超时保护（默认5分钟）
- 线程安全的状态查询
"""
import queue
import threading
import time
import logging
from enum import Enum
from typing import Optional, Callable

logger = logging.getLogger("Nexie.TaskQueue")


class QueueStatus(Enum):
    """队列状态"""
    IDLE = "idle"           # 空闲，无任务
    BUSY = "busy"           # 正在处理
    QUEUED = "queued"       # 队列中有等待任务


class TaskQueue:
    """
    线程安全的FIFO任务队列。

    使用方式:
        tq = TaskQueue(max_size=5, timeout=300)
        tq.start_worker(process_func)   # 启动工作线程

        # 提交任务（从UI线程）
        status, msg = tq.enqueue("用户消息")
        if status == "accepted":
            print("任务已提交")
        elif status == "queued":
            print(f"任务已排队，前面还有{N}个")
        elif status == "rejected":
            print("队列已满，请稍后重试")
    """

    # ── 默认配置 ──
    DEFAULT_MAX_SIZE = 5          # 最大排队数（不含当前正在执行的）
    DEFAULT_TIMEOUT = 300         # 单个任务超时秒数（5分钟）
    TICK_INTERVAL = 0.1           # 工作线程轮询间隔

    def __init__(self, max_size: int = None, timeout: float = None):
        self._max_size = max_size or self.DEFAULT_MAX_SIZE
        self._timeout = timeout or self.DEFAULT_TIMEOUT

        # ── 线程安全组件 ──
        self._queue: queue.Queue = queue.Queue(maxsize=self._max_size)
        self._lock = threading.Lock()
        self._state_change = threading.Condition(self._lock)

        # ── 状态 ──
        self._current_task: Optional[str] = None   # 当前正在处理的任务
        self._current_start: float = 0.0           # 当前任务开始时间
        self._worker_thread: Optional[threading.Thread] = None
        self._cancel_flag = False
        self._running = False
        self._total_processed = 0
        self._total_rejected = 0

        # ── 回调 ──
        self._process_func: Optional[Callable] = None
        self._on_status_change: Optional[Callable] = None  # 状态变化回调（UI更新用）

    # ══════════════════════════════════════
    # 任务提交
    # ══════════════════════════════════════

    def enqueue(self, task_data) -> tuple[str, str]:
        """
        提交任务到队列。

        返回:
            ("accepted", msg)  — 任务被接受，即将开始处理
            ("queued", msg)    — 任务已加入队列，前面有N个等待
            ("rejected", msg)  — 队列已满，拒绝任务
        """
        with self._lock:
            # 检查队列是否已满
            if self._queue.full():
                self._total_rejected += 1
                logger.warning("任务队列已满(%d/%d)，拒绝新任务",
                             self._queue.qsize(), self._max_size)
                return ("rejected",
                       f"任务队列已满（{self._queue.qsize()}/{self._max_size}），"
                       "请等待当前任务完成后再发送。")

            try:
                self._queue.put_nowait(task_data)
            except queue.Full:
                self._total_rejected += 1
                return ("rejected", "队列已满，请稍后再试。")

            qsize = self._queue.qsize()
            self._total_processed += 1

            if qsize == 1 and self._current_task is None:
                # 队列中只有当前任务，且没有正在处理的 → 即将开始
                self._state_change.notify()
                return ("accepted", "任务已接受，正在处理...")
            else:
                # 前面还有任务在排队
                ahead = qsize - 1
                if self._current_task is not None:
                    ahead += 1  # 加上当前正在执行的任务
                logger.info("任务已排队 | 前面还有%d个任务 | 队列:%d/%d",
                           ahead, qsize, self._max_size)
                return ("queued", f"任务已排队（前面还有{ahead}个），请稍候...")

    # ══════════════════════════════════════
    # 工作线程
    # ══════════════════════════════════════

    def start_worker(self, process_func: Callable):
        """
        启动后台工作线程。
        process_func(task_data) → None
        处理函数内部通过回调更新UI。
        """
        if self._running:
            logger.warning("工作线程已在运行")
            return

        self._process_func = process_func
        self._running = True
        self._cancel_flag = False
        self._worker_thread = threading.Thread(
            target=self._worker_loop,
            name="Nexie-Worker",
            daemon=True,
        )
        self._worker_thread.start()
        logger.info("工作线程已启动 | 队列上限:%d | 超时:%ds",
                   self._max_size, self._timeout)

    def stop_worker(self):
        """停止工作线程"""
        self._running = False
        self._cancel_flag = True
        with self._lock:
            self._state_change.notify()
        if self._worker_thread and self._worker_thread.is_alive():
            self._worker_thread.join(timeout=3.0)
        logger.info("工作线程已停止 | 共处理:%d | 拒绝:%d",
                   self._total_processed, self._total_rejected)

    def _worker_loop(self):
        """工作线程主循环：从队列取任务→处理→重复"""
        while self._running:
            task_data = None
            try:
                # 阻塞等待任务，同时响应取消信号
                task_data = self._queue.get(timeout=self.TICK_INTERVAL)
            except queue.Empty:
                # 超时无任务 → 继续循环
                continue

            if not self._running:
                break

            # ── 处理任务 ──
            self._cancel_flag = False
            with self._lock:
                self._current_task = str(task_data)[:80]
                self._current_start = time.time()

            self._notify_status_change()

            try:
                logger.debug("开始处理任务: %s", self._current_task)
                self._process_func(task_data)
            except Exception as e:
                logger.error("任务处理异常: %s", e, exc_info=True)
            finally:
                with self._lock:
                    self._current_task = None
                    self._current_start = 0.0
                self._queue.task_done()
                self._notify_status_change()

        logger.debug("工作线程退出")

    def _notify_status_change(self):
        """通知外部状态变化（线程安全）"""
        if self._on_status_change:
            try:
                self._on_status_change(self.get_status())
            except Exception:
                pass

    # ══════════════════════════════════════
    # 任务取消
    # ══════════════════════════════════════

    def cancel_current(self):
        """取消当前正在处理的任务"""
        self._cancel_flag = True
        logger.info("当前任务已标记取消: %s", self._current_task)

    def clear_queue(self):
        """清空等待队列（不影响当前正在处理的任务）"""
        cleared = 0
        while True:
            try:
                self._queue.get_nowait()
                self._queue.task_done()
                cleared += 1
            except queue.Empty:
                break
        if cleared:
            logger.info("已清空%d个排队任务", cleared)
        return cleared

    @property
    def cancelled(self) -> bool:
        return self._cancel_flag

    # ══════════════════════════════════════
    # 超时检查
    # ══════════════════════════════════════

    def check_timeout(self) -> bool:
        """检查当前任务是否超时，返回到期状态"""
        with self._lock:
            if self._current_task is None:
                return False
            elapsed = time.time() - self._current_start
            return elapsed > self._timeout

    def get_elapsed(self) -> float:
        """获取当前任务已用时间（秒）"""
        with self._lock:
            if self._current_task is None:
                return 0.0
            return time.time() - self._current_start

    # ══════════════════════════════════════
    # 状态查询（线程安全）
    # ══════════════════════════════════════

    def get_status(self) -> dict:
        """获取完整状态信息"""
        with self._lock:
            qsize = self._queue.qsize()
            status = QueueStatus.IDLE
            if self._current_task is not None:
                status = QueueStatus.BUSY
                if qsize > 0:
                    status = QueueStatus.QUEUED

            return {
                "status": status,
                "status_str": status.value,
                "current_task": self._current_task,
                "elapsed": time.time() - self._current_start if self._current_task else 0.0,
                "queue_size": qsize,
                "max_size": self._max_size,
                "queue_full": self._queue.full(),
                "total_processed": self._total_processed,
                "total_rejected": self._total_rejected,
            }

    def is_idle(self) -> bool:
        """是否空闲（无当前任务且队列为空）"""
        with self._lock:
            return self._current_task is None and self._queue.empty()

    def is_busy(self) -> bool:
        """是否忙碌（有正在处理的任务）"""
        with self._lock:
            return self._current_task is not None

    def queue_size(self) -> int:
        """当前排队任务数"""
        return self._queue.qsize()

    def set_status_callback(self, callback: Callable):
        """设置状态变化回调，用于UI更新"""
        self._on_status_change = callback


# ═══════════════════════════════════════════
# 全局单例
# ═══════════════════════════════════════════

_task_queue: Optional[TaskQueue] = None


def get_task_queue(max_size: int = 5, timeout: float = 300) -> TaskQueue:
    """获取任务队列全局单例"""
    global _task_queue
    if _task_queue is None:
        _task_queue = TaskQueue(max_size=max_size, timeout=timeout)
    return _task_queue


def reset_task_queue():
    """重置任务队列（测试用）"""
    global _task_queue
    if _task_queue is not None:
        _task_queue.stop_worker()
    _task_queue = None
