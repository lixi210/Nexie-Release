#!/usr/bin/env python3
# Nexie — AI Coding Agent
# Copyright (c) 2026. All rights reserved.
# 本软件仅供个人学习与研究使用，禁止未经授权的商业用途、分发或修改后闭源。
# Licensed for personal, educational, and non-commercial use only.
"""
Nexie 云端中继服务器 — relay_server.py
============================================
部署于公网服务器 (VPS/云主机)，实现 PC ↔ 手机 跨网通信。

使用方法:
  python relay_server.py --port 9528 --host 0.0.0.0

安全:
  - 基于 room_secret 的房间隔离
  - 仅转发消息，不存储任何数据
  - 支持 TLS (通过 Nginx/Caddy 反代)

通信流程:
  1. PC 端注册为 "host"
  2. 手机端注册为 "client" (同 room_secret)
  3. 服务器匹配同房间的设备，转发消息
"""

import asyncio
import json
import time
import argparse
import logging
import sys
from typing import Optional

# 尝试导入 websockets
try:
    import websockets
    from websockets.server import serve, WebSocketServerProtocol
    from websockets.exceptions import ConnectionClosed
except ImportError:
    print("请先安装 websockets: pip install websockets")
    sys.exit(1)

# ==================== 日志 ====================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("Nexie.Relay")


# ==================== 房间管理 ====================

class RelayRoom:
    """中继房间 — 按 room_secret 隔离"""

    def __init__(self, room_secret: str):
        self.room_secret = room_secret
        self.host: Optional[WebSocketServerProtocol] = None      # PC 端
        self.clients: dict[str, WebSocketServerProtocol] = {}    # 手机端 (device_id → ws)
        self.created_at = time.time()

    @property
    def is_empty(self) -> bool:
        return self.host is None and len(self.clients) == 0

    @property
    def total_connections(self) -> int:
        return (1 if self.host else 0) + len(self.clients)


class RelayServer:
    """中继服务器核心"""

    HEARTBEAT_INTERVAL = 30   # 心跳间隔
    HEARTBEAT_TIMEOUT = 90    # 超时踢出

    def __init__(self, host: str = "0.0.0.0", port: int = 9528):
        self.host = host
        self.port = port
        self.rooms: dict[str, RelayRoom] = {}  # room_secret → RelayRoom
        self._ws_to_room: dict[WebSocketServerProtocol, str] = {}  # ws → room_secret
        self._start_time = time.time()

    # ═══ 启动 ═══
    async def start(self):
        """启动中继服务器"""
        logger.info(f"Nexie 中继服务器启动: {self.host}:{self.port}")
        async with serve(
            self._handle_connection,
            self.host,
            self.port,
            ping_interval=20,
            ping_timeout=10,
            close_timeout=5,
        ):
            await asyncio.Future()  # 永久运行

    # ═══ 连接处理 ═══
    async def _handle_connection(self, ws: WebSocketServerProtocol):
        """处理新连接"""
        peer = ws.remote_address
        logger.info(f"[连接] 新连接: {peer}")

        room_secret = None
        role = None
        device_id = "unknown"

        try:
            # 1) 等待注册消息
            raw = await asyncio.wait_for(ws.recv(), timeout=15)
            msg = json.loads(raw)

            if msg.get("type") != "register":
                await ws.send(json.dumps({"type": "error", "reason": "请先注册"}))
                return

            role = msg.get("role", "client")
            room_secret = msg.get("room_secret", "")
            device_id = msg.get("device_name", "unknown")

            if not room_secret:
                await ws.send(json.dumps({"type": "error", "reason": "缺少 room_secret"}))
                return

            # 2) 注册到房间
            room = self._get_or_create_room(room_secret)

            if role == "host":
                # PC 端 — 踢掉旧 Host
                if room.host:
                    try:
                        await room.host.send(json.dumps({
                            "type": "disconnect",
                            "reason": "新Host已注册，你被踢下线",
                        }))
                        await room.host.close()
                    except Exception:
                        pass
                room.host = ws
                logger.info(f"[注册] Host 注册: {device_id} → 房间 {room_secret[:12]}...")

            else:
                # 手机端
                room.clients[device_id] = ws
                logger.info(f"[注册] Client 注册: {device_id} → 房间 {room_secret[:12]}...")

            self._ws_to_room[ws] = room_secret

            # 3) 确认注册成功
            await ws.send(json.dumps({
                "type": "registered",
                "role": role,
                "device_id": device_id,
                "room_info": {
                    "host_online": room.host is not None,
                    "client_count": len(room.clients),
                },
            }))

            # 4) 通知同房间的设备
            await self._notify_peers(room, ws, {
                "type": "peer_online",
                "role": role,
                "device_id": device_id,
            })

            # 5) 消息循环
            await self._message_loop(ws, room, role, device_id)

        except asyncio.TimeoutError:
            logger.warning(f"[超时] 注册超时: {peer}")
        except json.JSONDecodeError:
            logger.warning(f"[错误] 无效JSON: {peer}")
        except ConnectionClosed:
            pass
        except Exception as e:
            logger.error(f"[异常] {peer}: {e}")
        finally:
            # 清理
            await self._cleanup(ws, room_secret, role, device_id)

    async def _message_loop(self, ws: WebSocketServerProtocol, room: RelayRoom, role: str, device_id: str):
        """接收消息并转发"""
        last_hb = time.time()

        async for raw in ws:
            try:
                msg = json.loads(raw)
                msg_type = msg.get("type", "")

                # 心跳
                if msg_type == "heartbeat":
                    last_hb = time.time()
                    await ws.send(json.dumps({"type": "heartbeat_ack", "server_time": int(time.time() * 1000)}))
                    continue

                # 转发消息到对方
                if role == "host":
                    # PC → 指定手机 或 广播给所有手机
                    target = msg.get("to_device")
                    if target and target in room.clients:
                        await self._safe_send(room.clients[target], {
                            "type": "forward",
                            "from_role": "host",
                            "from_device": device_id,
                            "payload": msg,
                        })
                    else:
                        # 广播给所有手机
                        for cid, cws in list(room.clients.items()):
                            await self._safe_send(cws, {
                                "type": "forward",
                                "from_role": "host",
                                "from_device": device_id,
                                "payload": msg,
                            })

                else:
                    # 手机 → PC Host
                    if room.host:
                        await self._safe_send(room.host, {
                            "type": "forward",
                            "from_role": "client",
                            "from_device": device_id,
                            "payload": msg,
                        })

            except json.JSONDecodeError:
                continue
            except ConnectionClosed:
                break
            except Exception as e:
                logger.error(f"[消息循环] {device_id}: {e}")

    # ═══ 房间管理 ═══
    def _get_or_create_room(self, room_secret: str) -> RelayRoom:
        if room_secret not in self.rooms:
            self.rooms[room_secret] = RelayRoom(room_secret)
            logger.info(f"[房间] 创建房间: {room_secret[:12]}... (共 {len(self.rooms)} 个)")
        return self.rooms[room_secret]

    async def _cleanup(self, ws, room_secret: Optional[str], role: Optional[str], device_id: str):
        """清理断开的连接"""
        self._ws_to_room.pop(ws, None)

        if room_secret and room_secret in self.rooms:
            room = self.rooms[room_secret]

            if role == "host" and room.host == ws:
                room.host = None
                logger.info(f"[断开] Host 离开: {device_id}")
                # 通知所有客户端
                for cws in room.clients.values():
                    await self._safe_send(cws, {"type": "host_offline"})

            elif role == "client":
                room.clients.pop(device_id, None)
                logger.info(f"[断开] Client 离开: {device_id}")
                # 通知 Host
                if room.host:
                    await self._safe_send(room.host, {
                        "type": "peer_offline",
                        "device_id": device_id,
                    })

            # 清理空房间
            if room.is_empty:
                del self.rooms[room_secret]
                logger.info(f"[房间] 销毁房间: {room_secret[:12]}... (剩余 {len(self.rooms)} 个)")

    async def _notify_peers(self, room: RelayRoom, exclude_ws, msg: dict):
        """通知房间内其他设备"""
        targets = []
        if room.host and room.host != exclude_ws:
            targets.append(room.host)
        for cws in room.clients.values():
            if cws != exclude_ws:
                targets.append(cws)

        for ws in targets:
            await self._safe_send(ws, msg)

    @staticmethod
    async def _safe_send(ws, msg: dict):
        try:
            await ws.send(json.dumps(msg, ensure_ascii=False))
        except Exception:
            pass

    # ═══ 状态 ═══
    def get_stats(self) -> dict:
        total_clients = sum(len(r.clients) for r in self.rooms.values())
        return {
            "rooms": len(self.rooms),
            "total_hosts": sum(1 for r in self.rooms.values() if r.host),
            "total_clients": total_clients,
            "uptime_seconds": int(time.time() - self._start_time),
            "protocol_version": "1.0.0",
        }


# ==================== 入口 ====================

def main():
    parser = argparse.ArgumentParser(description="Nexie 中继服务器")
    parser.add_argument("--host", default="0.0.0.0", help="监听地址 (默认: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=9528, help="监听端口 (默认: 9528)")
    args = parser.parse_args()

    server = RelayServer(host=args.host, port=args.port)

    try:
        asyncio.run(server.start())
    except KeyboardInterrupt:
        logger.info("服务器已停止")


if __name__ == "__main__":
    main()
