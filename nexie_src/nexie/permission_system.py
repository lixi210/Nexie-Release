# Nexie — AI Coding Agent
# Copyright (c) 2026. All rights reserved.
# 本软件仅供个人学习与研究使用，禁止未经授权的商业用途、分发或修改后闭源。
# Licensed for personal, educational, and non-commercial use only.
"""
Nexie — 权限控制系统
全盘放开读写访问，仅黑名单拦截破坏性指令。
Agent可自由操作本地任意文件，黑名单拦截：del/rm/format/shutdown/diskpart/注册表删除。
"""
import sys
import os
import re
import shlex
import logging
import threading
from pathlib import Path
from typing import Optional

logger = logging.getLogger("Nexie.Permission")

# ═══════════════════════════════════════════
# 黑名单：破坏性指令拦截规则
# ═══════════════════════════════════════════

# 命令级黑名单（正则匹配）
BLOCKED_COMMAND_PATTERNS = [
    # 文件删除
    r'\brm\s+-rf\b',              # rm -rf (Unix)
    r'\brm\s+-r\b',               # rm -r
    r'\bdel\b.*\/[fFqQ]',         # del /f /q (Windows)
    r'\bdel\s+\/[fF]\s+\/[sS]',   # del /f /s (递归删除)
    r'\berase\b',                  # erase
    r'\brmdir\b.*\/[sS]',         # rmdir /s (递归删除目录)

    # 磁盘操作
    r'\bformat\b',                # format (格式化)
    r'\bdiskpart\b',              # diskpart (磁盘分区)
    r'\bfdisk\b',                 # fdisk
    r'\bmkfs\b',                  # mkfs (创建文件系统)
    r'\bdd\s+if=',                # dd (磁盘复制)

    # 系统破坏
    r'\bshutdown\b',              # shutdown
    r'\breboot\b',                # reboot
    r'\bhalt\b',                  # halt
    r'\bpoweroff\b',              # poweroff
    r'\binit\s+[06]\b',           # init 0/6

    # 注册表删除
    r'\breg\s+delete\b',          # reg delete
    r'\breg\s+del\b',             # reg del
    r'reg\s+add\b.*\/d\s*["\']?\s*["\']?',  # reg add 空值（变相删除）
    r'Remove-ItemProperty\b',     # PowerShell 删除注册表
    r'Remove-Item\s+.*HKLM',     # PowerShell 删除HKLM
    r'DeleteSubKey\b',            # .NET 删除注册表子键

    # 危险系统修改
    r'\bchmod\s+777\b',           # 过度权限
    r'\bchmod\s+-R\s+777\b',      # 递归过度权限
    r'\bchown\s+root\b',          # 改变所有者为root
    r'>\s*\/dev\/sd[a-z]',        # 直接写入块设备

    # 批量危险操作
    r'\brm\s+.*\*',               # rm 通配符
    r'\bdel\s+.*\*',              # del 通配符
]

# 路径黑名单（不允许删除/修改的路径）
PROTECTED_PATHS = [
    # Windows 系统目录
    r'C:\\Windows\\System32',
    r'C:\\Windows\\SysWOW64',
    r'C:\\Windows\\Boot',
    r'C:\\Windows\\System',
    r'C:\\Windows\\WinSxS',
    # Windows 引导
    r'C:\\Boot',
    r'C:\\EFI',
    # Unix 系统目录
    r'/etc/(?!.*\.conf$)',        # /etc 但允许修改.conf
    r'/boot',
    r'/sys',
    r'/proc',
    r'/dev/(?!null|zero|random|urandom)',  # /dev 但允许null/zero/random
    r'/usr/lib(?!.*python)',     # /usr/lib 但允许python
    r'/usr/lib64',
    r'/lib/modules',
]

# 写操作黑名单（这些工具函数涉及写操作时检查路径）
WRITE_OPERATIONS = {"write_file", "edit_file", "move_file", "rename_file",
                    "create_directory", "delete_file", "delete_directory"}

# 删除操作（完全禁止在受保护路径上执行）
DELETE_OPERATIONS = {"delete_file", "delete_directory", "rm", "del", "rmdir"}


class PermissionController:
    """
    权限控制器：全盘读写默认允许，黑名单拦截破坏性指令。
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._block_count = 0
        self._allow_count = 0
        self._block_log: list[dict] = []  # 最近拦截记录

    def check_command(self, command: str, cwd: str = None) -> tuple[bool, str]:
        """
        检查命令是否被拦截。
        返回: (allowed: bool, reason: str)
        """
        if not command or not command.strip():
            return True, ""

        cmd_stripped = command.strip()

        # 检查命令级黑名单
        for pattern in BLOCKED_COMMAND_PATTERNS:
            if re.search(pattern, cmd_stripped, re.IGNORECASE):
                reason = f"🚫 拦截破坏性指令 (匹配规则: {pattern})"
                self._record_block(command, pattern, reason)
                return False, reason

        # 允许执行
        with self._lock:
            self._allow_count += 1
        return True, ""

    def check_file_operation(self, operation: str, file_path: str) -> tuple[bool, str]:
        """
        检查文件操作是否被拦截。
        - operation: write_file, delete_file, edit_file, etc.
        - file_path: 目标文件路径
        返回: (allowed: bool, reason: str)
        """
        if not file_path:
            return True, ""

        normalized_path = os.path.abspath(os.path.expanduser(file_path))

        # 删除操作 - 检查受保护路径
        if operation in DELETE_OPERATIONS:
            for ppattern in PROTECTED_PATHS:
                try:
                    if re.search(ppattern, normalized_path, re.IGNORECASE):
                        reason = f"🚫 禁止删除受保护路径: {normalized_path} (规则: {ppattern})"
                        self._record_block(f"{operation} {file_path}", ppattern, reason)
                        return False, reason
                except re.error:
                    # 普通字符串匹配
                    if ppattern.replace('\\', '/').lower() in normalized_path.replace('\\', '/').lower():
                        reason = f"🚫 禁止删除受保护路径: {normalized_path}"
                        self._record_block(f"{operation} {file_path}", ppattern, reason)
                        return False, reason

        # 写操作 - 对于受保护路径，只允许修改配置文件
        if operation in WRITE_OPERATIONS:
            for ppattern in PROTECTED_PATHS:
                try:
                    if re.search(ppattern, normalized_path, re.IGNORECASE):
                        # 允许编辑 .conf, .ini, .json, .yaml 配置文件
                        if normalized_path.endswith(('.conf', '.ini', '.json', '.yaml', '.yml', '.toml', '.cfg')):
                            continue  # 允许配置修改
                        reason = f"🚫 禁止修改受保护系统路径: {normalized_path}"
                        self._record_block(f"{operation} {file_path}", ppattern, reason)
                        return False, reason
                except re.error:
                    pass

        with self._lock:
            self._allow_count += 1
        return True, ""

    def check_path_access(self, path: str, access_type: str = "read") -> tuple[bool, str]:
        """
        检查路径访问权限。
        全盘读写放开，仅检查删除操作。
        """
        if access_type in ("delete", "rm", "remove"):
            return self.check_file_operation("delete_file", path)

        # 读/写/创建 全部放行
        return True, ""

    def _record_block(self, command: str, rule: str, reason: str):
        """记录拦截事件"""
        from datetime import datetime
        with self._lock:
            self._block_count += 1
            self._block_log.append({
                "time": datetime.now().isoformat(),
                "command": command[:200],
                "rule": rule,
                "reason": reason,
            })
            # 只保留最近100条
            if len(self._block_log) > 100:
                self._block_log = self._block_log[-100:]

        logger.warning("权限拦截: %s | %s", command[:100], reason)

    def get_stats(self) -> dict:
        with self._lock:
            return {
                "allowed": self._allow_count,
                "blocked": self._block_count,
                "recent_blocks": self._block_log[-10:],
            }

    def get_block_report(self) -> str:
        """获取拦截报告"""
        stats = self.get_stats()
        lines = [
            f"🔒 权限控制统计 (全盘放开+黑名单拦截)",
            "=" * 50,
            f"  放行操作: {stats['allowed']}",
            f"  拦截操作: {stats['blocked']}",
        ]
        if stats["recent_blocks"]:
            lines.append("  最近拦截:")
            for b in stats["recent_blocks"][-5:]:
                lines.append(f"    [{b['time'][:19]}] {b['command'][:60]}")
        return "\n".join(lines)


# ═══════════════════════════════════════════
# 管理员权限检测与提升
# ═══════════════════════════════════════════

def is_admin() -> bool:
    """检查当前进程是否以管理员权限运行"""
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return os.geteuid() == 0 if hasattr(os, "geteuid") else False


def needs_admin_for_path(path: str) -> bool:
    """检查路径是否需要管理员权限才能写入"""
    if not path:
        return False
    normalized = os.path.abspath(os.path.expanduser(path)).lower()
    admin_paths = [
        r"c:\\windows\\",
        r"c:\\program files\\",
        r"c:\\program files (x86)\\",
        r"c:\\programdata\\",
    ]
    for ap in admin_paths:
        if normalized.startswith(ap):
            # 允许用户目录下的 AppData 等
            if "appdata" in normalized or "users\\" in normalized:
                continue
            return True
    return False


def needs_admin_for_command(command: str) -> bool:
    """检查命令是否需要管理员权限"""
    if not command:
        return False
    cmd_lower = command.lower().strip()
    admin_patterns = [
        r'\bnet\s+(start|stop)\b',
        r'\bsc\s+(start|stop|config)\b',
        r'\breg\s+(add|import|load)\b',
        r'\bbcdedit\b',
        r'\bsfc\b',
        r'\bdism\b',
        r'\bchkdsk\b',
        r'\bpowercfg\b',
        r'\bnetsh\b',
        r'\bipconfig\s+\/(renew|release|flushdns)\b',
        r'\broute\s+add\b',
        r'\bmklink\b',
        r'\bicacls\b',
        r'\btakeown\b',
        r'\bcacls\b',
        r'\bgpupdate\b',
        r'\bmsiexec\b',
        r'pip\s+install\b',
        r'npm\s+install\s+-g\b',
        r'choco\s+install\b',
        r'winget\s+install\b',
    ]
    import re
    for pattern in admin_patterns:
        if re.search(pattern, cmd_lower):
            return True
    return False


def restart_as_admin() -> bool:
    """以管理员权限重启当前进程，返回 True 表示已发起重启"""
    import subprocess
    try:
        import ctypes
        exe = sys.executable
        args = sys.argv[1:] if hasattr(sys, 'argv') else []
        ctypes.windll.shell32.ShellExecuteW(
            None, "runas", exe, " ".join(args), None, 1
        )
        return True
    except Exception:
        return False


def run_cmd_as_admin(command: str, cwd: str = None) -> tuple[bool, str]:
    """
    以管理员权限运行命令。
    - 已管理员身份运行：直接用 subprocess（不弹UAC）
    - 非管理员：ShellExecute(runas) 触发UAC确认
    """
    import subprocess

    # 已管理员 → 直接执行，不走UAC弹窗
    if is_admin():
        try:
            cwd_param = cwd or os.getcwd()
            result = subprocess.run(
                command, shell=True, cwd=cwd_param,
                capture_output=True, creationflags=0x08000000, text=True, timeout=120,
            )
            ok = result.returncode == 0
            out = (result.stdout + result.stderr)[:500]
            logger.info("管理员执行(%s): %s", "成功" if ok else "失败", command[:80])
            return ok, out if out else f"执行{'成功' if ok else '失败'}"
        except subprocess.TimeoutExpired:
            return False, "命令超时(120s)"
        except Exception as e:
            logger.error("管理员执行异常: %s", e)
            return False, f"执行异常: {e}"

    # 非管理员 → ShellExecute(runas) 触发UAC
    try:
        import ctypes
        cwd_param = cwd or os.getcwd()
        result = ctypes.windll.shell32.ShellExecuteW(
            None, "runas", "cmd.exe", f'/c "{command}"', cwd_param, 1)
        if result > 32:
            logger.info("UAC提权启动: %s", command[:80])
            return True, f"已提权启动: {command[:80]}..."
        else:
            error_codes = {2: "文件未找到", 3: "路径未找到", 5: "用户取消UAC",
                          8: "内存不足", 32: "DLL未找到"}
            reason = error_codes.get(result, f"错误码{result}")
            logger.warning("提权失败(%s): %s", reason, command[:80])
            return False, f"提权失败({reason}): {command[:80]}"
    except Exception as e:
        logger.error("run_cmd_as_admin异常: %s", e)
        return False, f"异常: {e}"


# ═══════════════════════════════════════════
# 全局单例
# ═══════════════════════════════════════════

_permission_controller: Optional[PermissionController] = None


def get_permission() -> PermissionController:
    """获取权限控制器全局单例"""
    global _permission_controller
    if _permission_controller is None:
        _permission_controller = PermissionController()
    return _permission_controller


def downgrade_command(command: str, cwd: str = "") -> tuple[str, str]:
    """
    能力28: 权限降级。
    将需要管理员权限的命令转换为用户空间的安全替代方案。
    返回 (downgraded_command, warning)
    """
    cmd_lower = command.lower().strip()
    warning = ""

    # pip install → pip install --user
    if 'pip install' in cmd_lower and '--user' not in cmd_lower and '-r' not in cmd_lower:
        warning = "已降级: 安装到用户目录(--user)"
        return command + " --user", warning

    # npm install -g → npm install (局部)
    if 'npm install -g' in cmd_lower or 'npm i -g' in cmd_lower:
        warning = "已降级: 全局安装→局部安装"
        return command.replace(" -g", "").replace("--global", ""), warning

    # systemctl → 检查状态(只读)
    if 'systemctl start' in cmd_lower or 'systemctl stop' in cmd_lower or 'systemctl restart' in cmd_lower:
        warning = "已降级: 服务操作→状态查询"
        return command.replace('start', 'status').replace('stop', 'status').replace('restart', 'status'), warning

    # chmod 777 → chmod 755
    if 'chmod 777' in cmd_lower:
        warning = "已降级: 777→755(更安全)"
        return command.replace('777', '755'), warning

    # net start/stop → sc query
    if 'net start' in cmd_lower or 'net stop' in cmd_lower:
        warning = "已降级: 服务操作→查询"
        if 'start' in cmd_lower:
            return command.replace('start', 'query'), warning
        return command.replace('stop', 'query'), warning

    return command, ""


def suggest_user_space_alternative(command: str) -> str:
    """为需要权限的操作建议用户空间替代方案"""
    cmd_lower = command.lower()

    suggestions = {
        "pip install": "pip install --user PACKAGE",
        "npm install -g": "npm install PACKAGE  (局部安装)",
        "apt-get install": "使用 pip/conda 代替系统包管理器",
        "systemctl": "使用 nohup COMMAND & 启动后台进程",
        "docker": "使用 podman (rootless) 代替 docker",
    }
    for key, suggestion in suggestions.items():
        if key in cmd_lower:
            return f"建议替代方案: {suggestion}"
    return ""


def reset_permission():
    """重置权限控制器（测试用）"""
    global _permission_controller
    _permission_controller = None
