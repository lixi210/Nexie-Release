# Nexie — AI Coding Agent
# Copyright (c) 2026. All rights reserved.
# 本软件仅供个人学习与研究使用，禁止未经授权的商业用途、分发或修改后闭源。
# Licensed for personal, educational, and non-commercial use only.
"""
Nexie 通信模块 — WebSocket Server + Relay Client
支持: LAN直连 / 云端中继跨网连接 / AES-GCM加密 / 心跳保活 / 断线自动重连
统一通信协议, 完美预留 UniApp 手机端对接接口
"""
import asyncio
import json
import time
import uuid
import base64
import hashlib
import secrets
import struct
import threading
import logging
from pathlib import Path
from typing import Callable, Optional
from datetime import datetime

# ==================== 加密模块 ====================

try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    _CRYPTO_AVAILABLE = True
except ImportError:
    _CRYPTO_AVAILABLE = False


class MessageCipher:
    """AES-256-GCM 消息加解密，密钥由连接密钥派生"""

    def __init__(self, shared_secret: str):
        # 从共享密钥派生 AES-256 密钥
        self._key = hashlib.sha256(shared_secret.encode("utf-8")).digest()
        self._aes = AESGCM(self._key) if _CRYPTO_AVAILABLE else None

    def encrypt(self, plaintext: str) -> str:
        """加密明文 → Base64( nonce(12B) + ciphertext )"""
        if not self._aes:
            return plaintext  # 无 cryptography 库时明文传输
        nonce = secrets.token_bytes(12)
        ct = self._aes.encrypt(nonce, plaintext.encode("utf-8"), None)
        return base64.b64encode(nonce + ct).decode("ascii")

    def decrypt(self, encoded: str) -> str:
        """解密 Base64(nonce + ciphertext) → 明文"""
        if not self._aes:
            return encoded
        try:
            raw = base64.b64decode(encoded)
            nonce, ct = raw[:12], raw[12:]
            return self._aes.decrypt(nonce, ct, None).decode("utf-8")
        except Exception:
            return ""  # 解密失败返回空


# ==================== 通信协议 ====================

# 消息类型定义 (统一协议，UniApp 端按此规范对接)
MSG_TYPES = {
    "auth":        "身份认证",       # PC/Mobile → Server: 携带 room_code + device_id
    "auth_ok":     "认证成功",       # Server → Client: 返回 device_info
    "chat":        "聊天消息",       # 双向: 文本消息
    "file":        "文件传输",       # 双向: base64编码的文件数据
    "screenshot":  "屏幕截图",       # PC → Mobile: 截屏推送
    "heartbeat":   "心跳保活",       # 双向: ping/pong
    "heartbeat_ack":"心跳应答",
    "command":     "远程指令",       # Mobile → PC: 执行命令
    "command_result":"指令结果",     # PC → Mobile: 命令执行结果
    "status":      "状态同步",       # 双向: 在线状态/进度
    "error":       "错误消息",
    "ack":         "消息确认",       # 收发确认
    "disconnect":  "断连通知",
}

# 协议版本
PROTOCOL_VERSION = "1.0.0"


def build_message(msg_type: str, payload: dict = None, msg_id: str = None) -> dict:
    """构建标准消息包"""
    return {
        "version": PROTOCOL_VERSION,
        "type": msg_type,
        "id": msg_id or uuid.uuid4().hex[:12],
        "timestamp": int(time.time() * 1000),
        "payload": payload or {},
    }


def build_chat_message(text: str, sender: str = "pc") -> dict:
    """构建聊天消息"""
    return build_message("chat", {
        "sender": sender,
        "text": text,
        "content_type": "text",
    })


def build_file_message(filename: str, file_data_b64: str, mime_type: str = "application/octet-stream",
                       file_size: int = 0, sender: str = "pc") -> dict:
    """构建文件传输消息"""
    return build_message("file", {
        "sender": sender,
        "filename": filename,
        "data": file_data_b64,
        "mime_type": mime_type,
        "size": file_size,
    })


def build_screenshot_message(image_b64: str, width: int = 0, height: int = 0) -> dict:
    """构建截屏推送消息"""
    return build_message("screenshot", {
        "image_data": image_b64,
        "format": "jpeg",
        "width": width,
        "height": height,
    })


# ==================== WebSocket 服务器 ====================

logger = logging.getLogger("Nexie.Communication")


class ConnectionManager:
    """管理所有活跃的 WebSocket 连接"""

    def __init__(self):
        self._connections: dict[str, any] = {}  # device_id → websocket
        self._lock = threading.Lock()

    def add(self, device_id: str, ws):
        with self._lock:
            self._connections[device_id] = ws
            logger.info(f"[通信] 设备已连接: {device_id} (当前 {len(self._connections)} 个连接)")

    def remove(self, device_id: str):
        with self._lock:
            if device_id in self._connections:
                del self._connections[device_id]
                logger.info(f"[通信] 设备已断开: {device_id} (剩余 {len(self._connections)} 个连接)")

    def get(self, device_id: str):
        with self._lock:
            return self._connections.get(device_id)

    @property
    def active_count(self) -> int:
        return len(self._connections)

    def get_all_ids(self) -> list:
        with self._lock:
            return list(self._connections.keys())

    async def broadcast(self, message: dict, exclude: str = None):
        """向所有连接广播消息"""
        with self._lock:
            connections = [(did, ws) for did, ws in self._connections.items() if did != exclude]

        for device_id, ws in connections:
            try:
                await ws.send(json.dumps(message, ensure_ascii=False))
            except Exception:
                pass

    async def send_to(self, device_id: str, message: dict) -> bool:
        """向指定设备发送消息"""
        ws = self.get(device_id)
        if ws:
            try:
                await ws.send(json.dumps(message, ensure_ascii=False))
                return True
            except Exception as e:
                logger.error(f"[通信] 发送失败 [{device_id}]: {e}")
                self.remove(device_id)
        return False


class NexieServer:
    """
    PC 端 WebSocket 服务器
    - 监听本地端口，接受手机端直连 (LAN)
    - 同时可作为客户端连接到云端中继 (WAN)
    - 心跳保活 + 断线自动重连
    """

    DEFAULT_PORT = 9527  # 默认监听端口

    def __init__(self, room_secret: str = None, port: int = None):
        self.port = port or self.DEFAULT_PORT
        # 连接密钥 — 持久化保存，重启不变化，仅手动刷新
        self._secret_file = self._get_data_dir() / "mobile_secret.json"
        self.room_secret = room_secret or self._load_or_create_secret()
        self.cipher = MessageCipher(self.room_secret)
        self.conn_manager = ConnectionManager()
        self._server: Optional[asyncio.AbstractServer] = None
        self._running = False
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None

        # ═══ 回调钩子 (由 mobile_bridge 注册) ═══
        self.on_mobile_message: Optional[Callable[[dict, str], None]] = None  # (message, device_id)
        self.on_mobile_connected: Optional[Callable[[str, dict], None]] = None  # (device_id, info)
        self.on_mobile_disconnected: Optional[Callable[[str], None]] = None   # (device_id)

        # 中继客户端
        self._relay_client: Optional[any] = None
        self._relay_url: str = ""
        self._use_relay: bool = False

        # 心跳
        self._heartbeat_interval = 30  # 秒
        self._heartbeat_timeout = 90   # 超时秒数
        self._last_heartbeat: dict[str, float] = {}

    # ═══ 房间密钥管理 ═══
    @staticmethod
    def _get_data_dir() -> Path:
        """获取持久数据目录（统一入口）"""
        from nexie import get_data_dir
        return get_data_dir()

    def _load_or_create_secret(self) -> str:
        """从文件加载持久化密钥，不存在则创建新密钥并保存"""
        self._secret_file.parent.mkdir(parents=True, exist_ok=True)
        if self._secret_file.exists():
            try:
                data = json.loads(self._secret_file.read_text("utf-8"))
                saved = data.get("room_secret", "")
                if saved:
                    logger.info(f"[通信] 加载已保存的密钥: {saved[:8]}...")
                    return saved
            except Exception:
                pass
        # 首次运行：生成新密钥并保存
        new_secret = secrets.token_hex(16)
        self._save_secret(new_secret)
        logger.info(f"[通信] 生成新密钥并保存: {new_secret[:8]}...")
        return new_secret

    def _save_secret(self, secret: str):
        """持久化保存密钥到文件"""
        try:
            self._secret_file.parent.mkdir(parents=True, exist_ok=True)
            self._secret_file.write_text(
                json.dumps({"room_secret": secret, "created_at": datetime.now().isoformat()},
                           ensure_ascii=False, indent=2),
                "utf-8",
            )
        except Exception as e:
            logger.error(f"[通信] 密钥保存失败: {e}")

    def get_room_secret(self) -> str:
        return self.room_secret

    def regenerate_secret(self) -> str:
        """手动刷新连接密钥 — 断开所有设备，生成新密钥并持久化"""
        old = self.room_secret
        self.room_secret = secrets.token_hex(16)
        self.cipher = MessageCipher(self.room_secret)
        self._save_secret(self.room_secret)
        # 断开所有现有连接（密钥变了，旧连接失效）
        for device_id in list(self.conn_manager.get_all_ids()):
            self.conn_manager.remove(device_id)
            self._last_heartbeat.pop(device_id, None)
        logger.info(f"[通信] 密钥已刷新并保存: {old[:8]}... → {self.room_secret[:8]}...")
        return self.room_secret

    # ═══ 连接信息 (用于二维码) ═══
    @staticmethod
    def _get_all_local_ips() -> list[str]:
        """获取本机所有局域网IP地址 (过滤回环和虚拟网卡)"""
        import socket
        ips = []
        try:
            # 方法1: UDP连接探测 — 获取默认路由对应的IP
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.settimeout(1)
            try:
                s.connect(("8.8.8.8", 80))
                default_ip = s.getsockname()[0]
                if default_ip and not default_ip.startswith("127."):
                    ips.append(default_ip)
            except Exception:
                pass
            finally:
                s.close()

            # 方法2: 遍历所有网络接口
            hostname = socket.gethostname()
            try:
                all_ips = socket.gethostbyname_ex(hostname)[2]
                for ip in all_ips:
                    if ip not in ips and not ip.startswith("127."):
                        ips.append(ip)
            except Exception:
                pass

            # 方法3: 使用 netifaces (如果已安装)
            try:
                import netifaces
                for iface in netifaces.interfaces():
                    addrs = netifaces.ifaddresses(iface)
                    if netifaces.AF_INET in addrs:
                        for addr in addrs[netifaces.AF_INET]:
                            ip = addr.get("addr", "")
                            if ip and not ip.startswith("127.") and ip not in ips:
                                # 过滤虚拟网卡 (通常以169.254, 172.17+ 开头的是Docker/虚拟机)
                                if not ip.startswith("169.254."):
                                    ips.append(ip)
            except ImportError:
                pass

        except Exception as e:
            logger.warning(f"[通信] 获取IP列表失败: {e}")

        # 优先返回192.168.x.x (最常见的内网地址)
        ip192 = [ip for ip in ips if ip.startswith("192.168.")]
        ip10 = [ip for ip in ips if ip.startswith("10.")]
        ip172 = [ip for ip in ips if ip.startswith("172.") and not ip.startswith("172.17.")]
        sorted_ips = ip192 + ip10 + ip172 + [ip for ip in ips if ip not in ip192 + ip10 + ip172]
        return sorted_ips

    def get_connection_info(self) -> dict:
        """获取连接信息，用于生成二维码（含UPnP公网地址）"""
        all_ips = self._get_all_local_ips()
        local_ip = all_ips[0] if all_ips else "127.0.0.1"

        info = {
            "version": PROTOCOL_VERSION,
            "room_secret": self.room_secret,
            "host": local_ip,
            "port": self.port,
            "use_relay": self._use_relay,
            "all_ips": all_ips,  # 手机端可尝试所有IP
        }
        # UPnP公网地址
        try:
            from nexie.upnp import get_upnp_mapper
            upnp = get_upnp_mapper(self.port)
            if upnp.public_url:
                info["public_url"] = upnp.public_url
                info["public_ip"] = upnp._public_ip
                info["public_port"] = upnp._external_port
        except Exception:
            pass
        if self._use_relay and self._relay_url:
            info["relay_url"] = self._relay_url
        return info

    def get_qr_data(self) -> str:
        """获取用于二维码的 JSON 数据"""
        return json.dumps(self.get_connection_info(), ensure_ascii=False)

    # ═══ 服务器启动/停止 ═══
    def start(self, relay_url: str = ""):
        """在独立线程中启动 WebSocket 服务器"""
        if self._running:
            return

        self._relay_url = relay_url
        self._use_relay = bool(relay_url)

        self._thread = threading.Thread(
            target=self._run_event_loop,
            name="Nexie-WS-Server",
            daemon=True,
        )
        self._thread.start()
        self._running = True
        logger.info(f"[通信] 服务器启动: 端口={self.port}, 中继={'启用' if self._use_relay else '仅LAN'}")

    def stop(self):
        """停止服务器"""
        self._running = False
        if self._loop and self._loop.is_running():
            try:
                # 取消所有任务
                for task in asyncio.all_tasks(self._loop):
                    task.cancel()
            except Exception:
                pass
            self._loop.call_soon_threadsafe(self._loop.stop)

        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3.0)

        logger.info("[通信] 服务器已停止")

    def _run_event_loop(self):
        """在独立线程中运行 asyncio 事件循环"""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._async_main())
        except Exception as e:
            logger.error(f"[通信] 事件循环异常: {e}")
        finally:
            self._loop.close()

    async def _async_main(self):
        """异步主入口: 启动 WS 服务器 + 可选中继连接"""
        tasks = []

        # 1) 启动本地 WebSocket 服务器
        try:
            self._server = await asyncio.start_server(
                self._handle_connection,
                host="0.0.0.0",
                port=self.port,
            )
            logger.info(f"[通信] WebSocket 服务器监听: 0.0.0.0:{self.port}")
        except OSError as e:
            logger.error(f"[通信] 端口 {self.port} 被占用: {e}")
            for alt_port in range(self.port + 1, self.port + 100):
                try:
                    self._server = await asyncio.start_server(
                        self._handle_connection, host="0.0.0.0", port=alt_port)
                    self.port = alt_port
                    logger.info(f"[通信] 改用端口: {alt_port}")
                    break
                except OSError:
                    continue

        if not self._server:
            logger.error("[通信] 无法绑定任何端口，服务器启动失败")
            return

        # 2) 可选: 连接到云端中继
        if self._use_relay:
            tasks.append(asyncio.create_task(self._relay_connect_loop()))

        # 3) 心跳检查
        tasks.append(asyncio.create_task(self._heartbeat_checker()))

        # 运行直到停止
        try:
            async with self._server:
                await self._server.serve_forever()
        except asyncio.CancelledError:
            pass
        except RuntimeError:
            pass
        except Exception as e:
            logger.error(f"[通信] 服务器异常: {e}")
        finally:
            # 优雅关闭
            for t in tasks:
                t.cancel()
            # 等待任务取消完成
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

    # ═══ WebSocket 连接处理 ═══
    async def _handle_connection(self, reader, writer):
        """处理原始 TCP 连接 → WebSocket 升级"""
        ws = None
        device_id = "unknown"
        try:
            # 尝试 WebSocket 升级
            request_line = await asyncio.wait_for(reader.readline(), timeout=10)
            if not request_line:
                return

            request_text = request_line.decode("utf-8", errors="replace")

            if "HTTP/1.1" in request_text or "HTTP/1.0" in request_text:
                # 标准 HTTP → WebSocket 升级 (UniApp connectSocket 行为)
                ws = await self._http_websocket_upgrade(reader, writer, request_text)
            else:
                # 非标准请求 — 尝试直接 WebSocket 握手（极少发生）
                ws = await self._websocket_handshake(reader, writer, request_text)

            if not ws:
                return

            # 认证
            device_id = await self._authenticate(ws)
            if not device_id:
                await self._safe_send(ws, json.dumps(build_message("auth_ok", {
                    "success": False,
                    "reason": "密钥验证失败",
                })))
                await self._safe_close(ws)
                return

            # 注册连接
            self.conn_manager.add(device_id, ws)
            self._last_heartbeat[device_id] = time.time()

            # 发送认证成功
            await self._safe_send(ws, json.dumps(build_message("auth_ok", {
                "success": True,
                "device_id": device_id,
                "server_info": {
                    "name": "Nexie5.0",
                    "version": PROTOCOL_VERSION,
                },
            })))

            # 触发连接回调
            if self.on_mobile_connected:
                self.on_mobile_connected(device_id, {"connected_at": time.time()})

            # 消息循环
            await self._message_loop(ws, device_id)

        except asyncio.TimeoutError:
            pass
        except ConnectionResetError:
            pass
        except Exception as e:
            logger.error(f"[通信] 连接处理异常 [{device_id}]: {e}")
        finally:
            if device_id != "unknown":
                self.conn_manager.remove(device_id)
                self._last_heartbeat.pop(device_id, None)
                if self.on_mobile_disconnected:
                    self.on_mobile_disconnected(device_id)
            if ws:
                await self._safe_close(ws)

    async def _websocket_handshake(self, reader, writer, first_line: str):
        """简易 WebSocket 握手 (处理无 HTTP 升级头的连接)"""
        try:
            # 读取 headers
            headers = {}
            while True:
                line = await asyncio.wait_for(reader.readline(), timeout=5)
                line_text = line.decode("utf-8", errors="replace").strip()
                if not line_text:
                    break
                if ":" in line_text:
                    key, val = line_text.split(":", 1)
                    headers[key.strip().lower()] = val.strip()

            ws_key = headers.get("sec-websocket-key", "")
            if not ws_key:
                return None

            # 生成 Accept Key
            GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
            accept_key = base64.b64encode(
                hashlib.sha1((ws_key + GUID).encode()).digest()
            ).decode()

            # 发送握手响应
            response = (
                "HTTP/1.1 101 Switching Protocols\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                f"Sec-WebSocket-Accept: {accept_key}\r\n"
                "\r\n"
            )
            writer.write(response.encode())
            await writer.drain()

            return _SimpleWebSocket(reader, writer)
        except Exception:
            return None

    async def _http_websocket_upgrade(self, reader, writer, request_line: str):
        """标准 HTTP → WebSocket 升级"""
        try:
            headers = {}
            while True:
                line = await asyncio.wait_for(reader.readline(), timeout=5)
                line_text = line.decode("utf-8", errors="replace").strip()
                if not line_text:
                    break
                if ":" in line_text:
                    key, val = line_text.split(":", 1)
                    headers[key.strip().lower()] = val.strip()

            ws_key = headers.get("sec-websocket-key", "")
            if not ws_key:
                return None

            GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
            accept_key = base64.b64encode(
                hashlib.sha1((ws_key + GUID).encode()).digest()
            ).decode()

            response = (
                "HTTP/1.1 101 Switching Protocols\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                f"Sec-WebSocket-Accept: {accept_key}\r\n"
                "\r\n"
            )
            writer.write(response.encode())
            await writer.drain()

            return _SimpleWebSocket(reader, writer)
        except Exception:
            return None

    async def _authenticate(self, ws) -> Optional[str]:
        """认证流程 — 等待客户端发送 auth 消息，验证密钥"""
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=15)
            msg = json.loads(raw)
            if msg.get("type") != "auth":
                return None

            payload = msg.get("payload", {})
            client_secret = payload.get("room_secret", "")
            device_id = payload.get("device_id", uuid.uuid4().hex[:8])

            # 验证连接密钥
            if client_secret != self.room_secret:
                logger.warning(f"[通信] 认证失败: 密钥不匹配 (收到: {client_secret[:8]}...)")
                return None

            logger.info(f"[通信] 设备认证成功: {device_id}")
            return device_id
        except asyncio.TimeoutError:
            return None
        except Exception as e:
            logger.error(f"[通信] 认证异常: {e}")
            return None

    async def _message_loop(self, ws, device_id: str):
        """消息接收循环"""
        while self._running:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=self._heartbeat_interval + 15)
                msg = json.loads(raw)
                msg_type = msg.get("type", "")

                if msg_type == "heartbeat":
                    # 心跳响应
                    self._last_heartbeat[device_id] = time.time()
                    await self._safe_send(ws, json.dumps(build_message("heartbeat_ack", {
                        "server_time": int(time.time() * 1000),
                    })))
                elif msg_type == "ack":
                    # 消息确认 — 记录日志即可
                    pass
                else:
                    # 业务消息 → 回调 mobile_bridge
                    self._last_heartbeat[device_id] = time.time()
                    if self.on_mobile_message:
                        # 在事件循环中调度回调 (非阻塞)
                        asyncio.get_event_loop().call_soon_threadsafe(
                            lambda m=msg, d=device_id: self.on_mobile_message(m, d)
                        )

            except asyncio.TimeoutError:
                # 超时无消息 — 检查心跳
                continue
            except (ConnectionResetError, BrokenPipeError, OSError):
                break
            except json.JSONDecodeError:
                continue
            except Exception as e:
                logger.error(f"[通信] 消息循环异常 [{device_id}]: {e}")
                break

    # ═══ 心跳检查 ═══
    async def _heartbeat_checker(self):
        """定期检查所有连接的心跳状态，踢掉超时连接"""
        while self._running:
            await asyncio.sleep(self._heartbeat_interval)
            now = time.time()
            timeout_ids = []
            for device_id, last_hb in list(self._last_heartbeat.items()):
                if now - last_hb > self._heartbeat_timeout:
                    timeout_ids.append(device_id)

            for device_id in timeout_ids:
                logger.warning(f"[通信] 心跳超时，断开: {device_id}")
                ws = self.conn_manager.get(device_id)
                if ws:
                    await self._safe_close(ws)
                self.conn_manager.remove(device_id)
                self._last_heartbeat.pop(device_id, None)
                if self.on_mobile_disconnected:
                    self.on_mobile_disconnected(device_id)

    # ═══ 中继连接 ═══
    async def _relay_connect_loop(self):
        """连接到云端中继并维持连接 (断线自动重连)"""
        while self._running:
            try:
                logger.info(f"[通信] 连接中继服务器: {self._relay_url}")
                # 中继连接实现 (由 relay_server.py 提供服务)
                relay_ws = await self._connect_to_relay()
                if relay_ws:
                    await self._relay_message_loop(relay_ws)
            except Exception as e:
                logger.error(f"[通信] 中继连接异常: {e}")

            # 断线重连等待
            if self._running:
                logger.info("[通信] 中继断线，5秒后重连...")
                await asyncio.sleep(5)

    async def _connect_to_relay(self):
        """连接到中继服务器"""
        try:
            import websockets
            ws = await websockets.connect(
                self._relay_url,
                ping_interval=20,
                ping_timeout=10,
                close_timeout=5,
            )
            # 向中继注册为 PC Host
            await ws.send(json.dumps({
                "type": "register",
                "role": "host",
                "room_secret": self.room_secret,
                "device_name": "Nexie-PC",
            }))
            resp = await asyncio.wait_for(ws.recv(), timeout=10)
            result = json.loads(resp)
            if result.get("type") == "registered":
                logger.info("[通信] 中继注册成功")
                return ws
            else:
                logger.error(f"[通信] 中继注册失败: {result}")
                await ws.close()
                return None
        except ImportError:
            logger.error("[通信] websockets 库未安装，无法连接中继")
            return None
        except Exception as e:
            logger.error(f"[通信] 连接中继失败: {e}")
            return None

    async def _relay_message_loop(self, ws):
        """中继消息循环 — 转发来自中继的消息"""
        import websockets
        try:
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                    msg_type = msg.get("type", "")

                    if msg_type == "heartbeat":
                        await ws.send(json.dumps(build_message("heartbeat_ack")))

                    elif msg_type == "forward":
                        # 来自手机端的转发消息
                        payload = msg.get("payload", {})
                        original = payload.get("message", {})
                        from_device = payload.get("from_device", "")
                        if self.on_mobile_message and original:
                            self.on_mobile_message(original, from_device)

                except json.JSONDecodeError:
                    continue
        except websockets.exceptions.ConnectionClosed:
            pass
        except Exception as e:
            logger.error(f"[通信] 中继消息循环异常: {e}")

    # ═══ 发送消息 API (供外部线程调用) ═══
    def send_message(self, device_id: str, msg: dict):
        """向指定设备发送消息 (线程安全)"""
        if self._loop and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(
                self.conn_manager.send_to(device_id, msg),
                self._loop,
            )

    def broadcast_message(self, msg: dict, exclude: str = None):
        """广播消息给所有设备 (线程安全)"""
        if self._loop and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(
                self.conn_manager.broadcast(msg, exclude),
                self._loop,
            )

    def send_chat(self, device_id: str, text: str):
        """发送聊天消息到指定设备"""
        self.send_message(device_id, build_chat_message(text))

    def send_file(self, device_id: str, filename: str, file_data_b64: str,
                  mime_type: str = "application/octet-stream", file_size: int = 0):
        """发送文件到指定设备"""
        self.send_message(device_id, build_file_message(filename, file_data_b64, mime_type, file_size))

    def send_screenshot(self, device_id: str, image_b64: str, width: int = 0, height: int = 0):
        """推送截屏到指定设备"""
        self.send_message(device_id, build_screenshot_message(image_b64, width, height))

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def active_devices(self) -> list:
        return self.conn_manager.get_all_ids()

    # ═══ 辅助方法 ═══
    @staticmethod
    async def _safe_send(ws, text: str):
        try:
            await ws.send(text)
        except Exception:
            pass

    @staticmethod
    async def _safe_close(ws):
        try:
            await ws.close()
        except Exception:
            pass


# ==================== 简易 WebSocket 帧协议 ====================
# 基于 RFC 6455 §5.2 — 最小实现

class _SimpleWebSocket:
    """简易 WebSocket 对象，支持 send(文本) 和 recv()"""

    OPCODE_TEXT = 0x1
    OPCODE_CLOSE = 0x8
    OPCODE_PING = 0x9
    OPCODE_PONG = 0xA

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self._reader = reader
        self._writer = writer
        self._closed = False

    async def send(self, text: str):
        """发送文本帧"""
        if self._closed:
            return
        data = text.encode("utf-8")
        frame = self._build_frame(self.OPCODE_TEXT, data)
        try:
            self._writer.write(frame)
            await self._writer.drain()
        except Exception:
            self._closed = True

    async def recv(self) -> str:
        """接收文本帧"""
        while not self._closed:
            try:
                # 读取帧头 (2字节)
                header = await asyncio.wait_for(self._reader.readexactly(2), timeout=120)
                byte0, byte1 = header[0], header[1]
                opcode = byte0 & 0x0F
                masked = (byte1 & 0x80) != 0
                payload_len = byte1 & 0x7F

                # 扩展长度
                if payload_len == 126:
                    ext = await asyncio.wait_for(self._reader.readexactly(2), timeout=5)
                    payload_len = struct.unpack(">H", ext)[0]
                elif payload_len == 127:
                    ext = await asyncio.wait_for(self._reader.readexactly(8), timeout=5)
                    payload_len = struct.unpack(">Q", ext)[0]

                # 安全限制: 单帧最大10MB (防止内存耗尽)
                MAX_FRAME = 10 * 1024 * 1024
                if payload_len > MAX_FRAME:
                    self._closed = True
                    return ""

                # Mask key (客户端→服务器必须)
                if masked:
                    mask_key = await asyncio.wait_for(self._reader.readexactly(4), timeout=5)

                # Payload — 根据大小动态调整超时 (大文件给更多时间)
                read_timeout = max(30, payload_len // (256 * 1024))  # ~256KB/s 最低保证
                read_timeout = min(read_timeout, 120)  # 上限120秒
                payload = await asyncio.wait_for(self._reader.readexactly(payload_len), timeout=read_timeout)

                # Unmask
                if masked:
                    payload = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))

                if opcode == self.OPCODE_TEXT:
                    return payload.decode("utf-8")
                elif opcode == self.OPCODE_CLOSE:
                    self._closed = True
                    return ""
                elif opcode == self.OPCODE_PING:
                    pong = self._build_frame(self.OPCODE_PONG, payload)
                    self._writer.write(pong)
                    await self._writer.drain()
                elif opcode == self.OPCODE_PONG:
                    pass  # 忽略 pong

            except asyncio.IncompleteReadError:
                self._closed = True
                return ""
            except Exception:
                self._closed = True
                return ""

    async def close(self):
        """发送关闭帧"""
        if not self._closed:
            self._closed = True
            try:
                self._writer.write(self._build_frame(self.OPCODE_CLOSE, b""))
                await self._writer.drain()
                self._writer.close()
            except Exception:
                pass

    @staticmethod
    def _build_frame(opcode: int, payload: bytes) -> bytes:
        """构建 WebSocket 帧 (服务器→客户端，不 mask)"""
        frame = bytes([0x80 | opcode])
        length = len(payload)
        if length < 126:
            frame += bytes([length])
        elif length < 65536:
            frame += bytes([126]) + struct.pack(">H", length)
        else:
            frame += bytes([127]) + struct.pack(">Q", length)
        frame += payload
        return frame


# ==================== 模块单例 ====================

_server_instance: Optional[NexieServer] = None


def get_server() -> NexieServer:
    """获取通信服务器单例"""
    global _server_instance
    if _server_instance is None:
        _server_instance = NexieServer()
    return _server_instance


def init_server(port: int = None, relay_url: str = "") -> NexieServer:
    """初始化并启动通信服务器"""
    global _server_instance
    if _server_instance is not None:
        _server_instance.stop()

    _server_instance = NexieServer(port=port)
    _server_instance.start(relay_url=relay_url)
    return _server_instance
