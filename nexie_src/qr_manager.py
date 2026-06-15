# Nexie — AI Coding Agent
# Copyright (c) 2026. All rights reserved.
# 本软件仅供个人学习与研究使用，禁止未经授权的商业用途、分发或修改后闭源。
# Licensed for personal, educational, and non-commercial use only.
"""
Nexie QR 管理器 — 专属互联二维码生成
- 二维码基于连接密钥(room_secret)生成
- 不自动刷新，用户可手动刷新重新生成密钥和二维码
- 支持 PIL/Pillow 渲染为 PNG 图片，供 Tkinter 显示
"""
import json
import io
import base64
from pathlib import Path
from typing import Optional

try:
    import qrcode
    from qrcode.image.pil import PilImage
    _QR_AVAILABLE = True
except ImportError:
    _QR_AVAILABLE = False

try:
    from PIL import Image, ImageDraw, ImageFont
    _PIL_AVAILABLE = True
except ImportError:
    _PIL_AVAILABLE = False


class QRManager:
    """
    QR 码管理器
    - 基于通信服务器的 room_secret 生成二维码
    - 二维码内容为标准 JSON，包含连接所需全部信息
    - 不自动刷新，仅提供手动 regenerate() 方法
    """

    # 二维码尺寸配置
    DEFAULT_SIZE = 320       # 输出图片尺寸 (px)
    BORDER_WIDTH = 2         # 二维码边框模块数
    FILL_COLOR = "#1b1b1f"   # 前景色 (深色模块)
    BACK_COLOR = "#ffffff"   # 背景色
    LOGO_SIZE_RATIO = 0.22   # 中心Logo占比

    def __init__(self, connection_info_provider):
        """
        Args:
            connection_info_provider: callable → dict
                返回连接信息的函数，如 communication.OpAgentServer.get_connection_info
        """
        self._info_provider = connection_info_provider
        self._qr_image: Optional[Image.Image] = None
        self._qr_b64: str = ""
        self._generated_at: str = ""

    # ═══ 二维码数据 ═══
    def get_connection_info(self) -> dict:
        """获取当前连接信息"""
        if callable(self._info_provider):
            return self._info_provider()
        return {}

    def get_qr_data(self) -> str:
        """获取二维码编码的原始 JSON 字符串"""
        info = self.get_connection_info()
        return json.dumps(info, ensure_ascii=False)

    # ═══ 生成二维码 ═══
    def generate(self) -> bool:
        """
        生成二维码图片
        Returns: 是否成功
        """
        if not _QR_AVAILABLE:
            return False

        try:
            data = self.get_qr_data()

            # 创建 QR 码
            qr = qrcode.QRCode(
                version=None,  # 自动版本
                error_correction=qrcode.constants.ERROR_CORRECT_H,  # 高纠错 (允许加Logo)
                box_size=10,
                border=self.BORDER_WIDTH,
            )
            qr.add_data(data)
            qr.make(fit=True)

            # 渲染为 PIL Image
            self._qr_image = qr.make_image(
                fill_color="black",
                back_color="white",
                image_factory=PilImage,
            ).convert("RGB")

            # 缩放到目标尺寸
            self._qr_image = self._qr_image.resize(
                (self.DEFAULT_SIZE, self.DEFAULT_SIZE),
                Image.LANCZOS if _PIL_AVAILABLE else Image.NEAREST,
            )

            # 添加中心 Logo (OpAgent 标识)
            self._add_center_logo()

            # 转换为 Base64
            buf = io.BytesIO()
            self._qr_image.save(buf, format="PNG")
            self._qr_b64 = base64.b64encode(buf.getvalue()).decode("ascii")

            from datetime import datetime
            self._generated_at = datetime.now().strftime("%H:%M:%S")

            return True
        except Exception as e:
            import logging
            logging.getLogger("Nexie.QR").error(f"二维码生成失败: {e}")
            return False

    def regenerate(self) -> bool:
        """
        手动刷新: 重新生成连接密钥并生成新二维码
        由通信模块调用 regenerate_secret() 后重新渲染
        """
        return self.generate()

    # ═══ 中心 Logo ═══
    def _add_center_logo(self):
        """在二维码中心添加 Nexie 标识"""
        if not _PIL_AVAILABLE or self._qr_image is None:
            return

        try:
            logo_size = int(self.DEFAULT_SIZE * self.LOGO_SIZE_RATIO)
            # 确保 logo 尺寸为奇数，使中心像素对齐
            if logo_size % 2 == 0:
                logo_size += 1

            # 创建圆形Logo
            logo = Image.new("RGBA", (logo_size, logo_size), (0, 0, 0, 0))
            draw = ImageDraw.Draw(logo)

            # 背景圆
            margin = 3
            draw.ellipse(
                [margin, margin, logo_size - margin, logo_size - margin],
                fill=(27, 27, 31, 255),  # #1b1b1f
                outline=(108, 108, 224, 255),  # #6c6ce0
                width=2,
            )

            # 文字 "NX" (Nexie)
            try:
                font_size = logo_size // 3
                font = ImageFont.truetype("arial.ttf", font_size)
            except Exception:
                font = ImageFont.load_default()

            text = "OP"
            bbox = draw.textbbox((0, 0), text, font=font)
            tw = bbox[2] - bbox[0]
            th = bbox[3] - bbox[1]
            tx = (logo_size - tw) // 2
            ty = (logo_size - th) // 2
            draw.text((tx, ty), text, fill=(108, 108, 224, 255), font=font)

            # 粘贴 Logo 到二维码中心
            pos_x = (self.DEFAULT_SIZE - logo_size) // 2
            pos_y = (self.DEFAULT_SIZE - logo_size) // 2

            qr_rgba = self._qr_image.convert("RGBA")
            qr_rgba.paste(logo, (pos_x, pos_y), logo)
            self._qr_image = qr_rgba

        except Exception:
            pass  # Logo 失败不影响主体 QR

    # ═══ 获取结果 ═══
    def get_image(self) -> Optional[Image.Image]:
        """获取 PIL Image 对象"""
        if self._qr_image is None:
            self.generate()
        return self._qr_image

    def get_image_base64(self) -> str:
        """获取 Base64 编码的 PNG 图片"""
        if not self._qr_b64:
            self.generate()
        return self._qr_b64

    def get_generated_time(self) -> str:
        return self._generated_at

    # ═══ 保存到文件 ═══
    def save_to_file(self, path: str) -> bool:
        """将二维码保存为 PNG 文件"""
        img = self.get_image()
        if img:
            try:
                img.save(path, format="PNG")
                return True
            except Exception:
                pass
        return False


# ==================== UniApp 对接规范 (文档) ====================

UNIAPP_INTEGRATION_SPEC = """
# Nexie 手机端 (UniApp) 对接接口规范

## 1. 通信协议
- 协议: WebSocket (RFC 6455)
- 编码: UTF-8 JSON
- 加密: AES-256-GCM (密钥 = SHA256(room_secret))
- 版本: 1.0.0

## 2. 扫码连接流程
1. 手机扫描电脑端二维码 → 解析 JSON 获取连接信息
2. 建立 WebSocket 连接到 ws://HOST:PORT 或 wss://RELAY_URL
3. 发送认证消息 (JSON):
   { "type": "auth", "version": "1.0.0", "id": "xxx", "timestamp": xxx,
     "payload": { "room_secret": "<密钥>", "device_id": "<唯一ID>", "device_name": "iPhone 15" } }
4. 收到 auth_ok → 连接成功，可收发消息

## 3. 消息类型
| type | 方向 | 说明 |
|------|------|------|
| chat | 双向 | 聊天消息 |
| file | 双向 | 文件传输(base64) |
| screenshot | PC→手机 | 截屏推送 |
| command | 手机→PC | 远程命令 |
| command_result | PC→手机 | 命令结果 |
| heartbeat | 双向 | 心跳(30s间隔) |
| status | 双向 | 状态同步 |

## 4. 心跳机制
- 客户端每 30s 发送 heartbeat
- 服务器 90s 未收到心跳则断开
- 断线后自动重连 (指数退避, 最大 30s)

## 5. 文件传输
- 最大单文件: 10MB
- 编码: Base64 嵌入 JSON
- 支持类型: image/jpeg, image/png, image/heic, application/pdf, text/*

## 6. 设备绑定
- 首次扫码后 room_secret 保存在手机本地
- 下次启动自动连接 (无需重新扫码)
- PC 端可手动刷新密钥 (手机会断开，需重新扫码)
"""
