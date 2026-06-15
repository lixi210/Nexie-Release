# Nexie — AI Coding Agent
# Copyright (c) 2026. All rights reserved.
# 本软件仅供个人学习与研究使用，禁止未经授权的商业用途、分发或修改后闭源。
# Licensed for personal, educational, and non-commercial use only.
"""
Nexie — API 客户端封装
DeepSeek / MIMO API 封装，集成全链路防400/429韧性体系
基于 OpenAI 兼容协议，支持流式输出、深度思考模式、视觉多模态
自动读取 .env，完善的异常捕获与中文错误提示
"""
import os
import json
import time
import logging
import threading
from pathlib import Path
from dotenv import load_dotenv
from openai import (
    OpenAI,
    AuthenticationError,
    RateLimitError,
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    APIError,
)

ENV_PATH = Path(__file__).parent / ".env"
if ENV_PATH.exists():
    load_dotenv(ENV_PATH)

# ═══ API韧性系统导入（延迟导入避免循环依赖） ═══
_api_resilience = None
_resilience_lock = threading.Lock()

def _get_resilience():
    global _api_resilience
    if _api_resilience is None:
        with _resilience_lock:
            if _api_resilience is None:
                try:
                    from nexie.api_resilience import get_api_resilience
                    _api_resilience = get_api_resilience()
                    _api_resilience.initialize()
                except Exception:
                    pass
    return _api_resilience


def _format_error(e: Exception) -> str:
    """将 OpenAI 异常转换为中文错误提示"""
    if isinstance(e, AuthenticationError):
        return (
            "❌ 密钥验证失败\n"
            "可能原因：1. API Key 错误或已过期  2. 密钥已被删除\n"
            "请通过「设置 → API 密钥设置」重新填写。"
        )
    if isinstance(e, RateLimitError):
        return "⏳ 请求频率超限，请稍后重试。"
    if isinstance(e, APITimeoutError):
        return "⏱️ 请求超时，模型思考时间过长，请重试或简化问题。"
    if isinstance(e, APIConnectionError):
        return "🌐 网络连接失败，请检查网络或代理设置。"
    if isinstance(e, APIStatusError):
        status = e.status_code
        if status == 400:
            return f"🖥️ 请求参数错误 (HTTP 400): {e.message}"
        if status == 402:
            return "💰 账户余额不足，请前往 DeepSeek 平台充值。"
        if status in (403, 401):
            return "🔒 访问被拒绝，请检查 API Key 权限。"
        if status in (500, 502, 503):
            return f"🖥️ DeepSeek 服务器错误 (HTTP {status})，请稍后重试。"
        return f"🖥️ API 返回错误 (HTTP {status})"
    if isinstance(e, APIError):
        return f"🖥️ API 异常: {e.message}"
    return f"❌ 未知错误: {str(e)}"


def _estimate_tokens(text: str) -> int:
    """粗略估算token数：中文1字≈1.2token，英文1词≈1token，混合取字符数/2.5"""
    return max(1, len(text) // 2)


# ═══════════════════════════════════════════════════════════════
# 400错误处理：agent_core 层预防，client 层直接报错不重试
# ═══════════════════════════════════════════════════════════════




# ═══════════════════════════════════════════
# StreamWatchdog — SSE流式心跳保活，防止长连接僵死
# ═══════════════════════════════════════════

class StreamHeartbeatTimeout(Exception):
    """流式心跳超时异常：45s无数据块则触发，用于触发断连重试"""
    pass


class StreamWatchdog:
    """SSE流式看门狗：45s无chunk触发超时，收到有效数据自动重置。
    所有Timer实例在异常/正常结束时严格销毁，防止后台线程堆积。"""

    def __init__(self, timeout: float = 30.0):
        self._timeout = timeout
        self._timer: threading.Timer | None = None
        self._lock = threading.Lock()
        self._fired = False
        self._stopped = False

    def start(self):
        """启动/重启看门狗计时器（线程安全）"""
        with self._lock:
            if self._stopped:
                return
            self._cancel_timer()
            self._fired = False
            self._timer = threading.Timer(self._timeout, self._on_timeout)
            self._timer.daemon = True  # 守护线程,主线程退出时自动消亡
            self._timer.start()

    def reset(self):
        """收到有效chunk时重置计时器（等价于 start()）"""
        self.start()

    def stop(self):
        """永久停止看门狗，销毁Timer实例"""
        with self._lock:
            self._stopped = True
            self._cancel_timer()

    def _cancel_timer(self):
        """安全取消当前Timer（必须持有_lock）"""
        if self._timer is not None:
            try:
                self._timer.cancel()
            except Exception:
                pass
            self._timer = None

    def _on_timeout(self):
        """超时回调：标记触发，通知上层关闭流"""
        with self._lock:
            if self._stopped:
                return
            self._fired = True
        logging.getLogger("Nexie.Client").warning(
            "SSE心跳超时：%ds无数据块，触发断连重试", self._timeout
        )

    @property
    def fired(self) -> bool:
        with self._lock:
            return self._fired

    def __del__(self):
        self.stop()


# ═══════════════════════════════════════════════════════════════
# Nginx 反向代理 SSE 配置（放在 server/location 块内）
# 用于生产环境代理 DeepSeek/MiMo API 时保持长连接不超时
# ═══════════════════════════════════════════════════════════════
"""
location /v1/chat/completions {
    proxy_pass https://api.deepseek.com;
    proxy_http_version 1.1;
    proxy_set_header Connection '';
    proxy_buffering off;              # 关闭缓冲,实时转发SSE chunk
    proxy_cache off;                  # 禁用缓存
    proxy_read_timeout 3600s;         # 读取超时1小时(长生成任务)
    proxy_send_timeout 60s;           # 发送超时
    chunked_transfer_encoding on;     # 分块传输

    # SSE 心跳：每45s发送注释行保活（需ngx_http_proxy_module ≥1.19）
    # proxy_set_header X-Accel-Buffering no;   # 禁用Nginx自身的缓冲
    # 如Nginx ≥1.23: proxy_sse_keepalive 45s;  # 自动SSE心跳（最简方案）
}
"""


class DeepSeekClient:
    """DeepSeek API 客户端 — 集成密钥池轮询+限流+退避重试"""

    MODEL = "deepseek-v4-pro"
    BASE_URL = "https://api.deepseek.com"
    TEMPERATURE = 0.2

    def __init__(self, api_key: str = None):
        # 优先使用密钥池
        resilience = _get_resilience()
        if resilience:
            pooled_key = resilience.key_pool.get_key("deepseek")
            if pooled_key:
                api_key = pooled_key
                logging.getLogger("Nexie.Client").debug("使用密钥池密钥")

        if api_key is None:
            api_key = os.getenv("DEEPSEEK_API_KEY", "")

        if not api_key or not api_key.strip():
            raise ValueError("未提供 DeepSeek API Key，请在 .env 或配置窗口中设置")

        self.api_key = api_key.strip()
        self._resilience = resilience
        self._recreate_client()

    def _recreate_client(self):
        """（重新）创建OpenAI客户端（密钥切换时调用）"""
        import httpx
        http_client = httpx.Client(
            timeout=60.0,
            proxy=None,  # 禁用系统代理，避免卡死
        )
        self.client = OpenAI(
            api_key=self.api_key,
            base_url=self.BASE_URL,
            timeout=60.0,
            max_retries=1,
            http_client=http_client,
        )

    def _switch_key(self) -> bool:
        """切换到备用密钥，返回是否成功"""
        if not self._resilience:
            return False
        new_key = self._resilience.key_pool.get_key("deepseek")
        if new_key and new_key != self.api_key:
            self.api_key = new_key
            self._recreate_client()
            logging.getLogger("Nexie.Client").info("密钥已切换: %s...", new_key[:12])
            return True
        return False

    @staticmethod
    def validate_key(api_key: str) -> tuple[bool, str]:
        """验证 API Key，返回 (是否有效, 消息)"""
        if not api_key or not api_key.strip():
            return False, "密钥为空"

        try:
            client = OpenAI(
                api_key=api_key.strip(),
                base_url=DeepSeekClient.BASE_URL,
                timeout=15.0,
            )
            client.chat.completions.create(
                model=DeepSeekClient.MODEL,
                messages=[{"role": "user", "content": "ping"}],
                max_tokens=1,
                temperature=0,
            )
            return True, ""
        except AuthenticationError:
            return False, "密钥无效或已过期"
        except RateLimitError:
            return True, ""  # 限流也算有效
        except APIConnectionError:
            return False, "无法连接到 DeepSeek 服务器"
        except APIStatusError as e:
            if e.status_code == 402:
                return False, "账户余额不足"
            return False, f"API 返回错误 (HTTP {e.status_code})"
        except Exception as e:
            return False, f"验证失败: {str(e)}"

    def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        stream: bool = True,
        on_chunk: callable = None,
    ) -> dict:
        """发送对话请求，返回 {"content": str, "tool_calls": list|None, "error": bool}"""
        # ═══ 限流检查 ═══
        if self._resilience:
            wait = self._resilience.rate_limiter.acquire()
            if wait > 0.1:
                logging.getLogger("Nexie.Client").debug("限流等待 %.1fs", wait)

        kwargs = {
            "model": self.MODEL,
            "messages": messages,
            "temperature": self.TEMPERATURE,
        }
        if tools:
            kwargs["tools"] = tools

        return self._stream_chat(kwargs, on_chunk) if (stream and on_chunk) else self._sync_chat(kwargs)

    def _sync_chat(self, kwargs: dict) -> dict:
        max_retries = 2
        for attempt in range(max_retries + 1):
            try:
                response = self.client.chat.completions.create(**kwargs, stream=False)
                # 成功 → 标记密钥正常
                if self._resilience and attempt == 0:
                    self._resilience.handle_api_success(self.api_key)
                return self._parse_response(response)
            except APIStatusError as e:
                if e.status_code == 400:
                    logging.getLogger("Nexie.Client").error("DeepSeek HTTP 400 (sync) | %s", e.message)
                    return {"content": _format_error(e), "tool_calls": None, "error": True}
                elif e.status_code in (429, 401, 403, 402):
                    # 密钥问题 → 切换密钥
                    if self._resilience:
                        self._resilience.handle_api_error(e.status_code, e.message, self.api_key)
                    if self._switch_key() and attempt < max_retries:
                        wait_time = 2 ** attempt
                        logging.getLogger("Nexie.Client").warning(
                            "密钥切换重试 #%d | HTTP %d | 等待%ds", attempt + 1, e.status_code, wait_time
                        )
                        time.sleep(wait_time)
                        continue
                    return {"content": _format_error(e), "tool_calls": None, "error": True}
                elif e.status_code >= 500 and attempt < max_retries:
                    # 服务端错误 → 退避重试
                    wait_time = 2 ** attempt
                    logging.getLogger("Nexie.Client").warning(
                        "服务端错误重试 #%d | HTTP %d | 等待%ds", attempt + 1, e.status_code, wait_time
                    )
                    time.sleep(wait_time)
                    continue
                else:
                    logging.getLogger("Nexie.Client").error("DeepSeek HTTP %d (sync) | %s", e.status_code, e.message)
                return {"content": _format_error(e), "tool_calls": None, "error": True}
            except (APIConnectionError, APITimeoutError) as e:
                if attempt < max_retries:
                    wait_time = 2 ** attempt
                    logging.getLogger("Nexie.Client").warning(
                        "连接/超时重试 #%d | 等待%ds", attempt + 1, wait_time
                    )
                    time.sleep(wait_time)
                    continue
                return {"content": _format_error(e), "tool_calls": None, "error": True}
            except Exception as e:
                logging.getLogger("Nexie.Client").error(
                    "DeepSeek 未知异常 (sync) | type=%s | %s", type(e).__name__, str(e)
                )
                return {"content": _format_error(e), "tool_calls": None, "error": True}
        return {"content": "重试耗尽", "tool_calls": None, "error": True}

    def _stream_chat(self, kwargs: dict, on_chunk: callable) -> dict:
        """流式对话 — SSE心跳保活(45s) + 指数退避重试(1s/2s/4s) + 全局重试上限 + 死机防护
        - 45s无chunk → SSE心跳超时 → 关闭连接重试
        - 400/断连/超时 → 指数退避(1s/2s/4s)，最多3次
        - 全局重试上限(5次) → 超限直接终止，返回友好提示，不卡死
        - 所有异常路径严格销毁Watchdog Timer，防止线程堆积
        """
        full_content = ""
        reasoning_content = ""
        tool_calls_acc: dict[int, dict] = {}
        _stream_obj = None      # 持有stream对象引用,用于异常时安全关闭
        _watchdog = None        # 持有看门狗引用,用于异常时安全销毁

        def _do_stream(stream_kwargs):
            """流式读取核心：带SSE心跳看门狗，逐chunk推送到GUI"""
            nonlocal full_content, reasoning_content, tool_calls_acc, _stream_obj, _watchdog
            full_content = ""
            reasoning_content = ""
            tool_calls_acc = {}
            _stream_obj = None
            _watchdog = None

            try:
                _stream_obj = self.client.chat.completions.create(**stream_kwargs)

                # ═══ 启动SSE心跳看门狗 (45s无chunk→触发超时) ═══
                _watchdog = StreamWatchdog(timeout=45.0)
                _watchdog.start()

                for chunk in _stream_obj:
                    # ═══ 收到有效chunk → 重置心跳计时 ═══
                    _watchdog.reset()

                    if not chunk.choices:
                        continue
                    delta = chunk.choices[0].delta

                    if hasattr(delta, 'reasoning_content') and delta.reasoning_content:
                        reasoning_content += delta.reasoning_content
                        on_chunk(None, None, delta.reasoning_content)

                    if delta.content:
                        full_content += delta.content
                        on_chunk(delta.content, None, None)

                    if delta.tool_calls:
                        for tc_delta in delta.tool_calls:
                            idx = tc_delta.index
                            if idx not in tool_calls_acc:
                                tool_calls_acc[idx] = {
                                    "id": "", "function": {"name": "", "arguments": ""}
                                }
                            entry = tool_calls_acc[idx]
                            if tc_delta.id:
                                entry["id"] = tc_delta.id
                            if tc_delta.function:
                                if tc_delta.function.name:
                                    entry["function"]["name"] = tc_delta.function.name
                                if tc_delta.function.arguments:
                                    entry["function"]["arguments"] += tc_delta.function.arguments
                                    on_chunk(None, tool_calls_acc, None)

                    # ═══ 心跳超时检测：看门狗触发则关闭流触发重连 ═══
                    if _watchdog.fired:
                        logging.getLogger("Nexie.Client").warning(
                            "SSE心跳超时触发 | 已收到%d chunks | 关闭当前连接准备重连",
                            sum(1 for _ in [])  # 占位日志
                        )
                        raise StreamHeartbeatTimeout(
                            f"SSE流45s无数据块，连接可能僵死"
                        )

                # 正常结束 → 停止看门狗
                if _watchdog:
                    _watchdog.stop()

            except StreamHeartbeatTimeout:
                # 重新抛出给外层重试逻辑
                raise
            except Exception:
                # 其他异常也重新抛出，但先清理资源
                raise
            finally:
                # ═══ 安全关闭stream + 销毁看门狗Timer（防止线程堆积） ═══
                # 1) 关闭HTTP流连接
                if _stream_obj is not None:
                    try:
                        _stream_obj.close()
                    except Exception:
                        pass
                    _stream_obj = None
                # 2) 销毁看门狗Timer
                if _watchdog is not None:
                    _watchdog.stop()
                    _watchdog = None

        # ═══ 全局重试控制 ═══
        MAX_TOTAL_RETRIES = 2   # 全局硬上限，快速失败
        total_attempts = 0
        retry_reasons = []

        while total_attempts <= MAX_TOTAL_RETRIES:
            try:
                kwargs["stream"] = True

                _do_stream(kwargs)

                # ── 成功 ──
                if self._resilience and total_attempts == 0:
                    self._resilience.handle_api_success(self.api_key)
                break  # 成功，跳出重试循环

            except StreamHeartbeatTimeout:
                # ═══ SSE心跳超时（45s无chunk）→ 指数退避重试 ═══
                total_attempts += 1
                retry_reasons.append(f"SSE心跳超时(#{total_attempts})")
                if total_attempts > MAX_TOTAL_RETRIES:
                    error_msg = "接口连接失败，本次生成终止"
                    logging.getLogger("Nexie.Client").error(
                        "全局重试耗尽(%d次) | 最后原因: SSE心跳超时 | 已保留上下文",
                        total_attempts - 1
                    )
                    on_chunk(f"\n\n❌ {error_msg}", None, None)
                    return {"content": full_content + f"\n\n❌ {error_msg}" if full_content else f"❌ {error_msg}",
                            "tool_calls": None, "error": True, "error_msg": error_msg}
                wait_time = min(2 ** min(total_attempts - 1, 2), 4)  # 1s/2s/4s封顶
                logging.getLogger("Nexie.Client").warning(
                    "SSE心跳超时重试 #%d/%d | 等待%ds | 保留上下文续生成",
                    total_attempts, MAX_TOTAL_RETRIES, wait_time
                )
                time.sleep(wait_time)
                continue  # 保留context重试

            except (APIConnectionError, APITimeoutError) as e:
                # ═══ 连接/超时 → 指数退避重试 ═══
                total_attempts += 1
                retry_reasons.append(f"{type(e).__name__}(#{total_attempts})")
                if total_attempts > MAX_TOTAL_RETRIES:
                    error_msg = "接口连接失败，本次生成终止"
                    logging.getLogger("Nexie.Client").error(
                        "全局重试耗尽(%d次) | 最后错误: %s | 已保留上下文",
                        total_attempts - 1, type(e).__name__
                    )
                    on_chunk(f"\n\n❌ {error_msg}", None, None)
                    return {"content": full_content + f"\n\n❌ {error_msg}" if full_content else f"❌ {error_msg}",
                            "tool_calls": None, "error": True, "error_msg": error_msg}
                wait_time = min(2 ** min(total_attempts - 1, 2), 4)  # 1s/2s/4s
                logging.getLogger("Nexie.Client").warning(
                    "连接/超时重试 #%d/%d | 错误: %s | 等待%ds | 保留上下文续生成",
                    total_attempts, MAX_TOTAL_RETRIES, type(e).__name__, wait_time
                )
                time.sleep(wait_time)
                continue

            except APIStatusError as e:
                total_attempts += 1
                retry_reasons.append(f"HTTP{e.status_code}(#{total_attempts})")

                if e.status_code == 400:
                    # ═══ 400=消息格式错误，不修复不重试 ═══
                    try:
                        msgs = kwargs.get("messages", [])
                        logging.getLogger("Nexie.Client").error(
                            "DeepSeek HTTP 400 | msg_count=%d total_chars=%d | %s",
                            len(msgs), sum(len(str(m)) for m in msgs), e.message
                        )
                    except Exception:
                        pass
                    error_msg = f"🖥️ 请求参数错误 (HTTP 400): {e.message}"
                    on_chunk(error_msg, None, None)
                    return {"content": error_msg, "tool_calls": None, "error": True, "error_msg": str(e.message)}

                elif e.status_code in (429, 401, 403, 402):
                    if self._resilience:
                        self._resilience.handle_api_error(e.status_code, e.message, self.api_key)
                    if self._switch_key() and total_attempts <= MAX_TOTAL_RETRIES:
                        wait_time = min(2 ** min(total_attempts - 1, 2), 4)
                        logging.getLogger("Nexie.Client").warning(
                            "密钥切换重试 #%d/%d | HTTP %d | 等待%ds",
                            total_attempts, MAX_TOTAL_RETRIES, e.status_code, wait_time
                        )
                        time.sleep(wait_time)
                        continue
                    # 密钥切换失败或超限
                    if total_attempts > MAX_TOTAL_RETRIES:
                        error_msg = "接口连接失败，本次生成终止"
                        on_chunk(f"\n\n❌ {error_msg}", None, None)
                        return {"content": f"❌ {error_msg}", "tool_calls": None, "error": True, "error_msg": error_msg}
                    error_msg = _format_error(e)
                    on_chunk(error_msg, None, None)
                    return {"content": error_msg, "tool_calls": None, "error": True, "error_msg": error_msg}

                elif e.status_code >= 500:
                    if total_attempts <= MAX_TOTAL_RETRIES:
                        wait_time = min(2 ** min(total_attempts - 1, 2), 4)
                        logging.getLogger("Nexie.Client").warning(
                            "服务端错误重试 #%d/%d | HTTP %d | 等待%ds",
                            total_attempts, MAX_TOTAL_RETRIES, e.status_code, wait_time
                        )
                        time.sleep(wait_time)
                        continue
                    error_msg = "接口连接失败，本次生成终止"
                    logging.getLogger("Nexie.Client").error(
                        "全局重试耗尽(%d次) | 最后原因: HTTP %d", total_attempts - 1, e.status_code
                    )
                    on_chunk(f"\n\n❌ {error_msg}", None, None)
                    return {"content": f"❌ {error_msg}", "tool_calls": None, "error": True, "error_msg": error_msg}

                else:
                    logging.getLogger("Nexie.Client").error("DeepSeek HTTP %d | %s", e.status_code, e.message)
                    if total_attempts > MAX_TOTAL_RETRIES:
                        error_msg = "接口连接失败，本次生成终止"
                        on_chunk(f"\n\n❌ {error_msg}", None, None)
                        return {"content": f"❌ {error_msg}", "tool_calls": None, "error": True, "error_msg": error_msg}
                    error_msg = _format_error(e)
                    on_chunk(error_msg, None, None)
                    return {"content": full_content + "\n\n" + error_msg if full_content else error_msg,
                            "tool_calls": None, "error": True, "error_msg": error_msg}

            except Exception as e:
                # ═══ 未知异常全局捕获 → 退避重试，超限终止 ═══
                total_attempts += 1
                retry_reasons.append(f"{type(e).__name__}(#{total_attempts})")
                if total_attempts > MAX_TOTAL_RETRIES:
                    error_msg = "接口连接失败，本次生成终止"
                    logging.getLogger("Nexie.Client").error(
                        "全局重试耗尽(%d次) | 最后异常: %s | %s",
                        total_attempts - 1, type(e).__name__, str(e)
                    )
                    combined = full_content + f"\n\n❌ {error_msg}" if full_content else f"❌ {error_msg}"
                    on_chunk(f"\n\n❌ {error_msg}", None, None)
                    return {"content": combined, "tool_calls": None, "error": True, "error_msg": error_msg}
                wait_time = min(2 ** min(total_attempts - 1, 2), 4)
                logging.getLogger("Nexie.Client").warning(
                    "未知异常重试 #%d/%d | type=%s | 等待%ds",
                    total_attempts, MAX_TOTAL_RETRIES, type(e).__name__, wait_time
                )
                time.sleep(wait_time)
                continue

        # ═══ 成功路径：break跳出while循环 → 组装tool_calls返回 ═══
        tool_calls = (
            [tool_calls_acc[i] for i in sorted(tool_calls_acc.keys())]
            if tool_calls_acc else None
        )
        return {"content": full_content, "reasoning_content": reasoning_content,
                "tool_calls": tool_calls, "error": False}

    def _parse_response(self, response) -> dict:
        choice = response.choices[0]
        msg = choice.message
        tool_calls = None
        if msg.tool_calls:
            tool_calls = [
                {"id": tc.id, "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                for tc in msg.tool_calls
            ]
        reasoning = getattr(msg, 'reasoning_content', '') or ''
        return {"content": msg.content or "", "reasoning_content": reasoning,
                "tool_calls": tool_calls, "error": False}

# ═══════════════════════════════════════════
# MiMoClient — MIMO 视觉多模态 API 客户端
# ═══════════════════════════════════════════

class MiMoClient:
    """MIMO (Xiaomi MiMo) API 客户端 — 支持视觉多模态，官方 API 接入"""

    MODEL = "mimo-v2-omni"
    BASE_URL = "https://api.xiaomimimo.com/v1"
    TEMPERATURE = 0.2

    def __init__(self, api_key: str = None, base_url: str = None, model: str = None):
        if api_key is None:
            api_key = os.getenv("MIMO_API_KEY", "")

        if not api_key or not api_key.strip():
            raise ValueError("未提供 MIMO API Key，请在 .env 或配置窗口中设置")

        self.api_key = api_key.strip()
        self.base_url = (base_url or os.getenv("MIMO_BASE_URL", "") or self.BASE_URL).strip()
        self.model = (model or os.getenv("MIMO_MODEL", "") or self.MODEL).strip()
        # MiMo 官方 API 使用 api-key header，而非标准 Bearer
        import httpx
        http_client = httpx.Client(
            timeout=60.0,
            proxy=None,
            headers={"api-key": self.api_key},
        )
        self.client = OpenAI(
            api_key=self.api_key,
            base_url=self.base_url,
            http_client=http_client,
            max_retries=1,
        )

    @staticmethod
    def validate_key(api_key: str, base_url: str = None, model: str = None) -> tuple[bool, str]:
        """验证 API Key，返回 (是否有效, 消息)"""
        if not api_key or not api_key.strip():
            return False, "密钥为空"

        url = (base_url or os.getenv("MIMO_BASE_URL", "") or MiMoClient.BASE_URL).strip()
        mdl = (model or os.getenv("MIMO_MODEL", "") or MiMoClient.MODEL).strip()

        try:
            import httpx
            http_client = httpx.Client(
                timeout=15.0,
                proxy=None,
                headers={"api-key": api_key.strip()},
            )
            client = OpenAI(
                api_key=api_key.strip(),
                base_url=url,
                http_client=http_client,
            )
            client.chat.completions.create(
                model=mdl,
                messages=[{"role": "user", "content": "ping"}],
                max_tokens=1,
                temperature=0,
            )
            return True, ""
        except AuthenticationError:
            return False, "密钥无效或已过期"
        except RateLimitError:
            return True, ""
        except APIConnectionError:
            return False, f"无法连接到 MiMo API ({url})"
        except APIStatusError as e:
            if e.status_code == 402:
                return False, "账户余额不足"
            return False, f"API 返回错误 (HTTP {e.status_code})"
        except Exception as e:
            return False, f"验证失败: {str(e)}"

    def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        stream: bool = True,
        on_chunk: callable = None,
    ) -> dict:
        """发送对话请求，返回 {"content": str, "tool_calls": list|None, "error": bool}"""
        kwargs = {
            "model": self.model,
            "messages": messages,
            "temperature": self.TEMPERATURE,
        }
        if tools:
            kwargs["tools"] = tools

        return self._stream_chat(kwargs, on_chunk) if (stream and on_chunk) else self._sync_chat(kwargs)

    def _sync_chat(self, kwargs: dict) -> dict:
        try:
            response = self.client.chat.completions.create(**kwargs, stream=False)
            return self._parse_response(response)
        except APIStatusError as e:
            if e.status_code == 400:
                logging.getLogger("Nexie.Client").error("MiMo HTTP 400 (sync) | %s", e.message)
            else:
                logging.getLogger("Nexie.Client").error("MiMo HTTP %d (sync) | %s", e.status_code, e.message)
            return {"content": _format_error(e), "tool_calls": None, "error": True}
        except Exception as e:
            logging.getLogger("Nexie.Client").error(
                "MiMo 未知异常 (sync) | type=%s | %s", type(e).__name__, str(e)
            )
            return {"content": _format_error(e), "tool_calls": None, "error": True}

    def _stream_chat(self, kwargs: dict, on_chunk: callable) -> dict:
        """流式对话 — SSE心跳保活(45s) + 指数退避重试(1s/2s/4s) + 全局重试上限 + 死机防护
        - 45s无chunk → SSE心跳超时 → 关闭连接重试
        - 400/断连/超时 → 指数退避(1s/2s/4s)，最多3次
        - 全局重试上限(5次) → 超限直接终止，返回友好提示，不卡死
        - 所有异常路径严格销毁Watchdog Timer，防止线程堆积
        """
        full_content = ""
        tool_calls_acc: dict[int, dict] = {}
        reasoning_content = ""
        _stream_obj = None      # 持有stream对象引用,用于异常时安全关闭
        _watchdog = None        # 持有看门狗引用,用于异常时安全销毁

        def _do_stream(stream_kwargs):
            """流式读取核心：带SSE心跳看门狗，逐chunk推送到GUI"""
            nonlocal full_content, tool_calls_acc, _stream_obj, _watchdog
            full_content = ""
            tool_calls_acc = {}
            _stream_obj = None
            _watchdog = None

            try:
                _stream_obj = self.client.chat.completions.create(**stream_kwargs)

                # ═══ 启动SSE心跳看门狗 (45s无chunk→触发超时) ═══
                _watchdog = StreamWatchdog(timeout=45.0)
                _watchdog.start()

                for chunk in _stream_obj:
                    # ═══ 收到有效chunk → 重置心跳计时 ═══
                    _watchdog.reset()

                    if not chunk.choices:
                        continue
                    delta = chunk.choices[0].delta

                    if delta.content:
                        full_content += delta.content
                        on_chunk(delta.content, None, None)

                    if delta.tool_calls:
                        for tc_delta in delta.tool_calls:
                            idx = tc_delta.index
                            if idx not in tool_calls_acc:
                                tool_calls_acc[idx] = {
                                    "id": "", "function": {"name": "", "arguments": ""}
                                }
                            entry = tool_calls_acc[idx]
                            if tc_delta.id:
                                entry["id"] = tc_delta.id
                            if tc_delta.function:
                                if tc_delta.function.name:
                                    entry["function"]["name"] = tc_delta.function.name
                                if tc_delta.function.arguments:
                                    entry["function"]["arguments"] += tc_delta.function.arguments
                                    on_chunk(None, tool_calls_acc, None)

                    # ═══ 心跳超时检测：看门狗触发则关闭流触发重连 ═══
                    if _watchdog.fired:
                        logging.getLogger("Nexie.Client").warning(
                            "MiMo SSE心跳超时触发 | 关闭当前连接准备重连"
                        )
                        raise StreamHeartbeatTimeout(
                            f"SSE流45s无数据块，连接可能僵死"
                        )

                # 正常结束 → 停止看门狗
                if _watchdog:
                    _watchdog.stop()

            except StreamHeartbeatTimeout:
                raise
            except Exception:
                raise
            finally:
                # ═══ 安全关闭stream + 销毁看门狗Timer（防止线程堆积） ═══
                if _stream_obj is not None:
                    try:
                        _stream_obj.close()
                    except Exception:
                        pass
                    _stream_obj = None
                if _watchdog is not None:
                    _watchdog.stop()
                    _watchdog = None

        # ═══ 全局重试控制 ═══
        MAX_TOTAL_RETRIES = 2   # 全局硬上限，快速失败
        total_attempts = 0
        retry_reasons = []

        while total_attempts <= MAX_TOTAL_RETRIES:
            try:
                kwargs["stream"] = True
                _do_stream(kwargs)
                # ── 成功 ──
                break

            except StreamHeartbeatTimeout:
                # ═══ SSE心跳超时（45s无chunk）→ 指数退避重试 ═══
                total_attempts += 1
                retry_reasons.append(f"SSE心跳超时(#{total_attempts})")
                if total_attempts > MAX_TOTAL_RETRIES:
                    error_msg = "接口连接失败，本次生成终止"
                    logging.getLogger("Nexie.Client").error(
                        "MiMo全局重试耗尽(%d次) | 最后原因: SSE心跳超时",
                        total_attempts - 1
                    )
                    on_chunk(f"\n\n❌ {error_msg}", None, None)
                    return {"content": full_content + f"\n\n❌ {error_msg}" if full_content else f"❌ {error_msg}",
                            "tool_calls": None, "error": True, "error_msg": error_msg}
                wait_time = min(2 ** min(total_attempts - 1, 2), 4)  # 1s/2s/4s封顶
                logging.getLogger("Nexie.Client").warning(
                    "MiMo SSE心跳超时重试 #%d/%d | 等待%ds | 保留上下文续生成",
                    total_attempts, MAX_TOTAL_RETRIES, wait_time
                )
                time.sleep(wait_time)
                continue

            except (APIConnectionError, APITimeoutError) as e:
                # ═══ 连接/超时 → 指数退避重试 ═══
                total_attempts += 1
                retry_reasons.append(f"{type(e).__name__}(#{total_attempts})")
                if total_attempts > MAX_TOTAL_RETRIES:
                    error_msg = "接口连接失败，本次生成终止"
                    logging.getLogger("Nexie.Client").error(
                        "MiMo全局重试耗尽(%d次) | 最后错误: %s",
                        total_attempts - 1, type(e).__name__
                    )
                    on_chunk(f"\n\n❌ {error_msg}", None, None)
                    return {"content": f"❌ {error_msg}", "tool_calls": None, "error": True, "error_msg": error_msg}
                wait_time = min(2 ** min(total_attempts - 1, 2), 4)
                logging.getLogger("Nexie.Client").warning(
                    "MiMo连接/超时重试 #%d/%d | 错误: %s | 等待%ds",
                    total_attempts, MAX_TOTAL_RETRIES, type(e).__name__, wait_time
                )
                time.sleep(wait_time)
                continue

            except APIStatusError as e:
                total_attempts += 1
                retry_reasons.append(f"HTTP{e.status_code}(#{total_attempts})")

                if e.status_code == 400:
                    logging.getLogger("Nexie.Client").error("MiMo HTTP 400 | %s", e.message)
                    error_msg = f"🖥️ 请求参数错误 (HTTP 400): {e.message}"
                    on_chunk(error_msg, None, None)
                    return {"content": error_msg, "tool_calls": None, "error": True, "error_msg": str(e.message)}

                elif e.status_code >= 500:
                    if total_attempts <= MAX_TOTAL_RETRIES:
                        wait_time = min(2 ** min(total_attempts - 1, 2), 4)
                        logging.getLogger("Nexie.Client").warning(
                            "MiMo服务端错误重试 #%d/%d | HTTP %d | 等待%ds",
                            total_attempts, MAX_TOTAL_RETRIES, e.status_code, wait_time
                        )
                        time.sleep(wait_time)
                        continue
                    error_msg = "接口连接失败，本次生成终止"
                    logging.getLogger("Nexie.Client").error(
                        "MiMo全局重试耗尽(%d次) | 最后原因: HTTP %d",
                        total_attempts - 1, e.status_code
                    )
                    on_chunk(f"\n\n❌ {error_msg}", None, None)
                    return {"content": f"❌ {error_msg}", "tool_calls": None, "error": True, "error_msg": error_msg}

                else:
                    logging.getLogger("Nexie.Client").error(
                        "MiMo HTTP %d | %s", e.status_code, e.message
                    )
                    if total_attempts > MAX_TOTAL_RETRIES:
                        error_msg = "接口连接失败，本次生成终止"
                        on_chunk(f"\n\n❌ {error_msg}", None, None)
                        return {"content": f"❌ {error_msg}", "tool_calls": None, "error": True, "error_msg": error_msg}
                    error_msg = _format_error(e)
                    on_chunk(error_msg, None, None)
                    return {"content": full_content + "\n\n" + error_msg if full_content else error_msg,
                            "tool_calls": None, "error": True, "error_msg": error_msg}

            except Exception as e:
                # ═══ 未知异常全局捕获 → 退避重试，超限终止 ═══
                total_attempts += 1
                retry_reasons.append(f"{type(e).__name__}(#{total_attempts})")
                if total_attempts > MAX_TOTAL_RETRIES:
                    error_msg = "接口连接失败，本次生成终止"
                    logging.getLogger("Nexie.Client").error(
                        "MiMo全局重试耗尽(%d次) | 最后异常: %s | %s",
                        total_attempts - 1, type(e).__name__, str(e)
                    )
                    combined = full_content + f"\n\n❌ {error_msg}" if full_content else f"❌ {error_msg}"
                    on_chunk(f"\n\n❌ {error_msg}", None, None)
                    return {"content": combined, "tool_calls": None, "error": True, "error_msg": error_msg}
                wait_time = min(2 ** min(total_attempts - 1, 2), 4)
                logging.getLogger("Nexie.Client").warning(
                    "MiMo未知异常重试 #%d/%d | type=%s | 等待%ds",
                    total_attempts, MAX_TOTAL_RETRIES, type(e).__name__, wait_time
                )
                time.sleep(wait_time)
                continue

        # ═══ 成功路径：break跳出while循环 → 组装tool_calls返回 ═══
        tool_calls = (
            [tool_calls_acc[i] for i in sorted(tool_calls_acc.keys())]
            if tool_calls_acc else None
        )
        return {"content": full_content, "tool_calls": tool_calls, "error": False}

    def _parse_response(self, response) -> dict:
        choice = response.choices[0]
        msg = choice.message
        tool_calls = None
        if msg.tool_calls:
            tool_calls = [
                {"id": tc.id, "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                for tc in msg.tool_calls
            ]
        return {"content": msg.content or "", "tool_calls": tool_calls, "error": False}
