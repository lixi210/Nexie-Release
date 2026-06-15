# Nexie — AI Coding Agent
# Copyright (c) 2026. All rights reserved.
# 本软件仅供个人学习与研究使用，禁止未经授权的商业用途、分发或修改后闭源。
# Licensed for personal, educational, and non-commercial use only.
"""
持久记忆管理器 — 跨会话保留用户偏好、关键事实、项目状态
存储在 exe/data 同级目录，所有聊天共享
"""
import json
from pathlib import Path
from datetime import datetime
from typing import Optional

from nexie import get_data_dir
DATA_ROOT = get_data_dir()


class MemoryManager:
    """管理 agent_profile.json + agent_memory.json"""

    def __init__(self, data_dir: Path = None):
        self.data_dir = Path(data_dir) if data_dir else DATA_ROOT
        self.profile_path = self.data_dir / "agent_profile.json"
        self.memory_path = self.data_dir / "agent_memory.json"
        self.profile = self._load_profile()
        self.memory = self._load_memory()

    # ═══ Profile (用户偏好) ═══

    def _load_profile(self) -> dict:
        if self.profile_path.exists():
            try:
                return json.loads(self.profile_path.read_text("utf-8"))
            except Exception:
                pass
        return {
            "agent_name": "",
            "user_name": "",
            "language": "zh-CN",
            "created": datetime.now().isoformat(),
        }

    def _save_profile(self):
        self.profile_path.write_text(json.dumps(self.profile, ensure_ascii=False, indent=2), "utf-8")

    def get_agent_name(self) -> str:
        return self.profile.get("agent_name", "")

    def set_agent_name(self, name: str):
        self.profile["agent_name"] = name
        self._save_profile()

    def get_user_name(self) -> str:
        return self.profile.get("user_name", "")

    # ═══ 知识图谱 (结构化记忆) ═══

    def create_entity(self, name: str, entity_type: str, observations: str = "") -> str:
        """创建知识图谱实体"""
        entities = self.memory.setdefault("entities", {})
        if name in entities:
            return f"实体已存在: {name}"
        obs_list = _parse_observations(observations)
        entities[name] = {
            "type": entity_type,
            "observations": obs_list,
            "created": datetime.now().isoformat(),
        }
        self._save_memory()
        return f"✅ 已创建实体: {name} ({entity_type})"

    def add_observations(self, entity_name: str, observations: str = "") -> str:
        """给实体添加观察记录"""
        entities = self.memory.setdefault("entities", {})
        if entity_name not in entities:
            return f"实体不存在: {entity_name}"
        obs_list = _parse_observations(observations)
        entities[entity_name].setdefault("observations", [])
        entities[entity_name]["observations"].extend(obs_list)
        # 去重
        entities[entity_name]["observations"] = list(set(entities[entity_name]["observations"]))
        self._save_memory()
        return f"✅ 已添加 {len(obs_list)} 条记录到: {entity_name}"

    def create_relation(self, from_entity: str, to_entity: str, relation_type: str) -> str:
        """创建实体间关系"""
        relations = self.memory.setdefault("relations", [])
        # 去重
        for r in relations:
            if r["from"] == from_entity and r["to"] == to_entity and r["type"] == relation_type:
                return "关系已存在"
        relations.append({"from": from_entity, "to": to_entity, "type": relation_type})
        self._save_memory()
        return f"✅ 已创建关系: {from_entity} →[{relation_type}]→ {to_entity}"

    def search_entities(self, query: str) -> str:
        """搜索知识图谱"""
        entities = self.memory.get("entities", {})
        relations = self.memory.get("relations", [])
        matches = []
        q = query.lower()
        for name, data in entities.items():
            if q in name.lower() or q in data.get("type", "").lower():
                matches.append((name, data))
            else:
                for obs in data.get("observations", []):
                    if q in obs.lower():
                        matches.append((name, data))
                        break
        if not matches:
            # 也搜索关系
            for r in relations:
                if q in r["from"].lower() or q in r["to"].lower() or q in r["type"].lower():
                    return f"🔗 关系: {r['from']} →[{r['type']}]→ {r['to']}"
            return f"🔍 未找到 '{query}' 相关信息"

        lines = [f"🔍 搜索 '{query}' ({len(matches)} 条结果)"]
        lines.append("=" * 40)
        for name, data in matches[:10]:
            lines.append(f"📦 {name} ({data.get('type', '?')})")
            for obs in data.get("observations", [])[:5]:
                lines.append(f"   • {obs}")
        # 相关关系
        related = [r for r in relations if r["from"] in [m[0] for m in matches] or r["to"] in [m[0] for m in matches]]
        if related:
            lines.append("─" * 40)
            for r in related[:5]:
                lines.append(f"🔗 {r['from']} →[{r['type']}]→ {r['to']}")
        return "\n".join(lines)

    def read_graph(self) -> str:
        """导出完整知识图谱"""
        entities = self.memory.get("entities", {})
        relations = self.memory.get("relations", [])
        if not entities:
            return "📊 知识图谱为空"
        lines = [f"📊 知识图谱 ({len(entities)} 实体, {len(relations)} 关系)"]
        lines.append("=" * 50)
        for name, data in entities.items():
            lines.append(f"📦 [{data.get('type', '?')}] {name}")
            for obs in data.get("observations", [])[:3]:
                lines.append(f"   • {obs}")
        if relations:
            lines.append("─" * 50)
            for r in relations:
                lines.append(f"🔗 {r['from']} →[{r['type']}]→ {r['to']}")
        return "\n".join(lines)

    # ═══ Memory (跨会话事实) ═══

    def _load_memory(self) -> dict:
        if self.memory_path.exists():
            try:
                return json.loads(self.memory_path.read_text("utf-8"))
            except Exception:
                pass
        return {
            "key_facts": [],          # 用户告知的关键信息
            "entities": {},           # 知识图谱: {name: {type, observations[], created}}
            "relations": [],          # 关系: [{from, to, type}]
            "completed_tasks": [],    # 已完成的重要任务
            "installed_deps": [],     # 已安装的依赖/工具
            "project_state": "",      # 项目当前状态简述
            "last_session": "",       # 上次会话时间
            "session_count": 0,
            "important_files": [],    # 生成的重要文件路径
            "session_actions": [],    # 当前会话的操作记录
            "session_history": [],    # 历史会话摘要（最近N轮）
        }

    def _save_memory(self):
        self.memory_path.write_text(json.dumps(self.memory, ensure_ascii=False, indent=2), "utf-8")

    def add_fact(self, fact: str):
        """记录用户告知的关键事实，去重"""
        if fact not in self.memory["key_facts"]:
            self.memory["key_facts"].append(fact)
            self._save_memory()

    def remove_fact(self, fact: str):
        if fact in self.memory["key_facts"]:
            self.memory["key_facts"].remove(fact)
            self._save_memory()

    def add_completed_task(self, task: str):
        ts = datetime.now().strftime("%m-%d %H:%M")
        self.memory["completed_tasks"].append(f"[{ts}] {task}")
        # 只保留最近50条
        if len(self.memory["completed_tasks"]) > 50:
            self.memory["completed_tasks"] = self.memory["completed_tasks"][-50:]
        self._save_memory()

    def add_installed_dep(self, dep: str):
        if dep not in self.memory["installed_deps"]:
            self.memory["installed_deps"].append(dep)
            self._save_memory()

    def is_dep_installed(self, dep: str) -> bool:
        return dep in self.memory["installed_deps"]

    def add_important_file(self, filepath: str):
        ts = datetime.now().strftime("%m-%d %H:%M")
        entry = f"[{ts}] {filepath}"
        if entry not in self.memory["important_files"]:
            self.memory["important_files"].append(entry)
            if len(self.memory["important_files"]) > 30:
                self.memory["important_files"] = self.memory["important_files"][-30:]
            self._save_memory()

    def update_project_state(self, state: str):
        self.memory["project_state"] = state
        self._save_memory()

    def record_session(self):
        self.memory["last_session"] = datetime.now().isoformat()
        self.memory["session_count"] += 1
        self._save_memory()

    # ═══ Session Action Tracking (操作记录) ═══

    def start_session(self):
        """开始新会话，清空当前操作记录"""
        self.memory["session_actions"] = []
        self.record_session()

    def add_action(self, action: str):
        """记录当前会话中的操作"""
        self.memory["session_actions"].append(action)

    def end_session(self) -> str:
        """结束当前会话，生成摘要并存入历史。返回摘要文本"""
        actions = self.memory.get("session_actions", [])
        if not actions:
            return ""

        # 去重合并
        seen = set()
        unique = []
        for a in actions:
            if a not in seen:
                seen.add(a)
                unique.append(a)

        # 限制每条摘要最多15个操作，超出截断（防止系统提示词膨胀→400）
        if len(unique) > 15:
            unique = unique[:15]
            unique.append("... (还有更多操作)")

        ts = datetime.now().strftime("%m-%d %H:%M")
        summary_lines = [f"[{ts}] 本轮完成以下操作:"]
        summary_lines.extend(f"  • {a}" for a in unique)
        summary = "\n".join(summary_lines)

        # 存入历史
        self.memory.setdefault("session_history", [])
        self.memory["session_history"].append(summary)
        if len(self.memory["session_history"]) > 20:
            self.memory["session_history"] = self.memory["session_history"][-20:]

        self.memory["session_actions"] = []
        self._save_memory()
        return summary

    # ═══ 生成注入到系统提示的记忆文本 ═══

    def build_memory_context(self) -> str:
        """生成注入到系统提示的记忆上下文，空则返回 ''"""
        parts = []

        name = self.get_agent_name()
        user = self.get_user_name()
        if name or user:
            greeting = name if name else "AI助手"
            who = f"用户: {user}" if user else ""
            parts.append(f"你的名字是「{greeting}」。{who}".strip())

        if self.memory["key_facts"]:
            facts = "\n".join(f"- {f}" for f in self.memory["key_facts"])
            parts.append(f"【用户告知的重要信息】\n{facts}")

        # 历史会话摘要 — 最近3次会话的简要记录
        if self.memory.get("session_history"):
            recent = self.memory["session_history"][-3:]
            if recent:
                session_lines = ["【近期会话】"]
                for i, s in enumerate(recent):
                    lines = s.split("\n")
                    title = lines[0] if lines else ""
                    # 截断过长的标题
                    if len(title) > 100:
                        title = title[:100] + "..."
                    session_lines.append(f"  {title}")
                parts.append("\n".join(session_lines))

        if self.memory["completed_tasks"]:
            recent = self.memory["completed_tasks"][-10:]
            tasks = "\n".join(f"- {t}" for t in recent)
            parts.append(f"【最近完成的任务】\n{tasks}")

        if self.memory["installed_deps"]:
            deps = ", ".join(self.memory["installed_deps"])
            parts.append(f"【已安装的依赖/工具】{deps}")

        if self.memory["important_files"]:
            recent_files = self.memory["important_files"][-10:]
            files = "\n".join(f"- {f}" for f in recent_files)
            parts.append(f"【重要文件记录】\n{files}")

        if self.memory["project_state"]:
            parts.append(f"【当前项目状态】{self.memory['project_state']}")

        return "\n\n".join(parts) if parts else ""


# 全局单例
_memory_instance: Optional[MemoryManager] = None


def _parse_observations(raw: str) -> list[str]:
    """将字符串解析为观察列表(逗号/换行分隔)"""
    if not raw or not raw.strip():
        return []
    if "\n" in raw:
        return [s.strip() for s in raw.split("\n") if s.strip()]
    return [s.strip() for s in raw.split(",") if s.strip()]


def get_memory() -> MemoryManager:
    global _memory_instance
    if _memory_instance is None:
        _memory_instance = MemoryManager()
    return _memory_instance
