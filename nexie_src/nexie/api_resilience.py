# Nexie — AI Coding Agent
# Copyright (c) 2026. All rights reserved.
# 本软件仅供个人学习与研究使用，禁止未经授权的商业用途、分发或修改后闭源。
# Licensed for personal, educational, and non-commercial use only.
"""
Nexie — 全链路防400/429报错体系
①请求频率限流 ②超长入参自动拆分 ③指数退避自动重试 ④多API密钥池轮询
"""
import os
import re
import time
import json
import random
import threading
import logging
from pathlib import Path
from datetime import datetime
from typing import Optional, Callable

logger = logging.getLogger("Nexie.ApiResilience")

# ═══════════════════════════════════════════
# 数据目录（统一入口）
# ═══════════════════════════════════════════
from nexie import get_data_dir
DATA_ROOT = get_data_dir()

# ═══════════════════════════════════════════
# ① 请求频率限流器
# ═══════════════════════════════════════════

class RateLimiter:
    """
    请求频率限流器：配置单时段最大请求数量，密集调用自动休眠错开。
    支持滑动窗口和固定窗口两种模式。
    """

    def __init__(self, max_requests_per_minute: int = 30,
                 max_requests_per_hour: int = 500,
                 min_interval_ms: int = 500):
        self.max_per_minute = max_requests_per_minute
        self.max_per_hour = max_requests_per_hour
        self.min_interval_ms = min_interval_ms  # 两次请求最小间隔(毫秒)

        self._minute_window: list[float] = []   # 最近1分钟请求时间戳
        self._hour_window: list[float] = []     # 最近1小时请求时间戳
        self._last_request_time = 0.0
        self._lock = threading.Lock()
        self._total_requests = 0
        self._throttled_count = 0

    def acquire(self) -> float:
        """
        获取请求许可，返回需要等待的秒数（0表示可立即发送）。
        调用方应在发送API请求前调用此方法。
        """
        with self._lock:
            now = time.time()
            self._total_requests += 1

            # 清理过期时间戳
            self._minute_window = [t for t in self._minute_window if now - t < 60]
            self._hour_window = [t for t in self._hour_window if now - t < 3600]

            wait_time = 0.0

            # 检查最小间隔
            elapsed_since_last = (now - self._last_request_time) * 1000
            if elapsed_since_last < self.min_interval_ms:
                interval_wait = (self.min_interval_ms - elapsed_since_last) / 1000
                wait_time = max(wait_time, interval_wait)

            # 检查分钟限制
            if len(self._minute_window) >= self.max_per_minute:
                # 需要等到最早的请求过期
                oldest = self._minute_window[0]
                minute_wait = 60 - (now - oldest) + 0.5  # 加0.5秒缓冲
                wait_time = max(wait_time, minute_wait)

            # 检查小时限制
            if len(self._hour_window) >= self.max_per_hour:
                oldest = self._hour_window[0]
                hour_wait = 3600 - (now - oldest) + 1.0
                wait_time = max(wait_time, hour_wait)

            if wait_time > 0:
                self._throttled_count += 1
                logger.debug("限流等待 %.2fs | 分钟:%d/%d 小时:%d/%d",
                           wait_time, len(self._minute_window), self.max_per_minute,
                           len(self._hour_window), self.max_per_hour)

            # 记录本次请求
            self._minute_window.append(now + wait_time)
            self._hour_window.append(now + wait_time)
            self._last_request_time = now + wait_time

            return wait_time

    def get_stats(self) -> dict:
        """获取限流统计"""
        with self._lock:
            now = time.time()
            self._minute_window = [t for t in self._minute_window if now - t < 60]
            self._hour_window = [t for t in self._hour_window if now - t < 3600]
            return {
                "total_requests": self._total_requests,
                "throttled": self._throttled_count,
                "minute_usage": f"{len(self._minute_window)}/{self.max_per_minute}",
                "hour_usage": f"{len(self._hour_window)}/{self.max_per_hour}",
            }


# ═══════════════════════════════════════════
# ② 超长入参自动拆分
# ═══════════════════════════════════════════

class InputSplitter:
    """
    超长入参自动拆分：检测超长工具调用/消息，自动拆分多段分步请求。
    杜绝一次性超大报文导致400错误。
    """

    MAX_TOOL_PARAM_CHARS = 50000    # 单工具参数最大字符数
    MAX_MESSAGE_CHARS = 80000       # 单消息最大字符数
    MAX_TOTAL_REQUEST_CHARS = 150000 # 单次请求总字符上限

    @staticmethod
    def estimate_tokens(text: str) -> int:
        """快速Token估算（从memory_layers复用）"""
        from nexie.memory_layers import estimate_tokens
        return estimate_tokens(text)

    @classmethod
    def should_split_message(cls, messages: list[dict]) -> bool:
        """检查消息列表是否需要拆分"""
        total_chars = sum(len(json.dumps(m, ensure_ascii=False)) for m in messages)
        return total_chars > cls.MAX_TOTAL_REQUEST_CHARS

    @classmethod
    def should_split_tool_params(cls, params: dict) -> bool:
        """检查工具参数是否需要拆分"""
        for key, value in params.items():
            if isinstance(value, str) and len(value) > cls.MAX_TOOL_PARAM_CHARS:
                return True
        return False

    @classmethod
    def split_tool_params(cls, tool_name: str, params: dict) -> list[dict]:
        """
        拆分超大工具参数为多组分步请求。
        例如：write_file 内容过长 → 拆分为多次 write_file 调用。
        """
        results = []

        # 找到最大参数
        max_key = None
        max_len = 0
        max_value = ""
        for key, value in params.items():
            if isinstance(value, str) and len(value) > max_len:
                max_len = len(value)
                max_key = key
                max_value = value

        if max_len <= cls.MAX_TOOL_PARAM_CHARS:
            return [params]  # 无需拆分

        # 拆分大文本参数
        chunks = []
        start = 0
        while start < len(max_value):
            # 找到合适的分割点（在换行处）
            end = min(start + cls.MAX_TOOL_PARAM_CHARS, len(max_value))
            if end < len(max_value):
                # 向前找最近的换行
                nl_pos = max_value.rfind('\n', start, end)
                if nl_pos > start + cls.MAX_TOOL_PARAM_CHARS // 2:
                    end = nl_pos + 1
            chunks.append(max_value[start:end])
            start = end

        # 生成多组分步请求参数
        for i, chunk in enumerate(chunks):
            split_params = dict(params)
            split_params[max_key] = chunk
            if len(chunks) > 1:
                split_params["_split_info"] = f"分段{i+1}/{len(chunks)}"
                # 对于文件写入，后续分段使用追加模式
                if tool_name in ("write_file", "write_text_file"):
                    split_params["mode"] = "append" if i > 0 else "overwrite"
            results.append(split_params)

        logger.info("参数拆分: %s → %d段 (总%d字符)", tool_name, len(chunks), max_len)
        return results

    @classmethod
    def split_messages_for_request(cls, messages: list[dict]) -> list[list[dict]]:
        """
        将超大消息列表拆分为多个子请求。
        保留system消息 + L2记忆在每个子请求中。
        按user消息边界分割，每个子请求不超过MAX_TOTAL_REQUEST_CHARS。
        """
        if not cls.should_split_message(messages):
            return [messages]

        system_msgs = [m for m in messages if m.get("role") == "system"]
        non_system = [m for m in messages if m.get("role") != "system"]

        batches = []
        current_batch = list(system_msgs)
        current_size = sum(len(json.dumps(m, ensure_ascii=False)) for m in current_batch)

        for m in non_system:
            m_size = len(json.dumps(m, ensure_ascii=False))
            if current_size + m_size > cls.MAX_TOTAL_REQUEST_CHARS and current_batch != system_msgs:
                batches.append(current_batch)
                current_batch = list(system_msgs)
                current_size = sum(len(json.dumps(m, ensure_ascii=False)) for m in current_batch)

            current_batch.append(m)
            current_size += m_size

        if current_batch and current_batch != system_msgs:
            batches.append(current_batch)

        logger.info("消息拆分: %d条 → %d批次", len(non_system), len(batches))
        return batches if batches else [messages]

    @classmethod
    def truncate_message_if_needed(cls, content: str, max_chars: int = None) -> tuple[str, bool]:
        """
        截断单条超长消息，返回(截断后内容, 是否被截断)。
        保留头尾，中间截断标记。
        """
        max_chars = max_chars or cls.MAX_MESSAGE_CHARS
        if len(content) <= max_chars:
            return content, False

        head_size = max_chars // 3
        tail_size = max_chars // 3
        truncated = (
            content[:head_size]
            + f"\n\n... [{len(content) - head_size - tail_size:,} 字符已截断] ...\n\n"
            + content[-tail_size:]
        )
        return truncated, True


# ═══════════════════════════════════════════
# ③ 指数退避自动重试机制
# ═══════════════════════════════════════════

class ExponentialBackoff:
    """
    指数退避重试：400/429/5xx自动重试，带抖动避免惊群效应。
    重试失败写入本地日志。
    """

    BASE_DELAY = 2.0             # 基础等待秒数
    MAX_DELAY = 120.0            # 最大等待秒数
    MAX_RETRIES = 5              # 最大重试次数
    JITTER_FACTOR = 0.3         # 抖动因子(±30%)

    RETRYABLE_STATUSES = {429, 500, 502, 503, 504}  # 可重试的HTTP状态码

    def __init__(self, max_retries: int = None, base_delay: float = None,
                 max_delay: float = None):
        self.max_retries = max_retries or self.MAX_RETRIES
        self.base_delay = base_delay or self.BASE_DELAY
        self.max_delay = max_delay or self.MAX_DELAY

        self._retry_log_path = DATA_ROOT / "logs" / "retry_errors.log"
        self._retry_log_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

        # 统计
        self._stats = {
            "total_retries": 0,
            "successful_retries": 0,
            "failed_retries": 0,
            "last_retry_time": None,
        }

    @classmethod
    def is_retryable(cls, status_code: int, error_message: str = "") -> bool:
        """判断错误是否可重试"""
        if status_code in cls.RETRYABLE_STATUSES:
            return True
        # 400中的某些情况可重试（如消息格式临时问题）
        if status_code == 400:
            retry_indicators = ["context_length", "too long", "invalid", "malformed"]
            msg_lower = error_message.lower()
            return any(ind in msg_lower for ind in retry_indicators)
        return False

    def calculate_delay(self, attempt: int) -> float:
        """
        计算第N次重试的等待时间。
        delay = min(base_delay * 2^attempt, max_delay) * (1 ± jitter)
        """
        delay = min(self.base_delay * (2 ** attempt), self.max_delay)
        # 添加随机抖动
        jitter = delay * self.JITTER_FACTOR * (random.random() * 2 - 1)
        return delay + jitter

    def retry_with_backoff(self, operation: Callable, *args,
                           on_retry: Callable = None, **kwargs) -> tuple[bool, any]:
        """
        执行操作并在失败时指数退避重试。
        - operation: 要执行的函数，应返回 (success: bool, result: any, status_code: int, error_msg: str)
        - 返回: (final_success: bool, final_result: any)

        operation 签名为: def op(*args, **kwargs) -> (bool, any, int, str)
        """
        last_error = None
        last_result = None

        for attempt in range(self.max_retries + 1):
            try:
                success, result, status_code, error_msg = operation(*args, **kwargs)

                if success:
                    if attempt > 0:
                        with self._lock:
                            self._stats["successful_retries"] += 1
                    return True, result

                # 第一次尝试失败，记录
                if attempt == 0 and status_code == 400:
                    # 400通常不可重试，但记录后仍尝试一次修复
                    self._log_retry_error(status_code, error_msg, attempt, args)
                    if not self.is_retryable(status_code, error_msg):
                        return False, result

                if not self.is_retryable(status_code, error_msg):
                    return False, result

                # 需要重试
                if attempt < self.max_retries:
                    delay = self.calculate_delay(attempt)
                    self._log_retry_error(status_code, error_msg, attempt, args)

                    with self._lock:
                        self._stats["total_retries"] += 1
                        self._stats["last_retry_time"] = datetime.now().isoformat()

                    if on_retry:
                        on_retry(attempt + 1, delay, status_code)

                    logger.warning(
                        "指数退避重试 #%d/%d | 等待%.1fs | HTTP %d",
                        attempt + 1, self.max_retries, delay, status_code
                    )
                    time.sleep(delay)
                    last_error = error_msg
                    last_result = result
                else:
                    with self._lock:
                        self._stats["failed_retries"] += 1
                    logger.error("重试耗尽 #%d | HTTP %d | %s", attempt, status_code, error_msg)

            except Exception as e:
                if attempt < self.max_retries:
                    delay = self.calculate_delay(attempt)
                    logger.warning("异常重试 #%d | 等待%.1fs | %s", attempt + 1, delay, str(e))
                    time.sleep(delay)
                    last_error = str(e)
                else:
                    logger.error("重试耗尽(异常) | %s", str(e))
                    return False, None

        return False, last_result

    def _log_retry_error(self, status_code: int, error_msg: str, attempt: int, context=None):
        """将重试错误写入本地日志"""
        try:
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            ctx_summary = ""
            if context:
                try:
                    # 只记录参数类型，不记录完整内容
                    ctx_summary = f" | context: {type(context).__name__}"
                except Exception:
                    pass
            log_line = f"[{ts}] HTTP{status_code} retry#{attempt}: {error_msg[:200]}{ctx_summary}\n"

            with self._lock:
                with open(self._retry_log_path, 'a', encoding='utf-8') as f:
                    f.write(log_line)
                # 日志轮转(1MB)
                if self._retry_log_path.stat().st_size > 1_000_000:
                    backup = self._retry_log_path.with_suffix('.log.bak')
                    backup.write_text(self._retry_log_path.read_text(encoding='utf-8'), encoding='utf-8')
        except Exception:
            pass

    def get_stats(self) -> dict:
        with self._lock:
            return dict(self._stats)


# ═══════════════════════════════════════════
# ④ 多API密钥池轮询
# ═══════════════════════════════════════════

class APIKeyPool:
    """
    多API密钥池：单密钥额度/超限报错自动无缝切换备用Key。
    密钥配置存放 Nexie_data/.env。
    支持 DeepSeek 和 MiMo 两种模型类型。
    """

    MAX_CONSECUTIVE_FAILURES = 3    # 单密钥连续失败次数上限
    KEY_COOLDOWN_SECONDS = 300      # 失败密钥冷却时间(5分钟)

    def __init__(self, data_root: Path = None):
        self._data_root = data_root or DATA_ROOT
        self._keys: list[dict] = []         # [{key, type, failures, last_fail, cooldown_until}]
        self._current_index = 0
        self._lock = threading.Lock()
        self._stats = {"switches": 0, "total_keys": 0, "active_keys": 0}

    def load_from_env(self, env_path: Path = None):
        """
        从 .env 文件加载所有API密钥。
        支持格式：
        - DEEPSEEK_API_KEY=sk-xxx (主密钥)
        - DEEPSEEK_API_KEY_2=sk-yyy (备用密钥)
        - DEEPSEEK_API_KEY_3=sk-zzz
        - MIMO_API_KEY=mimo-xxx (MiMo密钥)
        """
        env_path = env_path or self._data_root / ".env"
        if not env_path.exists():
            # 也尝试项目根目录
            env_path = (self._data_root.parent / ".env") if self._data_root.name == "Nexie_data" else Path(".env")

        if not env_path.exists():
            logger.warning("未找到 .env 文件，密钥池为空")
            return

        from dotenv import dotenv_values
        env_vars = dotenv_values(env_path)

        with self._lock:
            self._keys.clear()

            # 加载 DeepSeek 密钥
            ds_keys = []
            # 主密钥
            if env_vars.get("DEEPSEEK_API_KEY", "").strip():
                ds_keys.append(env_vars["DEEPSEEK_API_KEY"].strip())
            # 备用密钥 (DEEPSEEK_API_KEY_2, _3, _4 ...)
            for i in range(2, 11):
                backup_key = env_vars.get(f"DEEPSEEK_API_KEY_{i}", "").strip()
                if backup_key:
                    ds_keys.append(backup_key)

            for key in ds_keys:
                self._keys.append({
                    "key": key,
                    "type": "deepseek",
                    "failures": 0,
                    "last_fail": None,
                    "cooldown_until": None,
                })

            # 加载 MiMo 密钥
            mimo_keys = []
            if env_vars.get("MIMO_API_KEY", "").strip():
                mimo_keys.append(env_vars["MIMO_API_KEY"].strip())
            for i in range(2, 11):
                backup_key = env_vars.get(f"MIMO_API_KEY_{i}", "").strip()
                if backup_key:
                    mimo_keys.append(backup_key)

            for key in mimo_keys:
                self._keys.append({
                    "key": key,
                    "type": "mimo",
                    "failures": 0,
                    "last_fail": None,
                    "cooldown_until": None,
                })

            self._stats["total_keys"] = len(self._keys)
            self._stats["active_keys"] = len(self._keys)

        logger.info("密钥池加载完成: %d个密钥 (%d DeepSeek + %d MiMo)",
                   len(self._keys), len(ds_keys), len(mimo_keys))

    def add_key(self, api_key: str, key_type: str = "deepseek"):
        """手动添加密钥"""
        with self._lock:
            # 去重
            for k in self._keys:
                if k["key"] == api_key:
                    return
            self._keys.append({
                "key": api_key,
                "type": key_type,
                "failures": 0,
                "last_fail": None,
                "cooldown_until": None,
            })
            self._stats["total_keys"] += 1
            self._stats["active_keys"] += 1

    def get_key(self, key_type: str = "deepseek") -> Optional[str]:
        """
        获取一个可用的API密钥（轮询策略）。
        自动跳过冷却中的密钥。
        """
        with self._lock:
            now = time.time()

            # 筛选同类型且不在冷却中的密钥
            available = []
            for i, k in enumerate(self._keys):
                if k["type"] != key_type:
                    continue
                if k.get("cooldown_until") and now < k["cooldown_until"]:
                    continue  # 冷却中，跳过
                available.append((i, k))

            if not available:
                # 所有密钥都在冷却中，取冷却时间最短的
                all_same_type = [(i, k) for i, k in enumerate(self._keys) if k["type"] == key_type]
                if not all_same_type:
                    logger.error("无可用密钥 (类型:%s)", key_type)
                    return None
                # 取冷却最早结束的
                best = min(all_same_type,
                          key=lambda x: x[1].get("cooldown_until") or 0)
                logger.warning("所有密钥冷却中，选用最早恢复的 #%d", best[0])
                return best[1]["key"]

            # 轮询选择
            valid_indices = [i for i, k in available]
            # 从当前位置找下一个有效索引
            current_pool_idx = self._current_index % len(valid_indices) if valid_indices else 0
            chosen_idx = valid_indices[current_pool_idx]
            chosen_key = self._keys[chosen_idx]["key"]

            # 更新轮询指针
            self._current_index = (current_pool_idx + 1) % len(valid_indices)

            self._stats["active_keys"] = len(available)
            return chosen_key

    def mark_failure(self, api_key: str, status_code: int = 0):
        """标记密钥使用失败"""
        with self._lock:
            for k in self._keys:
                if k["key"] == api_key:
                    k["failures"] += 1
                    k["last_fail"] = datetime.now().isoformat()
                    if k["failures"] >= self.MAX_CONSECUTIVE_FAILURES:
                        # 进入冷却
                        k["cooldown_until"] = time.time() + self.KEY_COOLDOWN_SECONDS
                        logger.warning("密钥进入冷却 (连续%d次失败): %s...", k["failures"], api_key[:15])
                    break
            self._stats["switches"] += 1

    def mark_success(self, api_key: str):
        """标记密钥使用成功（重置失败计数）"""
        with self._lock:
            for k in self._keys:
                if k["key"] == api_key:
                    if k["failures"] > 0:
                        k["failures"] = 0
                        k["cooldown_until"] = None
                    break

    def get_available_key_count(self, key_type: str = "deepseek") -> int:
        """获取可用密钥数量"""
        now = time.time()
        with self._lock:
            return sum(1 for k in self._keys
                      if k["type"] == key_type
                      and (not k.get("cooldown_until") or now >= k["cooldown_until"]))

    def get_stats(self) -> dict:
        with self._lock:
            stats = dict(self._stats)
            stats["key_details"] = [
                {"type": k["type"], "failures": k["failures"],
                 "in_cooldown": bool(k.get("cooldown_until") and time.time() < k["cooldown_until"]),
                 "prefix": k["key"][:12] + "..."}
                for k in self._keys
            ]
            return stats


# ═══════════════════════════════════════════
# 综合API韧性管理器
# ═══════════════════════════════════════════

class APIResilienceManager:
    """
    API韧性管理器：整合限流、拆分、重试、密钥轮询四大功能。
    在AgentCore的API调用流程中统一使用。
    """

    def __init__(self, data_root: Path = None):
        self._data_root = data_root or DATA_ROOT
        self.rate_limiter = RateLimiter()
        self.input_splitter = InputSplitter()
        self.backoff = ExponentialBackoff()
        self.key_pool = APIKeyPool(self._data_root)
        self._lock = threading.Lock()

    def initialize(self):
        """初始化：加载密钥池"""
        self.key_pool.load_from_env()

    def pre_request_check(self, messages: list[dict], model_type: str = "deepseek") -> dict:
        """
        API请求前检查，返回:
        {
            "ready": bool,
            "wait_seconds": float,
            "api_key": str or None,
            "messages": list[dict] (可能已拆分/截断),
            "warnings": list[str],
        }
        """
        warnings = []
        ready = True

        # 1. 限流检查
        wait = self.rate_limiter.acquire()
        if wait > 0:
            time.sleep(wait)

        # 2. 获取可用密钥
        api_key = self.key_pool.get_key(model_type)
        if not api_key:
            warnings.append("⚠️ 无可用API密钥")
            ready = False

        # 3. 消息大小检查与截断
        if self.input_splitter.should_split_message(messages):
            total_chars = sum(len(json.dumps(m, ensure_ascii=False)) for m in messages)
            warnings.append(f"⚠️ 请求过大({total_chars:,}字符)，已自动拆分处理")

        # 4. 检查单条消息尺寸
        for i, m in enumerate(messages):
            content = m.get("content", "") or ""
            if isinstance(content, str) and len(content) > self.input_splitter.MAX_MESSAGE_CHARS:
                truncated, was_truncated = self.input_splitter.truncate_message_if_needed(content)
                if was_truncated:
                    messages[i] = dict(m)
                    messages[i]["content"] = truncated
                    warnings.append(f"⚠️ 消息#{i}过长，已截断({len(content):,}→{len(truncated):,}字符)")

        return {
            "ready": ready,
            "wait_seconds": wait,
            "api_key": api_key,
            "messages": messages,
            "warnings": warnings,
        }

    def wrap_api_call(self, api_call_func: Callable, *args, **kwargs) -> dict:
        """
        包装API调用，自动应用所有保护策略。
        api_call_func 应返回 {"success": bool, "result": any, "status_code": int, "error": str}
        """
        def resilient_operation(*op_args, **op_kwargs):
            try:
                result = api_call_func(*op_args, **op_kwargs)
                if isinstance(result, dict):
                    return (
                        result.get("success", True),
                        result.get("result"),
                        result.get("status_code", 200),
                        result.get("error", ""),
                    )
                return True, result, 200, ""
            except Exception as e:
                status = 500
                error_msg = str(e)
                # 尝试从异常中提取HTTP状态码
                if hasattr(e, 'status_code'):
                    status = e.status_code
                elif hasattr(e, 'response') and hasattr(e.response, 'status_code'):
                    status = e.response.status_code
                return False, None, status, error_msg

        return self.backoff.retry_with_backoff(resilient_operation, *args, **kwargs)

    def handle_api_error(self, error_status: int, error_msg: str, api_key: str = None):
        """处理API错误，更新密钥状态和统计数据"""
        if api_key:
            if error_status in (429, 402, 403, 401):
                # 密钥问题，标记失败
                self.key_pool.mark_failure(api_key, error_status)
            elif error_status >= 500:
                # 服务端问题，也标记（可能是IP限流）
                self.key_pool.mark_failure(api_key, error_status)

        # 记录到重试日志
        logger.warning("API错误 HTTP%d: %s", error_status, error_msg[:200])

    def handle_api_success(self, api_key: str = None):
        """处理API成功，重置密钥失败计数"""
        if api_key:
            self.key_pool.mark_success(api_key)

    def get_health_report(self) -> str:
        """获取API系统健康报告"""
        rl = self.rate_limiter.get_stats()
        bo = self.backoff.get_stats()
        kp = self.key_pool.get_stats()

        lines = [
            "🛡️ API韧性系统健康报告",
            "=" * 50,
            f"  限流器: {rl['total_requests']}次请求 | 节流{rl['throttled']}次",
            f"  分钟配额: {rl['minute_usage']} | 小时配额: {rl['hour_usage']}",
            f"  重试统计: {bo['total_retries']}次重试 | 成功{bo['successful_retries']} | 失败{bo['failed_retries']}",
            f"  密钥池: {kp['total_keys']}个密钥 | {kp['active_keys']}个活跃 | 切换{kp['switches']}次",
        ]
        return "\n".join(lines)


# ═══════════════════════════════════════════
# 全局单例
# ═══════════════════════════════════════════

_api_resilience: Optional[APIResilienceManager] = None


def get_api_resilience(data_root: Path = None) -> APIResilienceManager:
    """获取API韧性管理器全局单例"""
    global _api_resilience
    if _api_resilience is None:
        _api_resilience = APIResilienceManager(data_root)
    return _api_resilience


def reset_api_resilience():
    """重置API韧性管理器（测试用）"""
    global _api_resilience
    _api_resilience = None
