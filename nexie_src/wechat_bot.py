# Nexie — AI Coding Agent
# Copyright (c) 2026. All rights reserved.
# 本软件仅供个人学习与研究使用，禁止未经授权的商业用途、分发或修改后闭源。
# Licensed for personal, educational, and non-commercial use only.
"""
微信 ClawBot 接入模块 — iLink Bot API
使用腾讯官方 iLink Bot API，合法接入个人微信（无需 OpenClaw）
- 扫码登录
- 长轮询接收消息（文本/图片/文件）
- 发送消息/图片/文件
- CDN 图片下载（AES-128-ECB 解密）
- Token 持久化
- 与 AgentCore / MainWindow 集成
"""
import json
import time
import base64
import random
import threading
import logging
import ssl
import socket
import http.client
from pathlib import Path
from urllib.parse import urlparse

logger = logging.getLogger("OpAgent.WeChatBot")

# AES 解密（图片 CDN 下载用）
try:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    _CRYPTO_AVAILABLE = True
except ImportError:
    _CRYPTO_AVAILABLE = False

# ═══ iLink API 常量 ═══
BASE_URL = "https://ilinkai.weixin.qq.com"
BOT_TYPE = 3
LONGPOLL_TIMEOUT = 40       # HTTP 读超时（比服务端 35s 多 5s）
REQUEST_TIMEOUT = 15        # 普通请求超时

# ═══ 消息配额 ═══
MAX_REPLIES_PER_USER_MSG = 8   # 官方 10 条，保守用 8 条


def _generate_uin() -> str:
    """生成随机 X-WECHAT-UIN 头（uint32 → 十进制字符串 → base64）"""
    uin = random.randint(1, 2**32 - 1)
    return base64.b64encode(str(uin).encode()).decode()


def _build_headers(bot_token: str) -> dict:
    """构建 iLink API 请求头"""
    return {
        "Content-Type": "application/json",
        "AuthorizationType": "ilink_bot_token",
        "X-WECHAT-UIN": _generate_uin(),
        "Authorization": f"Bearer {bot_token}",
    }


# ═══════════════════════════════════════════
# 微信 Bot 主类
# ═══════════════════════════════════════════

class WeChatBot:
    """
    微信 ClawBot 接入层

    数据流:
      微信用户 → iLink API → WeChatBot → AgentCore.process_message() → WeChatBot → send_message → 微信用户

    架构:
      - 长轮询线程: POST /getupdates (hold 35s) → 收消息 → 派发 Agent 线程
      - Agent 线程: 处理消息，缓冲完整回复，发送到微信
      - 发送线程安全: send_message() 可被任意线程调用
    """

    def __init__(self, data_dir: Path = None):
        if data_dir is None:
            from nexie import get_data_dir
            data_dir = get_data_dir()
        self._data_dir = Path(data_dir)
        self._data_dir.mkdir(parents=True, exist_ok=True)
        self._config_path = self._data_dir / "wechat_bot.json"

        # ── 绑定对象 ──
        self._agent = None           # AgentCore
        self._gui = None             # MainWindow
        self._lock = threading.Lock()

        # ── 持久化状态 ──
        self._bot_token: str = ""
        self._ilink_bot_id: str = ""
        self._ilink_user_id: str = ""
        self._baseurl: str = BASE_URL

        # ── 运行时状态 ──
        self._running = False
        self._poll_thread = None  # threading.Thread or None
        self._cursor = ""            # getupdates 游标，防重复

        # ── 对话上下文（user_id → context_token） ──
        self._context_tokens: dict[str, str] = {}

        # ── 消息配额（user_id → 剩余回复数） ──
        self._reply_quotas: dict[str, int] = {}

        # ── 正在处理的用户集合（避免重复处理） ──
        self._processing_users: set = set()

        # 加载持久化配置
        self._load_config()

    # ═══ 持久化 ═══
    def _load_config(self):
        """从磁盘加载 bot_token 等持久化状态"""
        try:
            if self._config_path.exists():
                data = json.loads(self._config_path.read_text(encoding="utf-8"))
                self._bot_token = data.get("bot_token", "")
                self._ilink_bot_id = data.get("ilink_bot_id", "")
                self._ilink_user_id = data.get("ilink_user_id", "")
                self._baseurl = data.get("baseurl", BASE_URL)
                self._cursor = data.get("cursor", "")
                if self._bot_token:
                    logger.info(f"[微信] 已加载持久化令牌: bot_id={self._ilink_bot_id}")
        except Exception as e:
            logger.warning(f"[微信] 加载配置失败: {e}")

    def _save_config(self):
        """持久化当前状态到磁盘"""
        try:
            data = {
                "bot_token": self._bot_token,
                "ilink_bot_id": self._ilink_bot_id,
                "ilink_user_id": self._ilink_user_id,
                "baseurl": self._baseurl,
                "cursor": self._cursor,
                "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
            self._config_path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            logger.error(f"[微信] 保存配置失败: {e}")

    # ═══ HTTP 请求 ═══
    def _http_request(self, method: str, path: str, data: dict = None,
                      timeout: int = REQUEST_TIMEOUT, host: str = None) -> dict:
        """
        统一 HTTP 请求，基于 http.client（比 urllib 更好控制长轮询超时）
        长轮询时 socket timeout = 服务端 hold 时间 + 5s 余量
        """
        base = host or self._baseurl
        parsed = urlparse(base)
        hostname = parsed.hostname
        port = parsed.port or 443
        is_ssl = parsed.scheme == "https"

        body = json.dumps(data, ensure_ascii=False).encode("utf-8") if data else None
        headers = _build_headers(self._bot_token) if (host is None and self._bot_token) else {
            "Content-Type": "application/json",
        }
        if body:
            headers["Content-Length"] = str(len(body))

        conn = None
        try:
            ctx = ssl.create_default_context()
            if is_ssl:
                conn = http.client.HTTPSConnection(hostname, port, context=ctx, timeout=timeout)
            else:
                conn = http.client.HTTPConnection(hostname, port, timeout=timeout)

            # socket.setblocking(True) + settimeout 确保 read 阻塞等待
            conn.connect()
            sock = conn.sock
            if sock:
                sock.settimeout(timeout)

            conn.request(method, path, body=body, headers=headers)
            resp = conn.getresponse()
            resp_body = resp.read().decode("utf-8")

            if resp.status >= 400:
                logger.error(f"[微信] {method} {path} 失败: HTTP {resp.status} — {resp_body[:200]}")
                return {"ret": -1, "error": f"HTTP {resp.status}", "detail": resp_body[:200]}

            return json.loads(resp_body)

        except (socket.timeout, TimeoutError, OSError) as e:
            # 长轮询超时是正常行为（服务端 hold 35s 无消息超时）
            err_name = type(e).__name__
            if isinstance(e, OSError) and hasattr(e, 'errno') and e.errno:
                pass  # 有其他 errno 的 OSError
            return {"ret": -1, "error": f"{err_name}: {e}", "timeout": True}
        except (http.client.HTTPException, ssl.SSLError) as e:
            logger.error(f"[微信] {method} {path} 连接失败: {e}")
            return {"ret": -1, "error": str(e)}
        except json.JSONDecodeError as e:
            logger.error(f"[微信] {method} {path} JSON解析失败: {e}")
            return {"ret": -1, "error": f"JSON解析失败: {e}"}
        except Exception as e:
            logger.error(f"[微信] {method} {path} 异常: {type(e).__name__}: {e}")
            return {"ret": -1, "error": f"{type(e).__name__}: {e}"}
        finally:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass

    def _post(self, path: str, data: dict, timeout: int = REQUEST_TIMEOUT) -> dict:
        """POST 请求到 iLink API"""
        return self._http_request("POST", path, data=data, timeout=timeout)

    def _get(self, path: str, timeout: int = REQUEST_TIMEOUT) -> dict:
        """GET 请求到 iLink API（登录阶段使用，无需 auth）"""
        return self._http_request("GET", path, timeout=timeout, host=BASE_URL)

    # ═══ CDN 文件下载 ═══
    def _download_cdn_file(self, cdn_url: str, aes_key: str = "") -> bytes:
        """
        从微信 CDN 下载文件，可选 AES-128-ECB 解密
        返回文件二进制数据
        """
        parsed = urlparse(cdn_url)
        hostname = parsed.hostname
        path = parsed.path + ("?" + parsed.query if parsed.query else "")

        ctx = ssl.create_default_context()
        conn = http.client.HTTPSConnection(hostname, 443, context=ctx, timeout=30)
        try:
            conn.request("GET", path)
            resp = conn.getresponse()
            data = resp.read()

            # AES-128-ECB 解密（图片用）
            if aes_key and _CRYPTO_AVAILABLE:
                try:
                    key_bytes = aes_key.encode("utf-8") if isinstance(aes_key, str) else aes_key
                    if len(key_bytes) < 16:
                        key_bytes = key_bytes.ljust(16, b"\x00")
                    key_bytes = key_bytes[:16]
                    cipher = Cipher(algorithms.AES(key_bytes), modes.ECB())
                    decryptor = cipher.decryptor()
                    data = decryptor.update(data) + decryptor.finalize()
                    # 去除 PKCS7 padding
                    pad_len = data[-1]
                    if 0 < pad_len <= 16:
                        data = data[:-pad_len]
                except Exception as e:
                    logger.warning(f"[微信] AES解密失败: {e}，使用原始数据")

            return data
        finally:
            conn.close()

    def _save_media(self, data: bytes, prefix: str, ext: str = ".jpg") -> Path:
        """保存媒体文件到 Iagent_data/uploads"""
        uploads = self._data_dir / "uploads"
        uploads.mkdir(parents=True, exist_ok=True)
        ts = int(time.time() * 1000)
        filename = f"{prefix}_{ts}{ext}"
        filepath = uploads / filename
        filepath.write_bytes(data)
        logger.info(f"[微信] 媒体文件已保存: {filepath} ({len(data)} bytes)")
        return filepath

    # ═══ 登录流程 ═══
    def get_login_qrcode(self) -> dict:
        """
        步骤1: 获取登录二维码
        返回: {"qrcode": "...", "qrcode_img_url": "...", "ret": 0}
        """
        result = self._get(f"/ilink/bot/get_bot_qrcode?bot_type={BOT_TYPE}")
        if result.get("ret") == 0:
            qrcode_id = result.get("qrcode", "")
            qrcode_url = result.get("qrcode_img_content", "")
            logger.info(f"[微信] 获取二维码成功: {qrcode_id}")
            return {
                "success": True,
                "qrcode": qrcode_id,
                "qrcode_url": qrcode_url,
            }
        return {"success": False, "error": result.get("error", "获取二维码失败")}

    def poll_login_status(self, qrcode_id: str) -> dict:
        """
        步骤2: 轮询扫码状态
        返回状态: "pending" | "scanned" | "confirmed" | "expired" | "error"
        """
        result = self._get(f"/ilink/bot/get_qrcode_status?qrcode={qrcode_id}")
        ret = result.get("ret", -1)
        status = result.get("status", "")

        if ret == 0 and status == "confirmed":
            self._bot_token = result.get("bot_token", "")
            self._ilink_bot_id = result.get("ilink_bot_id", "")
            self._ilink_user_id = result.get("ilink_user_id", "")
            self._baseurl = result.get("baseurl", BASE_URL)
            self._cursor = ""
            self._save_config()
            logger.info(f"[微信] 登录成功: {self._ilink_bot_id}")
            return {"success": True, "status": "confirmed", "bot_id": self._ilink_bot_id}
        elif ret == 0 and status == "scanned":
            return {"success": True, "status": "scanned"}
        elif ret == 0 and status in ("pending", "waiting"):
            return {"success": True, "status": "pending"}
        elif status == "expired":
            return {"success": False, "status": "expired", "error": "二维码已过期"}
        else:
            return {"success": False, "status": "error", "error": result.get("error", "未知错误")}

    def login_with_callback(self, on_qrcode: callable, on_status: callable) -> bool:
        """
        完整登录流程（阻塞式，在独立线程中调用）
        on_qrcode(qrcode_url, qrcode_id) — 显示二维码
        on_status(status_text) — 更新状态文字
        返回: True=成功, False=失败
        """
        # 获取二维码
        qr = self.get_login_qrcode()
        if not qr["success"]:
            on_status(f"❌ 获取二维码失败: {qr.get('error', '')}")
            return False

        on_qrcode(qr["qrcode_url"], qr["qrcode"])
        on_status("📱 请用微信扫描二维码")

        # 轮询等待扫码确认（最长 5 分钟）
        deadline = time.time() + 300
        while time.time() < deadline:
            time.sleep(2)
            status = self.poll_login_status(qr["qrcode"])
            s = status.get("status", "")

            if s == "confirmed":
                on_status("✅ 登录成功！微信已连接")
                self._notify_gui_status(True)
                self.start_polling()
                return True
            elif s == "scanned":
                on_status("📱 已扫描，请在手机上确认...")
            elif s == "expired":
                on_status("⚠️ 二维码已过期")
                return False
            elif s == "error":
                on_status(f"❌ 登录失败: {status.get('error', '')}")
                return False
            # pending: 继续等待

        on_status("⏰ 登录超时（5分钟）")
        return False

    # ═══ 消息收发 ═══
    def start_polling(self):
        """启动长轮询消息接收线程"""
        if not self._bot_token:
            logger.warning("[微信] 未登录，无法启动轮询")
            return
        if self._running:
            return
        self._running = True
        self._poll_thread = threading.Thread(
            target=self._poll_loop, name="WeChatBot-Poll", daemon=True
        )
        self._poll_thread.start()
        logger.info("[微信] 长轮询已启动")

    def stop_polling(self):
        """停止长轮询"""
        self._running = False
        if self._poll_thread and self._poll_thread.is_alive():
            self._poll_thread.join(timeout=5.0)
        logger.info("[微信] 长轮询已停止")

    def _poll_loop(self):
        """长轮询循环：POST /getupdates (hold 35s) → 接收消息 → 派发处理"""
        logger.info("[微信] 长轮询循环开始")
        consecutive_errors = 0

        while self._running:
            try:
                data = {
                    "get_updates_buf": self._cursor,
                    "base_info": {"channel_version": "1.0.2"},
                }
                result = self._post(
                    "/ilink/bot/getupdates", data,
                    timeout=LONGPOLL_TIMEOUT + 10,
                )

                if not self._running:
                    break

                ret = result.get("ret", 0)  # 成功时不返回 ret 字段，默认 0
                if ret != 0:
                    err = result.get("error", "")
                    is_timeout = result.get("timeout", False)

                    # 长轮询超时 = 正常（服务端 hold 35s 无消息主动断开）
                    if is_timeout:
                        consecutive_errors = 0
                        continue  # 立即重试，不 sleep

                    logger.warning(f"[微信] getupdates 错误(ret={ret}): {err[:100]}")
                    consecutive_errors += 1

                    # token 失效
                    if "auth" in err.lower() or "token" in err.lower():
                        logger.error("[微信] Token 失效，停止轮询")
                        self._bot_token = ""
                        self._save_config()
                        self._notify_gui_status(False)
                        break

                    # 连续错误过多
                    if consecutive_errors > 10:
                        logger.error("[微信] 连续错误过多，停止轮询")
                        break

                    time.sleep(min(consecutive_errors * 2, 30))
                    continue

                consecutive_errors = 0

                # 更新游标
                new_cursor = result.get("get_updates_buf", "")
                if new_cursor:
                    self._cursor = new_cursor

                # 处理消息
                msgs = result.get("msgs", [])
                if msgs:
                    logger.info(f"[微信] 收到 {len(msgs)} 条消息")
                for msg in msgs:
                    self._handle_incoming_message(msg)

            except Exception as e:
                if self._running:
                    logger.error(f"[微信] 轮询异常: {type(e).__name__}: {e}")
                    consecutive_errors += 1
                    time.sleep(min(consecutive_errors * 2, 30))

        logger.info("[微信] 长轮询循环结束")

    def _handle_incoming_message(self, msg: dict):
        """处理收到的微信消息"""
        try:
            from_user = msg.get("from_user_id", "")
            to_user = msg.get("to_user_id", "")
            msg_type = int(msg.get("message_type", 0))
            context_token = msg.get("context_token", "")
            msg_id = msg.get("msg_id", "")

            logger.info(f"[微信] 收到消息: type={msg_type}, from={from_user[:30]}..., items={len(msg.get('item_list', []))}")

            # 只处理用户发给 bot 的消息 (message_type=1)
            if msg_type != 1:
                logger.debug(f"[微信] 跳过非用户消息 type={msg_type}")
                return

            # 保存 context_token 用于回复
            if context_token and from_user:
                self._context_tokens[from_user] = context_token
            # 重置配额（用户新消息 = 配额重置）
            self._reply_quotas[from_user] = MAX_REPLIES_PER_USER_MSG

            # 解析消息内容
            item_list = msg.get("item_list", [])
            text_parts = []
            media_files = []       # (type, filename, filepath) — 已下载
            media_meta = []        # str — 未下载文件的元信息

            for item in item_list:
                item_type = item.get("type", 0)
                if item_type == 1:  # 文本
                    text = item.get("text_item", {}).get("text", "")
                    if text:
                        text_parts.append(text)
                elif item_type == 2:  # 图片 — 从 CDN 下载
                    img_item = item.get("image_item", {})
                    cdn_url = img_item.get("cdn_url", "")
                    aes_key = img_item.get("aes_key", "")
                    if cdn_url:
                        try:
                            logger.info(f"[微信] 下载图片: {cdn_url[:60]}...")
                            img_data = self._download_cdn_file(cdn_url, aes_key)
                            fp = self._save_media(img_data, "wechat_img", ".jpg")
                            media_files.append(("image", fp.name, str(fp)))
                            text_parts.append(f"[图片已接收: {fp.name}]")
                        except Exception as e:
                            logger.error(f"[微信] 图片下载失败: {e}")
                            text_parts.append("[图片(下载失败)]")
                    else:
                        text_parts.append("[图片]")
                elif item_type == 3:  # 语音
                    text_parts.append("[语音]")
                elif item_type == 4:  # 文件
                    file_item = item.get("file_item", {})
                    filename = file_item.get("filename", "文件")
                    cdn_url = file_item.get("cdn_url", "") or file_item.get("url", "")
                    # 打印完整 file_item 以便调试
                    logger.info(f"[微信] file_item keys: {list(file_item.keys())}, "
                                f"filename={filename}, cdn_url={'有' if cdn_url else '无'}")
                    if cdn_url:
                        try:
                            logger.info(f"[微信] 下载文件: {filename} ({cdn_url[:60]}...)")
                            file_data = self._download_cdn_file(cdn_url)
                            ext = Path(filename).suffix or ".bin"
                            fp = self._save_media(file_data, "wechat_file", ext)
                            media_files.append(("file", filename, str(fp)))
                            text_parts.append(f"[文件已接收: {filename} → {fp.name}]")
                        except Exception as e:
                            logger.error(f"[微信] 文件下载失败: {e}")
                            text_parts.append(f"[文件: {filename}(下载失败)]")
                    else:
                        text_parts.append(f"[文件: {filename}]")
                        # 无法下载时，记录元信息让 AI 尝试在本地查找
                        media_meta.append(
                            f"[微信文件] 文件名: {filename}, "
                            f"字段: {json.dumps({k:v for k,v in file_item.items() if k!='data'}, ensure_ascii=False)[:200]}"
                        )

            full_text = "".join(text_parts).strip()
            if not full_text:
                return

            logger.info(f"[微信] 收到消息: from={from_user[:20]}... text={full_text[:80]}")

            # 记录媒体文件并注入 Agent 上下文
            media_context_parts = list(media_meta)  # 先加未下载文件的元信息
            if media_files:
                for t, name, filepath in media_files:
                    try:
                        fp = Path(filepath)
                        fsize = fp.stat().st_size
                        media_context_parts.append(
                            f"[微信收到的{'图片' if t == 'image' else '文件'}] 文件名: {name}, "
                            f"大小: {fsize} bytes, 保存路径: {filepath}"
                        )
                        # 自动读取文本文件内容注入上下文
                        if t == "file":
                            text_exts = {'.txt', '.py', '.js', '.ts', '.java', '.c', '.cpp', '.h',
                                         '.json', '.xml', '.yaml', '.yml', '.md', '.csv', '.log',
                                         '.html', '.css', '.sql', '.sh', '.bat', '.ini', '.cfg', '.toml'}
                            if fp.suffix.lower() in text_exts and fsize < 100_000:
                                try:
                                    content = fp.read_text(encoding="utf-8", errors="replace")
                                    media_context_parts.append(
                                        f"\n--- 文件内容 ({name}) ---\n{content}\n--- 内容结束 ---"
                                    )
                                    logger.info(f"[微信] 已自动读取文本文件: {name} ({len(content)} 字)")
                                except Exception:
                                    pass
                        # 图片：MIMO 模型下次截图时会自动分析；DeepSeek 给路径
                        if t == "image" and self._agent:
                            media_context_parts.append(
                                f"(提示: 你可以用 read_file 读取此图片路径，"
                                f"MIMO 模型支持视觉识别，DeepSeek 模型只能读取文件字节)"
                            )
                    except Exception as e:
                        logger.error(f"[微信] 读取媒体文件失败: {e}")

            # 注入到 AgentCore
            if media_context_parts:
                injected = "[系统提示] 以下是通过微信收到的文件，请根据需要读取和处理：\n" + "\n".join(media_context_parts)
                if self._agent:
                    self._agent._safe_append({"role": "user", "content": injected})
                full_text = full_text + "\n" + injected[:500]

            # GUi 通知
            if self._gui:
                try:
                    self._gui.gui_queue.put(("wechat_message", f"💬 微信: {full_text[:100]}", from_user))
                    self._gui.gui_queue.put(("wechat_status", "typing"))
                    # 显示收到的文件列表
                    if media_files:
                        for t, name, path in media_files:
                            self._gui.gui_queue.put(("mobile_file", name, path,
                                                     "image/jpeg" if t == "image" else "application/octet-stream"))
                except Exception:
                    pass

            # 交给 AgentCore 处理（异步，避免阻塞轮询）
            if self._agent:
                threading.Thread(
                    target=self._process_with_agent,
                    args=(full_text, from_user),
                    daemon=True,
                ).start()

        except Exception as e:
            logger.error(f"[微信] 处理消息异常: {e}")

    def _process_with_agent(self, user_text: str, user_id: str):
        """
        在独立线程中使用 AgentCore 处理微信消息
        - 先发「正在思考…」确认
        - 拦截 send_screenshot / send_file / send_text → 自动转发微信
        - 流式缓冲，完成后一次性发送到微信
        """
        if user_id in self._processing_users:
            logger.debug(f"[微信] 用户 {user_id[:20]} 已在处理中，跳过")
            return

        self._processing_users.add(user_id)
        full_response = []
        context_token = self._context_tokens.get(user_id, "")
        agent_start = time.time()
        ack_sent = False

        # ── 微信发送拦截器 → 让 tools.py 的 send_* 直接走 WeChat ──
        from tools import set_send_interceptor, clear_send_interceptor

        def _wechat_send_handler(action: str, kwargs: dict) -> str | None:
            """微信只拦截文字。图片/文件返回None让手机WebSocket通道处理。"""
            if action == "text":
                txt = kwargs.get("text", "")
                if txt.strip():
                    self.send_message(user_id, txt, context_token)
                    return "✅ 文本已发送到微信"
            # 图片/文件不拦截 → 走手机WebSocket直连
            return None

        try:
            # 发送 typing 指示
            self.send_typing(user_id, context_token)

            def on_text(delta: str):
                full_response.append(delta)
                nonlocal ack_sent
                if not ack_sent and time.time() - agent_start > 3:
                    # 3秒还没出结果，发"正在处理"确认
                    self.send_message(user_id, "⏳ 正在处理，请稍候…", context_token)
                    ack_sent = True
                if self._gui:
                    try:
                        self._gui.gui_queue.put(("stream", delta))
                    except Exception:
                        pass

            def on_tool_start(name: str, args: dict):
                if self._gui:
                    self._gui.gui_queue.put(("tool_start", name, args))

            def on_tool_result(name: str, result: str):
                if self._gui:
                    self._gui.gui_queue.put(("tool_result", name, result))

            # 构建带微信上下文的消息
            wechat_msg = (
                f"[来自微信用户的消息]\n{user_text}\n\n"
                f"你正在通过微信与用户对话。你可以使用 send_screenshot 发送截屏给用户、"
                f"使用 send_file 发送文件给用户（传入文件绝对路径）、"
                f"使用 send_text 发送文本给用户。这些工具会自动转发到微信。"
                f"回复尽量简洁清晰，微信单条消息不超过 1800 字。"
            )

            # 激活微信发送拦截器 → 工具 send_* 直接走微信
            set_send_interceptor(_wechat_send_handler)
            try:
                # 微信端请求: 跳过PC权限弹窗
                self._agent._skip_permission = True
                self._agent.process_message(
                    user_message=wechat_msg,
                    on_text=on_text,
                    on_tool_start=on_tool_start,
                    on_tool_result=on_tool_result,
                    on_done=lambda r: None,
                )
            finally:
                clear_send_interceptor()

            # 组装完整回复
            response_text = "".join(full_response).strip()

            if response_text:
                self._reply_text(user_id, response_text, context_token)
            elif not ack_sent:
                self.send_message(user_id, "✅ 已处理", context_token)

            # 通知 GUI 完成
            if self._gui:
                self._gui.gui_queue.put(("processing_done",))
                self._gui.gui_queue.put(("wechat_status", "idle"))

        except Exception as e:
            logger.error(f"[微信] Agent 处理异常: {e}")
            self.send_message(user_id, f"❌ 处理出错: {e}", context_token)
            if self._gui:
                self._gui.gui_queue.put(("processing_done",))
        finally:
            self._processing_users.discard(user_id)

    def _reply_text(self, user_id: str, text: str, context_token: str = ""):
        """发送文本回复到微信用户，自动拆分长消息"""
        if not text.strip():
            return
        if not context_token:
            context_token = self._context_tokens.get(user_id, "")

        # WeChat 单条消息限制约 2000 字符，超长拆分
        max_len = 1800
        if len(text) <= max_len:
            self.send_message(user_id, text, context_token)
        else:
            parts = []
            while len(text) > max_len:
                # 尽量在换行符处断开
                split_at = text.rfind("\n", 0, max_len)
                if split_at < max_len // 2:
                    split_at = text.rfind(" ", 0, max_len)
                if split_at < max_len // 2:
                    split_at = max_len
                parts.append(text[:split_at])
                text = text[split_at:].lstrip()
            if text:
                parts.append(text)

            for i, part in enumerate(parts):
                self.send_message(user_id, part, context_token)
                if i < len(parts) - 1:
                    time.sleep(0.5)  # 避免发送过快

    def send_message(self, to_user_id: str, text: str, context_token: str = "") -> dict:
        """
        发送消息到微信用户
        POST /ilink/bot/sendmessage
        """
        if not self._bot_token:
            return {"ret": -1, "error": "未登录"}

        client_id = f"moagent-{random.randint(100000, 999999)}"
        data = {
            "msg": {
                "to_user_id": to_user_id,
                "client_id": client_id,
                "message_type": 2,       # Bot 发出的消息
                "message_state": 2,
                "context_token": context_token or "",
                "item_list": [
                    {"type": 1, "text_item": {"text": text}}
                ],
            },
            "base_info": {"channel_version": "1.0.2"},
        }

        result = self._post("/ilink/bot/sendmessage", data)
        if result.get("ret") == 0:
            logger.debug(f"[微信] 发送成功: {text[:30]}...")
        else:
            logger.error(f"[微信] 发送失败: {result.get('error', '')}")
        return result

    # ═══ 媒体发送 — 本地HTTP URL + WeChat服务端下载 ═══

    def _get_local_url(self, filepath: str) -> str:
        """生成本地HTTP下载URL"""
        import socket
        from urllib.parse import quote
        fname = quote(Path(filepath).name, safe="")
        local_ip = "127.0.0.1"
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            local_ip = s.getsockname()[0]
            s.close()
        except Exception:
            pass
        return f"http://{local_ip}:9528/files/{fname}"

    def _save_to_uploads(self, filepath: str) -> Path:
        """复制文件到 uploads 目录（HTTP服务器根目录）"""
        uploads = self._data_dir / "uploads"
        uploads.mkdir(parents=True, exist_ok=True)
        src = Path(filepath)
        dest = uploads / src.name
        if src != dest:
            dest.write_bytes(src.read_bytes())
        return dest

    def send_image(self, to_user_id: str, image_path: str, context_token: str = "") -> dict:
        """发送图片 — 本地HTTP URL + type=2 image_item"""
        if not self._bot_token:
            return {"ret": -1, "error": "未登录"}
        fp = self._save_to_uploads(image_path)
        url = self._get_local_url(str(fp))
        client_id = f"moagent-{random.randint(100000, 999999)}"
        data = {
            "msg": {
                "to_user_id": to_user_id,
                "client_id": client_id,
                "message_type": 2,
                "message_state": 2,
                "context_token": context_token or "",
                "item_list": [{"type": 2, "image_item": {"url": url}}],
            },
            "base_info": {"channel_version": "1.0.2"},
        }
        result = self._post("/ilink/bot/sendmessage", data)
        if result.get("ret") == 0:
            logger.info(f"[微信] 图片已发送: {Path(image_path).name} → {url}")
        else:
            logger.error(f"[微信] 图片发送失败(ret={result.get('ret')}): {result}")
        return result

    def send_file_direct(self, to_user_id: str, filepath: str, context_token: str = "") -> dict:
        """发送文件 — 本地HTTP URL + type=4 file_item"""
        if not self._bot_token:
            return {"ret": -1, "error": "未登录"}
        fp = Path(filepath)
        if not fp.exists():
            return {"ret": -1, "error": "文件不存在"}
        url = self._get_local_url(str(fp))
        # 确保文件在HTTP可访问目录
        if not str(fp).startswith(str(self._data_dir / "uploads")):
            fp = self._save_to_uploads(filepath)
            url = self._get_local_url(str(fp))
        client_id = f"moagent-{random.randint(100000, 999999)}"
        data = {
            "msg": {
                "to_user_id": to_user_id,
                "client_id": client_id,
                "message_type": 2,
                "message_state": 2,
                "context_token": context_token or "",
                "item_list": [{"type": 4, "file_item": {
                    "url": url,
                    "filename": fp.name,
                }}],
            },
            "base_info": {"channel_version": "1.0.2"},
        }
        result = self._post("/ilink/bot/sendmessage", data)
        if result.get("ret") == 0:
            logger.info(f"[微信] 文件已发送: {fp.name} → {url}")
        else:
            logger.error(f"[微信] 文件发送失败(ret={result.get('ret')}): {result}")
        return result

    def send_typing(self, to_user_id: str, context_token: str = "") -> bool:
        """
        发送「正在输入…」状态
        先获取 typing_ticket，再发送 typing 状态
        """
        if not self._bot_token:
            return False

        try:
            # 获取 typing_ticket
            cfg = self._post("/ilink/bot/getconfig", {})
            ticket = cfg.get("typing_ticket", "")
            if not ticket:
                return False

            data = {
                "to_user_id": to_user_id,
                "context_token": context_token or "",
                "typing_ticket": ticket,
                "base_info": {"channel_version": "1.0.2"},
            }
            result = self._post("/ilink/bot/sendtyping", data)
            return result.get("ret") == 0
        except Exception as e:
            logger.debug(f"[微信] 发送 typing 失败: {e}")
            return False

    # ═══ 集成接口 ═══
    def bind_agent(self, agent):
        """绑定 AgentCore 实例"""
        self._agent = agent

    def bind_gui(self, gui):
        """绑定 MainWindow 实例"""
        self._gui = gui

    def start(self):
        """启动：如果已登录则开始轮询"""
        if self.is_logged_in:
            self.start_polling()
            self._notify_gui_status(True)
            logger.info("[微信] 自动恢复连接")
        else:
            logger.info("[微信] 未登录，等待扫码")

    def stop(self):
        """停止：停止轮询"""
        self.stop_polling()

    def logout(self):
        """退出登录，清除令牌"""
        self.stop_polling()
        self._bot_token = ""
        self._ilink_bot_id = ""
        self._ilink_user_id = ""
        self._cursor = ""
        self._context_tokens.clear()
        self._reply_quotas.clear()
        self._save_config()
        self._notify_gui_status(False)
        logger.info("[微信] 已退出登录")

    def _notify_gui_status(self, connected: bool):
        """通知 GUI 更新微信连接状态"""
        if self._gui:
            try:
                self._gui.gui_queue.put(
                    ("wechat_status", "connected" if connected else "disconnected")
                )
            except Exception:
                pass

    # ═══ 属性 ═══
    @property
    def is_logged_in(self) -> bool:
        return bool(self._bot_token)

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def bot_name(self) -> str:
        return self._ilink_bot_id or "未登录"


# ═══════════════════════════════════════════
# 全局单例
# ═══════════════════════════════════════════

_wechat_bot_instance = None  # WeChatBot singleton


def get_wechat_bot() -> WeChatBot:
    """获取微信 Bot 单例"""
    global _wechat_bot_instance
    if _wechat_bot_instance is None:
        _wechat_bot_instance = WeChatBot()
    return _wechat_bot_instance


def init_wechat_bot() -> WeChatBot:
    """初始化并启动微信 Bot（如已登录则自动连接）"""
    bot = get_wechat_bot()
    bot.start()
    return bot
