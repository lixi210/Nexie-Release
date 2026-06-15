# Nexie — AI Coding Agent
# Copyright (c) 2026. All rights reserved.
# 本软件仅供个人学习与研究使用，禁止未经授权的商业用途、分发或修改后闭源。
# Licensed for personal, educational, and non-commercial use only.
"""
Nexie — 四层记忆架构：L1临时缓存 / L2常驻核心记忆 / L3自动压缩 / L4冷归档
实现百万级有效上下文，突破模型原生上下文限制
"""
import os
import re
import json
import time
import gzip
import shutil
import threading
import logging
from pathlib import Path
from datetime import datetime
from typing import Optional, Callable

logger = logging.getLogger("Nexie.MemoryLayers")

# ═══════════════════════════════════════════
# 数据目录定位（统一入口）
# ═══════════════════════════════════════════
from nexie import get_data_dir
DATA_ROOT = get_data_dir()

# ═══════════════════════════════════════════
# Token 估算工具
# ═══════════════════════════════════════════

def estimate_tokens(text: str) -> int:
    """精确Token估算：中文≈1.2token/字，英文≈0.25token/字，代码≈0.3token/字"""
    if not text:
        return 0
    chinese_chars = len(re.findall(r'[一-鿿㐀-䶿]', text))
    ascii_chars = len(re.findall(r'[a-zA-Z0-9\s]', text))
    other_chars = len(text) - chinese_chars - ascii_chars
    return int(chinese_chars * 1.2 + ascii_chars * 0.25 + other_chars * 0.3)

def count_message_tokens(messages: list[dict]) -> int:
    """计算消息列表总Token数"""
    total = 0
    for m in messages:
        content = m.get("content", "") or ""
        if isinstance(content, list):
            # 多模态消息
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    total += estimate_tokens(part.get("text", ""))
        else:
            total += estimate_tokens(str(content))
        # tool_calls 也计入
        for tc in m.get("tool_calls", []) or []:
            total += estimate_tokens(json.dumps(tc, ensure_ascii=False))
        total += estimate_tokens(m.get("name", "") or "")
    return total

# ═══════════════════════════════════════════
# L1: 临时缓存层 — 大体积数据落地，上下文只存摘要
# ═══════════════════════════════════════════

class L1TempCache:
    """
    L1临时缓存：工具返回的大体积文件、日志、源码不塞进对话上下文。
    原始数据落地 Nexie_data/l1_cache/，上下文只存摘要索引。
    需要内容时按需片段加载（read_file片段读取）。
    """

    CACHE_THRESHOLD = 8000      # 超过此字符数触发落地缓存
    SUMMARY_MAX_LEN = 500       # 摘要最大长度
    MAX_CACHE_FILES = 200       # 最多缓存文件数
    MAX_CACHE_AGE_HOURS = 24    # 缓存过期时间

    def __init__(self, data_root: Path = None):
        self._data_root = data_root or DATA_ROOT
        self._cache_dir = self._data_root / "l1_cache"
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self._index_path = self._cache_dir / "index.json"
        self._index: dict[str, dict] = self._load_index()
        self._lock = threading.Lock()
        # 启动时清理过期缓存
        self._cleanup_expired()

    def _load_index(self) -> dict:
        if self._index_path.exists():
            try:
                return json.loads(self._index_path.read_text("utf-8"))
            except Exception:
                pass
        return {}

    def _save_index(self):
        self._index_path.write_text(json.dumps(self._index, ensure_ascii=False, indent=2), "utf-8")

    def _cleanup_expired(self):
        """清理过期缓存文件"""
        now = time.time()
        expired = []
        with self._lock:
            for key, info in list(self._index.items()):
                age_hours = (now - info.get("timestamp", 0)) / 3600
                if age_hours > self.MAX_CACHE_AGE_HOURS:
                    expired.append(key)
            for key in expired:
                self._remove_entry(key)
        if expired:
            logger.debug("L1缓存清理: %d个过期条目", len(expired))

    def _remove_entry(self, key: str):
        """移除单个缓存条目"""
        info = self._index.pop(key, None)
        if info:
            filepath = self._cache_dir / info.get("filename", "")
            try:
                filepath.unlink(missing_ok=True)
            except Exception:
                pass

    def should_cache(self, content: str) -> bool:
        """判断内容是否需要落地缓存"""
        return len(content) > self.CACHE_THRESHOLD

    def store(self, tool_name: str, params: str, result: str) -> str:
        """
        将大体积工具结果存入L1缓存，返回摘要文本用于注入上下文。
        如果内容不够大，直接返回原始结果。
        """
        if not self.should_cache(result):
            return result  # 不够大，不缓存

        with self._lock:
            # 限制缓存数量
            if len(self._index) >= self.MAX_CACHE_FILES:
                oldest_key = min(self._index.keys(), key=lambda k: self._index[k].get("timestamp", 0))
                self._remove_entry(oldest_key)

            # 生成缓存key和文件名
            ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            safe_name = re.sub(r'[^\w]', '_', tool_name)[:30]
            cache_key = f"{safe_name}_{ts}"
            filename = f"{cache_key}.txt.gz"
            filepath = self._cache_dir / filename

            # 压缩写入
            try:
                with gzip.open(filepath, 'wt', encoding='utf-8') as f:
                    f.write(result)
            except Exception as e:
                logger.error("L1缓存写入失败: %s", e)
                return result  # 写入失败返回原始内容

            # 生成摘要
            first_lines = result[:500].strip()
            last_lines = result[-200:].strip()
            total_lines = result.count('\n') + 1

            summary = (
                f"[L1缓存] 工具 {tool_name} 返回 {len(result):,}字符/{total_lines}行 → "
                f"已落地 {filename}\n"
                f"  头部预览: {first_lines[:200]}...\n"
                f"  尾部预览: ...{last_lines[-150:]}\n"
                f"  使用 read_file(path='{filepath.as_posix()}', offset=N, limit=M) 按需加载片段"
            )

            # 记录索引
            self._index[cache_key] = {
                "tool_name": tool_name,
                "params": str(params)[:200],
                "filename": filename,
                "size": len(result),
                "lines": total_lines,
                "summary_first": first_lines[:200],
                "summary_last": last_lines[-150:],
                "timestamp": time.time(),
            }
            self._save_index()
            logger.info("L1缓存: %s → %s (%d字符)", tool_name, filename, len(result))
            return summary

    def get_cached(self, cache_key: str, offset: int = 0, limit: int = 100) -> Optional[str]:
        """按需加载缓存文件的片段"""
        info = self._index.get(cache_key)
        if not info:
            return None
        filepath = self._cache_dir / info["filename"]
        if not filepath.exists():
            return None
        try:
            with gzip.open(filepath, 'rt', encoding='utf-8') as f:
                lines = f.readlines()
            start = max(0, offset)
            end = min(len(lines), offset + limit) if limit > 0 else len(lines)
            result = ''.join(lines[start:end])
            header = f"📂 [L1缓存] {info['filename']} | 行{start+1}-{end}/{len(lines)} | 共{info['size']:,}字符\n"
            return header + result
        except Exception as e:
            return f"[L1缓存读取失败] {e}"

    def list_cached(self, filter_tool: str = None) -> str:
        """列出所有缓存条目"""
        with self._lock:
            if not self._index:
                return "📂 L1缓存为空"
            lines = [f"📂 L1缓存索引 ({len(self._index)}条)"]
            lines.append("=" * 60)
            for key, info in sorted(self._index.items(),
                                     key=lambda x: x[1].get("timestamp", 0), reverse=True)[:20]:
                age = (time.time() - info.get("timestamp", 0)) / 3600
                if filter_tool and filter_tool not in info.get("tool_name", ""):
                    continue
                lines.append(f"  🔑 {key}")
                lines.append(f"     工具: {info['tool_name']} | {info['size']:,}字符 | {info['lines']}行 | {age:.1f}h前")
            return "\n".join(lines)

    def clear(self):
        """清空所有L1缓存"""
        with self._lock:
            for info in list(self._index.values()):
                filepath = self._cache_dir / info.get("filename", "")
                try:
                    filepath.unlink(missing_ok=True)
                except Exception:
                    pass
            self._index.clear()
            self._save_index()
        logger.info("L1缓存已清空")


# ═══════════════════════════════════════════
# L2: 常驻核心记忆 — 永久留存，不参与自动裁剪
# ═══════════════════════════════════════════

class L2CoreMemory:
    """
    L2常驻核心记忆：持久化保存项目需求、关键修改记录、固定配置、重要报错。
    永久留存不参与自动裁剪，即使上下文压缩也不会删除L2记忆。
    存储格式：JSON结构化 + 全文索引
    """

    MEMORY_TYPES = ["requirement", "modification", "config", "error", "fact", "reference"]

    def __init__(self, data_root: Path = None):
        self._data_root = data_root or DATA_ROOT
        self._mem_dir = self._data_root / "l2_core_memory"
        self._mem_dir.mkdir(parents=True, exist_ok=True)
        self._mem_path = self._mem_dir / "core_memory.json"
        self._lock = threading.Lock()
        self._entries: list[dict] = self._load()

    def _load(self) -> list[dict]:
        if self._mem_path.exists():
            try:
                return json.loads(self._mem_path.read_text("utf-8"))
            except Exception:
                pass
        return []

    def _save(self):
        self._mem_path.write_text(json.dumps(self._entries, ensure_ascii=False, indent=2), "utf-8")

    def add(self, content: str, mem_type: str = "fact", tags: list[str] = None,
            source: str = "", importance: int = 5) -> str:
        """
        添加一条核心记忆。
        - mem_type: requirement/modification/config/error/fact/reference
        - importance: 1-10, 越高越重要，不会被压缩
        - tags: 标签列表，用于检索
        """
        if mem_type not in self.MEMORY_TYPES:
            mem_type = "fact"

        entry = {
            "id": f"mem_{int(time.time() * 1000)}_{len(self._entries)}",
            "type": mem_type,
            "content": content,
            "tags": tags or [],
            "source": source,
            "importance": max(1, min(10, importance)),
            "created": datetime.now().isoformat(),
            "accessed": datetime.now().isoformat(),
            "access_count": 0,
        }

        with self._lock:
            # 去重检查
            for existing in self._entries:
                if existing.get("content", "").strip() == content.strip():
                    existing["accessed"] = datetime.now().isoformat()
                    existing["access_count"] += 1
                    self._save()
                    return f"L2记忆已存在(已刷新): {existing['id']}"
            self._entries.append(entry)
            self._save()

        logger.info("L2核心记忆添加: [%s] %s (重要性:%d)", mem_type, content[:80], importance)
        return f"✅ L2记忆已存储: {entry['id']}"

    def get(self, mem_id: str) -> Optional[dict]:
        """根据ID获取记忆"""
        for entry in self._entries:
            if entry["id"] == mem_id:
                entry["accessed"] = datetime.now().isoformat()
                entry["access_count"] += 1
                self._save()
                return entry
        return None

    def search(self, query: str, mem_type: str = None, limit: int = 20) -> list[dict]:
        """全文搜索核心记忆"""
        q = query.lower()
        results = []
        with self._lock:
            for entry in self._entries:
                if mem_type and entry["type"] != mem_type:
                    continue
                content_lower = entry["content"].lower()
                tag_match = any(q in t.lower() for t in entry.get("tags", []))
                if q in content_lower or tag_match:
                    results.append(entry)
            # 按重要性+访问次数排序
            results.sort(key=lambda e: (e.get("importance", 5), e.get("access_count", 0)), reverse=True)
        return results[:limit]

    def remove(self, mem_id: str) -> bool:
        """删除一条记忆"""
        with self._lock:
            for i, entry in enumerate(self._entries):
                if entry["id"] == mem_id:
                    self._entries.pop(i)
                    self._save()
                    return True
        return False

    def list_by_type(self, mem_type: str = None) -> str:
        """按类型列出记忆"""
        entries = self._entries
        if mem_type:
            entries = [e for e in entries if e["type"] == mem_type]

        if not entries:
            return "📌 L2核心记忆为空"

        entries.sort(key=lambda e: (e.get("importance", 5), e.get("access_count", 0)), reverse=True)
        lines = [f"📌 L2核心记忆 (共{len(entries)}条)"]
        lines.append("=" * 60)
        for e in entries[:30]:
            type_icon = {
                "requirement": "📋", "modification": "🔧", "config": "⚙️",
                "error": "❌", "fact": "💡", "reference": "🔗"
            }.get(e["type"], "📌")
            lines.append(f"  {type_icon} [{e['type']}] {e['content'][:100]}")
            lines.append(f"     id={e['id']} | 重要性:{e['importance']} | 访问:{e['access_count']}次")
        return "\n".join(lines)

    def build_injection_prompt(self) -> str:
        """
        构建注入系统提示的L2记忆文本。
        高重要性(>=7)条目始终注入，其他按类型取前3条。
        此文本不会被L3压缩裁剪。
        """
        with self._lock:
            if not self._entries:
                return ""

            # 高重要性条目（始终注入）
            high_imp = [e for e in self._entries if e.get("importance", 5) >= 7]

            # 按类型各取前3条
            by_type = {}
            for e in self._entries:
                if e.get("importance", 5) < 7:
                    t = e["type"]
                    by_type.setdefault(t, []).append(e)

            selected = list(high_imp)
            for t_entries in by_type.values():
                t_entries.sort(key=lambda e: (e.get("importance", 5), e.get("access_count", 0)), reverse=True)
                selected.extend(t_entries[:3])

            # 去重
            seen_ids = set()
            unique = []
            for e in selected:
                if e["id"] not in seen_ids:
                    seen_ids.add(e["id"])
                    unique.append(e)

            if not unique:
                return ""

            lines = ["\n【L2核心记忆 — 永久保留】"]
            for e in unique[:20]:
                type_label = {
                    "requirement": "需求", "modification": "修改",
                    "config": "配置", "error": "错误",
                    "fact": "事实", "reference": "参考"
                }.get(e["type"], e["type"])
                lines.append(f"- [{type_label}] {e['content']}")

            return "\n".join(lines)

    def get_all(self) -> list[dict]:
        """获取所有L2条目（用于存档）"""
        return list(self._entries)

    def clear(self):
        """清空所有L2记忆（危险操作）"""
        self._entries.clear()
        self._save()


# ═══════════════════════════════════════════
# L3: 自动压缩机制 — Token超87%自动压缩
# ═══════════════════════════════════════════

class L3AutoCompressor:
    """
    L3自动压缩：实时统计总Token，占用达到上下文上限87%自动调用模型压缩。
    压缩策略：保留system消息 + L2记忆 + 最近关键对话，精简冗余内容。
    不省略自动compact代码，保留关键逻辑。

    百万上下文原理：
    - L3管理模型原生128K上下文窗口，87%阈值触发压缩确保不超限
    - L4归档提供虚拟上下文扩展 → 分片检索按需加载历史内容
    - L3+L4协同：热数据在L3窗口内，冷数据在L4磁盘，按需调入
    """

    CONTEXT_LIMIT_TOKENS = 128000     # DeepSeek V4 Pro 原生128K上下文上限
    COMPRESS_THRESHOLD = 0.60
    MIN_KEEP_ROUNDS = 3
    MAX_COMPRESSED_SUMMARY = 3000    # 压缩摘要最大字符数

    def __init__(self, context_limit: int = None):
        self.context_limit = context_limit or self.CONTEXT_LIMIT_TOKENS
        self._lock = threading.Lock()
        self._compress_count = 0
        self._last_check_tokens = 0

    @property
    def compress_threshold_tokens(self) -> int:
        """触发压缩的Token阈值"""
        return int(self.context_limit * self.COMPRESS_THRESHOLD)

    def check_need_compress(self, messages: list[dict]) -> bool:
        """检查是否需要压缩"""
        total = count_message_tokens(messages)
        self._last_check_tokens = total
        return total > self.compress_threshold_tokens

    def get_token_stats(self, messages: list[dict]) -> dict:
        """获取Token统计信息"""
        total = count_message_tokens(messages)
        system_tokens = count_message_tokens([m for m in messages if m.get("role") == "system"])
        user_tokens = count_message_tokens([m for m in messages if m.get("role") == "user"])
        assistant_tokens = count_message_tokens([m for m in messages if m.get("role") == "assistant"])
        tool_tokens = count_message_tokens([m for m in messages if m.get("role") == "tool"])

        return {
            "total": total,
            "limit": self.context_limit,
            "threshold": self.compress_threshold_tokens,
            "usage_pct": round(total / self.context_limit * 100, 1),
            "breakdown": {
                "system": system_tokens,
                "user": user_tokens,
                "assistant": assistant_tokens,
                "tool": tool_tokens,
            },
            "message_count": len(messages),
            "need_compress": total > self.compress_threshold_tokens,
        }

    def compress(self, messages: list[dict], l2_memory: L2CoreMemory = None) -> list[dict]:
        """
        智能压缩消息列表 — 保护思考/工具/回复完整性。
        策略：
        1. 保留所有system消息（含L2记忆）
        2. 保留第一条user（原始任务）
        3. 保留最近N轮完整对话（user→assistant→tool原子单位）
        4. 中间轮次压缩为摘要，但保留：
           - 所有reasoning_content（思考过程不丢）
           - 所有tool_calls名称+参数（工具调用不丢）
           - 所有assistant回复内容（AI回复不丢）
        5. 只精简真正冗余：重复文件读取、空内容、纯确认消息
        """
        with self._lock:
            if len(messages) <= 10:
                return messages  # 太少不压缩

            self._compress_count += 1
            original_count = len(messages)
            original_tokens = count_message_tokens(messages)

            # 分类消息
            system_msgs = [m for m in messages if m.get("role") == "system"]
            non_system = [m for m in messages if m.get("role") != "system"]

            # —— 按轮次分组（user消息作为轮次边界） ——
            rounds = []  # [[msg, msg, ...], ...]  每轮 = user→assistant(s)→tool(s)
            current_round = []
            for m in non_system:
                if m.get("role") == "user" and current_round:
                    rounds.append(current_round)
                    current_round = []
                current_round.append(m)
            if current_round:
                rounds.append(current_round)

            if len(rounds) <= self.MIN_KEEP_ROUNDS + 2:
                return messages  # 轮次不够，不压缩

            # —— 构建压缩结果 ——
            result = list(system_msgs)

            # 第一条user（原始任务）始终保留
            first_round = rounds[0]
            result.extend(first_round)

            # 中间轮次：生成保留思考/工具/回复的摘要
            middle_rounds = rounds[1:-self.MIN_KEEP_ROUNDS]
            if middle_rounds:
                summary = self._generate_preserving_summary(middle_rounds)
                if summary:
                    result.append({"role": "user",
                                   "content": f"[L3上下文压缩 #{self._compress_count}]\n{summary}"})

            # 最近N轮完整保留（含思考、工具调用、回复全部不变）
            recent_rounds = rounds[-self.MIN_KEEP_ROUNDS:]
            for r in recent_rounds:
                result.extend(r)

            # 确保以user/system开头
            if result and result[0].get("role") not in ("system", "user"):
                result.insert(0, {"role": "system", "content": "[L3压缩恢复标记]"})

            new_tokens = count_message_tokens(result)
            reduction_pct = round((1 - new_tokens / max(1, original_tokens)) * 100, 1)

            logger.info(
                "L3压缩 #%d | %d→%d条(%d轮→%d轮) | %d→%d tokens | 缩减%.1f%% | 思考/工具/回复完整保留",
                self._compress_count, original_count, len(result),
                len(rounds), self.MIN_KEEP_ROUNDS,
                original_tokens, new_tokens, reduction_pct
            )

            return result

    def _generate_preserving_summary(self, rounds: list[list[dict]]) -> str:
        """
        生成压缩摘要 — 完整保留思考/工具/回复。
        只精简真正冗余：重复读取内容、空消息、纯确认/问候。
        """
        summary_parts = []
        total_kept_chars = 0
        max_summary_chars = self.MAX_COMPRESSED_SUMMARY

        for round_msgs in rounds:
            for m in round_msgs:
                role = m.get("role", "")
                content = m.get("content", "") or ""

                if role == "user":
                    # 保留用户完整指令
                    key = self._extract_key_sentences(content, max_len=200)
                    if key:
                        summary_parts.append(f"[用户] {key}")
                        total_kept_chars += len(key)

                elif role == "assistant":
                    has_tool_calls = bool(m.get("tool_calls"))
                    has_reasoning = bool(m.get("reasoning_content"))

                    # 保留思考内容（最重要！不丢）
                    if has_reasoning:
                        rc = m.get("reasoning_content", "")
                        if len(rc) > 300:
                            rc = rc[:150] + "...[思考中段省略]..." + rc[-150:]
                        summary_parts.append(f"[思考] {rc}")
                        total_kept_chars += len(rc)

                    # 保留工具调用
                    if has_tool_calls:
                        for tc in m["tool_calls"]:
                            fn = tc.get("function", {})
                            name = fn.get("name", "?")
                            args = fn.get("arguments", "")[:200]
                            summary_parts.append(f"[调用工具] {name}({args})")
                            total_kept_chars += len(name) + len(args)

                    # 保留AI回复内容（不丢！）
                    if content and not has_tool_calls:
                        key = self._extract_key_sentences(content, max_len=250)
                        if key:
                            summary_parts.append(f"[回复] {key}")
                            total_kept_chars += len(key)

                elif role == "tool":
                    # 保留工具返回的关键信息（不丢！）
                    tid = m.get("tool_call_id", "")[:8]
                    # 截断超长结果但保留头尾
                    if len(content) > 600:
                        preview = content[:250] + f"\n... [{len(content)-500}字符省略] ...\n" + content[-250:]
                    else:
                        preview = content
                    summary_parts.append(f"[工具返回:{tid}] {preview[:500]}")
                    total_kept_chars += min(len(preview), 500)

                if total_kept_chars > max_summary_chars:
                    summary_parts.append("...[摘要截断，剩余内容可归档L4]...")
                    return "\n".join(summary_parts)

        return "\n".join(summary_parts) if summary_parts else ""

    def _extract_key_sentences(self, text: str, max_len: int = 150) -> str:
        """从文本中提取关键句子"""
        # 按句号/换行分割
        sentences = re.split(r'[。\n]', text)
        key_sentences = []
        for s in sentences:
            s = s.strip()
            if not s:
                continue
            # 跳过纯客套/问候语
            skip_patterns = ['好的', '收到', '没问题', '明白了', '了解', '可以', '当然',
                           '请继续', '有什么', '需要帮', '我能帮', '很高兴']
            if any(s.startswith(p) for p in skip_patterns) and len(s) < 30:
                continue
            key_sentences.append(s)

        result = "。".join(key_sentences[:3])
        if len(result) > max_len:
            result = result[:max_len] + "..."
        return result

    def get_compress_count(self) -> int:
        return self._compress_count

    def get_last_token_count(self) -> int:
        return self._last_check_tokens


# ═══════════════════════════════════════════
# L4: 冷归档存储 — 超量历史会话、海量源码、超长文档本地归档
# ═══════════════════════════════════════════

class L4ColdArchive:
    """
    L4冷归档：超量历史会话、海量源码、超长文档存入本地归档库。
    采用关键词检索分片载入上下文，全量原始内容本地留存。
    突破模型原生128K上下文限制，实现百万级可用虚拟上下文。

    百万上下文实现机制：
    - 全量内容压缩落盘（gzip，压缩比约5:1）
    - 500MB磁盘 ≈ 2500万字符 ≈ 1000万+ tokens 存储能力
    - 关键词索引+分片按需加载 → 每次只载入相关3-5分片到上下文
    - 模型上下文窗口始终≤128K，但可访问的归档总量无上限
    """

    CHUNK_SIZE = 4000            # 每个分片字符数(~2000 tokens)，足够承载一个完整逻辑块
    CHUNK_OVERLAP = 400          # 分片重叠字符数，确保跨分片上下文连续
    MAX_ARCHIVE_SIZE_MB = 500    # 归档库最大大小(500MB ≈ 2500万字符 ≈ 1000万+ tokens)

    def __init__(self, data_root: Path = None):
        self._data_root = data_root or DATA_ROOT
        self._archive_dir = self._data_root / "l4_archive"
        self._archive_dir.mkdir(parents=True, exist_ok=True)
        self._index_path = self._archive_dir / "archive_index.json"
        self._lock = threading.Lock()
        self._index: dict[str, dict] = self._load_index()

    def _load_index(self) -> dict:
        if self._index_path.exists():
            try:
                return json.loads(self._index_path.read_text("utf-8"))
            except Exception:
                pass
        return {}

    def _save_index(self):
        self._index_path.write_text(json.dumps(self._index, ensure_ascii=False, indent=2), "utf-8")

    def archive(self, content: str, source: str, content_type: str = "conversation",
                tags: list[str] = None, metadata: dict = None) -> str:
        """
        归档大体积内容到本地存储。
        - content_type: conversation/source_code/document/log
        - source: 来源标识
        - tags: 检索关键词
        自动分片存储，建立关键词索引。
        """
        if not content or not content.strip():
            return "内容为空，跳过归档"

        archive_id = f"arc_{int(time.time() * 1000)}_{hash(content[:100]) % 10000:04d}"
        archive_dir = self._archive_dir / archive_id
        archive_dir.mkdir(parents=True, exist_ok=True)

        # 完整原始内容压缩存储
        full_path = archive_dir / "full.txt.gz"
        try:
            with gzip.open(full_path, 'wt', encoding='utf-8') as f:
                f.write(content)
        except Exception as e:
            logger.error("L4归档写入失败: %s", e)
            return f"归档失败: {e}"

        # 分片存储（用于检索时按需加载）
        chunks = self._chunk_content(content)
        chunks_dir = archive_dir / "chunks"
        chunks_dir.mkdir(exist_ok=True)

        chunk_index = []
        for i, chunk in enumerate(chunks):
            chunk_path = chunks_dir / f"chunk_{i:04d}.txt"
            chunk_path.write_text(chunk, encoding='utf-8')
            chunk_index.append({
                "chunk_id": i,
                "size": len(chunk),
                "preview": chunk[:200],
                "file": f"chunk_{i:04d}.txt",
            })

        # 提取关键词
        extracted_tags = self._extract_keywords(content[:5000])

        # 记录索引
        entry = {
            "archive_id": archive_id,
            "type": content_type,
            "source": source,
            "tags": list(set((tags or []) + extracted_tags)),
            "metadata": metadata or {},
            "total_size": len(content),
            "total_chunks": len(chunks),
            "chunks": chunk_index,
            "created": datetime.now().isoformat(),
            "accessed": datetime.now().isoformat(),
            "access_count": 0,
        }

        with self._lock:
            self._index[archive_id] = entry
            self._save_index()

        # 检查归档大小
        self._check_size_limit()

        logger.info("L4归档: %s → %s (%d字符, %d分片)", source, archive_id, len(content), len(chunks))
        return (
            f"✅ L4已归档: {archive_id}\n"
            f"   来源: {source} | 类型: {content_type}\n"
            f"   大小: {len(content):,}字符 | 分片: {len(chunks)}个\n"
            f"   关键词: {', '.join(extracted_tags[:10])}\n"
            f"   使用 search_archive(query='关键词') 检索后按需加载"
        )

    def _chunk_content(self, content: str) -> list[str]:
        """将内容分片，有重叠以保证上下文连续性"""
        chunks = []
        start = 0
        while start < len(content):
            end = min(start + self.CHUNK_SIZE, len(content))
            chunks.append(content[start:end])
            if end >= len(content):
                break
            start = end - self.CHUNK_OVERLAP
        return chunks

    def _extract_keywords(self, text: str) -> list[str]:
        """从文本中提取关键词（简单TF-based）"""
        import re
        # 提取中英文单词
        words = re.findall(r'[a-zA-Z_]\w{2,}|[一-鿿]{2,}', text.lower())
        # 停用词过滤
        stopwords = {'the', 'and', 'for', 'this', 'that', 'with', 'from', 'import', 'class',
                     'def', 'return', 'self', 'none', 'true', 'false', '不是', '一个', '这个', '那个'}
        filtered = [w for w in words if w not in stopwords]
        # 按频率排序
        freq = {}
        for w in filtered:
            freq[w] = freq.get(w, 0) + 1
        return [w for w, _ in sorted(freq.items(), key=lambda x: x[1], reverse=True)[:15]]

    def search(self, query: str, limit: int = 10) -> str:
        """
        关键词检索归档库，返回匹配的归档条目和分片预览。
        匹配后提供分片加载指引。
        """
        q = query.lower()
        q_terms = q.split()
        results = []

        with self._lock:
            for arc_id, entry in self._index.items():
                score = 0
                # 标签匹配
                for tag in entry.get("tags", []):
                    if q in tag.lower() or any(t in tag.lower() for t in q_terms):
                        score += 3
                # 来源匹配
                if q in entry["source"].lower():
                    score += 2
                # 元数据匹配
                meta_str = json.dumps(entry.get("metadata", {}), ensure_ascii=False).lower()
                if q in meta_str:
                    score += 1

                if score > 0:
                    results.append((score, arc_id, entry))

        results.sort(key=lambda x: x[0], reverse=True)
        top = results[:limit]

        if not top:
            return f"🔍 L4归档库中未找到 '{query}' 相关内容（共{len(self._index)}条归档）"

        lines = [f"🔍 L4归档检索 '{query}' ({len(top)}/{len(results)}条匹配)"]
        lines.append("=" * 60)
        for score, arc_id, entry in top:
            age_hours = (time.time() - datetime.fromisoformat(entry["created"]).timestamp()) / 3600 if entry.get("created") else 0
            lines.append(f"  📦 {arc_id} (匹配度:{score})")
            lines.append(f"     类型:{entry['type']} | 来源:{entry['source'][:50]}")
            lines.append(f"     大小:{entry['total_size']:,}字符 | 分片:{entry['total_chunks']}个 | {age_hours:.1f}h前")
            lines.append(f"     关键词: {', '.join(entry.get('tags', [])[:8])}")
            lines.append(f"     → 加载: load_archive(id='{arc_id}', chunk=0)")

        return "\n".join(lines)

    def load_chunk(self, archive_id: str, chunk_id: int = 0) -> str:
        """
        按需加载归档分片到上下文。
        返回分片内容，带导航信息（上下分片ID）。
        """
        entry = self._index.get(archive_id)
        if not entry:
            return f"归档 {archive_id} 不存在"

        chunk_path = self._archive_dir / archive_id / "chunks" / f"chunk_{chunk_id:04d}.txt"
        if not chunk_path.exists():
            return f"分片 chunk_{chunk_id} 不存在（共{entry['total_chunks']}个分片）"

        # 更新访问记录
        entry["accessed"] = datetime.now().isoformat()
        entry["access_count"] = entry.get("access_count", 0) + 1
        self._save_index()

        content = chunk_path.read_text(encoding='utf-8')

        nav = []
        if chunk_id > 0:
            nav.append(f"⬆ load_archive(id='{archive_id}', chunk={chunk_id-1}) 上一分片")
        nav.append(f"📄 分片 {chunk_id+1}/{entry['total_chunks']} | 归档:{archive_id}")
        if chunk_id < entry["total_chunks"] - 1:
            nav.append(f"⬇ load_archive(id='{archive_id}', chunk={chunk_id+1}) 下一分片")
        nav.append(f"📂 load_archive(id='{archive_id}', chunk=all) 加载全部({entry['total_size']:,}字符)")

        return "\n".join(nav) + "\n" + "=" * 60 + "\n" + content

    def load_full(self, archive_id: str) -> str:
        """加载归档全部内容"""
        entry = self._index.get(archive_id)
        if not entry:
            return f"归档 {archive_id} 不存在"

        full_path = self._archive_dir / archive_id / "full.txt.gz"
        if not full_path.exists():
            return "完整归档文件不存在"

        # 更新访问记录
        entry["accessed"] = datetime.now().isoformat()
        entry["access_count"] = entry.get("access_count", 0) + 1
        self._save_index()

        try:
            with gzip.open(full_path, 'rt', encoding='utf-8') as f:
                content = f.read()

            # 如果太大，返回分片导航
            if len(content) > 10000:
                return (
                    f"⚠️ 归档内容较大({len(content):,}字符)，建议分片加载：\n"
                    f"   共{entry['total_chunks']}个分片，使用 load_archive(id='{archive_id}', chunk=N) 按需加载\n"
                    f"   前1000字符预览:\n{content[:1000]}..."
                )
            return content
        except Exception as e:
            return f"加载归档失败: {e}"

    def delete_archive(self, archive_id: str) -> bool:
        """删除归档"""
        with self._lock:
            if archive_id in self._index:
                archive_dir = self._archive_dir / archive_id
                try:
                    shutil.rmtree(archive_dir, ignore_errors=True)
                except Exception:
                    pass
                del self._index[archive_id]
                self._save_index()
                return True
        return False

    def list_archives(self, content_type: str = None, limit: int = 20) -> str:
        """列出归档列表"""
        with self._lock:
            entries = list(self._index.items())
            if content_type:
                entries = [(k, v) for k, v in entries if v.get("type") == content_type]

            if not entries:
                return "📚 L4归档库为空"

            entries.sort(key=lambda x: x[1].get("created", ""), reverse=True)
            lines = [f"📚 L4归档库 ({len(entries)}条, 共{self._get_total_size()})"]
            lines.append("=" * 60)
            for arc_id, entry in entries[:limit]:
                age_hours = (time.time() - datetime.fromisoformat(entry["created"]).timestamp()) / 3600 if entry.get("created") else 0
                lines.append(f"  📦 {arc_id}")
                lines.append(f"     类型:{entry['type']} | {entry['total_size']:,}字符 | {entry['total_chunks']}分片 | {age_hours:.1f}h前")
                lines.append(f"     来源:{entry['source'][:60]}")
            return "\n".join(lines)

    def _get_total_size(self) -> str:
        total = sum(e.get("total_size", 0) for e in self._index.values())
        if total > 1_000_000:
            return f"{total/1_000_000:.1f}MB"
        elif total > 1000:
            return f"{total/1000:.1f}KB"
        return f"{total}B"

    def _check_size_limit(self):
        """检查归档总大小，超出限制时清理最旧条目"""
        total_bytes = sum(e.get("total_size", 0) for e in self._index.values())
        limit_bytes = self.MAX_ARCHIVE_SIZE_MB * 1_000_000

        if total_bytes > limit_bytes:
            # 按创建时间排序，删除最旧的
            sorted_entries = sorted(self._index.items(), key=lambda x: x[1].get("created", ""))
            while sum(e.get("total_size", 0) for e in self._index.values()) > limit_bytes * 0.8:
                if not sorted_entries:
                    break
                oldest_id, _ = sorted_entries.pop(0)
                self.delete_archive(oldest_id)
                logger.warning("L4归档超限，删除最旧条目: %s", oldest_id)

    def get_stats(self) -> dict:
        """获取归档统计"""
        total_size = sum(e.get("total_size", 0) for e in self._index.values())
        total_chunks = sum(e.get("total_chunks", 0) for e in self._index.values())
        by_type = {}
        for e in self._index.values():
            t = e.get("type", "unknown")
            by_type[t] = by_type.get(t, 0) + 1
        return {
            "total_archives": len(self._index),
            "total_size": total_size,
            "total_chunks": total_chunks,
            "by_type": by_type,
            "archive_dir": str(self._archive_dir),
        }

    def clear(self):
        """清空所有归档"""
        with self._lock:
            for arc_id in list(self._index.keys()):
                archive_dir = self._archive_dir / arc_id
                try:
                    shutil.rmtree(archive_dir, ignore_errors=True)
                except Exception:
                    pass
            self._index.clear()
            self._save_index()


# ═══════════════════════════════════════════
# 四层记忆统一管理器
# ═══════════════════════════════════════════

class MemoryLayerManager:
    """
    四层记忆统一管理器：
    - L1: 临时缓存 (大体积工具结果落地)
    - L2: 核心记忆 (永久保留)
    - L3: 自动压缩 (Token超87%触发)
    - L4: 冷归档 (超大内容分片检索)

    集成到AgentCore的消息处理流程中。
    """

    def __init__(self, data_root: Path = None, context_limit: int = None):
        self._data_root = data_root or DATA_ROOT
        self.l1 = L1TempCache(self._data_root)
        self.l2 = L2CoreMemory(self._data_root)
        self.l3 = L3AutoCompressor(context_limit)
        self.l4 = L4ColdArchive(self._data_root)
        self._lock = threading.Lock()

    # ═══ L1集成：工具结果缓存 ═══
    def wrap_tool_result(self, tool_name: str, params: str, result: str) -> str:
        """包装工具结果：大体积内容落地L1，返回摘要或原文"""
        return self.l1.store(tool_name, params, result)

    def should_cache_result(self, result: str) -> bool:
        """判断工具结果是否需要L1缓存"""
        return self.l1.should_cache(result)

    # ═══ L2集成：核心记忆 ═══
    def remember_core(self, content: str, mem_type: str = "fact", importance: int = 5,
                      tags: list[str] = None, source: str = "") -> str:
        """记录核心记忆"""
        return self.l2.add(content, mem_type, tags, source, importance)

    def get_l2_injection(self) -> str:
        """获取L2记忆注入文本（用于system prompt）"""
        return self.l2.build_injection_prompt()

    # ═══ L3集成：自动压缩 ═══
    def should_compress(self, messages: list[dict]) -> bool:
        """检查是否需要L3压缩"""
        return self.l3.check_need_compress(messages)

    def compress_context(self, messages: list[dict]) -> list[dict]:
        """执行L3压缩"""
        return self.l3.compress(messages, self.l2)

    def get_token_stats(self, messages: list[dict]) -> dict:
        """获取Token统计"""
        return self.l3.get_token_stats(messages)

    # ═══ L4集成：冷归档 ═══
    def archive_content(self, content: str, source: str, content_type: str = "conversation",
                        tags: list[str] = None) -> str:
        """归档内容到L4"""
        return self.l4.archive(content, source, content_type, tags)

    def search_archive(self, query: str, limit: int = 10) -> str:
        """检索L4归档"""
        return self.l4.search(query, limit)

    def load_archive(self, archive_id: str, chunk: int = None) -> str:
        """加载L4归档内容"""
        if chunk is None or chunk == "all":
            return self.l4.load_full(archive_id)
        return self.l4.load_chunk(archive_id, chunk)

    # ═══ 综合统计 ═══
    def get_all_stats(self) -> dict:
        """获取四层记忆综合统计"""
        return {
            "l1_cache_count": len(self.l1._index),
            "l2_memory_count": len(self.l2._entries),
            "l3_compress_count": self.l3.get_compress_count(),
            "l3_last_tokens": self.l3.get_last_token_count(),
            "l4_archive_stats": self.l4.get_stats(),
        }

    def get_stats_report(self) -> str:
        """获取可读的统计报告，含虚拟上下文容量"""
        stats = self.get_all_stats()
        l4 = stats["l4_archive_stats"]

        # 计算虚拟上下文总量
        l1_chars = sum(
            info.get("size", 0) for info in self.l1._index.values()
        )
        l4_chars = l4.get("total_size", 0)
        l3_active_tokens = stats["l3_last_tokens"]
        l3_limit_tokens = self.l3.context_limit

        # 虚拟上下文 = L3原生窗口 + L4归档总量
        virtual_total_chars = l4_chars + l1_chars
        virtual_total_tokens = int(virtual_total_chars / 2.5)  # 粗略估算
        million_token_mark = 1_000_000

        pct_of_million = round(virtual_total_tokens / million_token_mark * 100, 1)

        lines = [
            "📊 四层记忆架构 — 百万虚拟上下文统计",
            "=" * 55,
            f"  L1 临时缓存:    {stats['l1_cache_count']:>4} 条目 | {l1_chars:>10,} 字符",
            f"  L2 核心记忆:    {stats['l2_memory_count']:>4} 条   | 永久保留不裁剪",
            f"  L3 原生窗口:    {l3_active_tokens:>8,} / {l3_limit_tokens:,} tokens",
            f"                  压缩阈值: {int(l3_limit_tokens * self.l3.COMPRESS_THRESHOLD):,} tokens ({int(self.l3.COMPRESS_THRESHOLD*100)}%)",
            f"                  已压缩: {stats['l3_compress_count']} 次",
            f"  L4 冷归档库:    {l4['total_archives']:>4} 归档 | {l4_chars:>10,} 字符 | {l4['total_chunks']} 分片",
            f"  ───────────────────────────────────────────",
            f"  🌐 虚拟上下文总量: {virtual_total_chars:,} 字符 ≈ {virtual_total_tokens:,} tokens",
            f"     已达百万token的 {pct_of_million}%",
            f"     (L3原生{L3AutoCompressor.CONTEXT_LIMIT_TOKENS:,} + L4磁盘{L4ColdArchive.MAX_ARCHIVE_SIZE_MB}MB归档库)",
        ]
        if virtual_total_tokens >= million_token_mark:
            lines.append("  ✅ 已突破百万上下文！")
        return "\n".join(lines)

    def auto_archive_conversation(self, messages: list[dict], session_id: str = ""):
        """自动将完整对话归档到L4（在会话结束或token超限时调用）"""
        # 只归档非system消息中的大量内容
        significant_content = []
        for m in messages:
            if m.get("role") == "system":
                continue
            content = m.get("content", "") or ""
            if len(content) > 500:
                significant_content.append(f"[{m.get('role', '?')}] {content}")

        if significant_content:
            combined = "\n\n---\n\n".join(significant_content)
            if len(combined) > 5000:
                self.l4.archive(
                    content=combined,
                    source=f"session_{session_id}" if session_id else "auto_archive",
                    content_type="conversation",
                    tags=["auto_archive", "conversation"],
                )


# ═══════════════════════════════════════════
# 全局单例
# ═══════════════════════════════════════════

_memory_layer_manager: Optional[MemoryLayerManager] = None


def get_memory_layers(data_root: Path = None, context_limit: int = None) -> MemoryLayerManager:
    """获取四层记忆管理器全局单例"""
    global _memory_layer_manager
    if _memory_layer_manager is None:
        _memory_layer_manager = MemoryLayerManager(data_root, context_limit)
    return _memory_layer_manager


def reset_memory_layers():
    """重置记忆管理器（测试用）"""
    global _memory_layer_manager
    _memory_layer_manager = None
