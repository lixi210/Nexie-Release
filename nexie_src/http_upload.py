# Nexie — AI Coding Agent
# Copyright (c) 2026. All rights reserved.
# 本软件仅供个人学习与研究使用，禁止未经授权的商业用途、分发或修改后闭源。
# Licensed for personal, educational, and non-commercial use only.
"""
Nexie HTTP 文件上传服务器
手机端通过 HTTP POST 上传文件，避免 WebSocket 消息大小限制
"""
import json
import base64
import threading
import logging
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, unquote

logger = logging.getLogger("Nexie.HttpUpload")

# 上传接收目录
DATA_ROOT = Path(__file__).parent / "Iagent_data"
UPLOAD_DIR = DATA_ROOT / "uploads"


class UploadHandler(BaseHTTPRequestHandler):
    """处理手机端文件上传"""

    # 类变量: 接收到的文件回调
    on_file_received = None  # (filepath, filename, mime_type) -> None

    def do_OPTIONS(self):
        """CORS 预检 — 允许手机端跨域访问"""
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', '*')
        self.end_headers()

    def do_GET(self):
        """文件下载 — 手机端通过 HTTP GET 获取 PC 推送的文件"""
        if self.path.startswith('/files/'):
            filename = unquote(self.path[7:])  # /files/xxx → xxx (URL解码中文)
            filepath = UPLOAD_DIR / filename
            if filepath.exists() and filepath.is_file():
                self.send_response(200)
                self.send_header('Content-Type', 'application/octet-stream')
                self.send_header('Content-Length', str(filepath.stat().st_size))
                self.send_header('Access-Control-Allow-Origin', '*')
                self.end_headers()
                self.wfile.write(filepath.read_bytes())
            else:
                self.send_response(404)
                self.end_headers()
        else:
            self.send_response(200)
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(b'Nexie Upload Server')

    def do_POST(self):
        if self.path == '/upload':
            self._handle_upload()
        else:
            self.send_response(404)
            self.end_headers()

    def _handle_upload(self):
        try:
            content_type = self.headers.get('Content-Type', '')
            content_length = int(self.headers.get('Content-Length', 0))

            if content_length <= 0:
                self._send_json({"error": "空请求"}, 400)
                return

            if content_length > 50 * 1024 * 1024:  # 50MB上限
                self._send_json({"error": "文件过大"}, 413)
                return

            raw = self.rfile.read(content_length)

            # 解析 multipart/form-data
            if 'multipart/form-data' in content_type:
                # 提取 boundary
                boundary = None
                for part in content_type.split(';'):
                    part = part.strip()
                    if part.startswith('boundary='):
                        boundary = part[9:].strip('"')
                        break

                if not boundary:
                    self._send_json({"error": "缺少boundary"}, 400)
                    return

                # 解析 multipart body
                filename, file_data, mime_type = self._parse_multipart(raw, boundary.encode())
                if filename and file_data:
                    self._save_and_notify(filename, file_data, mime_type)
                    self._send_json({"ok": True, "filename": filename, "size": len(file_data)})
                else:
                    self._send_json({"error": "解析文件失败"}, 400)
            else:
                # 纯二进制上传 (备选)
                filename = self.headers.get('X-Filename', 'upload.bin')
                mime_type = self.headers.get('X-Mime-Type', 'application/octet-stream')
                self._save_and_notify(filename, raw, mime_type)
                self._send_json({"ok": True, "filename": filename, "size": len(raw)})

        except Exception as e:
            logger.error(f"[HTTP] 上传异常: {e}")
            self._send_json({"error": str(e)}, 500)

    def _parse_multipart(self, data: bytes, boundary: bytes):
        """简单 multipart 解析"""
        parts = data.split(b'--' + boundary)
        for part in parts:
            if b'Content-Disposition' not in part:
                continue

            header_end = part.find(b'\r\n\r\n')
            if header_end < 0:
                continue

            headers_raw = part[:header_end].decode('utf-8', errors='ignore')
            body = part[header_end + 4:]

            # 去掉末尾的 \r\n--
            if body.endswith(b'\r\n'):
                body = body[:-2]

            filename = None
            for line in headers_raw.split('\r\n'):
                if 'filename=' in line:
                    fn_start = line.index('filename="') + 10
                    fn_end = line.index('"', fn_start)
                    filename = line[fn_start:fn_end]

            if filename and body:
                mime_type = 'application/octet-stream'
                if 'Content-Type:' in headers_raw:
                    ct_start = headers_raw.index('Content-Type:') + 13
                    ct_end = headers_raw.find('\r\n', ct_start)
                    if ct_end > ct_start:
                        mime_type = headers_raw[ct_start:ct_end].strip()
                return filename, body, mime_type

        return None, None, None

    def _save_and_notify(self, filename: str, file_data: bytes, mime_type: str = ''):
        UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        filepath = UPLOAD_DIR / filename

        # 处理重名
        counter = 1
        stem, suffix = filepath.stem, filepath.suffix
        while filepath.exists():
            filepath = UPLOAD_DIR / f"{stem}_{counter}{suffix}"
            counter += 1

        filepath.write_bytes(file_data)
        logger.info(f"[HTTP] 收到文件: {filename} → {filepath} ({len(file_data)} bytes)")

        # 回调通知
        if UploadHandler.on_file_received:
            UploadHandler.on_file_received(str(filepath), filepath.name, mime_type)

    def _send_json(self, data: dict, status: int = 200):
        resp = json.dumps(data).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Content-Length', str(len(resp)))
        self.end_headers()
        self.wfile.write(resp)

    def log_message(self, format, *args):
        pass  # 关闭HTTP访问日志


class HttpUploadServer:
    """HTTP上传服务器 — 运行在独立线程"""

    def __init__(self, port: int = 9528):
        self._port = port
        self._server: HTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._running = False

    def start(self):
        if self._running:
            return
        try:
            self._server = HTTPServer(('0.0.0.0', self._port), UploadHandler)
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()
            self._running = True
            logger.info(f"[HTTP] 上传服务器已启动: 0.0.0.0:{self._port}")
        except OSError as e:
            logger.warning(f"[HTTP] 端口{self._port}被占用，尝试{self._port + 1}")
            self._port += 1
            self.start()

    def _run(self):
        try:
            self._server.serve_forever()
        except Exception:
            pass

    def stop(self):
        self._running = False
        if self._server:
            try:
                self._server.shutdown()
            except Exception:
                pass
        if self._thread:
            self._thread.join(timeout=2)

    @property
    def port(self) -> int:
        return self._port


# 单例
_http_server: HttpUploadServer | None = None


def get_http_upload_server(port: int = 9528) -> HttpUploadServer:
    global _http_server
    if _http_server is None:
        _http_server = HttpUploadServer(port)
    return _http_server


def set_file_callback(callback):
    """设置文件接收回调"""
    UploadHandler.on_file_received = callback
