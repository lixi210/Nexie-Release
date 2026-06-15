# Nexie — AI Coding Agent
# Copyright (c) 2026. All rights reserved.
# 本软件仅供个人学习与研究使用，禁止未经授权的商业用途、分发或修改后闭源。
# Licensed for personal, educational, and non-commercial use only.
"""
Nexie — AI 编程智能体 程序入口
"""
import sys
import os
from pathlib import Path

# 使用项目自带 tcl/tk DLL+脚本（修复Win11 Tk 8.6暗色窗口bug）
if not getattr(sys, 'frozen', False):
    _proj = Path(__file__).parent
    os.environ["TCL_LIBRARY"] = str(_proj / "lib" / "tcl8.6")
    os.environ["TK_LIBRARY"] = str(_proj / "lib" / "tk8.6")
    os.add_dll_directory(str(_proj))

import ctypes

if sys.platform == "win32":
    import ctypes.wintypes
    _mutex_name = "Global\\Nexie_SingleInstance_Mutex"
    _h_mutex = ctypes.windll.kernel32.CreateMutexW(None, False, _mutex_name)
    if ctypes.windll.kernel32.GetLastError() == 183:
        ctypes.windll.user32.MessageBoxW(0, "Nexie 已经在运行中", "Nexie", 0x40)
        sys.exit(0)

    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)  # Win8.1+ 推荐方式
    except Exception:
        ctypes.windll.user32.SetProcessDPIAware()  # 旧版兼容

    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Nexie.AI.Engineer")
    except Exception:
        pass

import logging
import threading

if __name__ == "__main__":
    from dotenv import load_dotenv
    # exe: sys.executable目录 | 源码: __file__目录 | 持久: %APPDATA%/Nexie
    if getattr(sys, 'frozen', False):
        load_dotenv(Path(sys.executable).parent / ".env")
    load_dotenv(Path(__file__).parent / ".env")
    from nexie import get_data_dir
    load_dotenv(get_data_dir() / ".env")

    model_type = os.getenv("AI_MODEL", "deepseek").strip()
    if model_type == "mimo":
        key = os.getenv("MIMO_API_KEY", "") or os.getenv("OPENROUTER_API_KEY", "")
    else:
        key = os.getenv("DEEPSEEK_API_KEY", "")
    key = key.strip()
    if not key:
        print("请设置 API Key")
        sys.exit(1)

    import tkinter as tk
    root = tk.Tk()
    root.withdraw()  # 初始化完成前隐藏，防闪窗
    root.config(bg="#161622", highlightthickness=0, highlightbackground="#161622")
    root.title("Nexie")

    from app_gui import MainWindow
    MainWindow(root, key, model_type)

    # Windows 11 暗色标题栏
    if sys.platform == "win32":
        root.update_idletasks()
        DWMWA_USE_IMMERSIVE_DARK_MODE = 20
        hwnd = ctypes.windll.user32.GetParent(root.winfo_id())
        try:
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd, DWMWA_USE_IMMERSIVE_DARK_MODE,
                ctypes.byref(ctypes.c_int(1)), ctypes.sizeof(ctypes.c_int))
        except Exception:
            pass

    root.deiconify()
    root.mainloop()
