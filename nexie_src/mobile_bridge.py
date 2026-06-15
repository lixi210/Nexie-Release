# Nexie — AI Coding Agent
# Copyright (c) 2026. All rights reserved.
# 本软件仅供个人学习与研究使用，禁止未经授权的商业用途、分发或修改后闭源。
# Licensed for personal, educational, and non-commercial use only.
"""
Nexie 手机桥接层 — 连接 AgentCore 与通信模块
- 双向消息路由: 手机指令→Agent处理→结果回推
- 文件/截屏传输管理
- 聊天同步 (双向实时)
- 所有核心业务在PC端执行，手机端仅做远程展示
"""
import json
import base64
import time
import threading
import logging
from pathlib import Path
from typing import Optional, Callable
from datetime import datetime

from communication import (
    get_server, NexieServer,
    build_message, build_chat_message, build_file_message, build_screenshot_message,
)

logger = logging.getLogger("Nexie.MobileBridge")


class MobileBridge:
    """
    手机桥接层 — 单例模式
    桥接 PC AgentCore ↔ 通信模块 ↔ 手机端

    数据流:
      手机 → WebSocket → MobileBridge → AgentCore.process_message() → 结果 → MobileBridge → WebSocket → 手机
    """

    def __init__(self):
        self._server: NexieServer = get_server()
        self._agent = None           # AgentCore 引用 (延迟绑定)
        self._gui = None             # MainWindow 引用 (用于聊天同步)

        # ═══ 聊天消息缓存 (手机端同步) ═══
        self._chat_history: list[dict] = []   # 同步到手机的聊天记录

        # ═══ 待处理队列 ═══
        self._pending_commands: list[dict] = []
        self._lock = threading.Lock()

        # ═══ 手机端权限确认 ═══
        self._perm_events: dict[str, threading.Event] = {}  # perm_id → Event
        self._perm_results: dict[str, bool] = {}            # perm_id → result

        # ═══ 状态 ═══
        self._active = False
        self._connected_devices: dict[str, dict] = {}  # device_id → info
        self._chunk_buffers: dict = {}  # file_id → 分片缓冲区

        # 注册通信服务器回调
        self._server.on_mobile_message = self._on_mobile_message
        self._server.on_mobile_connected = self._on_device_connected
        self._server.on_mobile_disconnected = self._on_device_disconnected

        # 文件接收目录
        self._received_files_dir: Path = Path.home() / "Desktop" / "Nexie_Received"

    # ═══ 初始化 ═══
    def bind_agent(self, agent):
        """绑定 AgentCore 实例 + 注册手机端权限确认钩子"""
        self._agent = agent
        # 注册手机端权限钩子（替代原来的 _skip_permission=True）
        agent._permission_hook = self._mobile_permission_hook

    def _mobile_permission_hook(self, func_name: str, desc: str, risk: str,
                                 allow_always: bool = False):
        """手机端权限确认：推送弹窗到手机，等待用户点击"""
        import uuid
        perm_id = str(uuid.uuid4())[:8]

        # 向所有已连接手机发送权限请求
        msg = {
            "type": "permission_ask",
            "payload": {
                "perm_id": perm_id,
                "tool": func_name,
                "description": desc,
                "risk": risk,
                "allow_always": allow_always,
            }
        }
        sent = False
        for device_id in self._connected_devices:
            try:
                self._server.send_message(device_id, json.dumps(msg))
                sent = True
            except Exception:
                pass

        if not sent:
            # 没有手机连接 → 自动允许（safe/moderate）或拒绝（high）
            logger.info(f"[桥接] 无手机连接，自动{'允许' if risk != 'high' else '拒绝'}: {func_name}")
            return risk != "high"

        # 等待手机响应
        event = threading.Event()
        self._perm_events[perm_id] = event
        logger.info(f"[桥接] 等待手机权限确认: {func_name} | perm_id={perm_id}")

        # 最多等15秒
        if event.wait(timeout=15.0):
            result = self._perm_results.pop(perm_id, True)
            self._perm_events.pop(perm_id, None)
            logger.info(f"[桥接] 手机权限确认: {'允许' if result else '拒绝'}")
            return result
        else:
            self._perm_events.pop(perm_id, None)
            self._perm_results.pop(perm_id, None)
            logger.warning(f"[桥接] 手机权限超时，默认拒绝")
            return False

    def bind_gui(self, gui):
        """绑定 MainWindow 实例 (用于 UI 同步)"""
        self._gui = gui

    def start(self, port: int = None, relay_url: str = ""):
        """启动桥接服务"""
        if not self._server.is_running:
            self._server.start(relay_url=relay_url)
        self._active = True
        self._received_files_dir.mkdir(parents=True, exist_ok=True)
        logger.info("[桥接] 手机桥接层已启动")

    def stop(self):
        """停止桥接服务"""
        self._active = False
        self._server.stop()
        logger.info("[桥接] 手机桥接层已停止")

    # ═══ 设备管理 ═══
    def _on_device_connected(self, device_id: str, info: dict):
        """设备连接回调"""
        self._connected_devices[device_id] = info
        logger.info(f"[桥接] 手机已连接: {device_id}")

        # 同步现有聊天记录到新设备
        if self._gui:
            self._gui.root.after(0, lambda: self._sync_history_to_device(device_id))

    def _on_device_disconnected(self, device_id: str):
        """设备断连回调"""
        self._connected_devices.pop(device_id, None)
        logger.info(f"[桥接] 手机已断开: {device_id}")

        if self._gui:
            self._gui.root.after(0, lambda: self._gui.update_connection_status(
                len(self._connected_devices)))

    @property
    def connected_count(self) -> int:
        return len(self._connected_devices)

    @property
    def is_connected(self) -> bool:
        return len(self._connected_devices) > 0

    # ═══ 核心: 手机消息处理 ═══
    def _on_mobile_message(self, msg: dict, device_id: str):
        """
        处理来自手机的原始消息
        所有 AI 计算在此分发到 AgentCore，手机端不做任何运算
        """
        msg_type = msg.get("type", "")
        payload = msg.get("payload", {})
        msg_id = msg.get("id", "")

        logger.debug(f"[桥接] 收到手机消息: type={msg_type}, device={device_id}")

        if msg_type == "chat":
            self._handle_chat_from_mobile(payload, device_id, msg_id)
        elif msg_type == "file":
            self._handle_file_from_mobile(payload, device_id, msg_id)
        elif msg_type == "file_chunk_start":
            self._handle_chunk_start(payload, device_id)
        elif msg_type == "file_chunk_data":
            self._handle_chunk_data(payload, device_id)
        elif msg_type == "file_chunk_end":
            self._handle_chunk_end(payload, device_id)
        elif msg_type == "command":
            self._handle_command_from_mobile(payload, device_id, msg_id)
        elif msg_type == "permission_response":
            perm_id = payload.get("perm_id", "")
            result = payload.get("result", False)
            if perm_id in self._perm_events:
                self._perm_results[perm_id] = result
                self._perm_events[perm_id].set()
                logger.info(f"[桥接] 手机权限回复: perm_id={perm_id} result={result}")
        elif msg_type == "heartbeat":
            pass  # 通信层已处理
        else:
            logger.warning(f"[桥接] 未知消息类型: {msg_type}")

    def _handle_chat_from_mobile(self, payload: dict, device_id: str, msg_id: str):
        """
        处理手机端发来的聊天消息
        1. 在 PC GUI 中显示
        2. 如果是 AI 对话请求，交给 AgentCore 处理
        3. AgentCore 输出流式推送到手机
        """
        text = payload.get("text", "").strip()
        sender = payload.get("sender", "mobile")

        if not text:
            return

        logger.info(f"[桥接] 手机消息: {text[:50]}...")

        # → 显示在 PC 界面上 (通过队列线程安全派发)
        if self._gui:
            self._gui.gui_queue.put(("mobile_message", text, device_id))

        # → 交给 AgentCore 处理 (所有AI计算在PC端)
        if self._agent:
            def process_in_thread():
                try:
                    # ═══ 回调: 同时推送到手机 + PC GUI ═══
                    def stream_to_mobile(delta_text: str):
                        # → 手机
                        self._server.send_message(device_id, build_chat_message(
                            delta_text, sender="pc"
                        ))
                        # → PC GUI (通过队列线程安全更新)
                        if self._gui:
                            try:
                                self._gui.gui_queue.put(("stream", delta_text))
                                logger.debug(f"[桥接] GUI流式: {delta_text[:30]}...")
                            except Exception as e:
                                logger.error(f"[桥接] GUI推送失败: {e}")

                    def tool_start(name: str, args: dict):
                        # → 手机
                        self._server.send_message(device_id, build_message("status", {
                            "phase": "tool_start",
                            "tool": name,
                            "args": str(args)[:200],
                        }))
                        # → PC GUI
                        if self._gui:
                            self._gui.gui_queue.put(("tool_start", name, args))

                    def tool_result(name: str, result: str):
                        short = result[:200] + "..." if len(result) > 200 else result
                        # → 手机
                        self._server.send_message(device_id, build_message("status", {
                            "phase": "tool_result",
                            "tool": name,
                            "result": short,
                        }))
                        # → PC GUI
                        if self._gui:
                            self._gui.gui_queue.put(("tool_result", name, result))

                    # 执行 Agent (使用手机端权限确认钩子)
                    logger.info(f"[桥接] 开始执行Agent, text={text[:30]}...")
                    full_response = self._agent.process_message(
                        user_message=text,
                        on_text=stream_to_mobile,
                        on_tool_start=tool_start,
                        on_tool_result=tool_result,
                        on_done=lambda r: None,
                    )

                    # PC GUI: 标记处理完成
                    if self._gui:
                        self._gui.gui_queue.put(("processing_done",))
                        logger.info(f"[桥接] Agent执行完成, 已通知GUI")

                    # 流式输出结束通知
                    self._server.send_message(device_id, build_message("status", {
                        "phase": "stream_end",
                    }))

                    # 完成后推送截屏 (如果是 MIMO 模型)
                    if hasattr(self._agent, 'model_type') and self._agent.model_type == 'mimo':
                        self.push_screenshot(device_id)

                except Exception as e:
                    logger.error(f"[桥接] 处理手机消息异常: {e}")
                    self._server.send_message(device_id, build_message("error", {
                        "reason": str(e),
                    }))

            threading.Thread(target=process_in_thread, daemon=True).start()
        else:
            # 无 Agent — 直接回显
            self._server.send_message(device_id, build_chat_message(
                f"收到: {text}\n(Agent 未就绪，消息已记录)",
                sender="pc",
            ))

    def _handle_file_from_mobile(self, payload: dict, device_id: str, msg_id: str):
        """
        处理手机端发来的文件 (图片/拍照/文档)
        保存到本地，加入 Agent 上下文
        """
        filename = payload.get("filename", f"mobile_file_{int(time.time())}")
        file_data_b64 = payload.get("data", "")
        mime_type = payload.get("mime_type", "application/octet-stream")
        file_size = payload.get("size", 0)

        if not file_data_b64:
            self._server.send_message(device_id, build_message("error", {
                "reason": "文件数据为空",
                "ref_id": msg_id,
            }))
            return

        try:
            # 解码并保存文件
            file_bytes = base64.b64decode(file_data_b64)
            save_path = self._received_files_dir / filename

            # 处理重名
            counter = 1
            stem = save_path.stem
            suffix = save_path.suffix
            while save_path.exists():
                save_path = self._received_files_dir / f"{stem}_{counter}{suffix}"
                counter += 1

            save_path.write_bytes(file_bytes)

            logger.info(f"[桥接] 收到手机文件: {filename} → {save_path} ({len(file_bytes)} bytes)")

            # 回复手机端
            self._server.send_message(device_id, build_message("status", {
                "phase": "file_received",
                "filename": save_path.name,
                "path": str(save_path),
                "size": len(file_bytes),
            }))

            # 通知 PC GUI
            if self._gui:
                self._gui.root.after(0, lambda: self._gui.display_mobile_file(
                    save_path.name, str(save_path), mime_type))

        except Exception as e:
            logger.error(f"[桥接] 文件保存失败: {e}")
            self._server.send_message(device_id, build_message("error", {
                "reason": f"文件保存失败: {e}",
                "ref_id": msg_id,
            }))

    # ═══ 分片文件接收 ═══
    def _handle_chunk_start(self, payload: dict, device_id: str):
        """接收分片起始消息，初始化缓冲区"""
        file_id = payload.get("file_id", "")
        if not file_id:
            return
        self._chunk_buffers[file_id] = {
            "filename": payload.get("filename", "file"),
            "mime_type": payload.get("mime_type", "application/octet-stream"),
            "total_size": payload.get("total_size", 0),
            "total_chunks": payload.get("total_chunks", 0),
            "chunks": {},
            "received_time": time.time(),
        }
        logger.debug(f"[桥接] 分片文件开始: {file_id} ({payload.get('filename')}, {payload.get('total_chunks')}片)")

    def _handle_chunk_data(self, payload: dict, device_id: str):
        """接收分片数据"""
        file_id = payload.get("file_id", "")
        chunk_idx = payload.get("chunk_index", 0)
        data = payload.get("data", "")

        buf = self._chunk_buffers.get(file_id)
        if not buf:
            logger.warning(f"[桥接] 分片文件无缓冲区: {file_id}")
            return

        buf["chunks"][chunk_idx] = data
        buf["received_time"] = time.time()

    def _handle_chunk_end(self, payload: dict, device_id: str):
        """接收分片结束消息，组装并保存文件"""
        file_id = payload.get("file_id", "")
        buf = self._chunk_buffers.pop(file_id, None)
        if not buf:
            logger.warning(f"[桥接] 分片结束但无缓冲区: {file_id}")
            return

        # 组装分片
        chunks = buf["chunks"]
        sorted_data = "".join(chunks[i] for i in sorted(chunks.keys()))
        filename = buf["filename"]
        mime_type = buf["mime_type"]
        total_size = buf["total_size"]

        # 检查完整性
        expected = buf["total_chunks"]
        actual = len(chunks)
        if actual < expected:
            logger.warning(f"[桥接] 分片不完整: {filename} ({actual}/{expected})")

        logger.info(f"[桥接] 分片文件完成: {filename} ({len(sorted_data)} bytes, {actual}片)")

        # 走正常文件保存流程
        self._handle_file_from_mobile({
            "filename": filename,
            "data": sorted_data,
            "mime_type": mime_type,
            "size": total_size,
        }, device_id, file_id)

    def _handle_command_from_mobile(self, payload: dict, device_id: str, msg_id: str):
        """
        处理手机端远程指令 (预留扩展)
        所有指令在 PC 端执行
        """
        command = payload.get("command", "")
        params = payload.get("params", {})

        logger.info(f"[桥接] 手机指令: {command}")

        # 支持的远程指令
        if command == "capture_screen":
            self.push_screenshot(device_id)
        elif command == "get_status":
            self._server.send_message(device_id, build_message("status", {
                "agent_running": self._agent.is_processing if self._agent else False,
                "connected_devices": self.connected_count,
                "server_time": int(time.time() * 1000),
            }))
        elif command == "cancel":
            if self._agent:
                self._agent.cancel()
            self._server.send_message(device_id, build_message("status", {
                "phase": "cancelled",
            }))
        else:
            self._server.send_message(device_id, build_message("command_result", {
                "command": command,
                "success": False,
                "reason": f"未知指令: {command}",
                "ref_id": msg_id,
            }))

    # ═══ 主动推送 (PC → 手机) ═══

    def push_chat(self, text: str, device_id: str = None):
        """推送聊天消息到手机"""
        msg = build_chat_message(text, sender="pc")
        if device_id:
            self._server.send_message(device_id, msg)
        else:
            self._server.broadcast_message(msg)

    def push_stream_chunk(self, delta: str):
        """推送流式文本片段到所有已连接设备"""
        self._server.broadcast_message(build_chat_message(delta, sender="pc"))

    def push_tool_status(self, tool_name: str, result_summary: str):
        """推送工具执行状态"""
        self._server.broadcast_message(build_message("status", {
            "phase": "tool_exec",
            "tool": tool_name,
            "summary": result_summary[:200],
        }))

    def push_screenshot(self, device_id: str = None):
        """截屏并推送到手机 — 存JPEG文件，发送HTTP下载URL"""
        try:
            from tools import get_last_screenshot, capture_screen
            from communication import NexieServer

            # 如果还没有截图，先截一个；否则直接用缓存的
            b64, w, h = get_last_screenshot()
            if not b64:
                capture_screen()
                b64, w, h = get_last_screenshot()

            if b64:
                img_bytes = base64.b64decode(b64)
                http_dir = Path(__file__).parent / "Iagent_data" / "uploads"
                http_dir.mkdir(parents=True, exist_ok=True)
                ts = int(time.time())
                filename = f"screenshot_{ts}.jpg"
                filepath = http_dir / filename
                filepath.write_bytes(img_bytes)

                msg = build_message("screenshot", {
                    "sender": "pc",
                    "width": w, "height": h,
                    "size": len(img_bytes),
                    "file_path": filename,
                    "download_url": f"/files/{filename}",
                })
                if device_id:
                    self._server.send_message(device_id, msg)
                else:
                    self._server.broadcast_message(msg)
                logger.info(f"[桥接] 截屏已推送: /files/{filename} ({w}x{h})")
        except Exception as e:
            logger.error(f"[桥接] 截屏推送失败: {e}")

    def push_file(self, filepath: str, device_id: str = None):
        """推送文件到手机 — 复制到HTTP目录，发下载URL（不走WS base64）"""
        import shutil
        try:
            path = Path(filepath)
            if not path.exists():
                return

            file_bytes = path.read_bytes()
            if len(file_bytes) > 20 * 1024 * 1024:
                self.push_error(f"文件过大({len(file_bytes)//1024//1024}MB)，无法推送", device_id)
                return

            http_dir = Path(__file__).parent / "Iagent_data" / "uploads"
            http_dir.mkdir(parents=True, exist_ok=True)
            dest = http_dir / path.name
            c = 1
            stem, suffix = dest.stem, dest.suffix
            while dest.exists():
                dest = http_dir / f"{stem}_{c}{suffix}"
                c += 1
            shutil.copy2(path, dest)

            # 发送文件路径，让手机端自己拼接URL（手机已知PC的IP）
            msg = build_message("file", {
                "sender": "pc",
                "filename": path.name,
                "mime_type": "application/octet-stream",
                "size": len(file_bytes),
                "file_path": dest.name,  # 相对路径，手机端自行拼接 http://host:9528/files/
                "download_url": f"/files/{dest.name}",  # 手机端会拼接完整URL
            })
            if device_id:
                self._server.send_message(device_id, msg)
            else:
                self._server.broadcast_message(msg)

            logger.info(f"[桥接] 文件已推送: {path.name} → /files/{dest.name}")
        except Exception as e:
            logger.error(f"[桥接] 文件推送失败: {e}")

    def push_error(self, error_text: str, device_id: str = None):
        """推送错误消息"""
        msg = build_message("error", {"reason": error_text})
        if device_id:
            self._server.send_message(device_id, msg)
        else:
            self._server.broadcast_message(msg)

    # ═══ 聊天历史同步 ═══
    def _sync_history_to_device(self, device_id: str):
        """将近期聊天历史同步到新连接的设备"""
        if not self._chat_history:
            return

        for entry in self._chat_history[-20:]:  # 最近20条
            if entry.get("type") == "user":
                self._server.send_message(device_id, build_chat_message(
                    entry.get("text", ""), sender="user"))
            elif entry.get("type") == "ai":
                self._server.send_message(device_id, build_chat_message(
                    entry.get("text", ""), sender="pc"))

    def record_chat(self, entry: dict):
        """记录聊天条目到同步历史"""
        self._chat_history.append(entry)
        if len(self._chat_history) > 200:
            self._chat_history = self._chat_history[-200:]

    # ═══ 连接信息 ═══
    def get_connection_info(self) -> dict:
        return self._server.get_connection_info()

    def get_qr_data(self) -> str:
        return self._server.get_qr_data()

    def refresh_secret(self) -> str:
        """手动刷新连接密钥"""
        return self._server.regenerate_secret()


# ==================== 全局单例 ====================

_bridge_instance: Optional[MobileBridge] = None


def get_bridge() -> MobileBridge:
    """获取桥接层单例"""
    global _bridge_instance
    if _bridge_instance is None:
        _bridge_instance = MobileBridge()
    return _bridge_instance


def init_bridge(port: int = None, relay_url: str = "") -> MobileBridge:
    """初始化桥接层"""
    bridge = get_bridge()
    bridge.start(port=port, relay_url=relay_url)
    return bridge
