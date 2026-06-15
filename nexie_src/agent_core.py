# Nexie — AI Coding Agent
# Copyright (c) 2026. All rights reserved.
# 本软件仅供个人学习与研究使用，禁止未经授权的商业用途、分发或修改后闭源。
# Licensed for personal, educational, and non-commercial use only.
"""
Nexie — AI 智能体核心调度引擎
插件式工具系统、稳定性增强、四层记忆架构、防400/429韧性体系
支持 DeepSeek / MIMO 多模型切换
"""
import json
import os
import sys
import platform
import time
import threading
import logging
from pathlib import Path
from datetime import datetime

from client import DeepSeekClient, MiMoClient

# 旧版工具兼容（保留原有桌面操控/截图工具）
from tools import (
    TOOL_MAP as LEGACY_TOOL_MAP,
    TOOL_DESCRIPTIONS as LEGACY_TOOL_DESCRIPTIONS,
    parse_tool_command,
    set_working_dir,
    get_last_screenshot, clear_last_screenshot,
)

# Nexie 新架构
from nexie.stability import get_heartbeat, get_executor
from nexie.memory_layers import get_memory_layers, estimate_tokens, count_message_tokens
from nexie.permission_system import get_permission
from nexie.workspace import get_workspace

logger = logging.getLogger("Nexie.AgentCore")

# ═══════════════════════════════════════════
# Effort 控制
# ═══════════════════════════════════════════
# DeepSeek没有原生effort参数，通过动态prompt模拟
_EFFORT_LEVEL = "low"  # low | medium | high
_EFFORT_MODIFIERS = {
    "low":    "【Effort: LOW·最高优先级】极简模式：不描述、不解释、不总结。工具调用不出文字。错误时才开口。",
    "medium": "【Effort: MEDIUM】适度简洁：必要时可简短说明，但避免啰嗦。",
    "high":   "",
}

# ═══════════════════════════════════════════
# 文本工具分离 — 废话模式检测
# ═══════════════════════════════════════════
_TOOL_FILLER_PATTERNS = [
    "我来", "让我", "好的", "收到", "首先", "接下来", "现在",
    "我会", "帮你", "需要先", "先让我", "让Nexie", "正在",
    "准备", "开始", "尝试", "马上", "这就", "立刻",
    "let me", "I will", "I'll", "first", "now",
]

def _is_tool_filler(text: str) -> bool:
    """检测文本是否为工具调用前的废话描述"""
    t = text.strip()
    if len(t) <= 3:
        return True
    for pat in _TOOL_FILLER_PATTERNS:
        if t.startswith(pat):
            return True
    return False

def _trim_tool_content(content: str) -> str:
    """精简工具调用时的文字内容：废话→占位符，否则截断到30字"""
    if not content or not content.strip():
        return "."
    if _is_tool_filler(content):
        return "."   # API要求content非空，用句点占位
    return content.strip()[:30]


# ==================== 系统提示词 ====================

_SYS = platform.system()
_SYS_VER = platform.release()
if _SYS == "Windows":
    try:
        build = sys.getwindowsversion().build
        _SYS_NAME = "Windows 11" if build >= 22000 else "Windows 10"
    except Exception:
        _SYS_NAME = f"Windows {_SYS_VER}"
else:
    _SYS_NAME = f"{_SYS} {_SYS_VER}"
_SYS_INFO = f"当前运行环境：{_SYS_NAME}，用户目录：{os.path.expanduser('~')}，桌面路径：{os.path.join(os.path.expanduser('~'), 'Desktop')}"

_BASE_PROMPT = f"""你是Nexie AI编程智能体。始终用中文。{_SYS_INFO}。

【行为准则·最高优先级】
① 行动优先：直接调用工具执行，禁止在工具调用前写"我来帮你..."、"首先需要..."等计划描述
② 沉默默认：工具调用前后不写文字，让工具执行结果说话。只在报错或需用户决策时开口
③ 完成即停：任务完成后只出结果（文件路径/命令输出/状态），不追加"已完成！"等总结
④ 极简格式：禁止Markdown、列表、客套话、追问句（如"需要我继续吗？"）
⑤ 截止时间：用户指定了截止时间必须严格遵守，到点立即停，不调试不修补

【工具选择·铁律】
⑥ 监测/轮询类任务直接用list_dir+read_file+write_file循环，禁止写Python脚本替代
⑦ 只有复杂计算/数据处理/API调用才用run_command写脚本，简单文件操作一律用内置工具
⑧ 工具优先：list_dir > run_command("dir")，write_file > run_command("echo...>")

规则：①可并发的工具调用一次全部发出 ②只做要求的事，完成即停 ③失败换方案，不无限重试 ④禁危险命令(rm -rf /等) ⑤完成后清理临时文件"""

_MIMO_EXTRA = "\n具备视觉能力，可看截图。"


def _build_system_prompt(model_type: str, memory_context: str = "", effort: str = None) -> str:
    """构建系统提示词，effort=None使用全局_EFFORT_LEVEL"""
    lvl = effort or _EFFORT_LEVEL
    modifier = _EFFORT_MODIFIERS.get(lvl, "")
    prompt = _BASE_PROMPT
    if modifier:
        prompt = modifier + "\n\n" + prompt
    if model_type == "mimo":
        prompt += _MIMO_EXTRA
    return prompt


# ==================== 工具定义构建 ====================

def _build_tool_definitions(registry, model_type: str = "deepseek", include_desktop: bool = False) -> list[dict]:
    """构建工具定义列表。include_desktop 控制桌面操控/手机推送工具是否暴露。"""
    tools = []

    # 1. 始终可用的核心工具
    always_tools = [
        "list_dir", "read_file", "write_file", "edit_file", "run_command",
        "search_files", "remember", "forget",
    ]
    # 桌面操控 + 手机推送 — 仅在用户明确需要时暴露
    desktop_tools = [
        "capture_screen", "get_screen_info", "click_at", "type_text",
        "press_key", "scroll_screen",
        "send_screenshot", "send_file", "send_text",
    ]
    legacy_tool_names = always_tools + (desktop_tools if include_desktop else [])

    for name in legacy_tool_names:
        func = LEGACY_TOOL_MAP.get(name)
        if not func:
            continue
        desc = LEGACY_TOOL_DESCRIPTIONS.get(name, "")
        tools.append(_make_legacy_tool_def(name, desc, func))

    # 2. Nexie 新工具 (从注册中心获取, 去重)
    legacy_names = set(legacy_tool_names)
    for tool_def in registry.list_enabled():
        if tool_def.name not in legacy_names:
            tools.append(tool_def.to_openai_tool())

    # 3. 任务追踪
    tools.append({
        "type": "function",
        "function": {
            "name": "task_create",
            "description": "创建任务到追踪列表",
            "parameters": {
                "type": "object",
                "properties": {
                    "subject": {"type": "string", "description": "任务标题"},
                    "description": {"type": "string", "description": "任务详情"},
                },
                "required": ["subject"],
            },
        },
    })
    tools.append({
        "type": "function",
        "function": {
            "name": "task_update",
            "description": "更新任务状态: pending/in_progress/completed/deleted",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "description": "任务ID"},
                    "status": {"type": "string", "description": "新状态"},
                },
                "required": ["task_id"],
            },
        },
    })
    tools.append({
        "type": "function",
        "function": {
            "name": "task_list",
            "description": "查看当前所有任务",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    })

    return tools


def _make_legacy_tool_def(name: str, description: str, func: callable) -> dict:
    """为旧版工具构建 OpenAI Function Calling 格式定义"""
    import inspect
    params = {"type": "object", "properties": {}, "required": []}
    try:
        sig = inspect.signature(func)
        for pname, param in sig.parameters.items():
            if pname in ('self', 'cls'):
                continue
            ptype = "string"
            if param.annotation != inspect.Parameter.empty:
                anno = param.annotation
                if anno == int: ptype = "integer"
                elif anno == float: ptype = "number"
                elif anno == bool: ptype = "boolean"
            params["properties"][pname] = {"type": ptype, "description": f"参数: {pname}"}
    except Exception:
        pass

    return {
        "type": "function",
        "function": {"name": name, "description": description, "parameters": params}
    }


# ==================== AgentCore (Nexie增强版) ====================

class AgentCore:
    """Nexie AI 智能体 — 核心调度引擎，插件式工具+自我进化"""

    # 不设轮次上限 —— 只有模型判断任务完成或用户取消才会停止
    MAX_TOOL_OUTPUT = 8000    # 工具输出上限
    HEAD_KEEP = 4000          # 截断时保留前N字符
    TAIL_KEEP = 2000          # 截断时保留后N字符
    CONTEXT_SUMMARIZE_ROUNDS = 4  # 每4轮语义压缩一次

    def __init__(self, api_key: str, working_dir: str = None, model_type: str = "deepseek"):
        self.model_type = model_type
        if model_type == "mimo":
            self.client = MiMoClient(api_key)
        else:
            self.client = DeepSeekClient(api_key)
        # 默认工作目录：桌面（exe模式）或当前目录（开发模式）
        if working_dir:
            self.working_dir = working_dir
        elif getattr(sys, 'frozen', False):
            self.working_dir = str(Path.home() / "Desktop")
        else:
            self.working_dir = os.getcwd()
        set_working_dir(self.working_dir)

        # ── Nexie 初始化 ──
        from nexie import init_nexie
        self.registry = init_nexie()
        self._tool_definitions = _build_tool_definitions(self.registry, model_type)

        # ── 稳定性模块(初始化心跳/超时执行器) ──
        get_heartbeat()
        get_executor()
        # ── 四层记忆架构 ──
        from memory_manager import get_memory
        self.memory = get_memory()
        self.memory.start_session()
        self.memory_layers = get_memory_layers()  # L1+L2+L3+L4统一管理

        # ── 会话记忆引擎（跨会话长期记忆）──
        from nexie.session_memory import get_session_memory
        self.session_memory = get_session_memory()
        self.session_memory.start_session()

        # ── 权限控制 ──
        self.permission = get_permission()

        # ═══ 记忆不再注入 system prompt（省token）──
        # 构建记忆上下文，作为独立消息插入（仅会话开始时一次）
        memory_context = self.memory.build_memory_context()
        l2_context = self.memory_layers.get_l2_injection()
        if l2_context:
            memory_context = (memory_context + "\n" + l2_context) if memory_context else l2_context
        session_context = self.session_memory.get_startup_context()
        if session_context:
            memory_context = (memory_context + "\n" + session_context) if memory_context else session_context
        learned = self.session_memory.get_learned_context()
        if learned:
            memory_context = (memory_context + "\n" + learned) if memory_context else learned

        system_prompt = _build_system_prompt(model_type)
        self.messages = [{"role": "system", "content": system_prompt}]
        # 记忆作为一次性"context"消息插入（不在system prompt里，压缩时会自然淘汰）
        if memory_context:
            self.messages.append({"role": "user", "content": f"[会话上下文] {memory_context[:2000]}", "name": "memory"})

        # ── 状态 ──
        self._state = "idle"  # idle → processing → (cancelling) → idle
        self._state_lock = threading.Condition()
        self.is_processing = False
        self._cancel_flag = False
        self._round_count = 0
        self._msg_lock = threading.Lock()
        self._pending_screenshot = False
        self._processing_lock = threading.Lock()
        self._permission_hook: callable = None
        self._skip_permission = False  # 手机端请求跳过PC弹窗
        self._global_skip = False      # 全局权限开关(持久,不会被重置)
        self._on_task_update: callable = None

        # ── 任务追踪 ──
        self._task_list: list[dict] = []  # [{id, subject, status: pending|in_progress|completed}]

        tool_count = len(self._tool_definitions)
        logger.info(f"Nexie 初始化 | 模型:{model_type} | {tool_count}工具 | 四层记忆+防400/429就绪")

        # ── 预算追踪（能力26）──
        try:
            from nexie.budget import get_budget_tracker
            self._budget = get_budget_tracker()
        except Exception:
            self._budget = None

    # ═══ 会话管理 ═══

    def reset_conversation(self):
        memory_context = self.memory.build_memory_context()
        l2_context = self.memory_layers.get_l2_injection()
        if l2_context:
            memory_context = (memory_context + "\n" + l2_context) if memory_context else l2_context
        session_context = self.session_memory.get_startup_context()
        if session_context:
            memory_context = (memory_context + "\n" + session_context) if memory_context else session_context

        system_prompt = _build_system_prompt(self.model_type)
        with self._msg_lock:
            self.messages = [{"role": "system", "content": system_prompt}]
            if memory_context:
                self.messages.append({"role": "user", "content": f"[会话上下文] {memory_context[:2000]}", "name": "memory"})
        self._round_count = 0
        self.session_memory.start_session()

    def set_messages(self, messages: list[dict]):
        with self._msg_lock:
            self.messages = list(messages)

    # ═══ 省流模式 ═══

    # ═══ Effort 切换 ═══

    def set_effort(self, level: str):
        """动态切换effort级别"""
        global _EFFORT_LEVEL
        if level in _EFFORT_MODIFIERS:
            _EFFORT_LEVEL = level
            if self.messages and self.messages[0].get("role") == "system":
                self.messages[0]["content"] = _build_system_prompt(self.model_type)
            logger.info("Effort切换: %s", level)

    def _safe_append(self, msg: dict):
        with self._msg_lock:
            self.messages.append(msg)

    def _compress_context(self):
        """语义压缩：中间轮次用摘要替代原文，首尾保留原文。"""
        with self._msg_lock:
            msgs = self.messages
            if len(msgs) <= 10:
                return

            system_msgs = [m for m in msgs if m.get("role") == "system"]
            non_system = [m for m in msgs if m.get("role") != "system"]

            # 按user消息分轮次
            rounds = []
            current = []
            for m in non_system:
                if m.get("role") == "user" and m.get("name") != "memory" and current:
                    rounds.append(current)
                    current = []
                current.append(m)
            if current:
                rounds.append(current)

            if len(rounds) <= 4:
                return

            # 保留：第一条user + 最后2轮原文
            kept_rounds = [rounds[0]] + rounds[-2:]
            middle_rounds = rounds[1:-2]

            if not middle_rounds:
                return

            # 生成语义摘要：提取关键决策+产物
            summary_lines = ["[上文摘要]"]
            for i, rnd in enumerate(middle_rounds):
                # 提取本轮的关键信息
                user_msgs = [m for m in rnd if m.get("role") == "user" and m.get("name") != "memory"]
                assistant_msgs = [m for m in rnd if m.get("role") == "assistant" and not m.get("tool_calls")]
                tool_msgs = [m for m in rnd if m.get("role") == "tool"]
                write_ops = [
                    m for m in rnd
                    if m.get("role") == "assistant" and m.get("tool_calls")
                    for tc in (m.get("tool_calls") or [])
                    if tc.get("function", {}).get("name") in ("write_file", "edit_file")
                ]

                # 用户说了什么
                if user_msgs:
                    u = user_msgs[0].get("content", "")[:80]
                    summary_lines.append(f"用户: {u}")

                # 写了什么文件
                if write_ops:
                    files = [tc["function"]["name"] for tc in
                             [tc for m in rnd if m.get("role") == "assistant" and m.get("tool_calls")
                              for tc in (m.get("tool_calls") or [])]]
                    summary_lines.append(f"  操作: {', '.join(files[:3])}")

                # AI回复了啥关键结论
                if assistant_msgs:
                    a = assistant_msgs[0].get("content", "")[:100]
                    summary_lines.append(f"  结论: {a}")

            summary = "\n".join(summary_lines[:20])  # 最多20行
            kept = []
            for r in kept_rounds:
                kept.extend(r)

            self.messages = system_msgs + [
                {"role": "user", "content": summary, "name": "compressed"}
            ] + kept

            logger.info(
                "语义压缩 | %d条(%d轮)→%d条 | 中间%d轮→摘要",
                len(msgs), len(rounds), len(self.messages), len(middle_rounds)
            )

    def cancel(self):
        """设置取消标志+终止当前命令"""
        self._cancel_flag = True
        from tools import cancel_current_command
        cancel_current_command()

    def cancel_and_wait(self):
        """取消并等待旧请求结束(最多2秒)"""
        if self._state == "idle":
            return
        self.cancel()
        with self._state_lock:
            deadline = time.time() + 2.0
            while self._state != "idle" and time.time() < deadline:
                self._state_lock.wait(timeout=0.2)

    def _safe_rollback(self):
        """强制清除所有不完整的assistant(tool_calls),防止400"""
        with self._msg_lock:
            self.messages = self._build_clean_messages(self.messages)

    @staticmethod
    def _build_clean_messages(msgs: list[dict]) -> list[dict]:
        """构建干净消息列表：tool_calls与tool_result严格配对，不完整的整轮剔除"""
        clean = []
        pending_ids = set()
        for m in msgs:
            role = m.get("role", "")
            if role == "assistant" and m.get("tool_calls"):
                if pending_ids:
                    # 上一轮有孤儿tool_call，跳过整轮（对应的tool结果不完整）
                    pending_ids.clear()
                    # 移除clean末尾的assistant(tool_calls)
                    for j in range(len(clean) - 1, -1, -1):
                        if clean[j].get("role") == "assistant" and clean[j].get("tool_calls"):
                            clean = clean[:j]
                            break
                pending_ids = {tc.get("id", "") for tc in m["tool_calls"]}
                clean.append(m)
            elif role == "tool":
                tid = m.get("tool_call_id", "")
                if tid in pending_ids:
                    pending_ids.discard(tid)
                    clean.append(m)
                # else: 孤儿tool，丢弃
            else:
                if pending_ids and role == "user":
                    # user前有未完成的tool_calls → 移除该assistant
                    for j in range(len(clean) - 1, -1, -1):
                        if clean[j].get("role") == "assistant" and clean[j].get("tool_calls"):
                            clean = clean[:j]
                            break
                    pending_ids.clear()
                clean.append(m)

        # 末尾不完整的assistant(tool_calls) → 移除
        for i in range(len(clean) - 1, -1, -1):
            if clean[i].get("role") == "assistant" and clean[i].get("tool_calls"):
                tc_ids = {tc.get("id") for tc in clean[i]["tool_calls"]}
                matched = all(
                    any(t.get("role") == "tool" and t.get("tool_call_id") == tid
                        for t in clean[i + 1:])
                    for tid in tc_ids
                )
                if not matched:
                    clean = clean[:i]
                break

        if len(clean) != len(msgs):
            logger.info("消息清理: %d条→%d条", len(msgs), len(clean))
        return clean

    def wait_processing_done(self, timeout: float = 5.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if not self.is_processing:
                return True
            time.sleep(0.1)
        return not self.is_processing

    def set_working_dir(self, path: str):
        self.working_dir = path
        set_working_dir(path)

    def end_session(self):
        self.memory.end_session()
        # 会话记忆自动提炼（规则驱动，零API成本）
        try:
            self.session_memory.end_session(self.messages)
        except Exception as e:
            logger.debug("会话记忆提炼跳过: %s", e)
        # L4冷归档: 自动归档超量对话到本地
        try:
            if len(self.messages) > 20:
                session_id = datetime.now().strftime("%Y%m%d_%H%M%S")
                self.memory_layers.auto_archive_conversation(self.messages, session_id)
                # 完整会话保存到工作空间（含思考/工具/回复，不丢任何内容）
                workspace = get_workspace()
                workspace.save_conversation(session_id, self.messages)
        except Exception:
            pass

    # ═══ 工具执行 ═══

    def _truncate_output(self, result: str) -> str:
        """头尾保留截断：前HEAD_KEEP + 后TAIL_KEEP，中间省略"""
        skipped = len(result) - self.HEAD_KEEP - self.TAIL_KEEP
        if skipped <= 0:
            return result
        return (
            result[:self.HEAD_KEEP]
            + f"\n\n... [{skipped} 字符已省略] ...\n\n"
            + result[-self.TAIL_KEEP:]
        )

    # 三级权限: (工具名 → 风险等级)
    #   "safe"     — 不需要确认
    #   "moderate" — 需要确认，可勾选"始终允许"
    #   "high"     — 每次都必须确认，不可跳过
    _TOOL_RISK = {
        "run_command": "high",   # 仅高危命令弹窗，白名单命令自动跳过
        "write_file": "safe",
        "edit_file": "safe",
        "click_at": "safe",
        "type_text": "safe",
        "press_key": "safe",
        "scroll_screen": "safe",
        "send_screenshot": "safe",
        "send_file": "safe",
        "send_text": "safe",
    }

    def _execute_tool(self, func_name: str, func_args: dict) -> str:
        """统一工具执行 — 权限检查 + 优先注册中心，fallback旧版"""
        # ═══ 黑名单拦截(并发路径也生效) ═══
        if func_name == "write_file" or func_name == "edit_file":
            file_path = func_args.get("path", "") or func_args.get("file_path", "")
            if file_path:
                allowed, reason = self.permission.check_file_operation(func_name, file_path)
                if not allowed:
                    return reason
        elif func_name == "run_command":
            cmd = func_args.get("command", "")
            if cmd:
                allowed, reason = self.permission.check_command(cmd)
                if not allowed:
                    return reason

        # ═══ 权限确认 (三级风险) ═══
        risk = self._TOOL_RISK.get(func_name, "safe")
        if self._skip_permission or self._global_skip:
            risk = "safe"  # 手机端请求/全局权限: 不弹PC窗口
        if risk == "high" and self._permission_hook:  # 仅真正高危弹窗
            # run_command: 白名单命令免确认(dir/ls/cat/type/grep/find/powershell读文件等)
            if func_name == "run_command":
                from tools import is_command_safe
                cmd = func_args.get("command", "")
                safe, _ = is_command_safe(cmd)
                if safe:
                    risk = "safe"  # 白名单命令直接放行
            if risk != "safe":
                desc = self._describe_action(func_name, func_args)
                allow_always_visible = (risk == "moderate")
                result = self._permission_hook(func_name, desc, risk, allow_always_visible)
                if not result:
                    return f"⏭️ [已跳过] 用户未批准: {func_name} — {desc}"
                if result == "always" and risk == "moderate":
                    self._TOOL_RISK[func_name] = "safe"

        # ═══ 管理员权限检测 ═══
        from nexie.permission_system import is_admin, needs_admin_for_path, needs_admin_for_command
        needs_elevation = False
        if func_name == "run_command":
            cmd = func_args.get("command", "")
            needs_elevation = needs_admin_for_command(cmd)
        elif func_name in ("write_file", "edit_file", "create_directory",
                           "move_file", "delete_file", "delete_directory"):
            path = func_args.get("path", "") or func_args.get("file_path", "")
            needs_elevation = needs_admin_for_path(path)

        if needs_elevation and not is_admin():
            # ── 能力28: 先尝试权限降级 ──
            if func_name == "run_command":
                from nexie.permission_system import downgrade_command
                downgraded, warning = downgrade_command(func_args.get("command", ""))
                if downgraded != func_args.get("command", ""):
                    func_args["command"] = downgraded
                    logger.info("权限降级: %s", warning)
                    # 降级后直接执行，不需要提权
                    needs_elevation = False

        if needs_elevation and not is_admin():
            # 全局权限/手机端: 跳过弹窗直接提权执行
            if self._skip_permission or self._global_skip:
                from nexie.permission_system import run_cmd_as_admin
                if func_name == "run_command":
                    cmd = func_args.get("command", "")
                    cwd = func_args.get("working_dir", None)
                    success, msg = run_cmd_as_admin(cmd, cwd)
                    return msg
                elif func_name in ("write_file", "edit_file", "create_directory",
                                   "move_file", "delete_file", "delete_directory"):
                    path = func_args.get("path", "") or func_args.get("file_path", "")
                    if func_name == "write_file":
                        content = func_args.get("content", "")
                        import base64
                        ps_cmd = (
                            f'powershell -Command "'
                            f'$c = [System.Text.Encoding]::UTF8.GetString([System.Convert]::FromBase64String(\\"'
                            f'{base64.b64encode(content.encode("utf-8")).decode()}\\"")); '
                            f'[System.IO.File]::WriteAllText(\\"{path}\\", $c)"'
                        )
                        success, msg = run_cmd_as_admin(ps_cmd)
                    else:
                        op_map = {
                            "edit_file": f'powershell -Command "echo 请手动编辑: {path}"',
                            "create_directory": f'mkdir "{path}"',
                            "move_file": f'move "{func_args.get("source", "")}" "{func_args.get("destination", path)}"',
                            "delete_file": f'del /f "{path}"',
                            "delete_directory": f'rmdir /s /q "{path}"',
                        }
                        cmd = op_map.get(func_name, f'echo "管理员操作: {func_name} {path}"')
                        success, msg = run_cmd_as_admin(cmd)
                    return msg
            # 弹窗确认后，以管理员身份运行目标命令（不重启 Nexie）
            if self._permission_hook:
                desc = f"此操作需要管理员权限：\n{func_name}: {func_args.get('path', func_args.get('command', ''))}"
                confirmed = self._permission_hook(
                    func_name,
                    f"{desc}\n\n⚠ 需要提升为管理员权限才能执行",
                    "high",
                    False,
                )
                if confirmed:
                    from nexie.permission_system import run_cmd_as_admin
                    if func_name == "run_command":
                        cmd = func_args.get("command", "")
                        cwd = func_args.get("working_dir", None)
                        success, msg = run_cmd_as_admin(cmd, cwd)
                        return msg
                    elif func_name in ("write_file", "edit_file", "create_directory",
                                       "move_file", "delete_file", "delete_directory"):
                        # 文件操作需要管理员权限的场景较少见
                        # 对于受保护路径的写操作，建议用 PowerShell 管理员执行
                        path = func_args.get("path", "") or func_args.get("file_path", "")
                        # 构造管理员复制/移动命令
                        if func_name == "write_file":
                            content = func_args.get("content", "")
                            import tempfile, base64
                            # 通过 PowerShell 以管理员写入
                            ps_cmd = (
                                f'powershell -Command "'
                                f'$c = [System.Text.Encoding]::UTF8.GetString([System.Convert]::FromBase64String(\\"'
                                f'{base64.b64encode(content.encode("utf-8")).decode()}\\"")); '
                                f'[System.IO.File]::WriteAllText(\\"{path}\\", $c)"'
                            )
                            success, msg = run_cmd_as_admin(ps_cmd)
                        else:
                            # 其他文件操作：通过 runas cmd 执行
                            op_map = {
                                "edit_file": f'powershell -Command "echo 请手动编辑: {path}"',
                                "create_directory": f'mkdir "{path}"',
                                "move_file": f'move "{func_args.get("source", "")}" "{func_args.get("destination", path)}"',
                                "delete_file": f'del /f "{path}"',
                                "delete_directory": f'rmdir /s /q "{path}"',
                            }
                            cmd = op_map.get(func_name, f'echo "管理员操作: {func_name} {path}"')
                            success, msg = run_cmd_as_admin(cmd)
                        return msg
                else:
                    return (
                        "🔐 [已取消管理员提升]\n"
                        "用户拒绝了管理员权限请求。\n"
                        "请尝试以下替代方案：\n"
                        "1. 使用用户目录下的路径代替系统路径\n"
                        "2. 使用不需要管理员权限的替代命令\n"
                        "3. 手动以管理员身份运行 Nexie"
                    )

        # 任务追踪工具
        if func_name == "task_create":
            return self._task_create(**func_args)
        if func_name == "task_update":
            return self._task_update(**func_args)
        if func_name == "task_list":
            return self._task_list()

        # 知识图谱工具
        if func_name == "create_entity":
            return self.memory.create_entity(**func_args)
        if func_name == "search_memory":
            return self.memory.search_entities(func_args.get("query", ""))

        # Grep 内容搜索
        if func_name == "search_content":
            return self._tool_search_content(**func_args)

        # WebSearch + WebFetch
        if func_name == "web_search":
            return self._tool_web_search(**func_args)
        if func_name == "web_fetch":
            return self._tool_web_fetch(**func_args)

        # Agent 子代理
        if func_name == "agent":
            return self._tool_agent(**func_args)

        # 注册中心工具(含重试/超时)
        if self.registry.get(func_name):
            return self.registry.execute(func_name, func_args)

        # Fallback: 旧版工具
        tool_func = LEGACY_TOOL_MAP.get(func_name)
        if tool_func:
            try:
                return tool_func(**func_args)
            except TypeError as e:
                logger.warning("Tool参数错误 | tool=%s | error=%s", func_name, str(e))
                return f"[参数错误] {str(e)}"
            except Exception as e:
                logger.error("Tool执行异常 | tool=%s | error=%s", func_name, str(e))
                return f"[异常] {str(e)}"

        logger.warning("未识别工具调用 | tool=%s", func_name)
        return f"[未识别工具: {func_name}]"

    def _describe_action(self, func_name: str, args: dict) -> str:
        """生成人类可读的操作描述，用于权限确认"""
        if func_name == "run_command":
            cmd = args.get("command", "?")[:100]
            return f"执行命令: {cmd}"
        elif func_name == "write_file":
            p = args.get("path", "?")
            size = len(args.get("content", ""))
            return f"写入文件: {Path(p).name} ({size} 字符)"
        elif func_name == "edit_file":
            p = args.get("path", "?")
            return f"编辑文件: {Path(p).name}"
        elif func_name in ("click_at", "type_text", "press_key", "scroll_screen"):
            return f"桌面操控: {func_name}"
        elif func_name == "send_screenshot":
            return "发送截图到手机"
        elif func_name == "send_file":
            return f"发送文件到手机: {args.get('filepath', '?')}"
        elif func_name == "send_text":
            text = str(args.get("text", ""))[:50]
            return f"发送文本到手机: {text}"
        return f"{func_name}"

    # ═══ 主处理循环 ═══

    def process_message(
        self,
        user_message: str,
        on_text: callable = None,
        on_tool_start: callable = None,
        on_tool_result: callable = None,
        on_done: callable = None,
        on_thinking: callable = None,
    ) -> str:
        """完整 Agent 循环: 用户指令 → 模型推理 → 工具执行 → 结果回传 → 循环"""
        # 防重入：取消旧请求 + 等待完全退出
        self.cancel_and_wait()
        self._processing_lock.acquire(timeout=10.0)

        try:
            # ═══ 状态机: idle → processing ═══
            with self._state_lock:
                self._state = "processing"
                self._state_lock.notify_all()

            self._cancel_flag = False
            self._error_streak = 0
            self._total_errors = 0
            self.is_processing = True
            self._round_count = 0

            _msg = user_message
            # ═══ Effort 指令解析 ═══
            if "#effort:" in _msg:
                import re
                m = re.search(r'#effort:(\w+)', _msg)
                if m and m.group(1) in _EFFORT_MODIFIERS:
                    self.set_effort(m.group(1))
                    _msg = re.sub(r'#effort:\w+', '', _msg).strip()
                    if not _msg:
                        resp = f"Effort → {m.group(1)}"
                        if on_text: on_text(resp)
                        self._processing_lock.release()
                        return resp

            # ═══ 桌面/手机工具按需启用 — 仅在用户明确要求时暴露 ═══
            _dt_keywords = ["点击","输入","按键","滚动","截屏","截图","鼠标","键盘",
                           "桌面","屏幕","窗口","操控","操作","坐标","发送到手机",
                           "推送到手机","手机","click","type","press","scroll","screenshot"]
            _need_desktop = any(kw in _msg for kw in _dt_keywords)
            if _need_desktop:
                self._tool_definitions = _build_tool_definitions(self.registry, self.model_type, include_desktop=True)
            else:
                self._tool_definitions = _build_tool_definitions(self.registry, self.model_type, include_desktop=False)

            # ═══ 每次请求前: 先清理孤儿消息 ═══
            self._safe_rollback()

            self._safe_append({"role": "user", "content": _msg})

            # 会话记忆：记录用户消息
            self.session_memory.record_user_message(_msg)

            full_response = ""
            task_complete = False
            start_time = time.time()

            def stream_callback(delta_content, tool_calls_acc, reasoning_delta):
                if reasoning_delta and on_thinking:
                    on_thinking(reasoning_delta)
                if delta_content and on_text:
                    on_text(delta_content)

            # ── 主循环(无上限，只有任务完成或用户取消才停) ──
            while not task_complete:
                if self._cancel_flag:
                    full_response += "\n\n⏹ 用户停止了操作。"
                    self._safe_rollback()
                    task_complete = True
                    break

                self._round_count += 1
                self._error_streak = 0

                # 视觉注入 (MiMo)
                if self._pending_screenshot:
                    self._pending_screenshot = False
                    b64, w, h = get_last_screenshot()
                    if b64:
                        self._safe_append({
                            "role": "user",
                            "content": [
                                {"type": "text", "text": "这是截图，请观察屏幕后继续。"},
                                {"type": "image_url", "image_url": {
                                    "url": f"data:image/png;base64,{b64}", "detail": "high"
                                }},
                            ],
                        })

                # ═══ L3自动压缩 ═══
                if self.memory_layers.should_compress(self.messages):
                    token_stats = self.memory_layers.get_token_stats(self.messages)
                    logger.warning(
                        "L3压缩触发 | tokens=%d/%d (%.1f%%) | 轮次=%d",
                        token_stats["total"], token_stats["limit"],
                        token_stats["usage_pct"], self._round_count
                    )
                    self.messages = self.memory_layers.compress_context(self.messages)

                # 调用模型 — 用干净消息副本防止400
                clean_msgs = self._build_clean_messages(self.messages)
                result = self.client.chat(
                    messages=clean_msgs,
                    tools=self._tool_definitions,
                    stream=True,
                    on_chunk=stream_callback,
                )

                # ── 预算追踪 ──
                if self._budget and not result.get("error"):
                    in_tok = count_message_tokens(self.messages)
                    out_tok = estimate_tokens(result.get("content", ""))
                    self._budget.record(self.client.MODEL, in_tok, out_tok)

                if result.get("error"):
                    self._error_streak += 1
                    self._total_errors += 1
                    err_text = result.get("content", "")
                    err_full = (err_text or "") + (result.get("error_msg", "") or "")
                    # 仅第一次错误显示给用户，重试不重复推送
                    if self._total_errors == 1:
                        full_response += err_full or err_text
                    logger.warning(
                        "API错误 (第%d次, 累计%d次) | 轮次=%d | %s",
                        self._error_streak, self._total_errors, self._round_count,
                        err_full[:300] or err_text[:300]
                    )

                    # ═══ 死机防护①：客户端已耗尽重试 → 永久错误，直接终止 ═══
                    FATAL_MARKER = "接口连接失败，本次生成终止"
                    if FATAL_MARKER in str(err_full) or FATAL_MARKER in str(err_text):
                        logger.error("客户端重试已耗尽 → 终止")
                        task_complete = True
                        break

                    # ═══ 死机防护②：智能体级别硬限制 → 防止无限循环 ═══
                    MAX_AGENT_ERROR_ROUNDS = 5
                    if self._total_errors >= MAX_AGENT_ERROR_ROUNDS:
                        logger.error("错误轮次超限 → 终止")
                        task_complete = True
                        break

                    # ═══ 死机防护③：连续错误不重置 → 连续N次直接终止 ═══
                    if self._error_streak >= 3:
                        logger.error("连续%d次API错误 → 终止", self._error_streak)
                        task_complete = True
                        break

                    # ═══ 400特殊处理：修复消息后重试（仅1次，不重复显示） ═══
                    is_400 = ("400" in str(err_full) or "400" in str(err_text)
                              or "invalid_request_error" in str(err_full)
                              or ("tool_calls" in str(err_full) and "insufficient" in str(err_full)))
                    if is_400:
                        self._safe_rollback()
                        if self._error_streak <= 1:
                            continue
                        task_complete = True
                        break

                    # ═══ 死机防护④：连续2次非400错误 → 压缩上下文后重试（仅1次压缩） ═══
                    if self._error_streak >= 2 and self._total_errors < MAX_AGENT_ERROR_ROUNDS:
                        logger.warning(
                            "连续%d次API错误，自动压缩上下文后重试（本轮仅此1次压缩）",
                            self._error_streak
                        )
                        self._compress_context()
                        self._error_streak = 0
                        continue

                    # 单次非400错误 → 继续重试
                    continue

                content = result.get("content", "")
                tool_calls = result.get("tool_calls")

                if not tool_calls:
                    if content:
                        full_response += content
                        # 不存reasoning_content——占50-80%token且后续轮次无用
                        assistant_msg = {"role": "assistant", "content": content}
                        self._safe_append(assistant_msg)
                    task_complete = True
                    break

                # 工具调用：废话检测+截断
                assistant_msg = {
                    "role": "assistant",
                    "content": _trim_tool_content(content or ""),
                    "tool_calls": [
                        {"id": tc["id"], "type": "function", "function": tc["function"]}
                        for tc in tool_calls
                    ],
                }
                self._safe_append(assistant_msg)

                if content:
                    full_response += content

                # ═══ 并发执行工具 (不设上限 + 去重 + 权限批量确认) ═══
                import concurrent.futures

                # 收集+解析
                parsed_calls = []
                for tc in tool_calls:
                    if self._cancel_flag:
                        for remaining in tool_calls:
                            self._safe_append({"role": "tool", "tool_call_id": remaining["id"],
                                               "content": "⏹ 用户取消了操作"})
                        task_complete = True
                        break

                    func_name = tc["function"]["name"]
                    raw_args = tc["function"]["arguments"]
                    try:
                        func_args = json.loads(raw_args)
                    except json.JSONDecodeError:
                        self._safe_append({"role": "tool", "tool_call_id": tc["id"],
                                           "content": f"[参数错误] {raw_args}"})
                        if on_tool_result: on_tool_result(func_name, f"[参数错误] {raw_args}")
                        continue

                    parsed_calls.append({"tc": tc, "func_name": func_name, "func_args": func_args})

                if task_complete:
                    break

                # ═══ 权限批量确认 ═══
                if self._permission_hook and parsed_calls:
                    perm_groups = {}
                    for pc in parsed_calls:
                        risk = self._TOOL_RISK.get(pc["func_name"], "safe")
                        if risk in ("safe", "moderate"):
                            continue
                        if pc["func_name"] == "run_command":
                            from tools import is_command_safe
                            safe, _ = is_command_safe(pc["func_args"].get("command", ""))
                            if safe:
                                continue
                        perm_groups.setdefault(risk, []).append(pc)
                    for risk, pcs in perm_groups.items():
                        if not pcs:
                            continue
                        if len(pcs) == 1:
                            desc = self._describe_action(pcs[0]["func_name"], pcs[0]["func_args"])
                        else:
                            desc = f"批量执行 {len(pcs)} 个操作:\n" + "\n".join(
                                f"  {i+1}. {self._describe_action(pc['func_name'], pc['func_args'])}"
                                for i, pc in enumerate(pcs)
                            )
                        allow_always = (risk == "moderate")
                        result = self._permission_hook(pcs[0]["func_name"], desc, risk, allow_always)
                        if not result:
                            for pc in pcs:
                                self._safe_append({
                                    "role": "tool", "tool_call_id": pc["tc"]["id"],
                                    "content": f"⏭️ [已跳过] 用户未批准: {pc['func_name']}",
                                })
                            pcs.clear()
                        elif result == "always" and risk == "moderate":
                            self._TOOL_RISK[pcs[0]["func_name"]] = "safe"

                if parsed_calls:
                    results_map = {}

                    def _exec_one(pc):
                        fn = pc["func_name"]
                        tc_id = pc["tc"]["id"]
                        try:
                            fa = self._inject_working_dir(fn, pc["func_args"])
                            if on_tool_start: on_tool_start(fn, fa)
                            result = self._execute_tool(fn, fa)
                            if len(result) > self.MAX_TOOL_OUTPUT:
                                result = self._truncate_output(result)
                            return tc_id, fn, result
                        except Exception as e:
                            import traceback
                            err = f"[工具异常] {fn}: {e}\n{traceback.format_exc()[-500:]}"
                            logger.error("工具执行异常 | tool=%s | %s", fn, str(e))
                            return tc_id, fn, err

                    nw = len(parsed_calls)
                    with concurrent.futures.ThreadPoolExecutor(max_workers=nw) as executor:
                        futures = {executor.submit(_exec_one, pc): pc for pc in parsed_calls}
                        for future in concurrent.futures.as_completed(futures):
                            ret = future.result()
                            if ret:
                                tid, fn, result = ret
                                results_map[tid] = (fn, result)
                                if on_tool_result: on_tool_result(fn, result)

                    # 按原始顺序append结果
                    for pc in parsed_calls:
                        tid = pc["tc"]["id"]
                        if tid not in results_map:
                            continue
                        fn, result = results_map[tid]

                        self._safe_append({
                            "role": "tool", "tool_call_id": tid, "content": result,
                        })
                        self._record_action(fn, pc["func_args"], result)

                        if fn in ("send_screenshot", "send_file", "send_text") and not result.startswith("["):
                            task_complete = True
                        if fn == "write_file" and not result.startswith("["):
                            self.memory.add_important_file(pc["func_args"].get("path", ""))
                        if fn == "capture_screen" and self.model_type == "mimo":
                            self._pending_screenshot = True

                if task_complete:
                    break

                # ═══ 上下文压缩：每N轮后汇总旧消息，防止token爆炸 ═══
                if self._round_count > 0 and self._round_count % self.CONTEXT_SUMMARIZE_ROUNDS == 0:
                    self._compress_context()

            # 不再有轮次上限——只有模型判断完成或用户取消才会停

        except Exception as e:
            import traceback
            tb = traceback.format_exc()
            logger.error(
                "AgentCore异常 | type=%s | error=%s | 轮次=%d\n%s",
                type(e).__name__, str(e), self._round_count, tb
            )
            error_msg = f"\n\n❌ [异常] {str(e)}"
            full_response += error_msg
            if on_text:
                on_text(error_msg)

        finally:
            self.is_processing = False
            self._skip_permission = False  # 重置手机/微信跳过标记(全局权限走_global_skip)
            self._processing_lock.release()
            # ═══ 状态机: processing → idle ═══
            with self._state_lock:
                self._state = "idle"
                self._state_lock.notify_all()
            duration = time.time() - start_time

            # 会话记忆：记录AI完整回复
            if full_response and len(full_response) > 10:
                self.session_memory.record_ai_response(full_response)

            if on_done:
                on_done(full_response)

        return full_response

    # ═══ 工具调用记录 ═══

    def _record_action(self, func_name: str, func_args: dict, result: str):
        # 会话记忆记录（所有工具调用都记录，供自动提炼）
        result_summary = (result[:200] if result else "")
        self.session_memory.record_tool_call(func_name, func_args, result_summary)

        # —— 自动学习：记录错误模式 ——
        if result and any(m in result[:500] for m in
                         ("[错误]", "[异常]", "❌", "失败", "Error:", "Exception:", "failed")):
            self.session_memory.record_error(func_name, result[:200], str(func_args)[:200])

        error = result.startswith("[") if result else False
        status = "失败" if error else "完成"

        if func_name == "write_file":
            self.memory.add_action(f"创建/修改文件: {func_args.get('path', '?')}")
        elif func_name == "run_command":
            cmd = func_args.get("command", "?")
            self.memory.add_action(f"执行命令: {cmd[:80]} ({status})")
        elif func_name in ("edit_file", "edit_file_diff"):
            self.memory.add_action(f"编辑文件: {func_args.get('file_path', func_args.get('path', '?'))}")
        elif func_name in ("git_commit", "git_push", "git_create_pr"):
            self.memory.add_action(f"Git: {func_name}")
        elif func_name == "package_install":
            self.memory.add_action(f"安装依赖: {func_args.get('package', '?')}")
        elif func_name == "remember":
            self.memory.add_action(f"记住: {func_args.get('fact', '?')[:60]}")
        elif func_name in ("click_at", "type_text", "press_key", "scroll_screen"):
            pass
        else:
            pass

    # ═══ 工作目录注入 ═══

    def _inject_working_dir(self, func_name: str, args: dict) -> dict:
        wd = self.working_dir

        if func_name == "list_dir":
            if not args.get("path") or args.get("path") == ".":
                args["path"] = wd
        elif func_name in ("search_code", "search_semantic"):
            if not args.get("path"):
                args["path"] = wd
        elif func_name == "run_command":
            if not args.get("working_dir") or not args["working_dir"].strip():
                args["working_dir"] = wd
        elif func_name in ("git_status", "git_add", "git_commit", "git_push", "git_pull",
                           "git_diff", "git_log", "git_branch", "git_merge"):
            if not args.get("repo_path"):
                args["repo_path"] = wd

        return args

    # ═══ 旧版文本命令兼容 ═══

    def parse_and_execute_tool(self, command_text: str) -> str:
        func_name, params = parse_tool_command(command_text)
        if not func_name:
            return "[解析失败] 请使用格式: 【TOOL】工具名|参数1=值1"

        params = self._inject_working_dir(func_name, params)
        return self._execute_tool(func_name, params)

    # ═══ Nexie 特有: 自我状态 ═══

    def get_status(self) -> dict:
        return {
            "model": self.model_type,
            "tools_registered": len(self._tool_definitions),
            "tools_enabled": len(self.registry.list_enabled()),
            "rounds": self._round_count,
            "processing": self.is_processing,
            "memory_items": len(self.memory.get_all_facts()),
        }

    def refresh_tools(self):
        """刷新工具定义(热加载后调用)"""
        self._tool_definitions = _build_tool_definitions(self.registry, self.model_type)

    # ═══ 自我进化补丁工具 ═══


    def _task_create(self, subject: str, description: str = "") -> str:
        tid = str(len(self._task_list) + 1)
        task = {"id": tid, "subject": subject, "description": description, "status": "pending"}
        self._task_list.append(task)
        if self._on_task_update:
            self._on_task_update(self._task_list)
        return f"✅ 任务已创建 [{tid}] {subject}"

    def _task_update(self, task_id: str, status: str = "", subject: str = "") -> str:
        for t in self._task_list:
            if t["id"] == task_id:
                if status:
                    t["status"] = status
                if subject:
                    t["subject"] = subject
                if self._on_task_update:
                    self._on_task_update(self._task_list)
                icon = {"pending": "⏳", "in_progress": "🔄", "completed": "✅", "deleted": "❌"}.get(t["status"], "")
                return f"{icon} 任务 [{task_id}] → {t['status']}: {t['subject']}"
        return f"❌ 未找到任务 [{task_id}]"

    def _task_list(self) -> str:
        if not self._task_list:
            return "📊 暂无任务"
        lines = ["📊 任务列表"]
        lines.append("=" * 40)
        for t in self._task_list:
            if t["status"] == "deleted":
                continue
            icon = {"pending": "⏳", "in_progress": "🔄", "completed": "✅"}.get(t["status"], "❓")
            lines.append(f"  {icon} [{t['id']}] {t['subject']} ({t['status']})")
            if t.get("description"):
                lines.append(f"      {t['description'][:80]}")
        return "\n".join(lines)

    # ═══════════════════════════════════════════
    # 自治执行循环
    # ═══════════════════════════════════════════

    def autonomous_execute(
        self, goal: str,
        on_progress: callable = None,
        on_text: callable = None,
        on_tool_start: callable = None,
        on_tool_result: callable = None,
        on_done: callable = None,
        on_thinking: callable = None,
        max_rounds: int = 99999,
        max_time: float = 86400,
        constraints: str = "",      # 能力4: 边界自治 — "不要改main分支"/"只在frontend/下操作"
        resume: bool = False,        # 能力3: 断点续执
    ) -> dict:
        """
        自治执行: Plan->Execute->Verify->Adapt 闭环.
        不限制轮次 — 只有任务完成(AUTO_COMPLETE)或用户取消才会结束.
        返回: {success, summary, rounds, tasks, checkpoint}
        """
        self.cancel_and_wait()
        self._processing_lock.acquire(timeout=10.0)

        try:
            with self._state_lock:
                self._state = "processing"
                self._state_lock.notify_all()

            self._cancel_flag = False
            self._error_streak = 0
            self._total_errors = 0
            self.is_processing = True
            self._round_count = 0
            self._task_list = []
            retry_counts = {}          # 自治模式工具失败计数

            self._safe_rollback()
            # 解析截止时间
            import re as _re
            deadline_info = ""
            _time_patterns = [
                (r'截至?到?\s*(\d{1,2}:\d{2})', '截止时间'),
                (r'到\s*(\d{1,2}:\d{2})\s*终止', '截止时间'),
                (r'(\d{1,2}:\d{2})\s*停', '截止时间'),
            ]
            for pat, label in _time_patterns:
                m = _re.search(pat, goal)
                if m:
                    deadline_info = f"\n【硬性截止】{label}: {m.group(1)}，到点必须停止，不调试不修补不续时。"
                    break
            instruction = f"{goal}{deadline_info}"
            if constraints:
                instruction = f"{instruction}\n约束: {constraints}"
            self._safe_append({"role": "user", "content": instruction})

            if on_progress:
                on_progress("planning", goal)

            full_response = ""
            start_time = time.time()
            def stream_callback(delta_content, tool_calls_acc, reasoning_delta):
                if reasoning_delta and on_thinking:
                    on_thinking(reasoning_delta)
                if delta_content and on_text:
                    on_text(delta_content)

            while True:
                if self._cancel_flag:
                    full_response += "\n\n⏹ 用户取消"
                    self._safe_rollback()
                    break

                self._round_count += 1

                self._safe_rollback()

                # —— L3压缩 ——
                if self.memory_layers.should_compress(self.messages):
                    self.messages = self.memory_layers.compress_context(self.messages)

                # —— 调用模型（干净副本防400）——
                clean_msgs = self._build_clean_messages(self.messages)
                result = self.client.chat(
                    messages=clean_msgs, tools=self._tool_definitions,
                    stream=True, on_chunk=stream_callback,
                )

                if self._budget and not result.get("error"):
                    in_tok = count_message_tokens(self.messages)
                    out_tok = estimate_tokens(result.get("content", ""))
                    self._budget.record(self.client.MODEL, in_tok, out_tok)

                if result.get("error"):
                    self._error_streak += 1
                    self._total_errors += 1
                    err_full = (result.get("content", "") or "") + (result.get("error_msg", "") or "")
                    logger.warning("自治-API错误(第%d次): %s", self._error_streak, err_full[:200])

                    FATAL = "接口连接失败，本次生成终止"
                    if FATAL in str(err_full) or self._total_errors >= 5 or self._error_streak >= 3:
                        if self._total_errors == 1:
                            full_response += f"\n\n❌ {FATAL}"
                        break
                    if ("400" in str(err_full) or "invalid_request_error" in str(err_full)) and self._error_streak <= 1:
                        self._safe_rollback()
                        continue
                    if self._error_streak >= 2:
                        self._compress_context()
                        self._error_streak = 0
                    continue

                content = result.get("content", "")
                tool_calls = result.get("tool_calls")

                # —— 检查完成标记 ——
                if not tool_calls:
                    if content:
                        full_response += content
                        self._safe_append({"role": "assistant", "content": content})
                    if "AUTO_COMPLETE:" in (content or ""):
                        if on_progress:
                            on_progress("complete", content.split("AUTO_COMPLETE:", 1)[-1].strip()[:200])
                        break
                    # 空响应 → 直接终止
                    self._empty_response_count = getattr(self, '_empty_response_count', 0) + 1
                    if getattr(self, '_empty_response_count', 0) >= 3:
                        logger.warning("自治-连续%d次空响应，强制终止", self._empty_response_count)
                        break
                    continue

                self._empty_response_count = 0
                # 死循环检测：相同工具调用5次强制终止
                tc_sig = json.dumps([(tc["function"]["name"], tc["function"]["arguments"]) for tc in tool_calls], sort_keys=True, ensure_ascii=False)
                if tc_sig == getattr(self, '_last_tc_sig', ''):
                    self._same_tc_count = getattr(self, '_same_tc_count', 0) + 1
                    if self._same_tc_count >= 5:
                        break
                else:
                    self._same_tc_count = 0
                self._last_tc_sig = tc_sig
                # 工具调用：废话检测+截断
                assistant_msg = {
                    "role": "assistant", "content": _trim_tool_content(content or ""),
                    "tool_calls": [{"id": tc["id"], "type": "function", "function": tc["function"]}
                                   for tc in tool_calls],
                }
                self._safe_append(assistant_msg)
                if content:
                    full_response += content

                # —— 并发执行工具 ——
                import concurrent.futures
                parsed_calls = []
                for tc in tool_calls:
                    if self._cancel_flag:
                        for r in tool_calls:
                            self._safe_append({"role": "tool", "tool_call_id": r["id"],
                                               "content": "⏹ 用户取消"})
                        break
                    fn = tc["function"]["name"]
                    try:
                        fa = json.loads(tc["function"]["arguments"])
                    except json.JSONDecodeError:
                        self._safe_append({"role": "tool", "tool_call_id": tc["id"],
                                           "content": f"[参数错误] {tc['function']['arguments']}"})
                        continue
                    parsed_calls.append({"tc": tc, "func_name": fn, "func_args": fa})

                if self._cancel_flag:
                    break

                # 并发执行
                results_map = {}
                def _exec_one(pc):
                    fn, fa = pc["func_name"], self._inject_working_dir(pc["func_name"], pc["func_args"])
                    try:
                        if on_tool_start: on_tool_start(fn, fa)
                        r = self._execute_tool(fn, fa)
                        if len(r) > self.MAX_TOOL_OUTPUT:
                            r = self._truncate_output(r)
                        return pc["tc"]["id"], fn, fa, r
                    except Exception as e:
                        import traceback
                        return pc["tc"]["id"], fn, fa, f"[工具异常] {fn}: {e}\n{traceback.format_exc()[-500:]}"

                if parsed_calls:
                    with concurrent.futures.ThreadPoolExecutor(max_workers=len(parsed_calls)) as ex:
                        futures = {ex.submit(_exec_one, pc): pc for pc in parsed_calls}
                        for future in concurrent.futures.as_completed(futures):
                            ret = future.result()
                            if ret:
                                tid, fn, fa, r = ret
                                results_map[tid] = (fn, fa, r)
                                if on_tool_result:
                                    on_tool_result(fn, r)

                # —— 按顺序append结果 + 自验证注入 + 失败检测 ——
                for pc in parsed_calls:
                    tid = pc["tc"]["id"]
                    if tid not in results_map:
                        continue
                    fn, fa, result = results_map[tid]
                    self._safe_append({"role": "tool", "tool_call_id": tid, "content": result})
                    self._record_action(fn, fa, result)

                    is_fail = _is_tool_failure(result)
                    if is_fail:
                        retry_counts[fn] = retry_counts.get(fn, 0) + 1
                    else:
                        retry_counts[fn] = 0

                    if on_progress:
                        on_progress("step", f"{fn}: {'OK' if not is_fail else 'FAIL'}")

            # —— 循环结束 ——
            success = "AUTO_COMPLETE:" in full_response

            # ── 能力3: 保存断点 ──
            try:
                ck = {"goal": goal, "rounds": self._round_count, "tasks": self._task_list,
                      "saved_at": datetime.now().isoformat(), "success": success}
                Path(self.working_dir, ".nexie_checkpoint.json").write_text(
                    json.dumps(ck, ensure_ascii=False, indent=2), "utf-8")
            except Exception:
                pass

            if on_progress:
                on_progress("done", full_response[-500:] if full_response else "")

            return {
                "success": success,
                "summary": full_response[-2000:] if full_response else "",
                "rounds": self._round_count,
                "tasks": list(self._task_list),
                "duration": time.time() - start_time,
                "checkpoint": str(Path(self.working_dir) / ".nexie_checkpoint.json"),
            }

        except Exception as e:
            import traceback
            logger.error("自治执行异常: %s\n%s", e, traceback.format_exc())
            if on_text:
                on_text(f"\n\n❌ [自治异常] {e}")
            return {"success": False, "summary": str(e), "rounds": self._round_count, "tasks": []}

        finally:
            self.is_processing = False
            self._skip_permission = False  # 重置手机端标记(全局权限走_global_skip)
            self._processing_lock.release()
            with self._state_lock:
                self._state = "idle"
                self._state_lock.notify_all()
            if on_done:
                on_done(full_response if 'full_response' in dir() else "")

    # ═══ 新能力工具处理器 ═══



    # ═══ Grep 内容搜索 ═══

    def _tool_search_content(self, pattern: str, path: str = "", file_types: str = "",
                              ignore_case: bool = False) -> str:
        """内容搜索 — ripgrep + Python回退"""
        try:
            from nexie.search_tools import search_code
            search_path = path or self.working_dir
            return search_code(pattern=pattern, path=search_path, file_types=file_types,
                              ignore_case=ignore_case, max_results=150)
        except Exception as e:
            # 内联回退：Python原生正则搜索
            return self._fallback_grep(pattern, path or self.working_dir, file_types, ignore_case)

    def _fallback_grep(self, pattern: str, path: str, file_types: str, ignore_case: bool) -> str:
        import re, os, fnmatch
        results = []
        search_dir = path
        if not os.path.exists(search_dir):
            return f"[错误] 目录不存在: {search_dir}"
        flags = re.IGNORECASE if ignore_case else 0
        try:
            regex = re.compile(pattern, flags)
        except re.error as e:
            return f"[错误] 正则无效: {e}"
        globs = [g.strip() for g in file_types.split(",") if g.strip()]
        skip = {'.git', '__pycache__', 'node_modules', 'venv', '.venv', 'build', 'dist'}
        for root, dirs, files in os.walk(search_dir):
            dirs[:] = [d for d in dirs if d not in skip]
            for fname in files:
                if globs and not any(fnmatch.fnmatch(fname, g) for g in globs):
                    continue
                if fname.endswith(('.pyc','.dll','.exe','.so','.bin','.jpg','.png','.gif','.mp4','.zip','.gz')):
                    continue
                fpath = os.path.join(root, fname)
                try:
                    if os.path.getsize(fpath) > 2*1024*1024:
                        continue
                    with open(fpath, 'r', encoding='utf-8', errors='replace') as f:
                        for lineno, line in enumerate(f, 1):
                            if regex.search(line):
                                results.append(f"{fpath}:{lineno}: {line.rstrip()[:200]}")
                                if len(results) >= 150:
                                    break
                except (PermissionError, OSError):
                    continue
                if len(results) >= 150:
                    break
            if len(results) >= 150:
                break
        if not results:
            return f"未找到匹配 '{pattern}'"
        return f"搜索 '{pattern}' ({len(results)}条):\n" + "\n".join(results[:150])

    # ═══ WebSearch + WebFetch ═══

    def _tool_web_search(self, query: str) -> str:
        """DuckDuckGo 网页搜索"""
        import urllib.parse, re
        try:
            import httpx
            url = f"https://html.duckduckgo.com/html/?q={urllib.parse.quote(query)}"
            r = httpx.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
            if r.status_code != 200:
                return f"搜索失败 HTTP {r.status_code}"
            results = []
            for match in re.finditer(r'<a[^>]*class="result__a"[^>]*href="([^"]*)"[^>]*>(.*?)</a>', r.text, re.DOTALL):
                href = urllib.parse.unquote(match.group(1))
                title = re.sub(r'<[^>]+>', '', match.group(2)).strip()
                if title and not href.startswith('//'):
                    results.append(f"{title}\n  {href}")
                    if len(results) >= 8:
                        break
            if not results:
                return f"未找到搜索结果: {query}"
            return f"搜索 '{query}':\n\n" + "\n\n".join(results)
        except Exception as e:
            return f"搜索异常: {e}"

    def _tool_web_fetch(self, url: str, max_length: int = 8000) -> str:
        """抓取网页内容并转为纯文本"""
        import re as _re
        try:
            import httpx
            r = httpx.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"},
                         follow_redirects=True)
            if r.status_code != 200:
                return f"抓取失败 HTTP {r.status_code}"
            # 简单 HTML→text
            text = r.text
            text = _re.sub(r'<script[^>]*>.*?</script>', '', text, flags=_re.DOTALL | _re.IGNORECASE)
            text = _re.sub(r'<style[^>]*>.*?</style>', '', text, flags=_re.DOTALL | _re.IGNORECASE)
            text = _re.sub(r'<[^>]+>', ' ', text)
            text = _re.sub(r'&amp;', '&', text)
            text = _re.sub(r'&lt;', '<', text)
            text = _re.sub(r'&gt;', '>', text)
            text = _re.sub(r'&nbsp;', ' ', text)
            text = _re.sub(r'\s+', ' ', text).strip()
            if len(text) > max_length:
                text = text[:max_length] + f"\n\n... [截断，原文 {len(text)} 字符]"
            return text
        except Exception as e:
            return f"抓取异常: {e}"

    # ═══ Agent 子代理 ═══

    def _tool_agent(self, name: str, prompt: str) -> str:
        """子代理 — 独立API+工具执行循环，最多5轮"""
        import concurrent.futures
        try:
            sub_msgs = [
                {"role": "system", "content": f"你是子Agent '{name}'。用中文完成并返回结果。"},
                {"role": "user", "content": prompt},
            ]
            # 只给子代理文件/命令/搜索工具
            sub_tools = [t for t in self._tool_definitions
                        if t["function"]["name"] in
                        ("list_dir","read_file","write_file","edit_file","run_command",
                         "search_files","search_content","web_search","web_fetch")]
            for _ in range(5):
                result = self.client.chat(messages=sub_msgs, tools=sub_tools, stream=False)
                if result.get("error"):
                    return f"[Agent错误] {result.get('content','')}"
                content = result.get("content", "")
                tool_calls = result.get("tool_calls")
                if not tool_calls:
                    return f"[Agent: {name}]\n{content[:5000]}" if content else f"[Agent: {name}] 已完成"
                # 添加assistant消息
                sub_msgs.append({"role": "assistant", "content": content or "",
                                "tool_calls": [{"id": tc["id"], "type": "function",
                                "function": tc["function"]} for tc in tool_calls]})
                # 执行工具
                for tc in tool_calls:
                    fn = tc["function"]["name"]
                    try:
                        fa = json.loads(tc["function"]["arguments"])
                    except Exception:
                        fa = {}
                    try:
                        r = self._execute_tool(fn, fa)
                    except Exception as e:
                        r = f"[异常] {e}"
                    sub_msgs.append({"role": "tool", "tool_call_id": tc["id"], "content": str(r)[:5000]})
            return f"[Agent: {name}] 已达最大轮次"
        except Exception as e:
            return f"[Agent异常] {e}"

# ═══ 模块级辅助函数 ═══

def _is_tool_failure(result: str) -> bool:
    """检测工具结果是否为失败"""
    if not result:
        return True
    markers = [
        "[错误]", "[异常]", "❌", "失败", "异常", "错误",
        "Error:", "Exception:", "error:", "exception:",
        "无法", "不存在", "权限不足", "拒绝访问",
        "cannot", "not found", "permission denied", "timed out",
        "failed", "FAILED", "fatal", "Fatal",
    ]
    r = result[:500]
    return any(m in r for m in markers)
