# Nexie — AI Coding Agent
# Copyright (c) 2026. All rights reserved.
# 本软件仅供个人学习与研究使用，禁止未经授权的商业用途、分发或修改后闭源。
# Licensed for personal, educational, and non-commercial use only.
"""
Nexie — 工作目录与默认存储路径管理
统一管理项目工作目录、文件保存路径、历史会话路径
"""
import os
import json
import threading
from pathlib import Path
from datetime import datetime
from typing import Optional

# ═══════════════════════════════════════════
# 数据目录（统一入口）
# ═══════════════════════════════════════════
from nexie import get_data_dir
DATA_ROOT = get_data_dir()

# ═══════════════════════════════════════════
# 工作空间配置
# ═══════════════════════════════════════════

class Workspace:
    """
    工作空间管理器：统一管理默认工作目录、文件保存路径等。
    配置持久化到 Nexie_data/workspace.json。
    """

    CONFIG_FILE = "workspace.json"

    def __init__(self, data_root: Path = None):
        self._data_root = data_root or DATA_ROOT
        self._config_path = self._data_root / self.CONFIG_FILE
        self._lock = threading.Lock()
        self._config = self._load()

    def _load(self) -> dict:
        if self._config_path.exists():
            try:
                return json.loads(self._config_path.read_text("utf-8"))
            except Exception:
                pass
        return self._defaults()

    def _defaults(self) -> dict:
        desktop = Path.home() / "Desktop"
        return {
            "working_dir": str(desktop),           # 默认工作目录
            "output_dir": str(desktop / "Nexie_Output"),  # 默认输出目录
            "download_dir": str(Path.home() / "Downloads"),  # 下载目录
            "project_dirs": [str(desktop)],        # 项目目录列表
            "auto_create_output": True,            # 自动创建输出目录
            "save_history_to_file": True,          # 历史会话保存到文件
            "history_dir": str(self._data_root / "history"),  # 历史会话存储目录
            "created": datetime.now().isoformat(),
            "updated": datetime.now().isoformat(),
        }

    def _save(self):
        self._config["updated"] = datetime.now().isoformat()
        self._config_path.write_text(json.dumps(self._config, ensure_ascii=False, indent=2), "utf-8")

    # ═══ 工作目录 ═══

    @property
    def working_dir(self) -> str:
        return self._config.get("working_dir", str(Path.home() / "Desktop"))

    def set_working_dir(self, path: str):
        """设置默认工作目录"""
        expanded = os.path.expanduser(path)
        abs_path = os.path.abspath(expanded)
        if not os.path.exists(abs_path):
            os.makedirs(abs_path, exist_ok=True)
        with self._lock:
            self._config["working_dir"] = abs_path
            self._save()

    # ═══ 输出目录 ═══

    @property
    def output_dir(self) -> str:
        d = self._config.get("output_dir", str(Path.home() / "Desktop" / "Nexie_Output"))
        if self._config.get("auto_create_output", True):
            os.makedirs(d, exist_ok=True)
        return d

    def set_output_dir(self, path: str):
        """设置默认输出目录"""
        expanded = os.path.expanduser(path)
        abs_path = os.path.abspath(expanded)
        os.makedirs(abs_path, exist_ok=True)
        with self._lock:
            self._config["output_dir"] = abs_path
            self._save()

    # ═══ 项目目录 ═══

    @property
    def project_dirs(self) -> list[str]:
        return self._config.get("project_dirs", [str(Path.home() / "Desktop")])

    def add_project_dir(self, path: str):
        """添加项目目录"""
        abs_path = os.path.abspath(os.path.expanduser(path))
        with self._lock:
            if abs_path not in self._config["project_dirs"]:
                self._config["project_dirs"].append(abs_path)
                self._save()

    def remove_project_dir(self, path: str):
        """移除项目目录"""
        abs_path = os.path.abspath(os.path.expanduser(path))
        with self._lock:
            if abs_path in self._config["project_dirs"]:
                self._config["project_dirs"].remove(abs_path)
                self._save()

    # ═══ 下载目录 ═══

    @property
    def download_dir(self) -> str:
        return self._config.get("download_dir", str(Path.home() / "Downloads"))

    def set_download_dir(self, path: str):
        abs_path = os.path.abspath(os.path.expanduser(path))
        os.makedirs(abs_path, exist_ok=True)
        with self._lock:
            self._config["download_dir"] = abs_path
            self._save()

    # ═══ 历史会话存储 ═══

    @property
    def history_dir(self) -> str:
        d = self._config.get("history_dir", str(self._data_root / "history"))
        os.makedirs(d, exist_ok=True)
        return d

    def save_conversation(self, session_id: str, messages: list[dict]):
        """保存完整会话到文件（含思考内容、工具调用、回复）"""
        if not self._config.get("save_history_to_file", True):
            return

        history_dir = Path(self.history_dir)
        history_dir.mkdir(parents=True, exist_ok=True)
        filepath = history_dir / f"session_{session_id}.json"

        # 完整保存所有消息（不截断）
        data = {
            "session_id": session_id,
            "saved_at": datetime.now().isoformat(),
            "message_count": len(messages),
            "messages": messages,
        }

        with self._lock:
            filepath.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")

    def load_conversation(self, session_id: str) -> Optional[list[dict]]:
        """从文件加载完整会话"""
        filepath = Path(self.history_dir) / f"session_{session_id}.json"
        if not filepath.exists():
            return None
        try:
            data = json.loads(filepath.read_text("utf-8"))
            return data.get("messages", [])
        except Exception:
            return None

    def list_sessions(self) -> list[dict]:
        """列出所有历史会话"""
        history_dir = Path(self.history_dir)
        if not history_dir.exists():
            return []

        sessions = []
        for f in sorted(history_dir.glob("session_*.json"), reverse=True):
            try:
                data = json.loads(f.read_text("utf-8"))
                sessions.append({
                    "session_id": data.get("session_id", f.stem),
                    "saved_at": data.get("saved_at", ""),
                    "message_count": data.get("message_count", 0),
                })
            except Exception:
                pass
        return sessions

    # ═══ 配置导出 ═══

    def get_config(self) -> dict:
        """获取完整配置"""
        return dict(self._config)

    def get_config_report(self) -> str:
        """获取可读配置报告"""
        c = self._config
        lines = [
            "📂 Nexie 工作空间配置",
            "=" * 50,
            f"  工作目录:   {c.get('working_dir', '?')}",
            f"  输出目录:   {c.get('output_dir', '?')}",
            f"  下载目录:   {c.get('download_dir', '?')}",
            f"  项目目录:   {len(c.get('project_dirs', []))} 个",
            f"  历史会话:   {c.get('history_dir', '?')}",
            f"  自动保存:   {c.get('save_history_to_file', True)}",
        ]
        for i, d in enumerate(c.get("project_dirs", [])[:5]):
            lines.append(f"    项目{i+1}: {d}")
        return "\n".join(lines)


# ═══════════════════════════════════════════
# 全局单例
# ═══════════════════════════════════════════

_workspace: Optional[Workspace] = None


def get_workspace(data_root: Path = None) -> Workspace:
    global _workspace
    if _workspace is None:
        _workspace = Workspace(data_root)
    return _workspace


def reset_workspace():
    global _workspace
    _workspace = None
