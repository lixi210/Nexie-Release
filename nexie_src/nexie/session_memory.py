# Nexie — AI Coding Agent
# Copyright (c) 2026. All rights reserved.
# 本软件仅供个人学习与研究使用，禁止未经授权的商业用途、分发或修改后闭源。
# Licensed for personal, educational, and non-commercial use only.
"""
Nexie — 会话记忆引擎
自动记录→提炼→累积，让 Nexie 越用越了解用户。
规则提取（零API成本）：从对话消息中自动提取关键信息，无需AI主动调用 remember。

生命周期：
  start_session() → record_tool_call()×N → end_session(messages)
  → 自动提炼会话摘要 + 更新用户画像
  → get_startup_context() 注入下次 system prompt
"""
import os
import re
import json
import time
import threading
import logging
from pathlib import Path
from datetime import datetime
from typing import Optional
from collections import Counter

from nexie import get_data_dir

logger = logging.getLogger("Nexie.SessionMemory")

DATA_ROOT = get_data_dir()

# ═══════════════════════════════════════════
# 规则提取 — 正则模式
# ═══════════════════════════════════════════

# 用户偏好表达模式
PREFERENCE_PATTERNS = [
    r'我(?:喜欢|习惯|偏好|倾向于|一般[都]?|通常)(?:用|使用|选|采用)([^。，,]{2,30})',
    r'(?:不要|别|禁止|千万别|绝对不要)(?:用|使用|选|采用)?([^。，,]{2,30})',
    r'(?:必须|一定要|务必|只能)(?:用|使用|选|采用)([^。，,]{2,30})',
    r'我(?:是|从事|做)([^。，,]{2,20})的',
    r'(?:默认|一直|始终)(?:用|使用)([^。，,]{2,30})',
]

# 关键决策表达模式
DECISION_PATTERNS = [
    r'(?:决定|选择|采用|最终用|确定用|选[了]?)([^。，,]{3,40})(?:方案|方法|方式|框架|库|工具)',
    r'(?:方案|方法|框架|库|工具|技术)(?:选择|决定|确定)([^。，,]{3,40})',
    r'(?:改用|换成|替换[为成])([^。，,]{3,40})',
    r'(?:最终|最[后终])(?:选[择定了]|确定[了]?)([^。，,]{3,40})',
]

# 文件扩展名 → 语言映射
EXT_TO_LANG = {
    '.py': 'python', '.js': 'javascript', '.ts': 'typescript', '.tsx': 'react',
    '.vue': 'vue', '.html': 'html', '.css': 'css', '.scss': 'scss',
    '.java': 'java', '.go': 'go', '.rs': 'rust', '.cpp': 'c++', '.c': 'c',
    '.cs': 'c#', '.rb': 'ruby', '.php': 'php', '.swift': 'swift',
    '.kt': 'kotlin', '.json': 'json', '.yaml': 'yaml', '.yml': 'yaml',
    '.toml': 'toml', '.md': 'markdown', '.sql': 'sql', '.sh': 'shell',
    '.ps1': 'powershell', '.bat': 'batch', '.dockerfile': 'docker',
    '.pptx': 'ppt', '.docx': 'word', '.xlsx': 'excel',
}

# 任务类型关键词映射
TASK_KEYWORDS = {
    'coding': ['写', '代码', '编程', '开发', '实现', '函数', '类', '模块', 'api',
               'create', 'implement', 'build', 'code', 'function', 'class'],
    'debugging': ['修复', 'bug', '报错', '错误', '调试', 'fix', 'debug', 'error',
                  'traceback', '异常', '崩溃', 'crash'],
    'docs': ['文档', '说明书', 'readme', 'doc', 'document', '写个说明', '记录'],
    'ppt': ['ppt', '演示', '幻灯片', 'presentation', '讲', '汇报'],
    'config': ['配置', '设置', '安装', '环境', 'config', 'setup', 'install',
               '部署', 'deploy', 'docker'],
    'research': ['搜索', '查找', '调研', '研究', 'search', 'research', '了解',
                 '什么是', '如何', '怎么'],
    'refactor': ['重构', '重写', '优化', '整理', 'refactor', 'rewrite', 'clean'],
    'data': ['数据', '分析', '处理', '爬', '抓取', 'database', 'sql', 'csv',
             'json', 'excel'],
}


class SessionMemory:
    """
    会话记忆引擎 — 自动记录、提炼、累积。

    规则驱动（不调AI），在会话结束时自动：
    1. 提炼会话摘要（目标、操作、决策、错误修复）
    2. 更新用户画像（语言偏好、目录习惯、任务类型）
    3. 存储到磁盘供下次启动注入
    """

    MAX_SESSIONS = 100            # 最多保留会话摘要数
    MAX_FILES_TRACKED = 30        # 单次会话最多追踪文件数
    MAX_COMMANDS_TRACKED = 15     # 单次会话最多追踪命令数
    SUMMARY_MAX_LEN = 300         # 会话摘要最大长度

    def __init__(self, data_root: Path = None):
        self._data_root = data_root or DATA_ROOT
        self._mem_dir = self._data_root / "session_memory"
        self._sessions_dir = self._mem_dir / "sessions"
        self._mem_dir.mkdir(parents=True, exist_ok=True)
        self._sessions_dir.mkdir(parents=True, exist_ok=True)

        self._profile_path = self._mem_dir / "user_profile.json"
        self._index_path = self._mem_dir / "session_index.json"

        self._lock = threading.Lock()

        # 当前会话状态
        self._session_id: str = ""
        self._session_start: str = ""
        self._user_messages: list[str] = []
        self._files_touched: list[str] = []
        self._commands_run: list[str] = []
        self._tool_calls: list[dict] = []
        self._ai_responses: list[str] = []
        self._error_encounters: list[str] = []
        self._active = False

        # 加载持久化数据
        self._profile = self._load_profile()
        self._index = self._load_index()

    # ══════════════════════════════════════
    # 生命周期
    # ══════════════════════════════════════

    def start_session(self) -> str:
        """开始新会话，返回 session_id"""
        with self._lock:
            self._session_id = datetime.now().strftime("%Y%m%d_%H%M%S")
            self._session_start = datetime.now().isoformat()
            self._user_messages = []
            self._files_touched = []
            self._commands_run = []
            self._tool_calls = []
            self._ai_responses = []
            self._error_encounters = []
            self._active = True
        logger.debug("会话开始: %s", self._session_id)
        return self._session_id

    def record_user_message(self, text: str):
        """记录用户消息"""
        if not self._active:
            return
        text = text.strip()
        if text and len(text) > 2:
            self._user_messages.append(text)
            # 只保留最近50条用户消息（防止内存膨胀）
            if len(self._user_messages) > 50:
                self._user_messages = self._user_messages[-50:]

    def record_tool_call(self, func_name: str, args: dict, result_summary: str = ""):
        """记录一次工具调用"""
        if not self._active:
            return

        entry = {
            "tool": func_name,
            "args_preview": str(args)[:200],
            "result_preview": result_summary[:200] if result_summary else "",
            "time": time.time(),
        }
        self._tool_calls.append(entry)

        # 提取文件操作
        path = args.get("path", "") or args.get("file_path", "") or args.get("source", "")
        if path and func_name in ("write_file", "edit_file", "create_directory",
                                   "move_file", "delete_file", "read_file"):
            if path not in self._files_touched:
                self._files_touched.append(path)
                if len(self._files_touched) > self.MAX_FILES_TRACKED:
                    self._files_touched = self._files_touched[-self.MAX_FILES_TRACKED:]

        # 提取命令
        if func_name == "run_command":
            cmd = args.get("command", "")
            if cmd and cmd not in self._commands_run:
                self._commands_run.append(cmd)
                if len(self._commands_run) > self.MAX_COMMANDS_TRACKED:
                    self._commands_run = self._commands_run[-self.MAX_COMMANDS_TRACKED:]

        # 检测错误
        if result_summary:
            is_error = any(kw in result_summary.lower() for kw in
                          ['error', 'traceback', 'exception', '失败', '错误', '异常',
                           'cannot', 'unable', 'permission denied', 'not found',
                           '拒绝访问', '找不到', '不存在'])
            if is_error:
                self._error_encounters.append({
                    "tool": func_name,
                    "error": result_summary[:300],
                    "time": time.time(),
                })

    def record_ai_response(self, text: str):
        """记录AI文本回复"""
        if not self._active or not text:
            return
        text = text.strip()
        if len(text) > 10:
            self._ai_responses.append(text)
            if len(self._ai_responses) > 30:
                self._ai_responses = self._ai_responses[-30:]

    def end_session(self, messages: list[dict] = None) -> Optional[dict]:
        """
        结束当前会话，自动提炼摘要并更新用户画像。
        返回会话摘要 dict，或 None（如果无有效内容）。
        """
        if not self._active:
            return None

        with self._lock:
            self._active = False

            # 无实质内容则跳过
            if not self._user_messages and not self._files_touched and not self._commands_run:
                logger.debug("会话 %s 无实质内容，跳过保存", self._session_id)
                return None

            # 从消息中提取信息（如果有 messages）
            if messages:
                self._extract_from_messages(messages)

            # 生成摘要
            summary = self._generate_summary()

            # 保存会话摘要
            self._save_session(summary)

            # 更新用户画像
            self._update_profile(summary)

            # 清理轮次数据（保留画像和索引）
            self._user_messages = []
            self._files_touched = []
            self._commands_run = []
            self._tool_calls = []
            self._ai_responses = []
            self._error_encounters = []

            logger.info("会话结束: %s | 文件:%d 命令:%d 工具:%d",
                        self._session_id,
                        len(summary.get("files_touched", [])),
                        len(summary.get("commands_run", [])),
                        len(self._tool_calls))

            return summary

    # ══════════════════════════════════════
    # 消息提取（规则驱动）
    # ══════════════════════════════════════

    def _extract_from_messages(self, messages: list[dict]):
        """从完整消息列表中提取额外信息（补充 tool_call 记录）"""
        for m in messages:
            role = m.get("role", "")
            content = m.get("content", "") or ""

            if role == "user" and content.strip():
                if content.strip() not in self._user_messages:
                    self._user_messages.append(content.strip())

            elif role == "assistant":
                # 提取文本回复
                if content and not m.get("tool_calls"):
                    if content.strip() not in self._ai_responses:
                        self._ai_responses.append(content.strip())

            elif role == "tool":
                # 检测工具返回中的错误
                is_error = any(kw in str(content).lower() for kw in
                              ['error', 'traceback', 'exception', '失败', '错误',
                               'cannot', 'unable', 'permission denied'])
                if is_error and len(str(content)) > 20:
                    self._error_encounters.append({
                        "tool": m.get("name", "unknown"),
                        "error": str(content)[:300],
                        "time": time.time(),
                    })

    # ══════════════════════════════════════
    # 摘要生成（规则驱动）
    # ══════════════════════════════════════

    def _generate_summary(self) -> dict:
        """从收集的数据中提炼会话摘要"""
        # 目标：首条用户消息
        goal = ""
        for msg in self._user_messages:
            clean = msg.replace("#省流开", "").replace("#省流关", "").strip()
            if len(clean) > 3:
                goal = clean[:120]
                break

        # 文件去重、取相对路径
        files = list(dict.fromkeys(self._files_touched))  # 保序去重
        files_clean = []
        for f in files:
            try:
                # 尝试转为相对桌面路径
                desktop = str(Path.home() / "Desktop")
                if f.startswith(desktop):
                    f = "~/Desktop" + f[len(desktop):]
                elif f.startswith(str(Path.home())):
                    f = "~" + f[len(str(Path.home())):]
            except Exception:
                pass
            files_clean.append(f)

        # 命令去重
        commands = list(dict.fromkeys(self._commands_run))

        # 关键决策：从 AI 回复 + 用户消息中匹配
        decisions = self._extract_decisions()

        # 错误修复
        errors_fixes = []
        for err in self._error_encounters[-5:]:  # 最近5个
            err_text = err.get("error", "")[:150]
            errors_fixes.append(err_text)

        # 标签
        tags = self._extract_tags(files_clean, commands)

        # 生成文本摘要
        summary_text = self._build_summary_text(goal, files_clean, commands, decisions, errors_fixes)

        return {
            "session_id": self._session_id,
            "timestamp": self._session_start,
            "goal": goal,
            "summary": summary_text,
            "files_touched": files_clean[-20:],
            "commands_run": commands[-10:],
            "key_decisions": decisions[:5],
            "errors_and_fixes": errors_fixes[:5],
            "tags": tags,
            "message_count": len(self._user_messages),
            "tool_call_count": len(self._tool_calls),
        }

    def _extract_decisions(self) -> list[str]:
        """从用户消息和AI回复中提取关键决策表述"""
        decisions = []
        all_text = "。".join(self._user_messages[-10:] + self._ai_responses[-10:])

        for pattern in DECISION_PATTERNS:
            for match in re.finditer(pattern, all_text):
                text = match.group(1).strip()
                if 2 < len(text) < 60 and text not in decisions:
                    decisions.append(text)

        # 限制
        return decisions[:5]

    def _extract_tags(self, files: list[str], commands: list[str]) -> list[str]:
        """从文件和命令中提取标签"""
        tags = set()

        # 从文件扩展名推断语言
        for f in files:
            ext = Path(f).suffix.lower()
            if ext in EXT_TO_LANG:
                tags.add(EXT_TO_LANG[ext])

        # 从命令推断工具
        for cmd in commands:
            cmd_lower = cmd.lower()
            if 'pip' in cmd_lower:
                tags.add('python')
            elif 'npm' in cmd_lower or 'yarn' in cmd_lower or 'node' in cmd_lower:
                tags.add('javascript')
            elif 'cargo' in cmd_lower:
                tags.add('rust')
            elif 'go ' in cmd_lower:
                tags.add('go')
            elif 'docker' in cmd_lower:
                tags.add('docker')
            elif 'git' in cmd_lower:
                tags.add('git')
            elif 'git clone' in cmd_lower:
                tags.add('git')
            elif 'build' in cmd_lower or 'compile' in cmd_lower:
                tags.add('build')
            elif 'install' in cmd_lower:
                tags.add('install')
            elif 'test' in cmd_lower or 'pytest' in cmd_lower:
                tags.add('testing')

        # 从用户消息推断任务类型
        for msg in self._user_messages[:5]:
            msg_lower = msg.lower()
            for task_type, keywords in TASK_KEYWORDS.items():
                if any(kw in msg_lower for kw in keywords):
                    tags.add(task_type)

        return sorted(tags)[:10]

    def _build_summary_text(self, goal: str, files: list[str], commands: list[str],
                            decisions: list[str], errors: list[str]) -> str:
        """生成人类可读的会话摘要"""
        parts = []

        if goal:
            parts.append(f"目标: {goal[:100]}")

        if files:
            file_names = [Path(f).name for f in files[:8]]
            parts.append(f"涉及文件: {', '.join(file_names)}")

        if commands:
            cmd_short = [c[:60] for c in commands[:5]]
            parts.append(f"执行命令: {'; '.join(cmd_short)}")

        if decisions:
            parts.append(f"关键决策: {'; '.join(decisions[:3])}")

        if errors:
            parts.append(f"遇到错误: {len(errors)}个（已处理）")

        summary = "。".join(parts)
        if len(summary) > self.SUMMARY_MAX_LEN:
            summary = summary[:self.SUMMARY_MAX_LEN] + "..."

        return summary

    # ══════════════════════════════════════
    # 用户画像（跨会话累积）
    # ══════════════════════════════════════

    def _load_profile(self) -> dict:
        if self._profile_path.exists():
            try:
                return json.loads(self._profile_path.read_text("utf-8"))
            except Exception:
                pass
        return {
            "preferred_languages": {},
            "common_dirs": {},
            "task_types": {},
            "tools_used": {},
            "named_preferences": [],
            "total_sessions": 0,
            "first_seen": datetime.now().isoformat(),
            "last_seen": "",
        }

    def _save_profile(self):
        self._profile["last_seen"] = datetime.now().isoformat()
        self._profile_path.write_text(json.dumps(self._profile, ensure_ascii=False, indent=2), "utf-8")

    def _update_profile(self, summary: dict):
        """根据本次会话摘要更新用户画像"""
        p = self._profile
        p["total_sessions"] += 1

        if not p.get("first_seen"):
            p["first_seen"] = summary.get("timestamp", datetime.now().isoformat())

        # 语言偏好
        for tag in summary.get("tags", []):
            if tag in EXT_TO_LANG.values():
                p["preferred_languages"][tag] = p["preferred_languages"].get(tag, 0) + 1

        # 目录习惯
        for f in summary.get("files_touched", []):
            try:
                parent = str(Path(f).parent)
                # 聚合到项目根目录
                parts = Path(f).parts
                if len(parts) >= 3:
                    # 取前3级目录作为项目标识
                    key = str(Path(*parts[:min(len(parts), 4)]))
                else:
                    key = parent
                p["common_dirs"][key] = p["common_dirs"].get(key, 0) + 1
            except Exception:
                pass

        # 任务类型
        for tag in summary.get("tags", []):
            if tag in TASK_KEYWORDS:
                p["task_types"][tag] = p["task_types"].get(tag, 0) + 1

        # 工具使用频率
        for tc in self._tool_calls:
            tool_name = tc.get("tool", "")
            if tool_name:
                p.setdefault("tools_used", {})
                p["tools_used"][tool_name] = p["tools_used"].get(tool_name, 0) + 1

        # 提取用户偏好表述
        for msg in self._user_messages:
            for pattern in PREFERENCE_PATTERNS:
                for match in re.finditer(pattern, msg):
                    pref = match.group(1).strip()
                    if 2 < len(pref) < 50 and pref not in p.get("named_preferences", []):
                        p.setdefault("named_preferences", [])
                        p["named_preferences"].append(pref)
                        if len(p["named_preferences"]) > 20:
                            p["named_preferences"] = p["named_preferences"][-20:]

        self._save_profile()

    def get_user_profile(self) -> dict:
        """获取当前用户画像"""
        return dict(self._profile)

    # ══════════════════════════════════════
    # 会话索引与存储
    # ══════════════════════════════════════

    def _load_index(self) -> list[dict]:
        if self._index_path.exists():
            try:
                return json.loads(self._index_path.read_text("utf-8"))
            except Exception:
                pass
        return []

    def _save_index(self):
        self._index_path.write_text(json.dumps(self._index, ensure_ascii=False, indent=2), "utf-8")

    def _save_session(self, summary: dict):
        """保存单次会话摘要到磁盘"""
        # 保存详细摘要
        session_path = self._sessions_dir / f"{self._session_id}.json"
        session_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), "utf-8")

        # 更新索引
        index_entry = {
            "session_id": summary["session_id"],
            "timestamp": summary["timestamp"],
            "goal": summary["goal"][:100],
            "tags": summary["tags"],
            "message_count": summary["message_count"],
            "tool_call_count": summary["tool_call_count"],
        }
        self._index.insert(0, index_entry)

        # 限制数量
        if len(self._index) > self.MAX_SESSIONS:
            removed = self._index[self.MAX_SESSIONS:]
            self._index = self._index[:self.MAX_SESSIONS]
            # 清理旧文件
            for old in removed:
                old_path = self._sessions_dir / f"{old['session_id']}.json"
                try:
                    old_path.unlink(missing_ok=True)
                except Exception:
                    pass

        self._save_index()

    def get_recent_sessions(self, count: int = 5) -> list[dict]:
        """获取最近 N 次会话摘要"""
        recent = []
        for entry in self._index[:count]:
            session_path = self._sessions_dir / f"{entry['session_id']}.json"
            if session_path.exists():
                try:
                    recent.append(json.loads(session_path.read_text("utf-8")))
                except Exception:
                    recent.append(entry)
            else:
                recent.append(entry)
        return recent

    def search_sessions(self, query: str, limit: int = 5) -> list[dict]:
        """搜索历史会话"""
        q = query.lower()
        results = []
        for entry in self._index:
            score = 0
            if q in entry.get("goal", "").lower():
                score += 3
            if any(q in t.lower() for t in entry.get("tags", [])):
                score += 2
            if score > 0:
                results.append((score, entry))
        results.sort(key=lambda x: x[0], reverse=True)
        return [e for _, e in results[:limit]]

    # ══════════════════════════════════════
    # 启动上下文生成（注入 system prompt）
    # ══════════════════════════════════════

    def get_startup_context(self) -> str:
        """
        生成启动时注入 system prompt 的记忆上下文。
        包含：用户画像摘要 + 最近会话概要 + 用户偏好列表。
        """
        parts = []

        # 1. 用户画像摘要
        profile_text = self._build_profile_context()
        if profile_text:
            parts.append(profile_text)

        # 2. 最近会话
        recent = self.get_recent_sessions(5)
        if recent:
            session_lines = ["【近期会话记录】"]
            for i, s in enumerate(recent):
                goal = s.get("goal", "")[:80]
                if not goal:
                    continue
                ts = s.get("timestamp", "")
                try:
                    dt = datetime.fromisoformat(ts)
                    ts_display = dt.strftime("%m/%d %H:%M")
                except Exception:
                    ts_display = ""
                session_lines.append(f"  {ts_display} | {goal}")
            if len(session_lines) > 1:
                parts.append("\n".join(session_lines))

        # 3. 当前会话如果还在进行中，不重复注入

        if not parts:
            return ""

        return "\n\n".join(parts)

    def _build_profile_context(self) -> str:
        """构建用户画像上下文文本"""
        p = self._profile

        if p.get("total_sessions", 0) < 1:
            return ""

        lines = ["【用户画像 — 跨会话学习】"]

        # 语言偏好
        langs = p.get("preferred_languages", {})
        if langs:
            top_langs = sorted(langs.items(), key=lambda x: x[1], reverse=True)[:3]
            lang_str = ", ".join(f"{l}({c}次)" for l, c in top_langs)
            lines.append(f"  常用语言: {lang_str}")

        # 任务类型
        tasks = p.get("task_types", {})
        if tasks:
            top_tasks = sorted(tasks.items(), key=lambda x: x[1], reverse=True)[:3]
            task_str = ", ".join(f"{t}({c}次)" for t, c in top_tasks)
            lines.append(f"  常见任务: {task_str}")

        # 常用目录
        dirs = p.get("common_dirs", {})
        if dirs:
            top_dirs = sorted(dirs.items(), key=lambda x: x[1], reverse=True)[:3]
            dir_str = ", ".join(f"{d}" for d, _ in top_dirs)
            lines.append(f"  常用目录: {dir_str}")

        # 用户偏好
        prefs = p.get("named_preferences", [])
        if prefs:
            lines.append(f"  用户偏好: {'; '.join(prefs[-5:])}")

        # 统计
        lines.append(f"  累计会话: {p.get('total_sessions', 0)}次")

        return "\n".join(lines)

    def get_relevant_context(self, query: str, limit: int = 3) -> str:
        """
        根据当前用户查询，搜索相关历史会话并返回上下文。
        用于首次消息后动态补充记忆。
        """
        matches = self.search_sessions(query, limit)
        if not matches:
            return ""

        lines = ["【相关历史会话】"]
        for m in matches:
            goal = m.get("goal", "")[:100]
            summary = m.get("summary", "")[:150]
            ts = m.get("timestamp", "")[:16]
            lines.append(f"  [{ts}] {goal}")
            if summary:
                lines.append(f"     → {summary}")

        return "\n".join(lines)

    # ══════════════════════════════════════
    # 统计与管理
    # ══════════════════════════════════════

    def get_stats(self) -> dict:
        """获取会话记忆统计"""
        return {
            "total_sessions_recorded": len(self._index),
            "profile_sessions": self._profile.get("total_sessions", 0),
            "preferred_languages": self._profile.get("preferred_languages", {}),
            "top_dirs": sorted(self._profile.get("common_dirs", {}).items(),
                              key=lambda x: x[1], reverse=True)[:5],
            "storage_dir": str(self._mem_dir),
            "active_session": self._session_id if self._active else None,
        }

    # ═══ 学习系统 (能力38+39) ═══

    def record_error(self, error_type: str, message: str, context: str = ""):
        """记录错误模式（能力38）：同类错误出现多次→自动学习规避策略"""
        with self._lock:
            errors = self._profile.setdefault("error_patterns", {})
            key = error_type.lower()
            if key not in errors:
                errors[key] = {"count": 0, "messages": [], "last_seen": ""}
            errors[key]["count"] += 1
            errors[key]["messages"].append(message[:200])
            errors[key]["last_seen"] = datetime.now().isoformat()
            if len(errors[key]["messages"]) > 20:
                errors[key]["messages"] = errors[key]["messages"][-20:]
            if errors[key]["count"] >= 3:
                logger.info("错误模式已学习: %s (出现%d次)", error_type, errors[key]["count"])

    def record_preference(self, key: str, value: str):
        """记录用户偏好（能力39）：框架选择、命名风格、工具偏好"""
        with self._lock:
            prefs = self._profile.setdefault("learned_preferences", {})
            if key not in prefs:
                prefs[key] = {"values": [], "last_updated": ""}
            if value not in prefs[key]["values"]:
                prefs[key]["values"].append(value)
                if len(prefs[key]["values"]) > 10:
                    prefs[key]["values"] = prefs[key]["values"][-10:]
            prefs[key]["last_updated"] = datetime.now().isoformat()
            # 同时记录到旧命名的偏好列表（兼容）
            self._profile["named_preferences"].append(f"{key}: {value}")
            if len(self._profile["named_preferences"]) > 50:
                self._profile["named_preferences"] = self._profile["named_preferences"][-50:]

    def get_learned_context(self) -> str:
        """生成已学习的上下文（错误规避+用户偏好），注入system prompt"""
        parts = []
        # —— 错误规避 ——
        patterns = self._profile.get("error_patterns", {})
        if patterns:
            frequent = {k: v for k, v in patterns.items() if v["count"] >= 3}
            if frequent:
                lines = ["【已学习的错误规避策略】"]
                for err_type, info in sorted(frequent.items(), key=lambda x: -x[1]["count"]):
                    lines.append(f"  - {err_type}: 已出现{info['count']}次，应自动避免")
                parts.append("\n".join(lines))

        # —— 用户偏好 ——
        prefs = self._profile.get("learned_preferences", {})
        if prefs:
            lines = ["【学习到的用户偏好】"]
            for k, v in prefs.items():
                latest = v["values"][-1] if v["values"] else ""
                lines.append(f"  - {k}: {latest}" + (f" (历史: {', '.join(v['values'][-3:])})" if len(v["values"]) > 1 else ""))
            parts.append("\n".join(lines))

        return "\n\n".join(parts) if parts else ""

    def clear(self):
        """清空所有会话记忆（危险操作）"""
        import shutil
        with self._lock:
            try:
                shutil.rmtree(self._mem_dir, ignore_errors=True)
            except Exception:
                pass
            self._mem_dir.mkdir(parents=True, exist_ok=True)
            self._sessions_dir.mkdir(parents=True, exist_ok=True)
            self._profile = {
                "preferred_languages": {}, "common_dirs": {},
                "task_types": {}, "tools_used": {},
                "named_preferences": [], "total_sessions": 0,
                "first_seen": datetime.now().isoformat(), "last_seen": "",
                "error_patterns": {}, "learned_preferences": {},
            }
            self._index = []
        logger.warning("会话记忆已清空")


# ═══════════════════════════════════════════
# 全局单例
# ═══════════════════════════════════════════

_session_memory: Optional[SessionMemory] = None


def get_session_memory(data_root: Path = None) -> SessionMemory:
    """获取会话记忆引擎全局单例"""
    global _session_memory
    if _session_memory is None:
        _session_memory = SessionMemory(data_root)
    return _session_memory


def reset_session_memory():
    """重置会话记忆（测试用）"""
    global _session_memory
    _session_memory = None
