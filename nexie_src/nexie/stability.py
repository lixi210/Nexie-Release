# Nexie — AI Coding Agent
# Copyright (c) 2026. All rights reserved.
# 本软件仅供个人学习与研究使用，禁止未经授权的商业用途、分发或修改后闭源。
# Licensed for personal, educational, and non-commercial use only.
"""
Nexie — 稳定性模块
心跳检测、流式输出、超时终止、后台任务、子进程清理、内存监控
解决长时间任务卡死问题
"""
import os
import sys
import gc
import time
import signal
import atexit
import threading
import subprocess
import logging
import queue
from pathlib import Path
from typing import Optional, Callable
from dataclasses import dataclass, field
from datetime import datetime

logger = logging.getLogger("Nexie.Stability")

# ═══════════════════════════════════════════
# 1. 心跳检测
# ═══════════════════════════════════════════

class HeartbeatMonitor:
    """心跳检测器 — 监控长时间运行的任务"""

    def __init__(self, check_interval: float = 10.0, stall_threshold: float = 30.0):
        self.check_interval = check_interval  # 检测间隔(秒)
        self.stall_threshold = stall_threshold  # 无输出超时阈值(秒)
        self._monitored: dict[int, dict] = {}  # pid → {proc, last_output, start_time}
        self._lock = threading.Lock()
        self._running = False
        self._thread = None
        self._on_stall: Callable = None  # 卡死回调
        self._active = True
        self._last_heartbeat = time.time()

    def start(self):
        """启动心跳监听线程"""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, name="Nexie-Heartbeat", daemon=True)
        self._thread.start()
        logger.info("[心跳] 监控已启动 (间隔%.1fs, 卡死阈值%.1fs)", self.check_interval, self.stall_threshold)

    def stop(self):
        """停止心跳监听"""
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)

    def register_process(self, proc: subprocess.Popen, on_stall: Callable = None):
        """注册需要监控的进程"""
        with self._lock:
            self._monitored[proc.pid] = {
                "proc": proc,
                "last_output": time.time(),
                "start_time": time.time(),
                "on_stall": on_stall,
            }
        logger.debug(f"[心跳] 注册进程 PID={proc.pid}")

    def unregister_process(self, pid: int):
        """取消监控"""
        with self._lock:
            self._monitored.pop(pid, None)

    def update_activity(self, pid: int):
        """更新进程活动时间（收到输出时调用）"""
        with self._lock:
            if pid in self._monitored:
                self._monitored[pid]["last_output"] = time.time()

    def check_status(self) -> dict:
        """检查所有监控进程的状态"""
        now = time.time()
        status = {"healthy": [], "stalled": [], "dead": []}
        with self._lock:
            for pid, info in list(self._monitored.items()):
                proc = info["proc"]
                poll = proc.poll()
                if poll is not None:
                    status["dead"].append({"pid": pid, "exit_code": poll})
                    self._monitored.pop(pid, None)
                elif now - info["last_output"] > self.stall_threshold:
                    status["stalled"].append({
                        "pid": pid, "stall_seconds": now - info["last_output"]
                    })
                else:
                    status["healthy"].append(pid)
        return status

    @property
    def is_alive(self):
        return self._active and (time.time() - self._last_heartbeat < self.stall_threshold)

    def _loop(self):
        """心跳检测循环"""
        while self._running:
            self._last_heartbeat = time.time()
            try:
                status = self.check_status()
                for stalled in status["stalled"]:
                    pid = stalled["pid"]
                    secs = stalled["stall_seconds"]
                    logger.warning(f"[心跳] PID={pid} 疑似卡死 ({secs:.0f}s无输出)")
                    info = self._monitored.get(pid)
                    if info and info.get("on_stall"):
                        try:
                            info["on_stall"](pid, secs)
                        except Exception as e:
                            logger.error(f"[心跳] 卡死回调异常: {e}")
            except Exception as e:
                logger.error(f"[心跳] 检测异常: {e}")
            time.sleep(self.check_interval)


# ═══════════════════════════════════════════
# 2. 超时终止
# ═══════════════════════════════════════════

class TimeoutExecutor:
    """带超时的命令执行器"""

    def __init__(self, default_timeout: int = 120):
        self.default_timeout = default_timeout
        self._current_process: subprocess.Popen = None
        self._heartbeat: HeartbeatMonitor = None

    def set_heartbeat(self, hb: HeartbeatMonitor):
        self._heartbeat = hb

    def run(self, command: str, cwd: str = None, timeout: int = None,
            env: dict = None, on_output: Callable = None) -> dict:
        """
        执行命令，返回 {stdout, stderr, returncode, timed_out, duration}
        on_output(line) — 流式输出回调，每行调用
        """
        effective_timeout = timeout or self.default_timeout
        start_time = time.time()

        try:
            shell = sys.platform == "win32"
            proc = subprocess.Popen(
                command,
                shell=shell,
                cwd=cwd or os.getcwd(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=env,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | 0x08000000 if sys.platform == "win32" else 0,
            )
            self._current_process = proc

            if self._heartbeat:
                self._heartbeat.register_process(proc)

            stdout_lines = []
            stderr_lines = []

            def read_stream(stream, collector, is_stderr=False):
                for line in iter(stream.readline, ""):
                    collector.append(line)
                    if self._heartbeat:
                        self._heartbeat.update_activity(proc.pid)
                    if on_output:
                        on_output(line.rstrip(), is_stderr)

            stdout_thread = threading.Thread(target=read_stream, args=(proc.stdout, stdout_lines), daemon=True)
            stderr_thread = threading.Thread(target=read_stream, args=(proc.stderr, stderr_lines, True), daemon=True)
            stdout_thread.start()
            stderr_thread.start()

            try:
                proc.wait(timeout=effective_timeout)
            except subprocess.TimeoutExpired:
                self._kill_process_tree(proc.pid)
                if self._heartbeat:
                    self._heartbeat.unregister_process(proc.pid)
                self._current_process = None
                return {
                    "stdout": "".join(stdout_lines), "stderr": "".join(stderr_lines),
                    "returncode": -1, "timed_out": True,
                    "duration": time.time() - start_time,
                    "error": f"命令超时 ({effective_timeout}s): {command[:80]}"
                }

            stdout_thread.join(timeout=5)
            stderr_thread.join(timeout=5)

            if self._heartbeat:
                self._heartbeat.unregister_process(proc.pid)
            self._current_process = None

            return {
                "stdout": "".join(stdout_lines), "stderr": "".join(stderr_lines),
                "returncode": proc.returncode, "timed_out": False,
                "duration": time.time() - start_time,
            }
        except Exception as e:
            return {
                "stdout": "", "stderr": str(e), "returncode": -1, "timed_out": False,
                "duration": time.time() - start_time, "error": str(e)
            }

    def cancel(self):
        """取消当前执行的命令"""
        if self._current_process and self._current_process.poll() is None:
            self._kill_process_tree(self._current_process.pid)
            return "已终止当前命令"
        return "没有正在执行的命令"

    @staticmethod
    def _kill_process_tree(pid: int):
        """递归终止进程树"""
        try:
            if sys.platform == "win32":
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                               capture_output=True, creationflags=0x08000000, timeout=10)
            else:
                try:
                    os.killpg(os.getpgid(pid), signal.SIGTERM)
                except (ProcessLookupError, OSError):
                    os.kill(pid, signal.SIGTERM)
                time.sleep(1)
                try:
                    os.killpg(os.getpgid(pid), signal.SIGKILL)
                except (ProcessLookupError, OSError):
                    os.kill(pid, signal.SIGKILL)
        except Exception:
            pass  # 进程可能已经退出


# ═══════════════════════════════════════════
# 3. 后台任务管理
# ═══════════════════════════════════════════

@dataclass
class BackgroundTask:
    """后台任务"""
    id: str
    name: str
    command: str
    status: str = "pending"  # pending | running | completed | failed | cancelled
    start_time: float = 0
    end_time: float = 0
    result: dict = None
    _process: subprocess.Popen = None
    _thread: threading.Thread = None
    progress: str = ""
    progress_callback: Callable = None


class BackgroundTaskManager:
    """后台任务管理器"""

    def __init__(self, max_concurrent: int = 4):
        self._tasks: dict[str, BackgroundTask] = {}
        self._lock = threading.Lock()
        self._max_concurrent = max_concurrent
        self._executor = TimeoutExecutor()
        self._task_counter = 0

    def submit(self, name: str, command: str, cwd: str = None,
               timeout: int = 600, on_progress: Callable = None) -> str:
        """提交后台任务，返回 task_id"""
        with self._lock:
            self._task_counter += 1
            task_id = f"bg_{self._task_counter}_{int(time.time())}"

        task = BackgroundTask(
            id=task_id, name=name, command=command,
            status="pending", progress_callback=on_progress,
        )

        with self._lock:
            self._tasks[task_id] = task

        # 启动后台线程执行
        thread = threading.Thread(
            target=self._run_task, args=(task, cwd, timeout),
            name=f"Nexie-BG-{task_id}", daemon=True
        )
        task._thread = thread
        thread.start()

        return task_id

    def _run_task(self, task: BackgroundTask, cwd: str, timeout: int):
        """在后台线程中执行任务"""
        with self._lock:
            task.status = "running"
            task.start_time = time.time()

        def on_output(line, is_stderr):
            task.progress = line
            if task.progress_callback:
                try:
                    task.progress_callback(task.id, line, is_stderr)
                except Exception:
                    pass

        result = self._executor.run(
            command=task.command, cwd=cwd, timeout=timeout,
            on_output=on_output
        )

        with self._lock:
            task.result = result
            task.end_time = time.time()
            if result.get("timed_out"):
                task.status = "failed"
            elif result.get("returncode", -1) == 0:
                task.status = "completed"
            else:
                task.status = "failed" if result.get("returncode", -1) != 0 else "completed"

    def get_status(self, task_id: str) -> dict:
        """获取任务状态"""
        with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                return {"error": f"任务不存在: {task_id}"}
            return {
                "id": task.id, "name": task.name, "status": task.status,
                "progress": task.progress, "duration": task.end_time - task.start_time if task.end_time else time.time() - task.start_time,
                "result": task.result,
            }

    def list_tasks(self) -> list[dict]:
        """列出所有任务"""
        with self._lock:
            return [
                {"id": t.id, "name": t.name, "status": t.status,
                 "duration": (t.end_time or time.time()) - (t.start_time or time.time())}
                for t in self._tasks.values()
            ]

    def cancel(self, task_id: str) -> str:
        """取消后台任务"""
        with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                return f"任务不存在: {task_id}"
            if task.status not in ("running", "pending"):
                return f"任务已完成，无法取消: {task_id}"
            task.status = "cancelled"
            if task._process:
                self._executor._kill_process_tree(task._process.pid)
        return f"已取消任务: {task.name}"

    def get_output(self, task_id: str) -> str:
        """获取已完成任务的输出"""
        with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                return f"任务不存在: {task_id}"
            if not task.result:
                return f"任务未完成: {task.status}"
            r = task.result
            return (
                f"任务: {task.name}\n状态: {task.status}\n耗时: {r.get('duration', 0):.1f}s\n"
                f"退出码: {r.get('returncode', 'N/A')}\n"
                f"{'='*50}\n{r.get('stdout', '')}\n{'='*50}\n"
                f"{'[stderr]' if r.get('stderr') else ''}\n{r.get('stderr', '')}"
            )


# ═══════════════════════════════════════════
# 5. 子进程自动清理
# ═══════════════════════════════════════════

_managed_processes: list[subprocess.Popen] = []
_cleanup_lock = threading.Lock()


def track_process(proc: subprocess.Popen):
    """注册子进程 — 主进程退出时自动清理"""
    with _cleanup_lock:
        _managed_processes.append(proc)


def untrack_process(proc: subprocess.Popen):
    """取消注册"""
    with _cleanup_lock:
        if proc in _managed_processes:
            _managed_processes.remove(proc)


def cleanup_all_processes():
    """主进程退出时清理所有子进程"""
    logger.info("[清理] 正在清理所有子进程...")
    with _cleanup_lock:
        for proc in _managed_processes:
            try:
                if proc.poll() is None:
                    if sys.platform == "win32":
                        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                                       capture_output=True, creationflags=0x08000000, timeout=5)
                    else:
                        proc.terminate()
                        try:
                            proc.wait(timeout=3)
                        except subprocess.TimeoutExpired:
                            proc.kill()
            except Exception:
                pass
        _managed_processes.clear()
    logger.info("[清理] 子进程清理完成")


# 注册 atexit 钩子
atexit.register(cleanup_all_processes)


# ═══════════════════════════════════════════
# 6. 内存监控
# ═══════════════════════════════════════════

class MemoryMonitor:
    """内存使用监控 — 占用过高自动释放缓存"""

    def __init__(self, warning_threshold_mb: int = 1024, critical_threshold_mb: int = 2048):
        self.warning_threshold = warning_threshold_mb * 1024 * 1024
        self.critical_threshold = critical_threshold_mb * 1024 * 1024
        self._last_check = time.time()
        self._check_interval = 30.0  # 每30秒检查一次
        self._cache_managers: list[Callable] = []  # 可释放缓存的回调

    def register_cache_manager(self, cleaner: Callable):
        """注册缓存清理回调"""
        self._cache_managers.append(cleaner)

    def get_current_usage(self) -> dict:
        """获取当前内存使用"""
        import psutil
        try:
            process = psutil.Process()
            mem = process.memory_info()
            sys_mem = psutil.virtual_memory()
            return {
                "rss_mb": mem.rss / (1024 * 1024),
                "vms_mb": mem.vms / (1024 * 1024),
                "percent": process.memory_percent(),
                "system_used_percent": sys_mem.percent,
                "system_available_mb": sys_mem.available / (1024 * 1024),
            }
        except ImportError:
            return {"error": "psutil未安装"}
        except Exception as e:
            return {"error": str(e)}

    def check_and_clean(self) -> str:
        """检查内存，必要时清理"""
        try:
            mem = self.get_current_usage()
            if "error" in mem:
                return ""

            rss = mem["rss_mb"] * 1024 * 1024
            if rss > self.critical_threshold:
                logger.warning(f"[内存] 严重超标 ({mem['rss_mb']:.0f}MB)，强制清理")
                return self._force_cleanup()
            elif rss > self.warning_threshold:
                logger.info(f"[内存] 超过警告阈值 ({mem['rss_mb']:.0f}MB)，主动清理")
                return self._gentle_cleanup()
        except Exception as e:
            logger.error(f"[内存] 检查失败: {e}")
        return ""

    def _gentle_cleanup(self) -> str:
        """温和清理：调用缓存管理器并触发GC"""
        for cleaner in self._cache_managers:
            try:
                cleaner()
            except Exception:
                pass
        gc.collect()
        return f"✅ 内存已优化 (当前: {self.get_current_usage().get('rss_mb', '?')} MB)"

    def _force_cleanup(self) -> str:
        """强制清理：清空所有缓存 + GC + 释放内存给OS"""
        for cleaner in self._cache_managers:
            try:
                cleaner()
            except Exception:
                pass
        gc.collect()
        # 在Linux上尝试释放内存给OS
        if sys.platform != "win32":
            try:
                import ctypes
                libc = ctypes.CDLL("libc.so.6")
                libc.malloc_trim(0)
            except Exception:
                pass
        return f"⚠️ 内存强制清理完成 (当前: {self.get_current_usage().get('rss_mb', '?')} MB)"


# ═══════════════════════════════════════════
# 流式输出缓冲器
# ═══════════════════════════════════════════

class StreamBuffer:
    """流式输出缓冲 — 实时传递进度，批量提交结果"""

    def __init__(self, max_lines: int = 50, flush_interval: float = 0.5):
        self._buffer: list[str] = []
        self._lock = threading.Lock()
        self._max_lines = max_lines
        self._flush_interval = flush_interval
        self._last_flush = time.time()
        self._subscribers: list[Callable] = []

    def write(self, line: str):
        """写入一行"""
        with self._lock:
            self._buffer.append(line)
            if len(self._buffer) >= self._max_lines:
                self._buffer = self._buffer[-self._max_lines:]

        now = time.time()
        if now - self._last_flush >= self._flush_interval:
            self._flush()

    def subscribe(self, callback: Callable):
        """订阅输出更新"""
        self._subscribers.append(callback)

    def _flush(self):
        """推送给所有订阅者"""
        self._last_flush = time.time()
        with self._lock:
            lines = list(self._buffer)
        for sub in self._subscribers:
            try:
                sub(lines)
            except Exception:
                pass

    def get_recent(self, n: int = 20) -> list[str]:
        """获取最近N行"""
        with self._lock:
            return list(self._buffer[-n:])

    def clear(self):
        with self._lock:
            self._buffer.clear()


# ═══════════════════════════════════════════
# 全局单例
# ═══════════════════════════════════════════

_heartbeat: HeartbeatMonitor = None
_timeout_executor: TimeoutExecutor = None
_bg_manager: BackgroundTaskManager = None
_memory_monitor: MemoryMonitor = None


def get_heartbeat() -> HeartbeatMonitor:
    global _heartbeat
    if _heartbeat is None:
        _heartbeat = HeartbeatMonitor()
    return _heartbeat


def get_executor() -> TimeoutExecutor:
    global _timeout_executor
    if _timeout_executor is None:
        _timeout_executor = TimeoutExecutor()
    return _timeout_executor


def get_bg_manager() -> BackgroundTaskManager:
    global _bg_manager
    if _bg_manager is None:
        _bg_manager = BackgroundTaskManager()
    return _bg_manager


def get_memory_monitor() -> MemoryMonitor:
    global _memory_monitor
    if _memory_monitor is None:
        _memory_monitor = MemoryMonitor()
    return _memory_monitor
