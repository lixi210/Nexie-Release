# Nexie — AI Coding Agent
# Copyright (c) 2026. All rights reserved.
# 本软件仅供个人学习与研究使用，禁止未经授权的商业用途、分发或修改后闭源。
# Licensed for personal, educational, and non-commercial use only.
"""
独立密钥配置窗口 — 输入、校验、保存 API Key，支持 DeepSeek / MIMO 模型选择
"""
import os
import sys
import tkinter as tk
from tkinter import ttk, messagebox
from pathlib import Path
from threading import Thread

if getattr(sys, 'frozen', False):
    RESOURCE_ROOT = Path(sys._MEIPASS)
else:
    RESOURCE_ROOT = Path(__file__).parent
PROJECT_ROOT = RESOURCE_ROOT

from nexie import get_data_dir
DATA_ROOT = get_data_dir()


class ConfigWindow:
    """API 密钥配置窗口"""

    def __init__(self, parent=None):
        self.window = tk.Toplevel(parent) if parent else tk.Tk()
        self.window.title("API 密钥配置")
        self.window.resizable(False, False)
        self.window.configure(bg="#1a1a2e")

        # 暗色 ttk 样式（独立于主界面）
        style = ttk.Style()
        style.theme_use("clam")
        style.configure(".", background="#1a1a2e", foreground="#e0e0e0",
                        fieldbackground="#16213e", troughcolor="#1a1a2e",
                        bordercolor="#1a1a2e", relief="flat")
        style.map("TCombobox", fieldbackground=[("readonly", "#16213e")],
                  foreground=[("readonly", "#e0e0e0")])

        self._dpi = max(1.0, self.window.winfo_fpixels('1i') / 72.0)
        w, h = int(560 * self._dpi), int(580 * self._dpi)
        self.window.geometry(f"{w}x{h}")

        self.api_key = None
        self.model_type = tk.StringVar(value="deepseek")
        self._build_ui()
        self._center_window()

        self.window.bind("<Return>", lambda e: self._save_and_start())
        self.window.bind("<Escape>", lambda e: self.window.destroy())

        if parent is None:
            self.window.protocol("WM_DELETE_WINDOW", self.window.destroy)

    def _center_window(self):
        self.window.update_idletasks()
        w = self.window.winfo_width()
        h = self.window.winfo_height()
        x = (self.window.winfo_screenwidth() - w) // 2
        y = (self.window.winfo_screenheight() - h) // 2
        self.window.geometry(f"{w}x{h}+{x}+{y}")

    def _fs(self, size: int, bold: bool = False) -> tuple:
        s = max(1, int(size * self._dpi))
        return ("Microsoft YaHei", s, "bold") if bold else ("Microsoft YaHei", s)

    def _fsm(self, size: int, bold: bool = False) -> tuple:
        s = max(1, int(size * self._dpi))
        return ("Consolas", s, "bold") if bold else ("Consolas", s)

    def _build_ui(self):
        bg = "#1a1a2e"
        fg = "#e0e0e0"
        accent = "#4fc3f7"
        entry_bg = "#16213e"
        btn_bg = "#0f3460"

        header = tk.Frame(self.window, bg=bg)
        header.pack(pady=(30, 10))

        tk.Label(
            header,
            text="🤖 AI 代码智能体",
            font=self._fs(20, bold=True),
            fg=accent,
            bg=bg,
        ).pack()

        tk.Label(
            header,
            text="请配置 AI 模型和 API 密钥以开始使用",
            font=self._fs(11),
            fg="#888",
            bg=bg,
        ).pack(pady=(5, 0))

        form = tk.Frame(self.window, bg=bg)
        form.pack(pady=20, padx=50, fill=tk.X)

        tk.Label(
            form,
            text="选择模型",
            font=self._fs(10, bold=True),
            fg=fg,
            bg=bg,
            anchor=tk.W,
        ).pack(fill=tk.X)

        model_frame = tk.Frame(form, bg=bg)
        model_frame.pack(fill=tk.X, pady=(5, 10))

        self.model_combo = ttk.Combobox(
            model_frame,
            textvariable=self.model_type,
            values=["deepseek", "mimo"],
            state="readonly",
            font=self._fs(11),
            width=28,
        )
        self.model_combo.pack(fill=tk.X, ipady=3)
        self.model_combo.bind("<<ComboboxSelected>>", self._on_model_change)

        self.model_desc_var = tk.StringVar(value="DeepSeek-V4-Pro · 深度思考 · 代码能力强")
        tk.Label(
            form,
            textvariable=self.model_desc_var,
            font=self._fs(8),
            fg="#6a6a7a",
            bg=bg,
        ).pack(anchor=tk.W, pady=(0, 10))

        self.key_label_var = tk.StringVar(value="DeepSeek API Key")
        tk.Label(
            form,
            textvariable=self.key_label_var,
            font=self._fs(10, bold=True),
            fg=fg,
            bg=bg,
            anchor=tk.W,
        ).pack(fill=tk.X)

        self.key_var = tk.StringVar()
        key_entry = tk.Entry(
            form,
            textvariable=self.key_var,
            font=self._fsm(12),
            bg=entry_bg,
            fg=fg,
            insertbackground=fg,
            relief=tk.FLAT,
            show="•",
        )
        key_entry.pack(fill=tk.X, ipady=8, pady=(5, 5))
        key_entry.focus()

        self.show_var = tk.BooleanVar(value=False)

        def toggle_show():
            key_entry.config(show="" if self.show_var.get() else "•")

        cb = tk.Checkbutton(
            form,
            text="显示密钥",
            variable=self.show_var,
            command=toggle_show,
            bg=bg,
            fg="#888",
            selectcolor=bg,
            activebackground=bg,
            activeforeground="#888",
        )
        cb.pack(anchor=tk.W)

        self.hint_var = tk.StringVar(value="密钥仅保存在本地 .env 文件中，不会上传或泄露\n每位用户使用自己的密钥，独立计费")
        tk.Label(
            form,
            textvariable=self.hint_var,
            font=self._fs(9),
            fg="#666",
            bg=bg,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(10, 0))

        self.link_frame = tk.Frame(form, bg=bg)
        self.link_frame.pack(anchor=tk.W, pady=(5, 0))
        self.link_label = tk.Label(
            self.link_frame,
            text="获取密钥：",
            font=self._fs(9),
            fg="#666",
            bg=bg,
        )
        self.link_label.pack(side=tk.LEFT)
        self.link_url = tk.Label(
            self.link_frame,
            text="https://platform.deepseek.com/api_keys",
            font=self._fs(9, bold=False),
            fg=accent,
            bg=bg,
            cursor="hand2",
        )
        self.link_url.pack(side=tk.LEFT)

        btn_frame = tk.Frame(self.window, bg=bg)
        btn_frame.pack(pady=(5, 5))

        tk.Label(
            btn_frame,
            text="点击下方按钮或按 Enter 键保存并启动",
            font=self._fs(9),
            fg="#888",
            bg=bg,
        ).pack(pady=(0, 12))

        btn_row = tk.Frame(btn_frame, bg=bg)
        btn_row.pack()

        btn_style = {
            "font": self._fs(11),
            "relief": tk.FLAT,
            "cursor": "hand2",
            "width": 14,
        }

        test_btn = tk.Button(
            btn_row,
            text="测试连接",
            command=self._test_connection,
            bg="#1a3a5c",
            fg=fg,
            **btn_style,
        )
        test_btn.pack(side=tk.LEFT, padx=8, ipady=5)

        save_btn = tk.Button(
            btn_row,
            text="★ 保存并启动",
            command=self._save_and_start,
            bg="#0d7377",
            fg="#ffffff",
            activebackground="#0f9a9f",
            activeforeground="#ffffff",
            **btn_style,
        )
        save_btn.pack(side=tk.LEFT, padx=8, ipady=5)

        skip_btn = tk.Button(
            btn_row,
            text="退出",
            command=self.window.destroy,
            bg="#333",
            fg="#aaa",
            **btn_style,
        )
        skip_btn.pack(side=tk.LEFT, padx=8, ipady=5)

        self.status_var = tk.StringVar(value="")
        status_lbl = tk.Label(
            self.window,
            textvariable=self.status_var,
            font=self._fs(9),
            fg="#f44336",
            bg=bg,
        )
        status_lbl.pack(pady=(12, 0))

    def _on_model_change(self, event=None):
        mt = self.model_type.get()
        if mt == "mimo":
            self.model_desc_var.set("Xiaomi MiMo-V2-Omni · 全模态视觉 · 屏幕理解")
            self.key_label_var.set("MiMo API Key")
            self.hint_var.set("使用小米 MiMo 官方 API\n在 platform.xiaomimimo.com 注册获取密钥")
            self.link_url.configure(text="https://platform.xiaomimimo.com")
            self.key_var.set("")
            existing = os.getenv("MIMO_API_KEY", "")
            if existing:
                self.key_var.set(existing)
        else:
            self.model_desc_var.set("DeepSeek-V4-Pro · 深度思考 · 代码能力强")
            self.key_label_var.set("DeepSeek API Key")
            self.hint_var.set("密钥仅保存在本地 .env 文件中，不会上传或泄露\n每位用户使用自己的密钥，独立计费")
            self.link_url.configure(text="https://platform.deepseek.com/api_keys")
            self.key_var.set("")
            existing = os.getenv("DEEPSEEK_API_KEY", "")
            if existing:
                self.key_var.set(existing)

    def _save_key_to_env(self):
        env_path = DATA_ROOT / ".env"
        key = self.key_var.get().strip()

        if not key:
            self.status_var.set("请输入 API Key")
            return False

        try:
            if env_path.exists():
                lines = env_path.read_text(encoding="utf-8").split("\n")
            else:
                lines = []

            mt = self.model_type.get()
            if mt == "mimo":
                key_name = "MIMO_API_KEY"
            else:
                key_name = "DEEPSEEK_API_KEY"

            found = False
            for i, line in enumerate(lines):
                if line.startswith(f"{key_name}="):
                    lines[i] = f"{key_name}={key}"
                    found = True
                    break

            if not found:
                lines.append(f"{key_name}={key}")

            model_found = False
            for i, line in enumerate(lines):
                if line.startswith("AI_MODEL="):
                    lines[i] = f"AI_MODEL={mt}"
                    model_found = True
                    break
            if not model_found:
                lines.append(f"AI_MODEL={mt}")

            env_path.write_text("\n".join(lines), encoding="utf-8")
            os.environ[key_name] = key
            os.environ["AI_MODEL"] = mt
            return True
        except Exception as e:
            self.status_var.set(f"保存失败: {str(e)}")
            return False

    def _test_connection(self):
        key = self.key_var.get().strip()
        if not key:
            self.status_var.set("请先输入 API Key")
            return

        self.status_var.set("正在测试连接...")
        self.window.update()

        def do_test():
            mt = self.model_type.get()
            if mt == "mimo":
                from client import MiMoClient
                valid, msg = MiMoClient.validate_key(key)
            else:
                from client import DeepSeekClient
                valid, msg = DeepSeekClient.validate_key(key)
            self.window.after(0, lambda: self._test_result(valid, msg))

        Thread(target=do_test, daemon=True).start()

    def _test_result(self, valid: bool, msg: str):
        if valid:
            self.status_var.set("✅ 连接成功！密钥有效")
        else:
            self.status_var.set(f"❌ {msg}")

    def _save_and_start(self):
        key = self.key_var.get().strip()
        if not key:
            self.status_var.set("请输入 API Key")
            return
        self.status_var.set("正在验证密钥...")
        self.window.update()
        mt = self.model_type.get()
        if mt == "mimo":
            from client import MiMoClient
            valid, msg = MiMoClient.validate_key(key)
        else:
            from client import DeepSeekClient
            valid, msg = DeepSeekClient.validate_key(key)
        if not valid:
            self.status_var.set(f"❌ 密钥无效: {msg}")
            return
        if self._save_key_to_env():
            self.api_key = key
            self.window.destroy()


if __name__ == "__main__":
    root = tk.Tk()
    root.withdraw()
    cfg = ConfigWindow()
    cfg.window.mainloop()
