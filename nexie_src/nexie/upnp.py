# Nexie — AI Coding Agent
# Copyright (c) 2026. All rights reserved.
# 本软件仅供个人学习与研究使用，禁止未经授权的商业用途、分发或修改后闭源。
# Licensed for personal, educational, and non-commercial use only.
"""
Nexie — UPnP 端口映射（零配置内网穿透）
启动时自动在路由器上打开端口，手机从公网直连，无需VPS。
纯标准库实现，无外部依赖。
"""
import socket
import re
import urllib.request
import urllib.parse
import http.client
import logging
import threading
from xml.etree import ElementTree as ET

logger = logging.getLogger("Nexie.UPnP")

# UPnP 常量
SSDP_ADDR = ("239.255.255.250", 1900)
SSDP_MX = 2  # 搜索超时秒数
SSDP_ST = "urn:schemas-upnp-org:device:InternetGatewayDevice:1"


class UPnPMapper:
    """
    UPnP 端口映射器 — 纯Python标准库实现
    自动在支持UPnP的路由器上打开/关闭端口映射
    """

    def __init__(self, local_port: int = 9527):
        self._local_port = local_port
        self._external_port = local_port
        self._control_url = ""        # IGD 控制URL
        self._service_type = ""       # WANIPConnection 或 WANPPPConnection
        self._public_ip = ""          # 公网IP
        self._mapped = False          # 是否已映射
        self._local_ip = self._get_local_ip()

    # ═══ 公开 API ═══

    def try_map(self, timeout: float = 5.0) -> bool:
        """
        尝试UPnP端口映射。成功返回True。
        整个过程<5秒，不影响启动速度。
        """
        try:
            # 1. 发现IGD设备
            if not self._discover_igd(timeout):
                logger.debug("未发现UPnP IGD设备（路由器可能不支持或已禁用UPnP）")
                return False

            # 2. 获取公网IP
            self._public_ip = self._get_external_ip()
            if not self._public_ip:
                logger.debug("无法获取公网IP")
                return False

            # 3. 添加端口映射
            if self._add_port_mapping():
                self._mapped = True
                logger.info(f"UPnP映射成功: {self._public_ip}:{self._external_port}"
                           f" → {self._local_ip}:{self._local_port}")
                return True
            return False
        except Exception as e:
            logger.debug(f"UPnP映射失败: {e}")
            return False

    def remove_map(self):
        """移除端口映射（应用退出时调用）"""
        if not self._mapped or not self._control_url:
            return
        try:
            self._send_soap("DeletePortMapping", f"""
                <NewRemoteHost></NewRemoteHost>
                <NewExternalPort>{self._external_port}</NewExternalPort>
                <NewProtocol>TCP</NewProtocol>
            """)
            logger.info("UPnP端口映射已移除")
            self._mapped = False
        except Exception as e:
            logger.debug(f"移除UPnP映射异常: {e}")

    @property
    def public_url(self) -> str:
        """公网连接地址"""
        if self._mapped and self._public_ip:
            return f"ws://{self._public_ip}:{self._external_port}"
        return ""

    @property
    def lan_url(self) -> str:
        """局域网连接地址"""
        return f"ws://{self._local_ip}:{self._local_port}"

    def get_connection_url(self) -> str:
        """获取最佳连接地址（公网优先，回退局域网）"""
        return self.public_url or self.lan_url

    # ═══ 内部实现 ═══

    def _get_local_ip(self) -> str:
        """获取本机局域网IP"""
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except Exception:
            return "127.0.0.1"

    def _discover_igd(self, timeout: float) -> bool:
        """通过SSDP发现UPnP IGD设备"""
        # 发送SSDP M-SEARCH
        msg = (
            "M-SEARCH * HTTP/1.1\r\n"
            f"HOST: {SSDP_ADDR[0]}:{SSDP_ADDR[1]}\r\n"
            "MAN: \"ssdp:discover\"\r\n"
            f"MX: {SSDP_MX}\r\n"
            f"ST: {SSDP_ST}\r\n\r\n"
        )

        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.settimeout(timeout)
            sock.sendto(msg.encode(), SSDP_ADDR)

            # 收集响应
            responses = []
            try:
                while True:
                    data, addr = sock.recvfrom(4096)
                    responses.append(data.decode(errors="ignore"))
            except socket.timeout:
                pass
            finally:
                sock.close()

            # 解析响应，提取LOCATION
            for resp in responses:
                location = self._parse_ssdp(resp)
                if location:
                    if self._fetch_control_url(location):
                        return True
            return False
        except Exception:
            return False

    def _parse_ssdp(self, response: str) -> str:
        """从SSDP响应中提取LOCATION"""
        for line in response.split("\r\n"):
            if line.lower().startswith("location:"):
                return line.split(":", 1)[1].strip()
        return ""

    def _fetch_control_url(self, location: str) -> bool:
        """从设备描述XML中提取WANIPConnection控制URL"""
        try:
            xml_data = self._http_get(location)
            if not xml_data:
                return False

            root = ET.fromstring(xml_data)

            # 查找 WANIPConnection 或 WANPPPConnection 服务
            ns = {"ns": "urn:schemas-upnp-org:device-1-0"}
            for service in root.iter("{urn:schemas-upnp-org:device-1-0}service"):
                st = service.find("{urn:schemas-upnp-org:device-1-0}serviceType")
                if st is not None and "WAN" in (st.text or ""):
                    cu = service.find("{urn:schemas-upnp-org:device-1-0}controlURL")
                    if cu is not None:
                        # 构建完整控制URL
                        base = location
                        if "#" in base:
                            base = base[:base.index("#")]
                        if not base.endswith("/"):
                            base = base[:base.rindex("/") + 1]
                        self._control_url = urllib.parse.urljoin(base, cu.text)
                        self._service_type = st.text
                        logger.debug(f"IGD发现: {self._control_url}")
                        return True
            return False
        except Exception as e:
            logger.debug(f"解析IGD描述失败: {e}")
            return False

    def _get_external_ip(self) -> str:
        """获取公网IP（优先从UPnP获取，失败则用在线服务）"""
        # 尝试从UPnP获取
        if self._control_url:
            try:
                resp = self._send_soap("GetExternalIPAddress", "")
                match = re.search(r"<NewExternalIPAddress>(.*?)</NewExternalIPAddress>", resp)
                if match:
                    return match.group(1).strip()
            except Exception:
                pass

        # 回退：在线服务
        try:
            with urllib.request.urlopen("https://api.ipify.org", timeout=3) as r:
                return r.read().decode().strip()
        except Exception:
            pass
        return ""

    def _add_port_mapping(self) -> bool:
        """添加TCP端口映射"""
        body = f"""
            <NewRemoteHost></NewRemoteHost>
            <NewExternalPort>{self._external_port}</NewExternalPort>
            <NewProtocol>TCP</NewProtocol>
            <NewInternalPort>{self._local_port}</NewInternalPort>
            <NewInternalClient>{self._local_ip}</NewInternalClient>
            <NewEnabled>1</NewEnabled>
            <NewPortMappingDescription>Nexie Remote</NewPortMappingDescription>
            <NewLeaseDuration>0</NewLeaseDuration>
        """
        try:
            resp = self._send_soap("AddPortMapping", body)
            return "<errorCode>" not in resp.lower()
        except Exception as e:
            logger.debug(f"添加端口映射失败: {e}")
            return False

    def _send_soap(self, action: str, body: str) -> str:
        """发送UPnP SOAP请求"""
        soap = (
            '<?xml version="1.0"?>'
            '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
            's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
            '<s:Body>'
            f'<u:{action} xmlns:u="{self._service_type}">'
            f'{body}'
            f'</u:{action}>'
            '</s:Body>'
            '</s:Envelope>'
        )

        url_parts = urllib.parse.urlparse(self._control_url)
        conn = http.client.HTTPConnection(url_parts.hostname, url_parts.port, timeout=3)
        headers = {
            "Content-Type": 'text/xml; charset="utf-8"',
            "SOAPACTION": f'"{self._service_type}#{action}"',
            "Content-Length": str(len(soap)),
        }
        conn.request("POST", url_parts.path, soap.encode(), headers)
        resp = conn.getresponse()
        result = resp.read().decode(errors="ignore")
        conn.close()
        return result

    @staticmethod
    def _http_get(url: str, timeout: float = 3.0) -> str:
        """HTTP GET请求"""
        try:
            with urllib.request.urlopen(url, timeout=timeout) as r:
                return r.read().decode(errors="ignore")
        except Exception:
            return ""


# ═══════════════════════════════════════════
# 全局单例
# ═══════════════════════════════════════════

_upnp_mapper: UPnPMapper = None


def get_upnp_mapper(port: int = 9527) -> UPnPMapper:
    global _upnp_mapper
    if _upnp_mapper is None:
        _upnp_mapper = UPnPMapper(port)
    return _upnp_mapper
