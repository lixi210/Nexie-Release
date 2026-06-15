# Nexie — AI Coding Agent
# Copyright (c) 2026. All rights reserved.
# 本软件仅供个人学习与研究使用，禁止未经授权的商业用途、分发或修改后闭源。
# Licensed for personal, educational, and non-commercial use only.
"""
本地工具集：目录浏览、文件读写、命令执行、屏幕操控、截图视觉，附带安全校验
统一工具调用格式：【TOOL】工具名|参数1=值1,参数2=值2
"""
import os
import re
import base64
import subprocess
import platform
import threading
from pathlib import Path

# ═══ 发送拦截器（WeChat ClawBot 用） ═══
# 如果设置了拦截器，send_screenshot / send_file / send_text 会优先走拦截器
# 而不是默认的 mobile_bridge 推送
_send_interceptor = None  # callable or None
_send_interceptor_lock = threading.Lock()


def set_send_interceptor(handler: callable):
    """
    设置发送拦截器。handler(action, kwargs) → str
    action: 'screenshot' | 'file' | 'text'
    kwargs: 工具参数
    返回: 成功消息字符串，或 None（回退到默认逻辑）
    """
    global _send_interceptor
    with _send_interceptor_lock:
        _send_interceptor = handler


def clear_send_interceptor():
    """清除发送拦截器，恢复默认 mobile_bridge 行为"""
    global _send_interceptor
    with _send_interceptor_lock:
        _send_interceptor = None

# ═══════════════════════════════════════════
# 操控模块 (已嵌入，避免 PyInstaller 跨模块导入问题)
# ═══════════════════════════════════════════

import time

_AUTO_SAFE_WAIT = 0.3
_AUTO_MAX_TYPE_LENGTH = 500


def get_screen_info() -> str:
    """获取屏幕分辨率和当前窗口列表"""
    try:
        import pyautogui
        sw, sh = pyautogui.size()
        pos = pyautogui.position()
        lines = [f"屏幕: {sw}x{sh}", f"鼠标: ({pos.x}, {pos.y})"]
        try:
            import pygetwindow as gw
            windows = gw.getAllWindows()
            visible = [w for w in windows if w.title.strip() and w.visible]
            lines.append(f"\n可见窗口 ({len(visible)}):")
            for w in visible[:10]:
                lines.append(f"  [{w.left},{w.top} {w.width}x{w.height}] {w.title[:50]}")
        except ImportError:
            lines.append("(pygetwindow 未安装)")
        return "\n".join(lines)
    except ImportError:
        return "[错误] pyautogui 未安装"
    except Exception as e:
        return f"[错误] 获取屏幕信息失败: {e}"


def click_at(x, y):
    """移动鼠标到 (x,y) 并左键点击"""
    try:
        import pyautogui
        pyautogui.FAILSAFE = True
        pyautogui.moveTo(int(x), int(y), duration=0.15)
        time.sleep(_AUTO_SAFE_WAIT)
        pyautogui.click()
        time.sleep(_AUTO_SAFE_WAIT)
        return f"✅ 左键点击 ({int(x)}, {int(y)})"
    except Exception as e:
        return f"[错误] 点击失败: {e}"


def double_click_at(x, y):
    """移动鼠标到 (x,y) 并双击"""
    try:
        import pyautogui
        pyautogui.FAILSAFE = True
        pyautogui.moveTo(int(x), int(y), duration=0.15)
        time.sleep(_AUTO_SAFE_WAIT)
        pyautogui.doubleClick()
        time.sleep(_AUTO_SAFE_WAIT)
        return f"✅ 双击 ({int(x)}, {int(y)})"
    except Exception as e:
        return f"[错误] 双击失败: {e}"


def right_click_at(x, y):
    """移动鼠标到 (x,y) 并右键点击"""
    try:
        import pyautogui
        pyautogui.FAILSAFE = True
        pyautogui.moveTo(int(x), int(y), duration=0.15)
        time.sleep(_AUTO_SAFE_WAIT)
        pyautogui.rightClick()
        time.sleep(_AUTO_SAFE_WAIT)
        return f"✅ 右键点击 ({int(x)}, {int(y)})"
    except Exception as e:
        return f"[错误] 右键失败: {e}"


def move_to(x, y):
    """移动鼠标到 (x,y)，不点击"""
    try:
        import pyautogui
        pyautogui.FAILSAFE = True
        pyautogui.moveTo(int(x), int(y), duration=0.2)
        return f"✅ 鼠标移动到 ({int(x)}, {int(y)})"
    except Exception as e:
        return f"[错误] 移动失败: {e}"


def type_text(text):
    """键盘输入文字。ASCII 用 typewrite，中文/非ASCII 用剪贴板粘贴。"""
    if len(text) > _AUTO_MAX_TYPE_LENGTH:
        return f"[错误] 输入文本过长 ({len(text)} 字符，限制 {_AUTO_MAX_TYPE_LENGTH})"
    try:
        import pyautogui
        pyautogui.FAILSAFE = True
        import pyperclip
        is_ascii = all(ord(c) < 128 for c in text)
        if is_ascii:
            pyautogui.typewrite(text, interval=0.02)
        else:
            pyperclip.copy(text)
            time.sleep(0.1)
            pyautogui.hotkey("ctrl", "v")
            time.sleep(0.2)
        time.sleep(_AUTO_SAFE_WAIT)
        return f"✅ 已输入 {len(text)} 个字符"
    except Exception as e:
        # 回退：剪贴板不可用时
        try:
            import pyautogui
            pyautogui.typewrite(text, interval=0.02)
            return f"✅ 已输入 {len(text)} 个字符 (回退模式)"
        except Exception:
            return f"[错误] 输入失败: {e}"


def press_key(key: str):
    """按单个键或组合键 (如 ctrl+c, enter, alt+f4)"""
    try:
        import pyautogui
        pyautogui.FAILSAFE = True
        parts = [k.strip().lower() for k in key.split("+")]
        if len(parts) == 1:
            pyautogui.press(parts[0])
        else:
            pyautogui.hotkey(*parts)
        time.sleep(_AUTO_SAFE_WAIT)
        return f"✅ 已按键: {key}"
    except Exception as e:
        return f"[错误] 按键失败: {e}"


def scroll_screen(amount):
    """滚轮滚动，正数向上，负数向下"""
    try:
        import pyautogui
        pyautogui.FAILSAFE = True
        pyautogui.scroll(int(amount))
        time.sleep(_AUTO_SAFE_WAIT)
        direction = "上" if int(amount) > 0 else "下"
        return f"✅ 滚轮{direction}滚动 {abs(int(amount))} 格"
    except Exception as e:
        return f"[错误] 滚动失败: {e}"


def drag_from_to(x1, y1, x2, y2):
    """从 (x1,y1) 拖拽到 (x2,y2)"""
    try:
        import pyautogui
        pyautogui.FAILSAFE = True
        pyautogui.moveTo(int(x1), int(y1), duration=0.1)
        pyautogui.drag(int(x2) - int(x1), int(y2) - int(y1), duration=0.3)
        time.sleep(_AUTO_SAFE_WAIT)
        return f"✅ 拖拽 ({int(x1)},{int(y1)}) → ({int(x2)},{int(y2)})"
    except Exception as e:
        return f"[错误] 拖拽失败: {e}"

# ═══════════════════════════════════════════
# 截图/视觉工具 (MIMO 多模态模型专用)
# ═══════════════════════════════════════════

_last_screenshot_b64: str | None = None
_last_screenshot_size: tuple[int, int] = (0, 0)


def get_last_screenshot() -> tuple[str | None, int, int]:
    """获取最近一次截图的 base64 数据，供 agent_core 注入视觉消息"""
    return _last_screenshot_b64, _last_screenshot_size[0], _last_screenshot_size[1]


def clear_last_screenshot():
    """清除缓存的截图数据"""
    global _last_screenshot_b64
    _last_screenshot_b64 = None


def capture_screen() -> str:
    """截取整个屏幕并压缩为 JPEG，供视觉模型分析。优化为 200KB 以内，传输更快。"""
    global _last_screenshot_b64, _last_screenshot_size
    try:
        import time as _t
        from PIL import Image  # 提前导入，确保两种截图路径都能用
        _t0 = _t.time()

        # mss 比 pyautogui 截图快 2-3 倍
        try:
            import mss
            with mss.mss() as sct:
                monitor = sct.monitors[1]
                img = sct.grab(monitor)
                screenshot = Image.frombytes("RGB", img.size, img.bgra, "raw", "BGRX")
        except Exception:
            import pyautogui
            screenshot = pyautogui.screenshot()

        sw, sh = screenshot.size
        _t1 = _t.time()

        # 缩放到最大 1280px 宽，大幅减少数据量
        max_w = 1280
        if sw > max_w:
            ratio = max_w / sw
            new_size = (max_w, int(sh * ratio))
            screenshot = screenshot.resize(new_size, Image.LANCZOS)

        # JPEG 压缩 (质量 65，屏幕截图足够清晰)
        import io
        buffer = io.BytesIO()
        screenshot.save(buffer, format="JPEG", quality=65)
        _last_screenshot_b64 = base64.b64encode(buffer.getvalue()).decode("utf-8")
        _last_screenshot_size = screenshot.size

        _t2 = _t.time()
        kb = len(_last_screenshot_b64) // 1024
        import pyautogui
        pos = pyautogui.position()
        lines = [
            f"✅ 截图已捕获 (捕获 {(_t1-_t0)*1000:.0f}ms / 压缩 {(_t2-_t1)*1000:.0f}ms)",
            f"原始: {sw}x{sh} → 缩放: {screenshot.size[0]}x{screenshot.size[1]}",
            f"大小: {kb} KB (JPEG) | 鼠标: ({pos.x}, {pos.y})",
        ]
        try:
            import pygetwindow as gw
            windows = gw.getAllWindows()
            visible = [w for w in windows if w.title.strip() and w.visible]
            lines.append(f"\n可见窗口 ({len(visible)}):")
            for w in visible[:8]:
                lines.append(f"  [{w.left},{w.top} {w.width}x{w.height}] {w.title[:40]}")
        except ImportError:
            pass

        # 不自动推送 — 由 push_to_mobile 工具负责推送
        return "\n".join(lines)
    except ImportError:
        return "[错误] pyautogui 未安装，无法截图"
    except Exception as e:
        return f"[错误] 截图失败: {e}"

# ==================== 安全配置 ====================

# 高危命令黑名单
COMMAND_BLACKLIST = [
    r'\brm\s+.*-rf?\b', r'\brm\s+.*-r\s+/',
    r'\bdel\s+/[fsq]\b', r'\bdel\s+/f\s+/s\b', r'\brd\s+/s\b',
    r'\bmkfs\b', r'\bformat\b', r'\bdiskpart\b', r'\bdd\s+if=',
    r'\bsudo\b', r'\bsu\b', r'\bchmod\s+777\b', r'\bchown\b',
    r'\bpasswd\b', r'\busermod\b', r'\bvisudo\b',
    r'\bshutdown\b', r'\breboot\b', r'\bhalt\b', r'\bpoweroff\b',
    r':\(\)\s*\{', r'fork\s*bomb',
    r'/etc/passwd', r'/etc/shadow', r'/etc/sudoers', r'/etc/ssh/',
    r'curl.*\|.*sh', r'curl.*\|.*bash', r'wget.*-O.*\|.*sh',
    r'nc\s+-[lL].*-[eE]', r'bash\s+-i\s*>&', r'/dev/tcp/',
]

# 安全命令白名单 — 涵盖开发、包管理、系统工具
COMMAND_WHITELIST = [
    # Python 生态
    r'^python', r'^python3', r'^py\b', r'^pip', r'^pip3', r'^pytest', r'^uv\b',
    r'^poetry\b', r'^conda\b', r'^pyinstaller\b', r'^tox\b', r'^flake8\b', r'^black\b',
    # Node.js 生态
    r'^node\b', r'^npm\b', r'^npx\b', r'^yarn\b', r'^pnpm\b', r'^tsc\b', r'^vite\b',
    # 版本控制
    r'^git\b', r'^gh\b',
    # 文件浏览
    r'^(ls|dir|tree|find)\b', r'^cat\b', r'^type\b', r'^where\b',
    r'^(echo|printf|print)\b', r'^mkdir\b', r'^rmdir\b',
    r'^(cp|copy|mv|move|ren|rename|xcopy|robocopy)\b', r'^touch\b',
    # 编译工具
    r'^(cargo|rustc|go|java|javac|gcc|g\+\+|clang|make|cmake|ninja|msbuild|dotnet)\b',
    # 网络工具
    r'^(curl|wget|ping|nslookup|ipconfig|netstat)\b',
    # 文本处理
    r'^(grep|findstr|sed|awk|sort|uniq|wc|head|tail|cut|tr)\b',
    # 进程/系统信息
    r'^(tasklist|ps|top|htop|taskkill|kill)\b',
    r'^(whoami|hostname|chdir|set|setx|path)\b',
    # ── 包管理器（新增）──
    r'^winget\b', r'^choco\b', r'^scoop\b',
    r'^apt\b', r'^apt-get\b', r'^brew\b', r'^snap\b', r'^dnf\b', r'^yum\b',
    # ── Windows 系统工具（新增）──
    r'^reg\b', r'^icacls\b', r'^takeown\b', r'^sc\b', r'^net\b', r'^netsh\b',
    r'^schtasks\b', r'^powercfg\b', r'^msiexec\b', r'^wmic\b',
    r'^powershell\b', r'^pwsh\b',
    # ── 压缩/解压 ──
    r'^tar\b', r'^zip\b', r'^unzip\b', r'^7z\b',
]

# 高危系统目录 —— 禁止写入
_RESTRICTED_PATTERNS_WIN = [
    r'[A-Za-z]:[\\/]Windows([\\/]|$)',
    r'[A-Za-z]:[\\/]Windows[\\/]System32',
    r'[A-Za-z]:[\\/]Windows[\\/]SysWOW64',
    r'[A-Za-z]:[\\/]Windows[\\/]System',
    r'[A-Za-z]:[\\/]Program Files([\\/]|$)',
    r'[A-Za-z]:[\\/]Program Files \(x86\)',
    r'[A-Za-z]:[\\/]ProgramData',
    r'[A-Za-z]:[\\/]Boot([\\/]|$)',
    r'[A-Za-z]:[\\/]Recovery',
    r'AppData[\\/]Roaming[\\/]Microsoft',
    r'AppData[\\/]Local[\\/]Microsoft',
]

_RESTRICTED_PATTERNS_UNIX = [
    r'/etc(/|$)', r'/sys(/|$)', r'/proc(/|$)', r'/boot(/|$)',
    r'/root(/|$)', r'/lib(/|$)', r'/usr/lib(/|$)', r'/usr/bin(/|$)',
    r'/usr/sbin(/|$)', r'/bin(/|$)', r'/sbin(/|$)',
    r'/dev(/|$)', r'/run(/|$)',
]

_SENSITIVE_FILES = [
    r'~/.ssh', r'~/.gnupg', r'~/.aws', r'~/.azure',
]


def _build_path_patterns() -> list[str]:
    """构建完整的路径拦截正则列表"""
    patterns = []
    patterns.extend(_RESTRICTED_PATTERNS_WIN)
    patterns.extend(_RESTRICTED_PATTERNS_UNIX)
    patterns.extend(_SENSITIVE_FILES)
    return patterns


RESTRICTED_PATHS = _build_path_patterns()


# ==================== 安全校验 ====================

def is_command_safe(command: str) -> tuple[bool, str]:
    """检查命令是否安全，返回 (是否安全, 拦截原因)"""
    cmd = command.strip()
    if not cmd:
        return False, "空命令"

    # 黑名单检查
    for pattern in COMMAND_BLACKLIST:
        if re.search(pattern, cmd, re.IGNORECASE):
            return False, f"危险命令已拦截（匹配: {pattern}）"

    # 命令注入特征检测（仅拦截危险组合）
    injection_checks = [
        (r'`[^`]+`', "反引号命令替换"),
        (r'\$\([^)]+\)', "$()命令替换"),
    ]
    for pattern, desc in injection_checks:
        if re.search(pattern, cmd):
            return False, f"命令注入特征已拦截（{desc}）"

    # 白名单检查
    for pattern in COMMAND_WHITELIST:
        if re.search(pattern, cmd, re.IGNORECASE):
            return True, ""

    # 不在白名单也不在黑名单 — 默认放行
    return True, ""


def is_path_safe(path: str, *, allow_write: bool = True) -> tuple[bool, str]:
    """
    动态路径安全检查 — 保护系统目录，放行用户常用目录
    读操作：仅拦截敏感系统文件
    写操作：拦截系统目录 + 敏感配置目录，放行 Desktop/Downloads/Documents/AppData
    """
    if not path:
        return False, "路径为空"

    try:
        resolved = str(Path(path).resolve())
    except Exception:
        return False, f"无法解析路径: {path}"

    resolved_lower = resolved.lower()

    # ── 路径穿越检测 ──
    wd = get_working_dir()
    if not resolved.startswith(str(Path(wd).resolve())):
        if '..' in Path(path).parts or '/../' in path or '\\..\\' in path:
            return False, f"路径穿越已拦截: 目标不在工作目录内"

    # ── 始终保护的系统目录 ──
    for pattern in RESTRICTED_PATHS:
        if re.search(pattern, path, re.IGNORECASE) or re.search(pattern, resolved, re.IGNORECASE):
            return False, f"受保护的系统路径: {pattern}"

    if allow_write:
        # ── 始终保护的敏感目录 ──
        for sensitive in ['.ssh', '.gnupg', '.aws', '.azure']:
            if sensitive in resolved_lower.split(os.sep):
                return False, f"受保护的敏感路径: {sensitive}"

    return True, ""


# ==================== 全局状态管理 ====================

_global_working_dir: str | None = None
_current_process = None


def cancel_current_command():
    """取消当前正在执行的命令"""
    global _current_process
    if _current_process and _current_process.poll() is None:
        try:
            _current_process.terminate()
            _current_process.wait(timeout=5)
        except Exception:
            try:
                _current_process.kill()
            except Exception:
                pass
        _current_process = None
        return "已终止当前命令"
    return "没有正在执行的命令"


def set_working_dir(path: str):
    """设置全局工作目录"""
    global _global_working_dir
    p = Path(path).resolve()
    if p.exists() and p.is_dir():
        _global_working_dir = str(p)
    else:
        raise ValueError(f"无效的工作目录: {path}")


def get_working_dir() -> str:
    """获取当前工作目录"""
    if _global_working_dir:
        return _global_working_dir
    return str(Path.cwd())


# ==================== 四大核心工具 ====================

def list_dir(path: str = "") -> str:
    """浏览目录内容。支持多个路径(逗号分隔), 一次调用全部列出"""
    paths = _split_paths(path)
    if not paths:
        paths = [get_working_dir()]

    results = []
    for p_str in paths:
        p_str = p_str.strip()
        if not p_str or p_str == ".":
            p_str = get_working_dir()

        p = Path(p_str)
        if not p.is_absolute():
            p = Path(get_working_dir()) / p
        p = p.resolve()

        safe, reason = is_path_safe(str(p), allow_write=False)
        if not safe:
            results.append(f"[{p_str}] 安全拦截: {reason}")
            continue
        if not p.exists():
            results.append(f"[{p_str}] 目录不存在")
            continue
        if not p.is_dir():
            results.append(f"[{p_str}] 不是目录")
            continue

        try:
            items = sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))
        except PermissionError:
            results.append(f"[{p_str}] 无权限")
            continue

        lines = [f"📁 {p}"]
        lines.append("─" * 50)
        dir_count, file_count = 0, 0
        for item in items:
            if item.is_dir():
                dir_count += 1
                skip_mark = " [跳过]" if item.name in ('.git', '__pycache__', 'node_modules', '.venv', 'venv') else ""
                lines.append(f"  📂 {item.name}/{skip_mark}")
            else:
                file_count += 1
                try:
                    size = item.stat().st_size
                    size_str = f" ({_fmt_size(size)})"
                except OSError:
                    size_str = ""
                lines.append(f"  📄 {item.name}{size_str}")
        lines.append(f"  → {dir_count} 目录, {file_count} 文件")
        if not items:
            lines.append("  (空)")
        results.append("\n".join(lines))

    return "\n\n".join(results) if len(results) > 1 else results[0]


def read_file(path: str, encoding: str = "") -> str:
    """
    读取文件内容（带行号）。支持多个路径(逗号分隔)，一次读取全部。
    自动尝试多种编码：UTF-8 → GBK → UTF-8-SIG → latin-1
    """
    paths = _split_paths(path)
    if not paths:
        return "[错误] 未指定文件路径"

    results = []
    for path_str in paths:
        path_str = path_str.strip()
        p = Path(path_str)
        if not p.is_absolute():
            p = Path(get_working_dir()) / p
        p = p.resolve()

        safe, reason = is_path_safe(str(p), allow_write=False)
        if not safe:
            results.append(f"[{path_str}] 🚫 {reason}")
            continue
        if not p.exists():
            results.append(f"[{path_str}] 文件不存在")
            continue
        if not p.is_file():
            results.append(f"[{path_str}] 不是文件")
            continue

        try:
            size = p.stat().st_size
            if size > 2 * 1024 * 1024:
                results.append(f"[{path_str}] 文件过大({_fmt_size(size)})，跳过")
                continue
        except OSError:
            pass

        encodings_to_try = [encoding] if encoding else []
        encodings_to_try.extend(['utf-8', 'gbk', 'utf-8-sig', 'latin-1'])
        content = None
        used_enc = ""
        for enc in encodings_to_try:
            try:
                content = p.read_text(encoding=enc)
                used_enc = enc
                break
            except (UnicodeDecodeError, LookupError):
                continue

        if content is None:
            results.append(f"[{path_str}] 无法解码")
            continue

        lines_list = content.split("\n")
        header = f"📄 {p} ({len(lines_list)}行, {len(content)}字符"
        if used_enc != 'utf-8':
            header += f", {used_enc}"
        header += ")"
        body = "\n".join(f"{i+1:>5} │ {line}" for i, line in enumerate(lines_list))
        results.append(header + "\n" + "─" * 50 + "\n" + body)

    return "\n\n".join(results) if len(results) > 1 else (results[0] if results else "[错误] 无有效文件")


def _strip_zone_identifier(filepath: str):
    """移除Windows Zone.Identifier ADS，防止SmartScreen弹窗"""
    if platform.system() != "Windows":
        return
    try:
        zone_path = filepath + ":Zone.Identifier"
        if os.path.exists(zone_path):
            os.remove(zone_path)
    except Exception:
        pass  # 删除失败不影响主流程


def write_file(path: str, content: str) -> str:
    """创建/覆盖文件，自动创建父目录"""
    safe, reason = is_path_safe(path, allow_write=True)
    if not safe:
        return f"[安全拦截] {reason}"

    p = Path(path)
    if not p.is_absolute():
        p = Path(get_working_dir()) / p
    p = p.resolve()

    try:
        p.parent.mkdir(parents=True, exist_ok=True)
    except (PermissionError, FileExistsError, NotADirectoryError) as e:
        return f"[错误] 无法创建目录 {p.parent}: {str(e)}"
    except Exception as e:
        return f"[错误] 创建目录失败: {str(e)}"

    action = "更新" if p.exists() else "创建"
    try:
        p.write_text(content, encoding="utf-8")
        # .ps1/.bat/.exe/.msi → 清除Zone.Identifier防SmartScreen卡住
        if p.suffix.lower() in ('.ps1', '.bat', '.cmd', '.exe', '.msi', '.vbs', '.reg'):
            _strip_zone_identifier(str(p))
        file_size = p.stat().st_size
        if file_size < 1024:
            size_info = f" ({file_size} B)"
        elif file_size < 1024 * 1024:
            size_info = f" ({file_size/1024:.1f} KB)"
        else:
            size_info = f" ({file_size/(1024*1024):.1f} MB)"
        return f"✅ 已{action}文件: {p}{size_info}"
    except PermissionError:
        return f"[错误] 无权限写入文件: {path}"
    except Exception as e:
        return f"[错误] 写入失败: {str(e)}"


# ═══ 全局绕过标记 — 开启后run_command绕过UAC/SmartScreen ═══
_global_bypass = False

def set_global_bypass(enabled: bool):
    global _global_bypass
    _global_bypass = enabled


def _bypass_system_blockers(command: str) -> str:
    """包装命令以绕过Windows安全弹窗"""
    if platform.system() != "Windows":
        return command
    # PowerShell: 强制绕过执行策略
    if command.lower().startswith("powershell"):
        if "-ExecutionPolicy" not in command:
            command = command.replace("powershell", "powershell -ExecutionPolicy Bypass -NoProfile", 1)
        return command
    if command.lower().startswith("pwsh"):
        if "-ExecutionPolicy" not in command:
            command = command.replace("pwsh", "pwsh -ExecutionPolicy Bypass -NoProfile", 1)
        return command
    # 直接运行exe → 先清Zone.Identifier
    parts = command.split(None, 1)
    if parts and parts[0].endswith('.exe'):
        exe_path = Path(parts[0])
        if not exe_path.is_absolute():
            exe_path = Path(get_working_dir()) / exe_path
        _strip_zone_identifier(str(exe_path.resolve()))
    return command


def _adapt_command(command: str) -> str:
    """将常见 Linux 命令自动适配为当前平台语法"""
    current_os = platform.system()
    if current_os != "Windows":
        return command

    # 常见 Linux → Windows 命令映射
    adaptations = [
        # mkdir -p path → mkdir path (Windows mkdir 自动创建父目录)
        (r'^mkdir\s+-p\s+', 'mkdir '),
        # ls → dir
        (r'^ls(\s|$)', r'dir\1'),
        # cp → copy
        (r'^cp\s+', 'copy '),
        # mv → move
        (r'^mv\s+', 'move '),
        # rm → del
        (r'^rm\s+', 'del '),
        # rm -rf → rmdir /s /q
        (r'^rm\s+-rf?\s+', 'rmdir /s /q '),
        # cat → type
        (r'^cat\s+', 'type '),
        # touch filename → type nul > filename
        (r'^touch\s+(.+)', r'type nul > \1'),
        # which → where
        (r'^which\s+', 'where '),
        # clear → cls
        (r'^clear$', 'cls'),
        # ~/Desktop → %USERPROFILE%/Desktop (forward slashes for re.sub safety)
        (r'~/Desktop', r'%USERPROFILE%/Desktop'),
        (r'~/Documents', r'%USERPROFILE%/Documents'),
        (r'~(?=/|$)', r'%USERPROFILE%'),
    ]

    adapted = command
    for pattern, replacement in adaptations:
        adapted = re.sub(pattern, replacement, adapted, flags=re.IGNORECASE)

    return adapted


def run_command(command: str, working_dir: str = "", timeout: int = 120) -> str:
    """执行终端命令，自动适配平台语法"""
    if not command.strip():
        return "[错误] 命令为空"

    # 自动平台适配
    original = command
    command = _adapt_command(command)
    adapted_note = f"\n🔄 已适配为: {command}" if command != original else ""

    safe, reason = is_command_safe(command)
    if not safe:
        return f"🚫 [安全拦截] {reason}"

    # 全局绕过：包装命令绕过SmartScreen/UAC
    if _global_bypass:
        command = _bypass_system_blockers(command)

    if working_dir and working_dir.strip():
        wd = Path(working_dir).resolve()
        if not wd.exists():
            return f"[错误] 工作目录不存在: {working_dir}"
        if not wd.is_dir():
            return f"[错误] 不是有效目录: {working_dir}"
    else:
        wd = Path(get_working_dir())

    shell_flag = platform.system() == "Windows"

    try:
        global _current_process
        proc = subprocess.Popen(
            command,
            shell=shell_flag,
            cwd=str(wd),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=0x08000000,  # 隐藏黑框
        )
        _current_process = proc

        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            _current_process = None
            return f"⏱️ [超时] 命令执行超过 {timeout} 秒已被终止: {command}"
        finally:
            _current_process = None

    except FileNotFoundError:
        cmd_name = command.split()[0] if command.split() else command
        return (
            f"[错误] 命令未找到: {cmd_name}\n"
            f"提示：请确认 {cmd_name} 已安装并添加到 PATH。\n"
            f"原始命令: {original}"
        )
    except Exception as e:
        return (
            f"[错误] 执行失败: {str(e)}\n"
            f"原始命令: {original}\n"
            f"提示：如果是平台不兼容，请使用 Windows 语法重试"
        )

    output_parts = []
    if stdout:
        output_parts.append(stdout.rstrip())
    if stderr:
        output_parts.append(f"[stderr]\n{stderr.rstrip()}")

    output = "\n".join(output_parts) if output_parts else "(无输出)"
    exit_info = f"退出码: {proc.returncode}" if proc.returncode != 0 else "退出码: 0"

    result = (
        f"💻 命令: {command}{adapted_note}\n"
        f"📂 目录: {wd}\n"
        f"{'─' * 50}\n"
        f"{output}\n"
        f"{'─' * 50}\n"
        f"✅ 执行完成 ({exit_info})"
    )

    if proc.returncode != 0:
        result += (
            f"\n⚠️ 命令执行失败（退出码: {proc.returncode}）\n"
            f"可能原因：\n"
            f"  1. 命令语法不兼容当前平台 ({platform.system()})\n"
            f"  2. 目标路径不存在或无权限\n"
            f"  3. 依赖工具未安装\n"
            f"建议：尝试使用 write_file 直接创建文件，或检查路径是否正确"
        )

    return result


# ==================== 辅助工具 ====================

def edit_file(path: str, old_text: str, new_text: str) -> str:
    """编辑文件 — 查找替换"""
    safe, reason = is_path_safe(path, allow_write=True)
    if not safe:
        return f"[安全拦截] {reason}"

    p = Path(path)
    if not p.is_absolute():
        p = Path(get_working_dir()) / p
    p = p.resolve()

    if not p.exists():
        return f"[错误] 文件不存在: {path}"

    try:
        content = p.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        try:
            content = p.read_text(encoding="gbk")
        except Exception as e:
            return f"[错误] 读取文件失败: {str(e)}"
    except Exception as e:
        return f"[错误] 读取文件失败: {str(e)}"

    if old_text not in content:
        return (
            f"[错误] 未在文件中找到要替换的文本。\n"
            f"请确认 old_text 与文件中原文完全一致（含缩进、空格、换行）。\n"
            f"建议先用 read_file 查看文件内容后再精确指定。"
        )

    count = content.count(old_text)
    new_content = content.replace(old_text, new_text)

    try:
        p.write_text(new_content, encoding="utf-8")
        return f"✅ 已编辑: {p}（替换 {count} 处）"
    except Exception as e:
        return f"[错误] 写入失败: {str(e)}"


def search_files(pattern: str, directory: str = "") -> str:
    """搜索匹配glob模式的文件。支持多个模式(逗号分隔)，一次搜索全部"""
    if not directory:
        directory = get_working_dir()

    search_dir = Path(directory)
    if not search_dir.is_absolute():
        search_dir = Path(get_working_dir()) / search_dir
    search_dir = search_dir.resolve()

    safe, reason = is_path_safe(str(search_dir), allow_write=False)
    if not safe:
        return f"[安全拦截] {reason}"
    if not search_dir.exists():
        return f"[错误] 目录不存在: {directory}"

    patterns = _split_paths(pattern)
    if not patterns:
        return "[错误] 未指定搜索模式"

    MAX_MATCHES = 5000  # 硬上限防止rglob扫全盘卡死
    seen = set()
    all_matches = []
    limit_reached = False
    for pat in patterns:
        if limit_reached:
            break
        pat = pat.strip()
        try:
            for m in search_dir.rglob(pat):
                mp = str(m)
                if mp not in seen:
                    seen.add(mp)
                    all_matches.append(mp)
                    if len(all_matches) >= MAX_MATCHES:
                        limit_reached = True
                        break
        except PermissionError:
            all_matches.append(f"[{pat}] 无权限")
            continue

    file_paths = [p for p in all_matches if isinstance(p, str) and not p.startswith("[")]
    errors = [p for p in all_matches if isinstance(p, str) and p.startswith("[")]

    if not file_paths:
        return f"🔍 未找到匹配文件（模式: {', '.join(patterns)}，搜索: {search_dir}）"

    file_paths.sort(key=len)
    limit_note = f" (已达{MAX_MATCHES}上限，结果已截断)" if limit_reached else ""
    lines = [f"🔍 找到 {len(file_paths)} 个文件（模式: {', '.join(patterns)}）{limit_note}"]
    lines.append("─" * 50)
    for p in file_paths:
        lines.append(f"  {p}")
    if errors:
        lines.append("─" * 50)
        lines.extend(errors)
    return "\n".join(lines)


def _split_paths(raw: str) -> list[str]:
    """将输入的路径字符串分割为列表(逗号/换行分隔)"""
    if not raw or not raw.strip():
        return []
    # 优先按换行分，再按逗号分
    if "\n" in raw:
        return [p.strip() for p in raw.split("\n") if p.strip()]
    return [p.strip() for p in raw.split(",") if p.strip()]


def _fmt_size(size: int) -> str:
    """格式化文件大小"""
    if size < 1024:
        return f"{size} B"
    elif size < 1024 * 1024:
        return f"{size/1024:.1f} KB"
    else:
        return f"{size/(1024*1024):.1f} MB"


# ==================== 【TOOL】格式解析 ====================

def parse_tool_command(text: str) -> tuple[str | None, dict | None]:
    """
    解析统一工具调用格式：【TOOL】工具名|参数1=值1,参数2=值2
    返回: (工具名, 参数字典) 或 (None, None)
    """
    pattern = r'【TOOL】(\w+)\s*[|｜](.+)'
    match = re.search(pattern, text.strip())
    if match:
        func_name = match.group(1)
        params_str = match.group(2).strip()
        params = {}
        if params_str:
            for part in _split_params(params_str):
                if '=' in part:
                    key, val = part.split('=', 1)
                    params[key.strip()] = val.strip().strip("'\"")
                else:
                    params[f"arg{len(params)}"] = part.strip().strip("'\"")
        return func_name, params

    # 不带参数: 【TOOL】工具名
    pattern2 = r'【TOOL】(\w+)'
    match2 = re.search(pattern2, text.strip())
    if match2:
        return match2.group(1), {}

    return None, None


def _split_params(s: str) -> list[str]:
    """分割参数字符串，处理引号内的逗号（包括全角逗号）"""
    parts = []
    current = []
    in_quote = False
    quote_char = None

    for ch in s:
        if ch in ('"', "'") and not in_quote:
            in_quote = True
            quote_char = ch
            current.append(ch)
        elif ch == quote_char and in_quote:
            in_quote = False
            quote_char = None
            current.append(ch)
        elif ch in (',', '，') and not in_quote:
            parts.append(''.join(current))
            current = []
        else:
            current.append(ch)

    if current:
        parts.append(''.join(current))

    return parts


# ==================== 工具映射表 ====================

# ═══════════════════════════════════════════
# 记忆工具（跨会话长期记忆）
# ═══════════════════════════════════════════

def _remember_fact(fact: str) -> str:
    """将事实存入长期记忆"""
    from memory_manager import get_memory
    m = get_memory()
    m.add_fact(fact)
    return f"✅ 已记住: {fact}"

def _forget_fact(fact: str) -> str:
    """从长期记忆中删除事实"""
    from memory_manager import get_memory
    m = get_memory()
    m.remove_fact(fact)
    return f"🗑️ 已忘记: {fact}"

def _set_agent_name(name: str) -> str:
    """设置智能体的称呼"""
    from memory_manager import get_memory
    m = get_memory()
    m.set_agent_name(name)
    return f"✅ 已记住，以后叫我「{name}」就好"

# ═══════════════════════════════════════════
# 应用操控工具（背后盲操）
# ═══════════════════════════════════════════

def start_application(app_path: str, args: str = "", working_dir: str = "", wait_sec: float = 2.0) -> str:
    """启动任意应用程序（IDE、浏览器、Office等），可选等待启动完成"""
    try:
        cmd = f'"{app_path}" {args}'.strip()
        shell = platform.system() == "Windows"
        proc = subprocess.Popen(cmd, shell=shell, cwd=working_dir or get_working_dir())
        if wait_sec > 0:
            time.sleep(wait_sec)
        return (
            f"✅ 已启动: {app_path}\n"
            f"参数: {args or '(无)'}\n"
            f"PID: {proc.pid}\n"
            f"等待: {wait_sec}s"
        )
    except FileNotFoundError:
        return f"[错误] 应用程序未找到: {app_path}\n提示：请确认路径正确或程序已安装"
    except Exception as e:
        return f"[错误] 启动失败: {e}"


def focus_window(title_part: str) -> str:
    """按标题查找窗口并聚焦到前台"""
    try:
        import pygetwindow as gw
        import pyautogui
        matches = [w for w in gw.getAllWindows() if title_part.lower() in w.title.lower() and w.title.strip()]
        if not matches:
            return f"[未找到] 没有标题包含 '{title_part}' 的窗口"
        # 选最大的可见窗口
        best = max(matches, key=lambda w: w.width * w.height)
        if best.isMinimized:
            best.restore()
        best.activate()
        pyautogui.sleep(0.3)
        return f"✅ 已聚焦窗口: '{best.title}' ({best.width}x{best.height})"
    except ImportError:
        return "[错误] pygetwindow 未安装，无法操控窗口"
    except Exception as e:
        return f"[错误] 聚焦窗口失败: {e}"


def send_hotkey(keys: str) -> str:
    """发送全局快捷键组合到当前活动窗口"""
    try:
        import pyautogui
        pyautogui.sleep(0.15)
        pyautogui.hotkey(*keys.split('+'))
        pyautogui.sleep(0.1)
        return f"✅ 已发送快捷键: {keys}"
    except Exception as e:
        return f"[错误] 发送快捷键失败: {e}"


# ═══════════════════════════════════════════
# 工具映射表
# ═══════════════════════════════════════════

# ═══════════════════════════════════════════
# OpAgent3.0: 推送工具 (→ 手机端)
# ═══════════════════════════════════════════

def send_screenshot() -> str:
    """发送截屏到手机（微信场景下自动发到微信）"""
    # ── 微信拦截器优先 ──
    if _send_interceptor:
        result = _send_interceptor("screenshot", {})
        if result:
            return result
    # ── 默认：手机推送 ──
    try:
        from mobile_bridge import get_bridge
        bridge = get_bridge()
        if not bridge.is_connected:
            return "[发送失败] 没有已连接的手机"
        bridge.push_screenshot()
        return "✅ 截屏已发送到手机"
    except ImportError:
        return "[发送失败] 手机互联模块未加载"

def send_file(filepath: str) -> str:
    """发送文件到手机（微信场景下自动发到微信）"""
    # ── 微信拦截器优先 ──
    if _send_interceptor:
        result = _send_interceptor("file", {"filepath": filepath})
        if result:
            return result
    # ── 默认：手机推送 ──
    try:
        from mobile_bridge import get_bridge
        bridge = get_bridge()
        if not bridge.is_connected:
            return "[发送失败] 没有已连接的手机"
        p = Path(filepath)
        if not p.exists():
            return f"[发送失败] 文件不存在: {filepath}"
        bridge.push_file(str(p))
        return f"✅ 文件已发送到手机: {p.name}"
    except ImportError:
        return "[发送失败] 手机互联模块未加载"

def send_text(text: str) -> str:
    """发送文本到手机（微信场景下自动发到微信）"""
    # ── 微信拦截器优先 ──
    if _send_interceptor:
        result = _send_interceptor("text", {"text": text})
        if result:
            return result
    # ── 默认：手机推送 ──
    try:
        from mobile_bridge import get_bridge
        bridge = get_bridge()
        if not bridge.is_connected:
            return "[发送失败] 没有已连接的手机"
        bridge.push_chat(text)
        return "✅ 文本已发送到手机"
    except ImportError:
        return "[发送失败] 手机互联模块未加载"

def push_to_mobile(content: str, content_type: str = "text") -> str:
    """将文本/文件/截屏推送到已连接的手机端（微信场景下自动发到微信）"""
    # ── 微信拦截器优先 ──
    if _send_interceptor:
        if content_type == "screenshot":
            result = _send_interceptor("screenshot", {})
        elif content_type == "file":
            result = _send_interceptor("file", {"filepath": content})
        else:
            result = _send_interceptor("text", {"text": content})
        if result:
            return result
    # ── 默认：手机推送 ──
    try:
        from mobile_bridge import get_bridge
        bridge = get_bridge()
        if not bridge.is_connected:
            return "[推送失败] 没有已连接的手机设备"

        if content_type == "screenshot":
            bridge.push_screenshot()
            return "✅ 截屏已自动捕获并推送到手机（通过HTTP下载链接，无需手动截图）"
        elif content_type == "file":
            p = Path(content)
            if p.exists():
                bridge.push_file(str(p))
                return f"✅ 文件已推送到手机: {p.name}（通过HTTP下载链接）"
            else:
                return f"[推送失败] 文件不存在: {content}"
        else:
            # 文本内容
            bridge.push_chat(content)
            return "✅ 内容已推送到手机"

    except ImportError:
        return "[推送失败] 手机互联模块未加载"
    except Exception as e:
        return f"[推送失败] {e}"


TOOL_MAP = {
    "list_dir": list_dir,
    "read_file": read_file,
    "write_file": write_file,
    "run_command": run_command,
    "edit_file": edit_file,
    "search_files": search_files,
    # 操控工具
    "get_screen_info": get_screen_info,
    "click_at": click_at,
    "double_click_at": double_click_at,
    "right_click_at": right_click_at,
    "move_to": move_to,
    "type_text": type_text,
    "press_key": press_key,
    "scroll_screen": scroll_screen,
    "drag_from_to": drag_from_to,
    "capture_screen": capture_screen,
    # 记忆工具
    "remember": _remember_fact,
    "forget": _forget_fact,
    "set_my_name": _set_agent_name,
    # 应用操控
    "start_application": start_application,
    "focus_window": focus_window,
    "send_hotkey": send_hotkey,
    # 手机推送（三个独立工具）
    "send_screenshot": send_screenshot,
    "send_file": send_file,
    "send_text": send_text,
    "push_to_mobile": push_to_mobile,  # 旧兼容
    # 兼容旧名称
    "list_directory": list_dir,
    "execute_command": run_command,
}

TOOL_DESCRIPTIONS = {
    "list_dir": "List directory contents",
    "read_file": "Read file(s), comma-separated paths",
    "write_file": "Create/overwrite file, auto-create parent dirs",
    "run_command": "Run shell command with timeout",
    "edit_file": "Edit file via find-and-replace",
    "search_files": "Search files by glob pattern(s)",
    "get_screen_info": "Get screen resolution, mouse pos, window list",
    "click_at": "Click at (x, y) coordinates",
    "type_text": "Type text via keyboard (max 500 chars)",
    "press_key": "Press key or combo (e.g. ctrl+c, enter)",
    "scroll_screen": "Scroll wheel, positive=up negative=down",
    "capture_screen": "Take screenshot for visual analysis",
}
