# Nexie — AI Coding Agent
# Copyright (c) 2026. All rights reserved.
# 本软件仅供个人学习与研究使用，禁止未经授权的商业用途、分发或修改后闭源。
# Licensed for personal, educational, and non-commercial use only.
"""
Nexie — 现代聊天界面
纯对话式设计，高DPI适配，全比例缩放响应式布局
"""
import os
import sys
import json
import ctypes
import queue
import logging
import threading
import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk, messagebox, filedialog
from pathlib import Path
from datetime import datetime

from agent_core import AgentCore

# ═══ 现代暗色主题 ═══
class Theme:
    BG = "#161622"
    SURFACE = "#1e1e2e"
    CARD = "#262640"
    ACCENT = "#7c5cf0"
    ACCENT_HOVER = "#9678f4"
    TEXT = "#e0e0f0"
    TEXT_SECONDARY = "#9090b0"
    TEXT_MUTED = "#5a5a70"
    USER_BUBBLE = "#3a3070"
    CODE_BG = "#111118"
    ERROR = "#f87171"
    SUCCESS = "#4ade80"
    WARNING = "#fbbf24"
    BORDER = "#2a2a40"
    SCROLLBAR = "#2a2a40"
    SCROLLBAR_HOVER = "#5a5a70"
    TITLE_BAR = "#0f0f18"
    STATUS_BAR = "#0f0f18"
    INPUT_BG = "#1e1e2e"
    BTN_HOVER = "#262640"
    CANVAS_BG = "#161622"
    DIVIDER = "#2a2a40"

# 注入到ttk（由 _init_dark_style 函数完成）
_WECHAT_BOT = None
def _get_wechat():
    global _WECHAT_BOT
    if _WECHAT_BOT is None:
        try:
            from wechat_bot import get_wechat_bot
            _WECHAT_BOT = get_wechat_bot()
        except Exception: pass
    return _WECHAT_BOT

if getattr(sys, 'frozen', False):
    RESOURCE_ROOT = Path(sys._MEIPASS)
    DATA_ROOT = Path(sys.executable).parent.parent / "Iagent_data"
else:
    RESOURCE_ROOT = Path(__file__).parent
    DATA_ROOT = Path(__file__).parent / "Iagent_data"
PROJECT_ROOT = RESOURCE_ROOT
DATA_ROOT.mkdir(parents=True, exist_ok=True)
CHATS_DIR = DATA_ROOT / "chats"
logger = logging.getLogger("Nexie.GUI")



class _RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


# ═══ 字体 ═══
FONT_FAMILY = "Microsoft YaHei"
MONO_FAMILY = "Consolas"

# 基准字号（窗口宽度 1100px 时的字号）
_BASE_SIZES = {
    "title": 22,      # 欢迎标题
    "subtitle": 11,   # 欢迎副标题
    "card_emoji": 16, # 卡片 emoji
    "body": 10,       # 正文 / 消息
    "small": 9,       # 卡片描述 / 按钮
    "tiny": 8,        # 提示 / 状态栏
    "mono": 9,        # 代码
    "hint": 8,        # 输入提示
}
BASE_WIDTH = 1200
BASE_HEIGHT = 820


# ═══ 暗色滚动条样式 ═══
_STYLE_INITIALIZED = False


def _init_dark_style(root_scale: float = 1.0):
    global _STYLE_INITIALIZED
    if _STYLE_INITIALIZED:
        return
    _STYLE_INITIALIZED = True
    style = ttk.Style()
    style.theme_use("clam")
    # 全局默认暗色（所有ttk组件的基础样式）
    style.configure(".", background=Theme.BG, foreground=Theme.TEXT,
                    fieldbackground=Theme.INPUT_BG, troughcolor=Theme.BG,
                    bordercolor=Theme.BG, relief="flat")
    sw = max(6, int(10 * root_scale))
    style.configure("Dark.Vertical.TScrollbar",
                    background=Theme.SCROLLBAR, troughcolor=Theme.BG,
                    bordercolor=Theme.BG, arrowcolor=Theme.TEXT_MUTED,
                    relief="flat", width=sw)
    style.configure("Dark.Horizontal.TScrollbar",
                    background=Theme.SCROLLBAR, troughcolor=Theme.BG,
                    bordercolor=Theme.BG, arrowcolor=Theme.TEXT_MUTED,
                    relief="flat", width=sw)
    style.map("Dark.Vertical.TScrollbar",
              background=[("active", Theme.SCROLLBAR_HOVER)],
              arrowcolor=[("active", Theme.TEXT_SECONDARY)])


def _reset_dark_style():
    """允许重新应用滚动条样式（缩放时调用）"""
    global _STYLE_INITIALIZED
    _STYLE_INITIALIZED = False


class MainWindow:
    """Nexie 主窗口 — 全比例缩放"""

    # ═══ 初始化 ═══
    def __init__(self, root: tk.Tk, api_key: str, model_type: str = "deepseek"):
        self.root = root
        # 禁用root内边距，消除grid默认留白
        self.root.grid_columnconfigure(0, weight=1, pad=0)
        self.root.grid_rowconfigure(0, weight=1, pad=0)
        self.root['padx'] = 0
        self.root['pady'] = 0
        self.root.title("Nexie")
        self.root.config(bg=Theme.BG, highlightthickness=0, highlightbackground=Theme.BG)
        # 遮罩框架
        self._bg = tk.Frame(self.root, bg=Theme.BG, highlightthickness=0)
        self._bg.place(x=-50, y=-50, relwidth=1.2, relheight=1.2)
        self.root.bind("<Configure>", lambda e: self._bg.place(
            x=-50, y=-50, relwidth=1.2, relheight=1.2))
        # 强制应用级暗色，不依赖Windows系统主题
        try:
            # AllowDarkModeForApp — 绕过系统设置
            ctypes.windll.uxtheme.SetPreferredAppMode(1)  # ForceDark
            ctypes.windll.uxtheme.FlushMenuThemes()
        except Exception:
            pass
        def _dark_frame():
            try:
                hwnd = self.root.winfo_id()
                dark = 0x00161622
                dwm = ctypes.windll.dwmapi
                dwm.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(ctypes.c_int(1)), ctypes.sizeof(ctypes.c_int))
                dwm.DwmSetWindowAttribute(hwnd, 35, ctypes.byref(ctypes.c_int(dark)), ctypes.sizeof(ctypes.c_int))
                dwm.DwmSetWindowAttribute(hwnd, 34, ctypes.byref(ctypes.c_int(dark)), ctypes.sizeof(ctypes.c_int))
            except Exception:
                pass
        self.root.after(50, _dark_frame)
        # 暗色样式必须在任何ttk组件创建前初始化
        _init_dark_style(1.0)
        # 关闭按钮 → 最小化到托盘，不退出
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._tray_icon = None
        self._tray_setup()
        # 不用overrideredirect — 原生窗口避免拖拽残影+任务栏异常
        self.model_type = model_type


        # ── 缩放系统 ──
        self._scale = 1.0
        self._scale_job = None

        # ── 边缘缩放 ──
        self._rs_edge = None
        self._rs_sx = self._rs_sy = 0
        self._rs_geom = (0, 0, 0, 0)
        self._rs_pend = None
        self._rs_tid = None

        # ── 字体对象（可动态改字号）──
        self.fonts = {}
        for name, size in _BASE_SIZES.items():
            fam = MONO_FAMILY if name == "mono" else FONT_FAMILY
            self.fonts[name] = tkfont.Font(family=fam, size=size)
        self.fonts["body_bold"] = tkfont.Font(family=FONT_FAMILY, size=_BASE_SIZES["body"], weight="bold")
        self.fonts["title_bold"] = tkfont.Font(family=FONT_FAMILY, size=_BASE_SIZES["title"], weight="bold")
        self.fonts["small_bold"] = tkfont.Font(family=FONT_FAMILY, size=_BASE_SIZES["small"], weight="bold")
        self.fonts["tiny_bold"] = tkfont.Font(family=FONT_FAMILY, size=_BASE_SIZES["tiny"], weight="bold")

        # ── 窗口尺寸 ──
        sw = root.winfo_screenwidth()
        sh = root.winfo_screenheight()
        w = min(BASE_WIDTH, int(sw * 0.7))
        h = min(BASE_HEIGHT, int(sh * 0.8))
        x = (sw - w) // 2
        y = (sh - h) // 2
        self.root.geometry(f"{w}x{h}+{x}+{y}")
        self.root.minsize(w, h)
        self._normal_geom = (w, h, x, y)
        self._maximized = False

        if sys.platform == "win32":
            pass  # 原生窗口无需额外样式

        self._set_app_icon()

        # ── Agent ──
        self.agent = AgentCore(api_key, working_dir=str(PROJECT_ROOT), model_type=model_type)
        self.gui_queue = queue.Queue()

        # ── 任务队列（UI线程/工作线程分离）──
        from nexie.task_queue import get_task_queue
        self._task_queue = get_task_queue(max_size=5, timeout=300)
        self._task_queue.set_status_callback(self._on_queue_status_change)
        self._task_queue.start_worker(self._worker_process)

        self._stream_buf = {}       # widget → 累积文本 (批量刷新防O(n²))
        self._stream_dirty = set()  # 待刷新widget集合
        self._thinking_buf = ""     # 思考文本累积缓冲
        self._flush_timer = None    # 定期刷新定时器ID
        self._processing = False
        self._display_log = []
        self._stream_lbl = None
        self._inject_buf = []         # 注入消息缓冲区
        self._inject_timer = None     # 防抖定时器
        self._current_chat_file = None
        self._auto_scroll = True        # 自动滚到底部（用户上滑时临时关闭）
        self._scroll_job = None         # 滚动节流after ID
        self._last_ai_saved_len = 0
        self._autonomous_mode = False  # 自治模式
        self._global_permission = False  # 全局权限：开启后跳过所有权限弹窗+管理员确认

        # ── 权限钩子: 三级风险弹窗 (默认允许, 回车即通过) ──
        self._perm_queue = queue.Queue()
        self._perm_always = set()  # 已选"始终允许"的工具(仅moderate风险)
        def _perm_hook(tool_name, description, risk="moderate", allow_always=True):
            # 全局权限开启 → 所有操作直接放行
            if self._global_permission:
                return True
            # moderate且已选始终允许 → 跳过弹窗
            if risk == "moderate" and tool_name in self._perm_always:
                return True
            # high风险 → 即使之前选过始终允许，也要每次确认
            self.gui_queue.put(("_perm_ask", tool_name, description, risk, allow_always))
            try:
                result = self._perm_queue.get(timeout=45)
                if result == "always":
                    self._perm_always.add(tool_name)
                return result
            except Exception:
                return False  # 超时默认拒绝
        self.agent._permission_hook = _perm_hook

        # ── Nexie: 初始化手机桥接层 ──
        self._mobile_bridge = None
        self._mobile_active = False
        try:
            from mobile_bridge import init_bridge
            from http_upload import get_http_upload_server, set_file_callback
            # 中继服务器URL（可从.env或环境变量配置，用于内网穿透远程访问）
            relay_url = os.environ.get("NEXIE_RELAY_URL", "")
            if not relay_url:
                try:
                    from dotenv import load_dotenv
                    env_vals = load_dotenv(Path(__file__).parent / ".env")
                    relay_url = os.environ.get("NEXIE_RELAY_URL", "")
                except Exception:
                    pass
            self._mobile_bridge = init_bridge(relay_url=relay_url)
            self._mobile_bridge.bind_agent(self.agent)
            self._mobile_bridge.bind_gui(self)
            # 启动HTTP文件上传服务器
            self._http_upload = get_http_upload_server(9528)
            self._http_upload.start()
            # 零配置内网穿透：UPnP优先 → SSH隧道后备
            self._upnp = None
            self._tunnel = None
            try:
                from nexie.upnp import get_upnp_mapper
                self._upnp = get_upnp_mapper(9527)
                t = threading.Thread(target=self._try_upnp_then_tunnel, daemon=True)
                t.start()
                self.root.after(5000, self._update_remote_status)
            except Exception:
                pass
            def on_http_file(filepath, filename, mime_type):
                """HTTP上传文件 → 通知GUI"""
                fp = Path(filepath)
                size = fp.stat().st_size
                # 通知 GUI 显示文件
                self.gui_queue.put(("mobile_file", filename, str(fp), mime_type, size))
                # 通知手机端文件已收到（广播给所有已连接设备）
                if self._mobile_bridge and self._mobile_bridge._server:
                    from communication import build_message
                    self._mobile_bridge._server.broadcast_message(build_message("status", {
                        "phase": "file_received",
                        "filename": filename,
                        "size": size,
                    }))
            set_file_callback(on_http_file)
            self._mobile_active = True
        except Exception:
            pass  # 通信模块不可用时静默降级

        # ── Nexie: 初始化微信 ClawBot ──
        self._wechat_bot = None
        try:
            from wechat_bot import init_wechat_bot
            self._wechat_bot = init_wechat_bot()
            self._wechat_bot.bind_agent(self.agent)
            self._wechat_bot.bind_gui(self)
            # 更新按钮状态
            if self._wechat_bot.is_logged_in:
                self._update_wechat_btn(True)
        except Exception:
            pass  # 微信模块不可用时静默降级

        self._build_ui()
        self._poll_queue()
        self._show_welcome()

        # ── 缩放监听 ──
        # 字体固定，不随窗口缩放变化
        self.root.bind('<Motion>', self._on_rs_motion, add='+')
        self.root.bind('<Button-1>', self._on_rs_down, add='+')
        self.root.bind('<B1-Motion>', self._on_rs_drag, add='+')
        self.root.bind('<ButtonRelease-1>', self._on_rs_up, add='+')

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(200, self._scroll_bottom)

    # ═══ 缩放系统（轻量：仅更新外壳，不碰消息内容） ═══
    def _get_scale(self) -> float:
        w = self.root.winfo_width()
        return max(0.85, min(2.2, w / BASE_WIDTH))

    def _s(self, base: int) -> int:
        return max(1, int(base * self._scale))

    def _apply_scale(self):
        """仅更新字体 + 外壳尺寸，不遍历消息组件"""
        self._scale_job = None
        s = self._scale

        # 字体
        for name, base in _BASE_SIZES.items():
            self.fonts[name].configure(size=max(5, int(base * s)))
        self.fonts["body_bold"].configure(size=max(5, int(_BASE_SIZES["body"] * s)))
        self.fonts["title_bold"].configure(size=max(5, int(_BASE_SIZES["title"] * s)))
        self.fonts["small_bold"].configure(size=max(5, int(_BASE_SIZES["small"] * s)))
        self.fonts["tiny_bold"].configure(size=max(5, int(_BASE_SIZES["tiny"] * s)))

        # 外壳
        pass  # 自定义标题栏已移除
        self.status_frame.configure(height=self._s(24))
        _reset_dark_style()
        _init_dark_style(s)
        self.input_text.configure(font=self.fonts["body"],
                                  height=max(2, self._s(3)))

    # ═══ 边缘缩放（50fps 节流，不碰 WindowProc） ═══
    def _rs_hit(self, event):
        x = event.x_root - self.root.winfo_rootx()
        y = event.y_root - self.root.winfo_rooty()
        w, h = self.root.winfo_width(), self.root.winfo_height()
        b = 6
        L, R, T, B = x < b, x > w - b, y < b, y > h - b
        if T and L: return 'nw'
        if T and R: return 'ne'
        if B and L: return 'sw'
        if B and R: return 'se'
        if L: return 'w'
        if R: return 'e'
        if T: return 'n'
        if B: return 's'
        return None

    def _on_rs_motion(self, event):
        if self._rs_edge: return
        cs = {'n':'top_side','s':'bottom_side','e':'right_side','w':'left_side',
              'ne':'top_right_corner','nw':'top_left_corner',
              'se':'bottom_right_corner','sw':'bottom_left_corner'}
        e = self._rs_hit(event)
        self.root.configure(cursor=cs.get(e, ''))

    def _on_rs_down(self, event):
        e = self._rs_hit(event)
        if e:
            self._rs_edge = e
            self._rs_sx, self._rs_sy = event.x_root, event.y_root
            self._rs_geom = (self.root.winfo_x(), self.root.winfo_y(),
                             self.root.winfo_width(), self.root.winfo_height())
            return "break"

    def _on_rs_drag(self, event):
        if not self._rs_edge: return
        dx = event.x_root - self._rs_sx
        dy = event.y_root - self._rs_sy
        ox, oy, ow, oh = self._rs_geom
        e = self._rs_edge
        nw, nh = ow, oh
        nx, ny = ox, oy
        if 'e' in e: nw = max(500, ow + dx)
        if 'w' in e: nw = max(500, ow - dx); nx = ox + ow - nw
        if 's' in e: nh = max(350, oh + dy)
        if 'n' in e: nh = max(350, oh - dy); ny = oy + oh - nh
        self._rs_pend = f"{nw}x{nh}+{nx}+{ny}"
        if not self._rs_tid:
            self._rs_tid = self.root.after(20, self._rs_flush)

    def _rs_flush(self):
        self._rs_tid = None
        if self._rs_pend:
            self.root.geometry(self._rs_pend)
            self._rs_pend = None

    def _on_rs_up(self, event):
        if self._rs_tid:
            self.root.after_cancel(self._rs_tid); self._rs_tid = None
        if self._rs_pend:
            self.root.geometry(self._rs_pend); self._rs_pend = None
        self._rs_edge = None
        self.root.configure(cursor='')


    # ═══ 窗口样式 ═══
    def _set_app_icon(self):
        try:
            ico_path = PROJECT_ROOT / "app_icon.ico"
            if ico_path.exists():
                self.root.iconbitmap(str(ico_path))
        except Exception:
            pass

    # ═══ UI 构建 ═══
    def _build_ui(self):
        self.root.grid_rowconfigure(1, weight=1)
        self.root.grid_columnconfigure(0, weight=1)
        # 工具栏(功能按钮) — 不是标题栏,不包含窗口控制按钮
        self._build_toolbar()
        self._build_chat()
        self._build_input()
        self._build_status()

    # ── 工具栏(功能按钮,不放窗口控制) ──
    def _build_toolbar(self):
        bar = tk.Frame(self.root, bg=Theme.TITLE_BAR, height=self._s(36))
        bar.grid(row=0, column=0, sticky="new")
        bar.grid_propagate(True)  # 允许内容撑开，防止按钮被裁剪
        bar.pack_propagate(False)

        fcfg = {"bg": Theme.SURFACE, "fg": Theme.TEXT, "font": self.fonts["tiny"],
                "relief": tk.FLAT, "cursor": "hand2", "bd": 0,
                "activebackground": Theme.SCROLLBAR, "activeforeground": Theme.TEXT,
                "padx": self._s(10), "pady": self._s(3)}

        # 左侧新增：自治模式 + 扫描
        self._auto_btn = tk.Button(bar, text="🤖 自治", command=self._toggle_autonomous,
                                   bg=Theme.SURFACE, fg=Theme.TEXT_SECONDARY, font=self.fonts["tiny"],
                                   relief=tk.FLAT, cursor="hand2", bd=0, padx=self._s(10), pady=self._s(3))
        self._auto_btn.pack(side=tk.LEFT, padx=self._s(4))
        tk.Button(bar, text="🔍 扫描", command=self._scan_project, **fcfg).pack(side=tk.LEFT, padx=self._s(2))

        tk.Button(bar, text="新建聊天", command=self._new_chat, **fcfg).pack(side=tk.RIGHT, padx=self._s(2))
        tk.Button(bar, text="历史记录", command=self._show_history, **fcfg).pack(side=tk.RIGHT, padx=self._s(2))
        tk.Button(bar, text="工作目录", command=self._change_working_dir, **fcfg).pack(side=tk.RIGHT, padx=self._s(2))
        tk.Button(bar, text="API密钥", command=self._open_config, **fcfg).pack(side=tk.RIGHT, padx=self._s(2))
        tk.Button(bar, text="清空对话", command=self._clear_conversation, **fcfg).pack(side=tk.RIGHT, padx=self._s(2))
        self._mobile_btn = tk.Button(bar, text="📱 手机互联", command=self._open_mobile_connect,
                                     bg="#2a3a5c", fg=Theme.ACCENT, font=self.fonts["tiny"],
                                     relief=tk.FLAT, cursor="hand2", bd=0,
                                     activebackground="#3a4a7c", activeforeground="#a0b0ff",
                                     padx=self._s(10), pady=self._s(3))
        self._mobile_btn.pack(side=tk.RIGHT, padx=self._s(4))
        self._wechat_btn = tk.Button(bar, text="💬 微信", command=self._open_wechat_login,
                                     bg="#2a4a3c", fg="#4a9a5a", font=self.fonts["tiny"],
                                     relief=tk.FLAT, cursor="hand2", bd=0,
                                     activebackground="#3a6a4c", activeforeground="#6ab87a",
                                     padx=self._s(10), pady=self._s(3))
        self._wechat_btn.pack(side=tk.RIGHT, padx=self._s(4))
        tk.Button(bar, text="📎 发文件给AI", command=self._send_file_to_wechat,
                  bg="#3a3a50", fg="#b0b0cc", font=self.fonts["tiny"],
                  relief=tk.FLAT, cursor="hand2", bd=0,
                  activebackground="#4a4a60", activeforeground="#d0d0e0",
                  padx=self._s(10), pady=self._s(3)).pack(side=tk.RIGHT, padx=self._s(4))

    # ── 聊天区 ──
    def _build_chat(self):
        _init_dark_style(self._scale)

        self.chat_frame = tk.Frame(self.root, bg=Theme.BG)
        self.chat_frame.grid(row=1, column=0, sticky="nsew")
        self.chat_frame.grid_rowconfigure(0, weight=1)
        self.chat_frame.grid_columnconfigure(0, weight=1)

        self.chat_canvas = tk.Canvas(self.chat_frame, bg=Theme.BG,
                                     highlightthickness=0, bd=0)
        self.chat_canvas.grid(row=0, column=0, sticky="nsew")

        self.scrollbar = ttk.Scrollbar(self.chat_frame, orient=tk.VERTICAL,
                                       command=self.chat_canvas.yview,
                                       style="Dark.Vertical.TScrollbar")
        self.scrollbar.grid(row=0, column=1, sticky="ns")
        self.chat_canvas.configure(yscrollcommand=self.scrollbar.set)

        self.msg_container = tk.Frame(self.chat_canvas, bg=Theme.BG)
        self.msg_container.bind("<Configure>",
                                lambda e: self.chat_canvas.configure(
                                    scrollregion=self.chat_canvas.bbox("all")))

        self.canvas_window = self.chat_canvas.create_window(
            (0, 0), window=self.msg_container, anchor="nw", tags="msg_window")

        self.chat_canvas.bind("<Configure>", self._on_canvas_resize)
        self.chat_canvas.bind("<Enter>", lambda e: self._bind_mousewheel())
        self.chat_canvas.bind("<Leave>", lambda e: self._unbind_mousewheel())

    def _on_canvas_resize(self, event):
        self.chat_canvas.itemconfig(self.canvas_window, width=event.width)

    def _bind_mousewheel(self):
        self.chat_canvas.bind_all("<MouseWheel>", self._on_mousewheel)

    def _unbind_mousewheel(self):
        self.chat_canvas.unbind_all("<MouseWheel>")

    def _on_mousewheel(self, event):
        """鼠标滚轮 — 边界钳制 + 追踪用户滚动意图（上滑关闭自动滚底，滑到底重新开启）"""
        top, bottom = self.chat_canvas.yview()
        delta = -1 * (event.delta / 120)
        # 已在顶部(top<=0)且向上滚(delta<0) → 拦截
        if delta < 0 and top <= 0.0:
            return
        # 已在底部(bottom>=1)且向下滚(delta>0) → 拦截
        if delta > 0 and bottom >= 1.0:
            return
        self.chat_canvas.yview_scroll(int(delta), "units")
        # 用户手动上滑 → 关闭自动滚底，允许阅读上方内容
        if delta < 0:
            self._auto_scroll = False
        # 用户手动滑到底 → 重新开启自动滚底
        self.root.after(100, self._check_auto_scroll)

    # ── 输入区 ──
    def _build_input(self):
        input_frame = tk.Frame(self.root, bg=Theme.BG)
        input_frame.grid(row=2, column=0, sticky="ew", padx=0, pady=0)

        tk.Frame(input_frame, bg=Theme.SCROLLBAR, height=1).pack(fill=tk.X)

        outer = tk.Frame(input_frame, bg=Theme.BG)
        outer.pack(fill=tk.X, padx=self._s(20), pady=(self._s(12), self._s(6)))

        self.input_frame = tk.Frame(outer, bg=Theme.SURFACE, highlightthickness=1,
                                    highlightbackground=Theme.SCROLLBAR_HOVER, highlightcolor=Theme.ACCENT)
        self.input_frame.pack(fill=tk.X)

        self.input_text = tk.Text(self.input_frame, wrap=tk.WORD,
                                  height=max(2, self._s(3)),
                                  bg=Theme.SURFACE, fg=Theme.TEXT,
                                  font=self.fonts["body"], relief=tk.FLAT,
                                  padx=self._s(14), pady=self._s(10), bd=0,
                                  insertbackground=Theme.ACCENT,
                                  selectbackground=Theme.USER_BUBBLE)
        self.input_text.pack(fill=tk.X, side=tk.LEFT, expand=True)

        self._placeholder = "输入编程问题或任务..."
        self.input_text.insert("1.0", self._placeholder)
        self.input_text.configure(fg="#5a5a6e")
        self.input_text.bind("<FocusIn>", self._on_input_focus_in)
        self.input_text.bind("<FocusOut>", self._on_input_focus_out)
        self.input_text.bind("<Return>", lambda e: self._send_message())
        self.input_text.bind("<Shift-Return>", lambda e: self._insert_newline())

        self.send_btn = tk.Button(self.input_frame, text="发送", command=self._send_message,
                                  bg=Theme.ACCENT, fg="#ffffff", font=self.fonts["body_bold"],
                                  relief=tk.FLAT, cursor="hand2", bd=0,
                                  padx=self._s(20), pady=self._s(10),
                                  activebackground=Theme.ACCENT, activeforeground="#ffffff")
        self.send_btn.pack(side=tk.RIGHT, padx=(self._s(8), self._s(8)), pady=self._s(6))

        # 全局权限按钮 — 开启后跳过所有权限弹窗+管理员确认，回到高效模式
        self._perm_btn = tk.Button(self.input_frame, text="🔓 权限", command=self._toggle_global_permission,
                                   bg=Theme.SURFACE, fg=Theme.TEXT_SECONDARY, font=self.fonts["tiny_bold"],
                                   relief=tk.FLAT, cursor="hand2", bd=0,
                                   padx=self._s(10), pady=self._s(10),
                                   activebackground=Theme.SCROLLBAR, activeforeground=Theme.TEXT)
        self._perm_btn.pack(side=tk.RIGHT, padx=(0, self._s(4)), pady=self._s(6))

        hint_frame = tk.Frame(outer, bg=Theme.BG)
        hint_frame.pack(fill=tk.X, pady=(0, self._s(8)))
        tk.Label(hint_frame, text="Enter 发送  ·  Shift+Enter 换行  ·  纯图形界面",
                 font=self.fonts["tiny"], fg="#4a4a55", bg=Theme.BG).pack(side=tk.LEFT)

        self.stop_btn = tk.Button(hint_frame, text="停止生成", command=self._stop_processing,
                                  bg=Theme.BG, fg=Theme.ERROR, font=self.fonts["tiny"],
                                  relief=tk.FLAT, cursor="hand2", bd=0,
                                  activebackground=Theme.BG, activeforeground="#ff7777",
                                  padx=self._s(10), pady=self._s(2),
                                  state=tk.DISABLED)
        self.stop_btn.pack(side=tk.RIGHT)

    # ── 状态栏 ──
    def _build_status(self):
        self.status_frame = tk.Frame(self.root, bg=Theme.TITLE_BAR, height=self._s(24))
        self.status_frame.grid(row=3, column=0, sticky="ew")
        self.status_frame.grid_propagate(False)
        tk.Frame(self.status_frame, bg=Theme.SCROLLBAR, height=1).pack(fill=tk.X)
        inner = tk.Frame(self.status_frame, bg=Theme.TITLE_BAR)
        inner.pack(fill=tk.X, padx=self._s(14), pady=self._s(1))
        self.status_var = tk.StringVar(value="就绪")
        tk.Label(inner, textvariable=self.status_var, font=self.fonts["tiny"],
                 fg="#5a5a6e", bg=Theme.TITLE_BAR).pack(side=tk.LEFT)
        # 手机连接状态
        self._mobile_status_var = tk.StringVar(value="")
        self._mobile_status_lbl = tk.Label(inner, textvariable=self._mobile_status_var,
                                           font=self.fonts["tiny"], fg="#4a9a5a", bg=Theme.TITLE_BAR)
        self._mobile_status_lbl.pack(side=tk.LEFT, padx=(self._s(12), 0))
        # 微信连接状态
        self._wechat_status_var = tk.StringVar(value="")
        self._wechat_status_lbl = tk.Label(inner, textvariable=self._wechat_status_var,
                                           font=self.fonts["tiny"], fg="#4a9a5a", bg=Theme.TITLE_BAR)
        self._wechat_status_lbl.pack(side=tk.LEFT, padx=(self._s(12), 0))
        self.model_label_var = tk.StringVar(
            value="MiMo-V2-Omni · 视觉" if self.model_type == "mimo" else "deepseek-v4-pro · 深度思考"
        )
        tk.Label(inner, textvariable=self.model_label_var,
                 font=self.fonts["tiny"], fg="#5a5a6e", bg=Theme.TITLE_BAR).pack(side=tk.RIGHT)

    # ═══ 消息渲染 ═══
    @staticmethod
    def _now() -> str:
        return datetime.now().strftime("%H:%M")

    def _ai_width(self) -> int:
        """AI回复内容宽度(字符数)，约为容器78%，与用户消息气泡对齐"""
        container_w = self.msg_container.winfo_width()
        char_w = max(20, (container_w or 700) // 12)
        return min(95, max(35, int(char_w * 0.78)))

    def _show_welcome(self):
        wf = tk.Frame(self.msg_container, bg=Theme.BG)
        wf.pack(fill=tk.X, pady=(self._s(60), self._s(20)))

        tk.Label(wf, text="Nexie", font=self.fonts["title_bold"],
                 fg=Theme.TEXT, bg=Theme.BG).pack()
        tk.Label(wf, text="您的专业编程助手，纯图形界面操作",
                 font=self.fonts["subtitle"], fg=Theme.TEXT_MUTED, bg=Theme.BG).pack(pady=(self._s(4), self._s(20)))

        cards = tk.Frame(wf, bg=Theme.BG)
        cards.pack(fill=tk.X, padx=self._s(20))
        for i in range(4):
            cards.grid_columnconfigure(i, weight=1)

        features = [
            ("📂", "浏览项目", "查看目录结构"),
            ("📝", "编写代码", "创建/编辑文件"),
            ("▶️", "运行命令", "安装依赖/执行"),
            ("🔍", "分析调试", "定位/修复Bug"),
        ]
        for i, (emoji, title, desc) in enumerate(features):
            card = tk.Frame(cards, bg=Theme.SURFACE, padx=self._s(18), pady=self._s(12))
            card.grid(row=0, column=i, padx=self._s(6), pady=self._s(4), sticky="n")
            tk.Label(card, text=emoji, font=self.fonts["card_emoji"], bg=Theme.SURFACE).pack()
            tk.Label(card, text=title, font=self.fonts["small_bold"],
                     fg="#d0d0d8", bg=Theme.SURFACE).pack(pady=(self._s(2), 0))
            tk.Label(card, text=desc, font=self.fonts["tiny"],
                     fg=Theme.TEXT_MUTED, bg=Theme.SURFACE).pack()

    def _add_user_message(self, text: str, timestamp: str = None):
        row = tk.Frame(self.msg_container, bg=Theme.BG)
        row.pack(fill=tk.X, padx=self._s(40), pady=(self._s(12), self._s(2)))

        tk.Frame(row, bg=Theme.BG).pack(side=tk.LEFT, fill=tk.X, expand=True)

        bubble = tk.Frame(row, bg=Theme.USER_BUBBLE, padx=2, pady=2, highlightthickness=0, bd=0)
        bubble.pack(side=tk.RIGHT)

        container_w = self.msg_container.winfo_width()
        char_w = max(20, (container_w or 700) // 12)
        bubble_width = min(50, max(15, char_w // 2))
        txt = tk.Text(bubble, font=self.fonts["body"], fg=Theme.TEXT, bg=Theme.USER_BUBBLE,
                      wrap=tk.WORD, relief=tk.FLAT, bd=0, width=bubble_width,
                      height=1, padx=self._s(12), pady=self._s(8),
                      selectbackground=Theme.ACCENT, selectforeground="#ffffff")
        txt.pack()  # 先放入布局，确定实际宽度
        txt.insert("1.0", text)
        txt.update_idletasks()  # 触发自动换行计算
        # count -displaylines 统计含自动换行的真实显示行数
        actual = int(txt.tk.call(txt._w, "count", "-displaylines", "1.0", "end"))
        txt.configure(height=max(1, actual))
        txt.configure(state=tk.DISABLED)

        ts = timestamp or self._now()
        tk.Label(row, text=ts + "  ", font=self.fonts["tiny"],
                 fg="#4a4a55", bg=Theme.BG).pack(side=tk.RIGHT)

        self._display_log.append({"type": "user", "text": text, "time": ts})

    def _add_ai_message(self, text: str):
        if not text.strip():
            return
        row = tk.Frame(self.msg_container, bg=Theme.BG)
        row.pack(fill=tk.X, padx=self._s(40), pady=(self._s(2), self._s(6)))

        txt = tk.Text(row, font=self.fonts["body"], fg="#d0d0d8", bg=Theme.BG,
                      wrap=tk.WORD, relief=tk.FLAT, bd=0,
                      width=self._ai_width(), height=1, padx=self._s(4), pady=0,
                      selectbackground=Theme.ACCENT, selectforeground="#ffffff")
        txt.pack(side=tk.LEFT, anchor="nw")
        txt.insert("1.0", text)
        txt.update_idletasks()
        actual = int(txt.tk.call(txt._w, "count", "-displaylines", "1.0", "end"))
        txt.configure(height=max(1, actual))
        txt.configure(state=tk.DISABLED)

    def _add_tool_log(self, name: str, summary: str):
        """所有工具步骤统一折叠在一个面板下，默认收起，带独立滚动"""
        # ═══ 写入日志，防止历史记录丢失 ═══
        self._display_log.append({"type": "tool", "name": name, "summary": summary})
        clean_name = name.replace("_结果", "")
        icon_map = {
            "list_dir": "📁", "read_file": "📄", "write_file": "✏️",
            "run_command": "💻", "edit_file": "🔧", "search_files": "🔍",
        }
        icon = icon_map.get(clean_name, "🔧")
        step_line = f"{icon} {name}"
        if summary:
            step_line += f" — {summary[:100]}"
        step_line += "\n"

        # 已有面板 → 追加步骤
        if hasattr(self, '_tool_panel') and self._tool_panel and self._tool_panel.winfo_exists():
            p = self._tool_panel
            bt = p._body._text
            bt.configure(state=tk.NORMAL)
            bt.insert(tk.END, step_line)
            bt.configure(state=tk.DISABLED)
            p._step_count += 1
            p._count_label.configure(text=f" ({p._step_count} 步)")
            return

        # 新建统一工具面板（左对齐，宽度略宽于AI文字）
        p = tk.Frame(self.msg_container, bg=Theme.BG)
        p.pack(anchor="w", padx=self._s(40), pady=(self._s(4), self._s(6)))

        # 头部
        hdr = tk.Frame(p, bg=Theme.SURFACE, cursor="hand2")
        hdr.pack(fill=tk.X)
        p._header = hdr
        tk.Label(hdr, text="🔧", font=self.fonts["tiny_bold"],
                 fg="#7a7a8a", bg=Theme.SURFACE).pack(side=tk.LEFT,
                 padx=self._s(8), pady=self._s(3))
        tk.Label(hdr, text="执行过程", font=self.fonts["tiny_bold"],
                 fg="#7a7a8a", bg=Theme.SURFACE).pack(side=tk.LEFT)
        p._count_label = tk.Label(hdr, text=" (1 步)", font=self.fonts["tiny"],
                                  fg="#5a5a6a", bg=Theme.SURFACE)
        p._count_label.pack(side=tk.LEFT)
        p._step_count = 1
        # 三角固定在标签右边（与思考面板一样），不用side=RIGHT避免折叠时位置跳变
        p._arrow = tk.Label(hdr, text=" ▶", font=self.fonts["tiny"],
                            fg="#7a7a8a", bg=Theme.SURFACE, cursor="hand2")
        p._arrow.pack(side=tk.LEFT, pady=self._s(3))

        # 体部（默认折叠，宽度与AI文字对齐，CHAR换行保完整显示）
        body = tk.Frame(p, bg=Theme.CODE_BG)
        p._body = body
        bt = tk.Text(body, font=self.fonts["mono"], fg=Theme.TEXT_SECONDARY, bg=Theme.CODE_BG,
                     wrap=tk.CHAR, relief=tk.FLAT, bd=0, width=self._ai_width(), height=5,
                     padx=self._s(6), pady=self._s(4),
                     selectbackground=Theme.ACCENT, selectforeground="#ffffff")
        sb = ttk.Scrollbar(body, orient=tk.VERTICAL, command=bt.yview,
                           style="Dark.Vertical.TScrollbar")
        bt.configure(yscrollcommand=sb.set)
        bt.pack(side=tk.LEFT, fill=tk.Y)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        body._text = bt

        # ── 鼠标滚轮隔离：在工具面板内滚动不影响主聊天 ──
        def _on_tool_enter(e):
            self.chat_canvas.unbind_all("<MouseWheel>")
            bt.bind("<MouseWheel>", _on_tool_wheel)
            sb.bind("<MouseWheel>", _on_tool_wheel)
        def _on_tool_leave(e):
            bt.unbind("<MouseWheel>")
            sb.unbind("<MouseWheel>")
            self.chat_canvas.bind_all("<MouseWheel>", self._on_mousewheel)
        def _on_tool_wheel(e):
            bt.yview_scroll(int(-1 * (e.delta / 120)), "units")
            return "break"
        bt.bind("<Enter>", _on_tool_enter)
        bt.bind("<Leave>", _on_tool_leave)
        sb.bind("<Enter>", _on_tool_enter)
        sb.bind("<Leave>", _on_tool_leave)

        bt.insert("1.0", step_line)
        bt.configure(state=tk.DISABLED)

        # 折叠/展开（三角固定在标签右边，不随面板宽度变化）
        def toggle(e=None):
            if body.winfo_ismapped():
                body.pack_forget()
                p._arrow.configure(text=" ▶")
            else:
                body.pack(fill=tk.X, after=hdr)
                p._arrow.configure(text=" ▼")
                self._scroll_bottom()
        hdr.bind("<Button-1>", toggle)
        p._arrow.bind("<Button-1>", toggle)

        self._tool_panel = p

    def _add_error_message(self, text: str):
        row = tk.Frame(self.msg_container, bg=Theme.BG)
        row.pack(fill=tk.X, padx=self._s(40), pady=self._s(4))

        txt = tk.Text(row, font=self.fonts["small"], fg=Theme.ERROR, bg=Theme.BG,
                      wrap=tk.WORD, relief=tk.FLAT, bd=0,
                      width=self._ai_width(), height=1, padx=self._s(4), pady=0,
                      selectbackground=Theme.ACCENT, selectforeground="#ffffff")
        txt.pack(side=tk.LEFT, anchor="nw")
        txt.insert("1.0", text)
        txt.update_idletasks()
        actual = int(txt.tk.call(txt._w, "count", "-displaylines", "1.0", "end"))
        txt.configure(height=max(1, actual))
        txt.configure(state=tk.DISABLED)
        self._display_log.append({"type": "error", "text": text})

    # ═══ 流式输出 ═══
    def _create_stream_label(self):
        """AI流式输出标签 — 宽度限制为容器62%，左对齐，与用户消息时间戳对齐"""
        row = tk.Frame(self.msg_container, bg=Theme.BG)
        row.pack(fill=tk.X, padx=self._s(40), pady=(self._s(2), self._s(6)))
        txt = tk.Text(row, font=self.fonts["body"],
                      fg="#d0d0d8", bg=Theme.BG, wrap=tk.WORD,
                      relief=tk.FLAT, bd=0, width=self._ai_width(), height=4,
                      padx=self._s(4), pady=0,
                      selectbackground=Theme.ACCENT, selectforeground="#ffffff",
                      state=tk.DISABLED)
        txt.pack(side=tk.LEFT, anchor="nw")
        return txt, row

    def _append_thinking(self, text: str, thinking_lbl=None):
        """展示模型思考过程。支持按任务标签路由。"""
        self._thinking_buf += text
        if len(self._thinking_buf) >= 150:
            self._flush_thinking_buf(thinking_lbl)

    def _flush_thinking_buf(self, thinking_lbl=None):
        """批量刷新思考文本到指定的思考面板（默认全局标签）"""
        if not self._thinking_buf:
            return
        target = thinking_lbl or self._thinking_lbl
        if not target:
            self._thinking_buf = ""
            return
        try:
            target.configure(state=tk.NORMAL)
            target.insert(tk.END, self._thinking_buf)
            target.configure(state=tk.DISABLED)
            target.see(tk.END)
            self._thinking_buf = ""
        except Exception:
            self._thinking_buf = ""

    def _create_thinking_label(self):
        """创建思考展示面板 — 左对齐，宽度略宽于AI文字，带独立滚动条"""
        frame = tk.Frame(self.msg_container, bg=Theme.CODE_BG, padx=2, pady=1)
        frame.pack(anchor="w", padx=self._s(40), pady=(self._s(1), 0))
        show_var = tk.BooleanVar(value=False)
        toggle = tk.Label(frame, text="🧠 思考中 ▶", font=self.fonts["tiny"],
                          fg=Theme.TEXT_MUTED, bg=Theme.CODE_BG, cursor="hand2")
        toggle.pack(anchor="w")
        frame._thinking_toggle = toggle

        # 内容行：Text + 滚动条（默认折叠，不pack body）
        body = tk.Frame(frame, bg=Theme.CODE_BG)
        # body 初始不pack，由_toggle()在展开时pack
        frame._thinking_body = body

        txt = tk.Text(body, font=self.fonts["tiny"], fg=Theme.TEXT_MUTED,
                      bg=Theme.CODE_BG, wrap=tk.WORD, height=6, borderwidth=0,
                      relief=tk.FLAT, state=tk.DISABLED, width=self._ai_width(),
                      padx=self._s(4))
        sb = ttk.Scrollbar(body, orient=tk.VERTICAL, command=txt.yview,
                           style="Dark.Vertical.TScrollbar")
        txt.configure(yscrollcommand=sb.set)
        txt.pack(side=tk.LEFT, fill=tk.Y)
        sb.pack(side=tk.RIGHT, fill=tk.Y)

        # 思考面板内滚轮隔离：不触发主聊天滚动
        def _on_think_enter(e):
            self.chat_canvas.unbind_all("<MouseWheel>")
            txt.bind("<MouseWheel>", _on_think_wheel)
            sb.bind("<MouseWheel>", _on_think_wheel)
        def _on_think_leave(e):
            txt.unbind("<MouseWheel>")
            sb.unbind("<MouseWheel>")
            self.chat_canvas.bind_all("<MouseWheel>", self._on_mousewheel)
        def _on_think_wheel(e):
            txt.yview_scroll(int(-1 * (e.delta / 120)), "units")
            return "break"
        txt.bind("<Enter>", _on_think_enter)
        txt.bind("<Leave>", _on_think_leave)
        sb.bind("<Enter>", _on_think_enter)
        sb.bind("<Leave>", _on_think_leave)

        # 折叠切换
        def _toggle():
            if show_var.get():
                body.pack_forget()
                toggle.configure(text="🧠 思考中 ▶")
                show_var.set(False)
            else:
                body.pack(fill=tk.X, pady=(self._s(1), 0), after=toggle)
                toggle.configure(text="🧠 思考中 ▼")
                show_var.set(True)
        toggle.bind("<Button-1>", lambda e: _toggle())
        return txt, frame

    def _append_stream_text(self, txt: tk.Text, text: str):
        """累积文本到buffer, 批量刷新避免O(n²)逐token布局重算"""
        self._stream_buf[txt] = self._stream_buf.get(txt, "") + text
        self._stream_dirty.add(txt)
        # 遇到换行或累积60+字符时立即刷新
        if "\n" in text or len(self._stream_buf[txt]) >= 60:
            self._flush_stream_buffers()
        else:
            self._schedule_flush()

    def _schedule_flush(self):
        """30ms后强制刷新（快速响应防延迟感）"""
        if self._flush_timer is not None:
            return  # 已有定时器
        self._flush_timer = self.root.after(30, self._flush_stream_buffers)

    def _flush_stream_buffers(self):
        """批量刷新所有待处理的流式文本"""
        if self._flush_timer is not None:
            self.root.after_cancel(self._flush_timer)
            self._flush_timer = None
        for txt in list(self._stream_dirty):
            buf = self._stream_buf.pop(txt, "")
            self._stream_dirty.discard(txt)
            if not buf:
                continue
            try:
                txt.configure(state=tk.NORMAL)
                txt.insert(tk.END, buf)
                txt.configure(state=tk.DISABLED)
                # 仅在含换行时重算高度（省去90%+的O(n)扫描）
                if "\n" in buf:
                    txt.update_idletasks()
                    actual = int(txt.tk.call(txt._w, "count", "-displaylines", "1.0", "end"))
                    txt.configure(height=max(4, actual))
            except Exception:
                pass
        # 同步刷新思考缓冲
        self._flush_thinking_buf()
        self._scroll_bottom()

    def _snapshot_ai_text_to_log(self):
        """增量截取_stream_lbl中新出现的AI文字写入日志，保持AI→工具→AI正确顺序"""
        if not self._stream_lbl or not self._stream_lbl.winfo_exists():
            return
        try:
            current = self._stream_lbl.get("1.0", "end-1c")
            new_text = current[self._last_ai_saved_len:].strip()
            if new_text:
                self._display_log.append({"type": "ai", "text": new_text})
                self._last_ai_saved_len = len(current)
        except Exception:
            pass

    def _scroll_bottom(self):
        """智能滚底：仅在自动滚动模式下生效，80ms节流防抽搐"""
        if not self._auto_scroll:
            return
        # 节流：取消上次未执行的滚动，80ms后才执行
        if self._scroll_job is not None:
            self.root.after_cancel(self._scroll_job)
        self._scroll_job = self.root.after(80, self._do_scroll_bottom)

    def _do_scroll_bottom(self):
        """实际执行滚到底部"""
        self._scroll_job = None
        if not self._auto_scroll:
            return
        # 不需要 update_idletasks — canvas yview_moveto 本身不依赖布局刷新
        self.chat_canvas.yview_moveto(1.0)

    def _check_auto_scroll(self):
        """检测用户是否滑到了底部，是则重开自动滚底"""
        _, bottom = self.chat_canvas.yview()
        if bottom >= 0.98:  # 距底部2%以内视为到底
            self._auto_scroll = True

    # ═══ 输入事件 ═══
    def _on_input_focus_in(self, event):
        if self.input_text.get("1.0", "end-1c").strip() == self._placeholder:
            self.input_text.delete("1.0", tk.END)
            self.input_text.configure(fg=Theme.TEXT)

    def _on_input_focus_out(self, event):
        if not self.input_text.get("1.0", "end-1c").strip():
            self.input_text.insert("1.0", self._placeholder)
            self.input_text.configure(fg="#5a5a6e")

    def _insert_newline(self, event=None):
        self.input_text.insert(tk.INSERT, "\n")
        return "break"

    # ═══ 消息发送（注入合并模式：多条消息收集后统一发给AI） ═══
    INJECT_DEBOUNCE_MS = 800  # 停止输入800ms后触发合并发送

    def _send_message(self):
        user_input = self.input_text.get("1.0", tk.END).strip()
        if not user_input or user_input == self._placeholder:
            return

        if self._processing:
            self.input_text.delete("1.0", tk.END)
            self.input_text.configure(fg=Theme.TEXT)
            self._add_user_message(user_input)
            self._scroll_bottom()

            self._inject_buf.append(user_input)

            if self._inject_timer is not None:
                self.root.after_cancel(self._inject_timer)
            self._inject_timer = self.root.after(
                self.INJECT_DEBOUNCE_MS, self._flush_inject_buf)
            self.status_var.set(f"💉 {len(self._inject_buf)}条消息待注入...")
            return

        self._do_send(user_input)

    def _flush_inject_buf(self):
        self._inject_timer = None
        if not self._inject_buf:
            return

        msgs = self._inject_buf
        self._inject_buf = []

        self.agent.cancel()
        self._task_queue.clear_queue()

        self._thinking_buf = ""
        self._stream_buf.clear()
        self._stream_dirty.clear()
        self._last_ai_saved_len = 0
        self._auto_scroll = True
        sl, _ = self._create_stream_label()
        self._stream_lbl, self._thinking_lbl = sl, None

        if len(msgs) == 1:
            combined = msgs[0]
        else:
            parts = ["[用户连续发送了以下多条消息，请综合分析后一次性统一回复]\n"]
            for i, m in enumerate(msgs, 1):
                parts.append(f"消息{i}: {m}")
            combined = "\n".join(parts)

        self._task_queue.enqueue(combined)
        self.status_var.set(f"💉 已注入{len(msgs)}条消息（上下文保留）")

    def _do_send(self, user_input: str):

        # ── 空闲 → 正常发送 ──
        status, msg = self._task_queue.enqueue(user_input)

        if status == "rejected":
            if not self._global_permission:
                messagebox.showwarning("队列已满", msg)
            return

        self.input_text.delete("1.0", tk.END)
        self.input_text.configure(fg=Theme.TEXT)
        self._add_user_message(user_input)
        self._scroll_bottom()

        self._thinking_buf = ""
        self._stream_buf.clear()
        self._stream_dirty.clear()
        self._last_ai_saved_len = 0
        self._auto_scroll = True
        sl, _ = self._create_stream_label()
        self._stream_lbl, self._thinking_lbl = sl, None

        if self._mobile_bridge:
            from communication import build_chat_message
            for device_id in self._mobile_bridge._connected_devices:
                self._mobile_bridge._server.send_message(
                    device_id, build_chat_message(user_input, sender="pc_user"))

        self._set_processing(True)
        if status == "queued":
            self.status_var.set(f"📋 {msg}")

    # ═══ 工作线程入口（任务队列回调）═══
    def _worker_process(self, user_message: str):
        """工作线程回调。自治模式→_run_autonomous，普通→_run_agent"""
        if self._autonomous_mode:
            self._run_autonomous(user_message)
        else:
            self._run_agent(user_message)

    # ── 队列状态回调 ──
    def _on_queue_status_change(self, status: dict):
        self.gui_queue.put(("_queue_status", status))

    # ═══ 原始 _run_agent（保持不变，由 _worker_process 或手机/微信直接调用）═══
    def _run_agent(self, user_message: str):
        try:
            from communication import build_message, build_chat_message

            # ═══ 回调: 同时推送PC GUI + 已连接手机 ═══
            def push_both(delta_text):
                self.gui_queue.put(("stream", delta_text))
                if self._mobile_bridge:
                    for device_id in self._mobile_bridge._connected_devices:
                        self._mobile_bridge._server.send_message(
                            device_id, build_chat_message(delta_text, sender="pc"))

            def tool_start_both(name, args):
                self.gui_queue.put(("tool_start", name, args))
                if self._mobile_bridge and self._mobile_bridge.is_connected:
                    self._mobile_bridge.push_tool_status(name, str(args)[:200])

            def tool_result_both(name, result):
                self.gui_queue.put(("tool_result", name, result))

            def thinking_cb(delta):
                self.gui_queue.put(("thinking", delta))

            self.agent.process_message(
                user_message=user_message,
                on_text=push_both,
                on_tool_start=tool_start_both,
                on_tool_result=tool_result_both,
                on_done=lambda f: self.gui_queue.put(("done", f)),
                on_thinking=thinking_cb,
            )
            if self._mobile_bridge and self._mobile_bridge.is_connected:
                self._mobile_bridge._server.broadcast_message(
                    build_message("status", {"phase": "stream_end"}))
        except Exception as e:
            self.gui_queue.put(("error", str(e)))
        finally:
            self.gui_queue.put(("processing_done",))

    # ═══ UI轮询（保持原始逻辑不变）═══
    def _poll_queue(self):
        MAX_MSGS_PER_TICK = 60
        msg_count = 0
        try:
            while msg_count < MAX_MSGS_PER_TICK:
                msg = self.gui_queue.get_nowait()
                msg_count += 1
                mt = msg[0]
                # ── 队列状态更新 ──
                if mt == "_queue_status":
                    st = msg[1]
                    qsize = st.get("queue_size", 0)
                    if st["status_str"] == "idle":
                        pass  # 等 processing_done 统一恢复UI
                    elif qsize > 0:
                        self.status_var.set(f"📋 队列中（{qsize}个等待）")
                # ── 原有消息类型（完全不变）──
                elif mt == "mobile_message":
                    self.display_mobile_message(msg[1], msg[2] if len(msg) > 2 else "")
                elif mt == "processing_done":
                    self._snapshot_ai_text_to_log()
                    # 先刷思考缓冲
                    self._flush_thinking_buf()
                    # 检查是否有实际思考内容
                    has_content = False
                    if hasattr(self, '_thinking_lbl') and self._thinking_lbl and self._thinking_lbl.winfo_exists():
                        try:
                            think_text = self._thinking_lbl.get("1.0", "end-1c").strip()
                            if think_text:
                                self._display_log.append({"type": "thinking", "text": think_text})
                                has_content = True
                        except Exception:
                            pass
                    if has_content:
                        # 有内容：折叠面板
                        try:
                            frame = self._thinking_lbl.master
                            if hasattr(frame, '_thinking_toggle'):
                                frame._thinking_toggle.configure(text="🧠 思考过程 ▼")
                        except Exception:
                            pass
                    elif self._thinking_lbl is not None:
                        # 无内容：销毁空白面板
                        try:
                            self._thinking_lbl.master.destroy()
                        except Exception:
                            pass
                        self._thinking_lbl = None
                    self._last_ai_saved_len = 0
                    self._tool_panel = None
                    self._flush_stream_buffers()
                    self._set_processing(False)
                elif mt == "wechat_message":
                    self._add_user_message(msg[1])
                    self._stream_lbl, _ = self._create_stream_label()
                    self._set_processing(True)
                    self._scroll_bottom()
                elif mt == "wechat_status":
                    status = msg[1]
                    if status == "connected":
                        self._wechat_status_var.set("💬 微信已连接")
                        self._wechat_status_lbl.configure(fg="#4a9a5a")
                        self._update_wechat_btn(True)
                    elif status == "disconnected":
                        self._wechat_status_var.set("")
                        self._update_wechat_btn(False)
                    elif status == "typing":
                        self._wechat_status_var.set("💬 微信: 思考中...")
                    elif status == "idle":
                        self._wechat_status_var.set("💬 微信已连接")
                elif mt == "mobile_file":
                    self.display_mobile_file(msg[1], msg[2], msg[3])
                elif mt == "thinking":
                    # 延迟创建：首次收到思考内容时才显示面板
                    if self._thinking_lbl is None:
                        self._thinking_lbl, _ = self._create_thinking_label()
                    self._append_thinking(msg[1])
                elif mt == "stream":
                    self._append_stream_text(self._stream_lbl, msg[1])
                elif mt == "tool_start":
                    self._snapshot_ai_text_to_log()
                    name, args = msg[1], msg[2]
                    summary = str(args).replace("{", "").replace("}", "")[:80]
                    self._add_tool_log(name, summary)
                elif mt == "tool_result":
                    name, result = msg[1], msg[2]
                    lines = result.strip().split("\n")
                    summary = ""
                    for line in lines:
                        if "✅" in line or "错误" in line or "创建" in line:
                            summary = line[:80]
                            break
                    if not summary and lines:
                        summary = lines[-1][:80]
                    self._add_tool_log(name + "_结果", summary)
                elif mt == "_perm_ask":
                    result = self._show_permission_dialog(msg[1], msg[2], msg[3], msg[4])
                    self._perm_queue.put(result)
                elif mt == "_status":
                    self.status_var.set(str(msg[1])[:120])
                elif mt == "_auto_progress":
                    self.status_var.set(f"🤖 [{msg[1]}] {str(msg[2])[:100]}"[:120])
                elif mt == "done":
                    pass
                elif mt == "error":
                    self._add_error_message(f"❌ {msg[1]}")
        except queue.Empty:
            pass
        finally:
            self.root.after(50, self._poll_queue)

    def _set_processing(self, processing: bool):
        """更新UI处理状态：按钮置灰、状态文字、输入框高亮"""
        self._processing = processing
        if processing:
            self.send_btn.configure(state=tk.NORMAL, text="思考中…", bg="#4a4a60")
            self.stop_btn.configure(state=tk.NORMAL)
            self.status_var.set("AI 正在思考...")
            self.input_frame.configure(highlightbackground=Theme.ACCENT)
        else:
            self.send_btn.configure(state=tk.NORMAL, text="发送", bg=Theme.ACCENT)
            self.stop_btn.configure(state=tk.DISABLED)
            self.status_var.set("就绪")
            self.input_frame.configure(highlightbackground=Theme.SCROLLBAR_HOVER)
            self._scroll_bottom()

    def _stop_processing(self):
        """停止当前任务：取消但不丢排队消息，不等后台线程"""
        self.agent.cancel()
        # 不clear_queue——保留排队中的消息，worker会继续处理
        self._add_error_message("⏹ 已停止")
        self._processing = False
        self._set_processing(False)

    # ═══ 菜单操作 ═══
    def _open_config(self):
        from config_gui import ConfigWindow
        cfg = ConfigWindow(self.root)
        self.root.wait_window(cfg.window)
        if cfg.api_key:
            mt = cfg.model_type.get()
            self.model_type = mt
            self.agent = AgentCore(cfg.api_key, working_dir=self.agent.working_dir, model_type=mt)
            self.model_label_var.set(
                "MiMo-V2-Omni · 视觉" if mt == "mimo" else "deepseek-v4-pro · 深度思考"
            )
            self.status_var.set(f"已切换至 {'MIMO' if mt == 'mimo' else 'DeepSeek'} 模型")

    def _change_working_dir(self):
        d = filedialog.askdirectory(title="选择工作目录", initialdir=self.agent.working_dir)
        if d:
            self.agent.set_working_dir(d)
            self.status_var.set(f"工作目录: {d}")

    def _clear_conversation(self):
        if self._processing:
            if not self._global_permission:
                messagebox.showinfo("提示", "正在处理中，请先停止")
            return
        self.agent.end_session()
        self.agent.reset_conversation()
        for w in self.msg_container.winfo_children():
            w.destroy()
        self._display_log = []
        self._stream_lbl = None
        self._thinking_lbl = None
        self._tool_panel = None
        self._current_chat_file = None
        self._show_welcome()
        self.status_var.set("对话已清空")

    # ═══ 对话历史 ═══
    def _chat_data(self) -> dict:
        log = list(self._display_log)
        # 增量保存已覆盖AI文字，不再重复追加（仅兜底：_stream_lbl有内容但日志没有ai条目时补一次）
        if self._stream_lbl and self._stream_lbl.winfo_exists():
            has_ai = any(e.get("type") == "ai" for e in log)
            if not has_ai:
                stream_text = self._stream_lbl.get("1.0", "end-1c").strip()
                if stream_text:
                    log.append({"type": "ai", "text": stream_text})
        return {
            "working_dir": self.agent.working_dir,
            "agent_messages": [m for m in self.agent.messages if m.get("role") != "system"],
            "display_log": log,
        }

    def _chat_title(self) -> str:
        for entry in self._display_log:
            if entry.get("type") == "user":
                title = entry["text"].strip()[:30]
                return title if title else "空对话"
        return "新对话"

    def _save_current_chat(self):
        data = self._chat_data()
        if not data["agent_messages"]:
            return
        try:
            CHATS_DIR.mkdir(exist_ok=True)
            if self._current_chat_file and self._current_chat_file.exists():
                filepath = self._current_chat_file
                try:
                    old = json.loads(filepath.read_text(encoding="utf-8"))
                    old["agent_messages"] = data["agent_messages"]
                    old["display_log"] = data["display_log"]
                    old["working_dir"] = data["working_dir"]
                    if not old.get("saved_at"):
                        ts_str = filepath.stem[:15]
                        try:
                            old["saved_at"] = datetime.strptime(ts_str, "%Y%m%d_%H%M%S").isoformat()
                        except Exception:
                            old["saved_at"] = datetime.now().isoformat()
                    data = old
                except Exception:
                    data["saved_at"] = datetime.now().isoformat()
            else:
                ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                title = self._chat_title()
                safe_title = "".join(c for c in title if c.isalnum() or c in "._- ")[:30]
                filename = f"{ts}_{safe_title}.json"
                filepath = CHATS_DIR / filename
                self._current_chat_file = filepath
                data["saved_at"] = datetime.now().isoformat()
            filepath.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as e:
            self.status_var.set(f"保存失败: {e}")

    def _new_chat(self):
        if self._processing:
            if not self._global_permission:
                messagebox.showinfo("提示", "AI 正在处理中，请先停止")
            return
        self._save_current_chat()
        self.agent.end_session()    # 触发会话自动提炼
        self.agent.reset_conversation()
        for w in self.msg_container.winfo_children():
            w.destroy()
        self._display_log = []
        self._stream_lbl = None
        self._thinking_lbl = None
        self._tool_panel = None
        self._current_chat_file = None
        self._show_welcome()
        self.status_var.set("新对话已就绪")

    def _show_history(self):
        self._save_current_chat()
        if not CHATS_DIR.exists() or not list(CHATS_DIR.glob("*.json")):
            messagebox.showinfo("历史记录", "暂无历史对话记录")
            return

        dlg = tk.Toplevel(self.root)
        dlg.withdraw()  # 先隐藏，定位后再显示，避免左上角闪一下
        dlg.title("对话历史")
        dlg.configure(bg=Theme.BG)
        dlg.transient(self.root)
        dlg.grab_set()

        dlg.update_idletasks()
        w, h = self._s(550), self._s(420)
        x = self.root.winfo_x() + (self.root.winfo_width() - w) // 2
        y = self.root.winfo_y() + (self.root.winfo_height() - h) // 2
        dlg.geometry(f"{w}x{h}+{x}+{y}")
        dlg.deiconify()  # 定位完成后再显示

        header = tk.Frame(dlg, bg=Theme.TITLE_BAR, height=self._s(40))
        header.pack(fill=tk.X)
        header.pack_propagate(False)
        tk.Label(header, text="对话历史记录", font=self.fonts["subtitle"],
                 fg=Theme.TEXT, bg=Theme.TITLE_BAR).pack(side=tk.LEFT, padx=self._s(14), pady=self._s(8))

        list_frame = tk.Frame(dlg, bg=Theme.BG)
        list_frame.pack(fill=tk.BOTH, expand=True, padx=self._s(10), pady=(self._s(8), self._s(4)))

        canvas = tk.Canvas(list_frame, bg=Theme.BG, highlightthickness=0)
        scrollbar = ttk.Scrollbar(list_frame, orient=tk.VERTICAL, command=canvas.yview,
                                  style="Dark.Vertical.TScrollbar")
        item_container = tk.Frame(canvas, bg=Theme.BG)
        canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        canvas_window = canvas.create_window((0, 0), window=item_container, anchor="nw")
        item_container.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfig(canvas_window, width=e.width))

        chat_files = sorted(CHATS_DIR.glob("*.json"), reverse=True)
        for cf in chat_files:
            try:
                info = json.loads(cf.read_text(encoding="utf-8"))
            except Exception:
                continue
            display_log = info.get("display_log", [])
            msgs = [e for e in display_log if e.get("type") == "user"]
            preview = msgs[0]["text"][:50] if msgs else "(空)"
            count = len(display_log)
            saved = info.get("saved_at", "")
            try:
                dt = datetime.fromisoformat(saved)
                ts_display = dt.strftime("%m-%d %H:%M")
            except Exception:
                ts_str = cf.stem[:15]
                try:
                    dt = datetime.strptime(ts_str, "%Y%m%d_%H%M%S")
                    ts_display = dt.strftime("%m-%d %H:%M")
                except Exception:
                    ts_display = ts_str

            row = tk.Frame(item_container, bg=Theme.BG, padx=4, pady=3)
            row.pack(fill=tk.X)

            preview_short = msgs[0]["text"][:20] if msgs else "(空)"
            info_text = f"{ts_display}  {count}条  {preview_short}"
            tk.Label(row, text=info_text, font=self.fonts["small"],
                     fg="#b0b0b8", bg=Theme.BG, anchor="w",
                     width=40).pack(side=tk.LEFT, padx=(0, 4))

            btn_row = tk.Frame(row, bg=Theme.BG)
            btn_row.pack(side=tk.RIGHT)
            bcfg = {"font": self.fonts["tiny"], "relief": tk.FLAT, "bd": 0,
                    "cursor": "hand2", "padx": self._s(10), "pady": self._s(3)}

            tk.Button(btn_row, text="加载", bg=Theme.USER_BUBBLE, fg=Theme.TEXT,
                      command=lambda p=cf: self._load_chat(p, dlg),
                      activebackground="#5a5a80", activeforeground="#fff",
                      **bcfg).pack(side=tk.LEFT, padx=2)
            tk.Button(btn_row, text="删除", bg=Theme.ERROR, fg="#ffffff",
                      command=lambda p=cf: self._delete_chat(p, dlg),
                      activebackground="#cc4444", activeforeground="#ffffff",
                      **bcfg).pack(side=tk.LEFT, padx=2)

        tk.Button(dlg, text="关闭", command=dlg.destroy,
                  bg=Theme.SURFACE, fg=Theme.TEXT_SECONDARY, font=self.fonts["small"],
                  relief=tk.FLAT, cursor="hand2", padx=self._s(20), pady=self._s(8),
                  activebackground=Theme.SCROLLBAR_HOVER, activeforeground=Theme.TEXT,
                  ).pack(pady=(self._s(4), self._s(12)))
        dlg.wait_window()

    def _load_chat(self, filepath: Path, dlg: tk.Toplevel = None):
        if self._processing:
            if not self._global_permission:
                messagebox.showinfo("提示", "AI 正在处理中，请先停止")
            return
        self.agent.end_session()
        try:
            data = json.loads(filepath.read_text(encoding="utf-8"))
            agent_msgs = data.get("agent_messages", [])
            if not agent_msgs:
                return
            for w in self.msg_container.winfo_children():
                w.destroy()
            self._display_log = []
            self._stream_lbl = None
            self._tool_panel = None
            self._thinking_lbl = None

            self.agent.set_messages([self.agent.messages[0]] + agent_msgs)
            self.agent.session_memory.start_session()  # 加载历史后开始新会话记录
            wd = data.get("working_dir", "")
            if wd and wd != str(PROJECT_ROOT):
                self.agent.set_working_dir(wd)

            display_log = data.get("display_log", [])
            for entry in display_log:
                t = entry.get("type")
                if t == "user":
                    self._add_user_message(entry["text"], entry.get("time"))
                elif t == "ai":
                    self._add_ai_message(entry["text"])
                elif t == "thinking":
                    # 思考面板：创建折叠面板+填入内容
                    self._thinking_lbl, _ = self._create_thinking_label()
                    try:
                        self._thinking_lbl.configure(state=tk.NORMAL)
                        self._thinking_lbl.insert(tk.END, entry.get("text", ""))
                        self._thinking_lbl.configure(state=tk.DISABLED)
                    except Exception:
                        pass
                elif t == "tool":
                    self._add_tool_log(entry.get("name", ""), entry.get("summary", ""))
                elif t == "error":
                    self._add_error_message(entry["text"])
            self._display_log = display_log
            self._current_chat_file = filepath
            self.status_var.set(f"已加载: {self._chat_title()}")
            if dlg:
                dlg.destroy()
            # 延迟滚动：等Tkinter完成所有widget布局+canvas重算scrollregion
            self.root.after(150, lambda: self._scroll_after_load())
        except Exception as e:
            messagebox.showerror("加载失败", str(e))

    def _scroll_after_load(self):
        """加载历史后延迟滚动到底（确保layout完成后再滚）"""
        try:
            self.chat_canvas.configure(scrollregion=self.chat_canvas.bbox("all"))
            self.chat_canvas.yview_moveto(1.0)
        except Exception:
            pass

    def _delete_chat(self, filepath: Path, dlg: tk.Toplevel):
        if messagebox.askyesno("确认删除", "确定要删除这条对话记录吗？"):
            try:
                filepath.unlink()
                dlg.destroy()
                self._show_history()
            except Exception as e:
                messagebox.showerror("删除失败", str(e))

    def _update_wechat_btn(self, connected: bool):
        """更新微信按钮样式"""
        if not hasattr(self, '_wechat_btn') or not self._wechat_btn.winfo_exists():
            return
        if connected:
            self._wechat_btn.configure(bg="#2a4a3c", fg="#4a9a5a", text="💬 微信 ✓")
        else:
            self._wechat_btn.configure(bg="#2a4a3c", fg="#4a9a5a", text="💬 微信")

    # ═══════════════════════════════════════════
    # 手机互联功能
    # ═══════════════════════════════════════════

    def _open_mobile_connect(self):
        """打开手机互联二维码弹窗"""
        dlg = tk.Toplevel(self.root)
        dlg.title("手机互联")
        w, h = self._s(440), self._s(580)
        dlg.geometry(f"{w}x{h}")
        dlg.configure(bg=Theme.BG)
        dlg.transient(self.root)
        dlg.grab_set()
        dlg.resizable(False, False)

        dlg.update_idletasks()
        x = self.root.winfo_x() + (self.root.winfo_width() - w) // 2
        y = self.root.winfo_y() + (self.root.winfo_height() - h) // 2
        dlg.geometry(f"+{x}+{y}")

        # 标题
        header = tk.Frame(dlg, bg=Theme.TITLE_BAR, height=self._s(44))
        header.pack(fill=tk.X)
        header.pack_propagate(False)
        tk.Label(header, text="📱 手机互联", font=self.fonts["title_bold"],
                 fg=Theme.TEXT, bg=Theme.TITLE_BAR).pack(side=tk.LEFT, padx=self._s(14), pady=self._s(8))

        # QR 码区域
        qr_frame = tk.Frame(dlg, bg=Theme.BG)
        qr_frame.pack(fill=tk.BOTH, expand=True, padx=self._s(20), pady=self._s(12))

        # QR Canvas
        qr_canvas = tk.Canvas(qr_frame, bg="#ffffff", highlightthickness=2,
                              highlightbackground=Theme.SCROLLBAR_HOVER,
                              width=self._s(300), height=self._s(300))
        qr_canvas.pack(pady=(self._s(10), self._s(10)))

        # 连接信息文本
        info_var = tk.StringVar(value="请使用手机扫描二维码连接")
        tk.Label(qr_frame, textvariable=info_var, font=self.fonts["small"],
                 fg=Theme.TEXT_SECONDARY, bg=Theme.BG, wraplength=self._s(380)).pack(pady=(0, self._s(4)))

        # 密钥信息
        secret_var = tk.StringVar(value="")
        secret_frame = tk.Frame(qr_frame, bg=Theme.SURFACE, padx=self._s(10), pady=self._s(6))
        secret_frame.pack(fill=tk.X, pady=(0, self._s(4)))
        tk.Label(secret_frame, text="连接密钥:", font=self.fonts["tiny"],
                 fg=Theme.TEXT_MUTED, bg=Theme.SURFACE).pack(side=tk.LEFT)
        tk.Label(secret_frame, textvariable=secret_var, font=self.fonts["tiny_bold"],
                 fg=Theme.ACCENT, bg=Theme.SURFACE).pack(side=tk.LEFT, padx=(self._s(6), 0))

        # IP 信息 (手动输入时需要)
        ip_var = tk.StringVar(value="")
        ip_frame = tk.Frame(qr_frame, bg=Theme.SURFACE, padx=self._s(10), pady=self._s(6))
        ip_frame.pack(fill=tk.X, pady=(0, self._s(4)))
        tk.Label(ip_frame, text="电脑IP:", font=self.fonts["tiny"],
                 fg=Theme.TEXT_MUTED, bg=Theme.SURFACE).pack(side=tk.LEFT)
        tk.Label(ip_frame, textvariable=ip_var, font=self.fonts["tiny_bold"],
                 fg=Theme.TEXT, bg=Theme.SURFACE).pack(side=tk.LEFT, padx=(self._s(6), 0))

        # 连接状态
        status_var = tk.StringVar(value="⏳ 等待连接...")
        tk.Label(qr_frame, textvariable=status_var, font=self.fonts["small"],
                 fg="#4a9a5a", bg=Theme.BG).pack(pady=(self._s(4), self._s(8)))

        # 按钮区
        btn_frame = tk.Frame(qr_frame, bg=Theme.BG)
        btn_frame.pack(pady=(0, self._s(8)))

        bcfg = {"font": self.fonts["small"], "relief": tk.FLAT, "bd": 0,
                "cursor": "hand2", "padx": self._s(16), "pady": self._s(8)}

        def do_refresh():
            """手动刷新二维码和密钥"""
            if self._mobile_bridge:
                new_secret = self._mobile_bridge.refresh_secret()
                secret_var.set(new_secret[:16] + "...")
                _render_qr()
                status_var.set("🔄 密钥已刷新，请重新扫码")
                info_var.set("密钥已刷新，旧二维码将失效")

        tk.Button(btn_frame, text="🔄 刷新密钥", command=do_refresh,
                  bg="#2a3a5c", fg=Theme.ACCENT,
                  activebackground="#3a4a7c", activeforeground="#a0b0ff",
                  **bcfg).pack(side=tk.LEFT, padx=self._s(4))

        tk.Button(btn_frame, text="关闭", command=dlg.destroy,
                  bg="#333", fg="#aaa",
                  activebackground="#444", activeforeground="#ccc",
                  **bcfg).pack(side=tk.LEFT, padx=self._s(4))

        # QR 渲染函数
        def _render_qr():
            err_msg = ""
            try:
                from qr_manager import QRManager
                if self._mobile_bridge:
                    qrm = QRManager(self._mobile_bridge.get_connection_info)
                    if not qrm.generate():
                        err_msg = "QR生成失败: qrcode/Pillow库未正确加载"
                        raise RuntimeError(err_msg)

                    # 在 Canvas 中显示 QR
                    from PIL import Image, ImageTk
                    img = qrm.get_image()
                    if img:
                        cw = self._s(300)
                        # LANCZOS 兼容: Pillow>=10 用 LANCZOS, 旧版用 ANTIALIAS
                        try:
                            resample = Image.Resampling.LANCZOS
                        except AttributeError:
                            try:
                                resample = Image.LANCZOS
                            except AttributeError:
                                resample = Image.NEAREST
                        img_resized = img.resize((cw, cw), resample)
                        photo = ImageTk.PhotoImage(img_resized)
                        qr_canvas.delete("all")
                        qr_canvas.create_image(cw // 2, cw // 2, image=photo)
                        qr_canvas.image = photo
                        secret_var.set(qrm.get_connection_info().get("room_secret", "")[:16] + "...")
                        conn_info = qrm.get_connection_info()
                        ip_var.set(f"{conn_info.get('host', '未知')}:{conn_info.get('port', 9527)}")
                        info_var.set(f"手机扫码连接，或手动输入密钥+IP")
                        return
                    else:
                        err_msg = "QR图片为空"
            except Exception as e:
                import traceback
                err_msg = f"{type(e).__name__}: {str(e)[:80]}"
                traceback.print_exc()

            # 回退: 显示错误信息 + 连接信息 (方便手动输入)
            qr_canvas.delete("all")
            qr_canvas.create_text(
                self._s(150), self._s(20),
                text="二维码生成失败",
                fill=Theme.ERROR, font=self.fonts["small_bold"]
            )
            qr_canvas.create_text(
                self._s(150), self._s(60),
                text=err_msg or "请检查 qrcode/Pillow 库是否安装",
                fill=Theme.TEXT_SECONDARY, font=self.fonts["tiny"],
                width=self._s(260)
            )
            # 即使二维码失败，仍显示连接信息供手动输入
            try:
                if self._mobile_bridge:
                    conn = self._mobile_bridge.get_connection_info()
                    secret_var.set(conn.get("room_secret", "")[:16] + "...")
                    ip_var.set(f"{conn.get('host', '未知')}:{conn.get('port', 9527)}")
                    info_var.set(f"请在手机端手动输入上方密钥和IP")
            except Exception:
                pass

        # 首次渲染 + 自动刷新（等隧道就绪后更新QR）
        _tunnel_captured = [False]  # 用list包装以支持闭包修改
        dlg.after(300, _render_qr)

        # 定时更新连接状态 + 检查隧道就绪
        def _update_status():
            if not dlg.winfo_exists():
                return
            if self._mobile_bridge and self._mobile_bridge.connected_count > 0:
                status_var.set(f"✅ 已连接 ({self._mobile_bridge.connected_count} 台设备)")
            else:
                # 检查隧道：是否刚就绪（之前没有，现在有了）
                tunnel_url = ""
                if self._tunnel and self._tunnel.ws_url:
                    tunnel_url = self._tunnel.ws_url
                elif self._upnp and self._upnp.public_url:
                    tunnel_url = self._upnp.public_url
                if tunnel_url and not _tunnel_captured[0]:
                    _tunnel_captured[0] = True
                    status_var.set(f"🌐 远程就绪！正在刷新二维码...")
                    dlg.after(500, _render_qr)  # 重新生成含远程地址的QR
                else:
                    status_var.set("⏳ 等待连接...")
            dlg.after(3000, _update_status)

        dlg.after(1000, _update_status)
        dlg.wait_window()

    # ═══════════════════════════════════════════
    # 微信 ClawBot 登录
    # ═══════════════════════════════════════════
    # PC 端发送文件给 AI（让智能体读取）
    # ═══════════════════════════════════════════

    def _send_file_to_wechat(self):
        """浏览文件 → 发给 AI 读取分析"""
        filepath = filedialog.askopenfilename(title="选择要发送给 AI 的文件")
        if not filepath:
            return

        fp = Path(filepath)
        fsize = fp.stat().st_size

        # 构建 AI 上下文消息
        text_exts = {'.txt', '.py', '.js', '.ts', '.java', '.c', '.cpp', '.h',
                     '.json', '.xml', '.yaml', '.yml', '.md', '.csv', '.log',
                     '.html', '.css', '.sql', '.sh', '.bat', '.ini', '.cfg', '.toml'}
        image_exts = {'.png', '.jpg', '.jpeg', '.gif', '.bmp', '.webp'}
        parts = [f"[用户发送了文件] 文件名: {fp.name}, 大小: {fsize} bytes, 路径: {filepath}"]
        ext = fp.suffix.lower()

        if ext in text_exts and fsize < 100_000:
            try:
                content = fp.read_text(encoding="utf-8", errors="replace")
                parts.append(f"\n--- 文件内容 ---\n{content}\n--- 内容结束 ---")
            except Exception:
                parts.append("(文本读取失败)")
        elif ext in image_exts:
            parts.append(f"(这是一个图片文件，如果你支持视觉能力可以查看此路径)")
            # MIMO 模型可以用 capture_screen 类似的机制注入图片
        else:
            parts.append(f"(二进制/Office文件，如需读取内容请使用对应工具)")

        # 注入 Agent 上下文
        user_msg = "\n".join(parts)
        self.agent._safe_append({"role": "user", "content": user_msg})

        # GUI 显示
        self._add_tool_log("发送文件给AI", f"📎 {fp.name} ({fsize} bytes)")
        self._add_user_message(f"📎 发送文件: {fp.name}")
        self.status_var.set(f"文件已发送给 AI: {fp.name}")

        # 自动触发 AI 响应
        if not self._processing:
            self._stream_lbl, _ = self._create_stream_label()
            self._set_processing(True)
            threading.Thread(
                target=self._run_agent,
                args=(f"请读取并分析我刚发送的文件: {fp.name}，保存在 {filepath}",),
                daemon=True,
            ).start()

    # ═══════════════════════════════════════════

    def _open_wechat_login(self):
        """打开微信扫码登录弹窗"""
        wb = _get_wechat()
        if not wb:
            messagebox.showerror("错误", "微信模块未加载")
            return

        # 已登录 → 显示状态/退出选项
        if wb.is_logged_in:
            if messagebox.askyesno("微信已连接",
                                   f"Bot: {wb.bot_name}\n\n是否退出登录？"):
                wb.logout()
                self._wechat_status_var.set("")
                self._update_wechat_btn(False)
            return

        # 未登录 → 显示二维码
        dlg = tk.Toplevel(self.root)
        dlg.title("微信登录")
        w, h = self._s(420), self._s(520)
        dlg.geometry(f"{w}x{h}")
        dlg.configure(bg=Theme.BG)
        dlg.transient(self.root)
        dlg.grab_set()
        dlg.resizable(False, False)

        dlg.update_idletasks()
        x = self.root.winfo_x() + (self.root.winfo_width() - w) // 2
        y = self.root.winfo_y() + (self.root.winfo_height() - h) // 2
        dlg.geometry(f"+{x}+{y}")

        # 标题
        header = tk.Frame(dlg, bg=Theme.TITLE_BAR, height=self._s(44))
        header.pack(fill=tk.X)
        header.pack_propagate(False)
        tk.Label(header, text="💬 微信扫码登录", font=self.fonts["title_bold"],
                 fg=Theme.TEXT, bg=Theme.TITLE_BAR).pack(side=tk.LEFT, padx=self._s(14), pady=self._s(8))

        # QR 区域
        qr_frame = tk.Frame(dlg, bg=Theme.BG)
        qr_frame.pack(fill=tk.BOTH, expand=True, padx=self._s(20), pady=self._s(12))

        # QR Canvas
        qr_canvas = tk.Canvas(qr_frame, bg="#ffffff", highlightthickness=2,
                              highlightbackground=Theme.SCROLLBAR_HOVER,
                              width=self._s(260), height=self._s(260))
        qr_canvas.pack(pady=(self._s(10), self._s(10)))

        # 状态文字
        info_var = tk.StringVar(value="正在生成二维码...")
        tk.Label(qr_frame, textvariable=info_var, font=self.fonts["small"],
                 fg=Theme.TEXT_SECONDARY, bg=Theme.BG, wraplength=self._s(360)).pack(pady=(0, self._s(4)))

        # 说明文字
        tip_frame = tk.Frame(qr_frame, bg=Theme.SURFACE, padx=self._s(10), pady=self._s(6))
        tip_frame.pack(fill=tk.X, pady=(0, self._s(8)))
        tk.Label(tip_frame, text="📱 微信扫一扫 → 确认登录 → 即可对话",
                 font=self.fonts["tiny"], fg=Theme.TEXT_SECONDARY, bg=Theme.SURFACE).pack()

        # 按钮
        btn_frame = tk.Frame(qr_frame, bg=Theme.BG)
        btn_frame.pack(pady=(0, self._s(8)))

        bcfg = {"font": self.fonts["small"], "relief": tk.FLAT, "bd": 0,
                "cursor": "hand2", "padx": self._s(16), "pady": self._s(8)}

        def do_refresh():
            """刷新二维码"""
            nonlocal login_started
            if login_started:
                return
            login_started = True
            info_var.set("正在重新获取二维码...")
            dlg.update()
            _start_login()

        tk.Button(btn_frame, text="🔄 刷新", command=do_refresh,
                  bg="#2a3a5c", fg=Theme.ACCENT,
                  activebackground="#3a4a7c", activeforeground="#a0b0ff",
                  **bcfg).pack(side=tk.LEFT, padx=self._s(4))

        tk.Button(btn_frame, text="关闭", command=dlg.destroy,
                  bg="#333", fg="#aaa",
                  activebackground="#444", activeforeground="#ccc",
                  **bcfg).pack(side=tk.LEFT, padx=self._s(4))

        login_started = False

        def _render_qr(qrcode_url: str, qrcode_id: str):
            """在 Canvas 中渲染二维码"""
            try:
                import qrcode
                from PIL import Image, ImageTk

                qr = qrcode.QRCode(box_size=4, border=2)
                qr.add_data(qrcode_url)
                qr.make(fit=True)
                img = qr.make_image(fill_color="black", back_color="white")

                cw = self._s(260)
                try:
                    resample = Image.Resampling.LANCZOS
                except AttributeError:
                    try:
                        resample = Image.LANCZOS
                    except AttributeError:
                        resample = Image.NEAREST
                img_resized = img.resize((cw, cw), resample)
                photo = ImageTk.PhotoImage(img_resized)
                qr_canvas.delete("all")
                qr_canvas.create_image(cw // 2, cw // 2, image=photo)
                qr_canvas.image = photo
            except Exception as e:
                qr_canvas.delete("all")
                qr_canvas.create_text(
                    self._s(130), self._s(50),
                    text="二维码生成失败",
                    fill=Theme.ERROR, font=self.fonts["small_bold"]
                )
                qr_canvas.create_text(
                    self._s(130), self._s(90),
                    text=str(e)[:100],
                    fill=Theme.TEXT_SECONDARY, font=self.fonts["tiny"],
                    width=self._s(230)
                )
                # 显示链接作为备选
                qr_canvas.create_text(
                    self._s(130), self._s(140),
                    text=f"请手动访问:\n{qrcode_url}",
                    fill=Theme.ACCENT, font=self.fonts["tiny"],
                    width=self._s(230)
                )

        def _start_login():
            """在后台线程中执行登录流程"""
            def _login_thread():
                try:
                    wb2 = _get_wechat()
                    if not wb2:
                        dlg.after(0, lambda: info_var.set("❌ 微信模块未加载"))
                        return

                    def on_qrcode(url, qid):
                        dlg.after(0, lambda: _render_qr(url, qid))
                        dlg.after(0, lambda: info_var.set("📱 请用微信扫描二维码"))

                    def on_status(text):
                        dlg.after(0, lambda: info_var.set(text))
                        if "成功" in text:
                            dlg.after(1500, dlg.destroy)

                    success = wb2.login_with_callback(on_qrcode, on_status)

                    if success:
                        dlg.after(0, lambda: self._update_wechat_btn(True))
                        dlg.after(0, lambda: self._wechat_status_var.set("💬 微信已连接"))
                        dlg.after(0, lambda: self._wechat_status_lbl.configure(fg="#4a9a5a"))
                except Exception as e:
                    dlg.after(0, lambda: info_var.set(f"❌ 登录失败: {e}"))

            threading.Thread(target=_login_thread, daemon=True).start()

        # 启动登录流程
        dlg.after(300, _start_login)
        dlg.wait_window()

    def display_mobile_message(self, text: str, device_id: str):
        """在PC界面显示来自手机的消息，并准备好流式输出标签"""
        self._add_user_message(f"📱 {text}")
        # 每轮手机消息创建新的流式输出标签
        self._stream_lbl, _ = self._create_stream_label()
        self._set_processing(True)
        self._scroll_bottom()

    def display_mobile_file(self, filename: str, path: str, mime_type: str):
        """在PC界面显示手机发来的文件，并注入AI上下文"""
        is_image = mime_type.startswith("image/")
        prefix = "🖼️" if is_image else "📎"
        self._add_tool_log("手机文件", f"{prefix} {filename} ({mime_type})")
        self._add_user_message(f"{prefix} 已接收: {filename}")
        self._scroll_bottom()
        # 注入AI上下文（不自动触发，等用户发消息）
        if self.agent:
            self.agent._safe_append({
                "role": "user",
                "content": f"[手机端发来文件] 文件名: {filename}, 类型: {mime_type}, 保存路径: {path}"
            })

    def update_connection_status(self, count: int):
        """更新状态栏连接状态"""
        if count > 0:
            self._mobile_status_var.set(f"📱 已连接 ({count})")
            self._mobile_status_lbl.configure(fg="#4a9a5a")
        else:
            self._mobile_status_var.set("")
        # 更新按钮样式
        if hasattr(self, '_mobile_btn') and self._mobile_btn.winfo_exists():
            if count > 0:
                self._mobile_btn.configure(bg="#2a4a3c", fg="#4a9a5a")
            else:
                self._mobile_btn.configure(bg="#2a3a5c", fg=Theme.ACCENT)

    def _show_permission_dialog(self, tool_name: str, description: str,
                                  risk: str = "moderate", allow_always: bool = True):
        """权限弹窗 — 全局权限开启时直接放行，不创建任何窗口"""
        if self._global_permission:
            return True
        result = [None]
        dlg = tk.Toplevel(self.root)
        dlg.title("权限确认")
        dlg.configure(bg=Theme.BG)
        dlg.resizable(False, False)
        dlg.transient(self.root)
        dlg.attributes("-topmost", True)

        # 风险配色
        if risk == "high":
            header_bg, header_fg, risk_badge = "#4a1a1a", "#ff6b6b", "高风险"
        else:
            header_bg, header_fg, risk_badge = "#3a3a1a", "#e0c040", "中等风险"

        dlg.update_idletasks()
        dpi_scale = max(1.0, dlg.winfo_fpixels('1i') / 72.0)
        w, h = int(520 * dpi_scale), int((210 if allow_always else 175) * dpi_scale)
        sw, sh = dlg.winfo_screenwidth(), dlg.winfo_screenheight()
        dlg.geometry(f"{w}x{h}+{(sw-w)//2}+{(sh-h)//2}")
        dlg.minsize(int(380 * dpi_scale), int(150 * dpi_scale))

        bar = tk.Frame(dlg, bg=header_bg, height=int(36 * dpi_scale))
        bar.pack(fill="x")
        bar.pack_propagate(False)
        tk.Label(bar, text=f"{'🔴' if risk=='high' else '🟡'} {risk_badge} — {tool_name}",
                 font=(FONT_FAMILY, int(11 * dpi_scale), "bold"), fg=header_fg, bg=header_bg,
                 padx=int(14 * dpi_scale)
                 ).pack(side="left", pady=int(4 * dpi_scale))

        frm = tk.Frame(dlg, bg=Theme.SURFACE,
                       padx=int(18 * dpi_scale), pady=int(12 * dpi_scale))
        frm.pack(fill="both", expand=True)
        tk.Message(frm, text=description, font=(FONT_FAMILY, int(11 * dpi_scale)),
                   fg=Theme.TEXT, bg=Theme.SURFACE, width=int(470 * dpi_scale)
                   ).pack(anchor="w", pady=(int(4 * dpi_scale), int(4 * dpi_scale)), fill="x")

        if risk == "high":
            tk.Label(frm, text="⚠ 每次都需要确认", font=(FONT_FAMILY, int(9 * dpi_scale)),
                     fg=Theme.ERROR, bg=Theme.SURFACE).pack(anchor="w")
        else:
            tk.Label(frm, text="可勾选「始终允许」", font=(FONT_FAMILY, int(9 * dpi_scale)),
                     fg=Theme.TEXT_MUTED, bg=Theme.SURFACE).pack(anchor="w")

        btn_frm = tk.Frame(frm, bg=Theme.SURFACE)
        btn_frm.pack(fill="x", pady=(int(14 * dpi_scale), 0))

        def _answer(v):
            result[0] = v
            try:
                dlg.grab_release()
                dlg.destroy()
            except Exception:
                pass

        def _close():
            if result[0] is None:
                result[0] = False
            _answer(result[0])

        deny_btn = tk.Label(btn_frm, text=" Esc·拒绝 ", font=(FONT_FAMILY, int(10 * dpi_scale)),
                           fg=Theme.TEXT_MUTED, bg=Theme.CARD,
                           padx=int(14 * dpi_scale), pady=int(8 * dpi_scale),
                           cursor="hand2")
        deny_btn.pack(side="left")
        deny_btn.bind("<Button-1>", lambda e: _answer(False))

        if allow_always:
            always_btn = tk.Label(btn_frm, text=" 始终允许 ", font=(FONT_FAMILY, int(10 * dpi_scale)),
                                  fg=Theme.ACCENT, bg=Theme.CARD,
                                  padx=int(14 * dpi_scale), pady=int(8 * dpi_scale), cursor="hand2")
            always_btn.pack(side="left", padx=(int(10 * dpi_scale), 0))
            always_btn.bind("<Button-1>", lambda e: _answer("always"))

        allow_btn = tk.Label(btn_frm, text=" Enter·允许 ",
                             font=(FONT_FAMILY, int(10 * dpi_scale), "bold"),
                             fg="#ffffff", bg=Theme.ACCENT,
                             padx=int(20 * dpi_scale), pady=int(8 * dpi_scale), cursor="hand2")
        allow_btn.pack(side="right")
        allow_btn.bind("<Button-1>", lambda e: _answer(True))

        dlg.bind("<Return>", lambda e: _answer(True))
        dlg.bind("<Escape>", lambda e: _answer(False))
        dlg.protocol("WM_DELETE_WINDOW", _close)
        dlg.grab_set()
        dlg.focus_force()
        # 30秒超时
        self.root.after(30000, lambda: _answer(False) if result[0] is None else None)

        self.root.wait_window(dlg)
        return result[0] if result[0] is not None else False

    def _try_upnp_then_tunnel(self):
        """UPnP优先 → FRP隧道 → SSH隧道 三级穿透"""
        # 1. UPnP（直连快）
        if self._upnp and self._upnp.try_map():
            return
        # 2. FRP（国内免费服务器，不被墙）
        logger.info("UPnP不可用，尝试FRP隧道...")
        try:
            from nexie.frp_client import get_frp_client
            self._tunnel = get_frp_client(9527)
            self._tunnel.start()
            return
        except Exception as e:
            logger.debug(f"FRP启动失败: {e}")
        # 3. SSH隧道（备用）
        logger.info("FRP不可用，尝试SSH隧道...")
        try:
            from nexie.tunnel import get_tunnel
            self._tunnel = get_tunnel(9527)
            self._tunnel.start()
        except Exception as e:
            logger.debug(f"SSH隧道启动失败: {e}")

    def _update_remote_status(self):
        """更新远程连接状态"""
        url = ""
        if self._upnp and self._upnp.public_url:
            url = self._upnp.public_url
        elif self._tunnel and self._tunnel.public_url:
            url = self._tunnel.ws_url
        if url:
            self.status_var.set(f"🌐 远程: {url[:60]}")

    # ═══ 自治模式 ═══
    def _toggle_autonomous(self):
        self._autonomous_mode = not self._autonomous_mode
        if self._autonomous_mode:
            self._auto_btn.configure(text="🤖 自治·开", bg=Theme.ACCENT, fg="#fff")
            self.status_var.set("自治模式：AI自行规划→执行→验证→完成")
        else:
            self._auto_btn.configure(text="🤖 自治", bg=Theme.SURFACE, fg=Theme.TEXT_SECONDARY)
            self.status_var.set("就绪")

    def _toggle_global_permission(self):
        """全局权限开关：开启后跳过所有权限弹窗+绕过UAC/SmartScreen"""
        self._global_permission = not self._global_permission
        self.agent._global_skip = self._global_permission
        # 联动tools模块绕过系统弹窗
        from tools import set_global_bypass
        set_global_bypass(self._global_permission)
        if self._global_permission:
            self._perm_btn.configure(text="🔓 权限·开", bg=Theme.WARNING, fg="#000")
            self.status_var.set("🔓 已绕过UAC/SmartScreen — 系统弹窗不会卡住工作")
        else:
            self._perm_btn.configure(text="🔓 权限", bg=Theme.SURFACE, fg=Theme.TEXT_SECONDARY)
            self.status_var.set("")
            self.status_var.set("🔒 权限管控已恢复")

    def _scan_project(self):
        target = filedialog.askdirectory(title="选择要扫描的项目目录(建议选项目根目录，不要选桌面)", initialdir=self.agent.working_dir)
        if not target:
            return
        # 大目录警告
        try:
            file_count = sum(1 for _ in os.walk(target))
            if file_count > 5000:
                if not messagebox.askyesno("大目录警告",
                    f"该目录超过5000个文件，扫描可能较慢。\n建议选择具体项目目录。\n\n是否继续？"):
                    return
        except: pass
        self.agent.set_working_dir(target)
        self.status_var.set(f"正在扫描...")
        def _scan():
            try:
                from nexie.project_scanner import get_scanner
                idx = get_scanner().scan(target)
                self.gui_queue.put(("_status", f"扫描完成: {idx.total_files}文件, {idx.total_lines:,}行"))
            except Exception as e:
                self.gui_queue.put(("_status", f"扫描失败: {e}"))
        threading.Thread(target=_scan, daemon=True).start()

    def _run_autonomous(self, goal):
        self.gui_queue.put(("stream", f"\n🤖 自治任务: {goal}\n"))
        self.gui_queue.put(("_status", "规划中..."))
        try:
            result = self.agent.autonomous_execute(
                goal=goal,
                on_progress=lambda p, d: self.gui_queue.put(("_auto_progress", p, str(d)[:100])),
                on_text=lambda t: self.gui_queue.put(("stream", t)),
                on_tool_start=lambda n, a: self.gui_queue.put(("tool_start", n, a)),
                on_tool_result=lambda n, r: self.gui_queue.put(("tool_result", n, r)),
                on_done=lambda f: self.gui_queue.put(("done", f)),
                on_thinking=lambda d: self.gui_queue.put(("thinking", d)),
            )
            self.gui_queue.put(("stream",
                f"\n{'✅' if result['success'] else '❌'} {result['rounds']}轮·{result['duration']:.0f}s\n"))
        except Exception as e:
            self.gui_queue.put(("error", str(e)))
        finally:
            self.gui_queue.put(("processing_done",))

    def _tray_setup(self):
        """创建系统托盘图标（后台运行）"""
        try:
            import pystray
            from PIL import Image
            # 用app_icon.ico创建托盘图标
            icon_path = Path(__file__).parent / "app_icon.ico"
            if icon_path.exists():
                img = Image.open(icon_path)
            else:
                # 备用：16x16 紫色方块
                img = Image.new("RGB", (16, 16), "#7c5cf0")
            menu = pystray.Menu(
                pystray.MenuItem("显示 Nexie", self._restore_from_tray, default=True),
                pystray.MenuItem("退出", self._exit_app),
            )
            self._tray_icon = pystray.Icon("Nexie", img, "Nexie", menu)
            threading.Thread(target=self._tray_icon.run, daemon=True).start()
        except Exception:
            self._tray_icon = None  # pystray不可用时降级

    def _restore_from_tray(self, _=None):
        """从托盘恢复窗口"""
        self.root.after(0, self.root.deiconify)
        self.root.after(0, self.root.lift)

    def _on_close(self):
        """关闭窗口 → 最小化到托盘"""
        if self._tray_icon:
            self.root.withdraw()  # 隐藏到托盘
        else:
            self._exit_app()      # 无托盘时直接退出

    def _exit_app(self, _=None):
        """秒退出：先隐藏销毁窗口，清理放后台"""
        try:
            if self._tray_icon:
                self._tray_icon.stop()
        except: pass
        self.root.withdraw()
        self.root.destroy()  # 立即销毁窗口
        # 清理操作放 daemon 线程，不阻塞退出
        def _cleanup():
            if self._processing:
                self.agent.cancel()
            try: self._task_queue.stop_worker()
            except: pass
            try: self._save_current_chat()
            except: pass
            try: self.agent.end_session()
            except: pass
            try:
                if hasattr(self, '_upnp') and self._upnp:
                    self._upnp.remove_map()
            except: pass
            try:
                if self._mobile_bridge:
                    self._mobile_bridge.stop()
            except: pass
            try:
                if self._wechat_bot:
                    self._wechat_bot.stop()
            except: pass
        threading.Thread(target=_cleanup, daemon=True).start()


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv(PROJECT_ROOT / ".env")
    model_type = os.getenv("AI_MODEL", "deepseek").strip()
    if model_type == "mimo":
        key = os.getenv("MIMO_API_KEY", "") or os.getenv("OPENROUTER_API_KEY", "")
    else:
        key = os.getenv("DEEPSEEK_API_KEY", "")
    key = key.strip()
    if not key:
        print("请设置 API Key")
        sys.exit(1)
    root = tk.Tk()
    MainWindow(root, key, model_type)
    root.mainloop()
