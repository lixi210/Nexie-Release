# Nexie — AI Coding Agent
# Copyright (c) 2026. All rights reserved.
# 本软件仅供个人学习与研究使用，禁止未经授权的商业用途、分发或修改后闭源。
# Licensed for personal, educational, and non-commercial use only.
"""
Nexie — 插件式工具注册系统
统一接口规范，热加载新工具无需改核心
每个工具: {name, description, parameters, function, category, risk_level}
"""
import time
import threading
import logging
from typing import Callable, Any
from dataclasses import dataclass, field

logger = logging.getLogger("Nexie.ToolRegistry")

# ═══════════════════════════════════════════
# 工具定义数据结构
# ═══════════════════════════════════════════

@dataclass
class ToolDef:
    """工具定义 — 统一接口规范"""
    name: str
    description: str
    parameters: dict  # JSON Schema properties
    function: Callable  # 实际执行函数
    category: str = "general"  # file | system | network | git | ide | test | db | docker | control | memory | push
    risk_level: str = "safe"  # safe | moderate | high | critical
    requires_confirmation: bool = False
    timeout: int = 60  # 默认超时秒数
    max_retries: int = 0  # 失败后自动重试次数
    retry_delay: float = 1.0  # 重试间隔(秒)
    enabled: bool = True
    # 新增字段
    required_params: list = field(default_factory=list)  # 必填参数名列表
    is_background: bool = False  # 是否允许放入后台执行

    def to_openai_tool(self) -> dict:
        """转换为 OpenAI Function Calling 格式"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": self.parameters,
                    "required": self.required_params,
                }
            }
        }


class ToolRegistry:
    """插件式工具注册中心 — 单例"""

    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._initialized = True
        self._tools: dict[str, ToolDef] = {}
        self._categories: dict[str, list[str]] = {}
        self._execution_stats: dict[str, list[dict]] = {}  # name → [{time, success, duration}]
        self._config = {
            "default_timeout": 120,
            "max_retries": 3,
            "retry_base_delay": 1.0,
            "retry_backoff": 2.0,  # 指数退避倍数
            "enable_execution_log": True,
        }

    # ═══ 注册/注销 ═══

    def register(self, tool: ToolDef) -> None:
        """注册工具 — 同名覆盖视为升级"""
        self._tools[tool.name] = tool
        self._categories.setdefault(tool.category, []).append(tool.name)
        logger.info(f"[Registry] 注册工具: {tool.name} (类别: {tool.category}, 风险: {tool.risk_level})")

    def unregister(self, name: str) -> bool:
        """注销工具"""
        if name in self._tools:
            tool = self._tools.pop(name)
            if tool.category in self._categories:
                self._categories[tool.category].remove(name)
            logger.info(f"[Registry] 注销工具: {name}")
            return True
        return False

    def register_many(self, tools: list[ToolDef]) -> None:
        """批量注册"""
        for tool in tools:
            self.register(tool)

    # ═══ 查询 ═══

    def get(self, name: str) -> ToolDef | None:
        return self._tools.get(name)

    def list_all(self) -> list[ToolDef]:
        return list(self._tools.values())

    def list_enabled(self) -> list[ToolDef]:
        return [t for t in self._tools.values() if t.enabled]

    def list_by_category(self, category: str) -> list[ToolDef]:
        names = self._categories.get(category, [])
        return [self._tools[n] for n in names if n in self._tools]

    def get_categories(self) -> list[str]:
        return list(self._categories.keys())

    # ═══ 执行 ═══

    def execute(self, name: str, params: dict) -> str:
        """执行工具 — 含重试、超时、日志"""
        tool = self._tools.get(name)
        if not tool:
            return f"[错误] 未知工具: {name}"
        if not tool.enabled:
            return f"[禁用] 工具 '{name}' 已被管理员禁用"

        start_time = time.time()
        last_error = None

        max_attempts = 1 + min(tool.max_retries, self._config["max_retries"])
        for attempt in range(max_attempts):
            try:
                result = tool.function(**params)
                duration = time.time() - start_time
                self._record_execution(name, True, duration)
                return result
            except Exception as e:
                last_error = str(e)
                logger.warning(f"[Tool] {name} 执行失败 (尝试 {attempt+1}/{max_attempts}): {e}")
                if attempt < max_attempts - 1:
                    delay = self._config["retry_base_delay"] * (self._config["retry_backoff"] ** attempt)
                    time.sleep(delay)

        duration = time.time() - start_time
        self._record_execution(name, False, duration)
        return f"[错误] 工具 '{name}' 执行失败 ({max_attempts}次尝试): {last_error}"

    def execute_with_timeout(self, name: str, params: dict, timeout: int = None) -> str:
        """带超时的工具执行"""
        tool = self._tools.get(name)
        effective_timeout = timeout or (tool.timeout if tool else 60)

        result = [None]
        error = [None]

        def _run():
            try:
                result[0] = self.execute(name, params)
            except Exception as e:
                error[0] = str(e)

        thread = threading.Thread(target=_run, daemon=True)
        thread.start()
        thread.join(timeout=effective_timeout)

        if thread.is_alive():
            return f"⏱️ [超时] 工具 '{name}' 执行超过 {effective_timeout} 秒，已终止"
        if error[0]:
            return f"[错误] {error[0]}"
        return result[0] or f"[错误] 工具 '{name}' 无返回结果"

    # ═══ 统计 ═══

    def _record_execution(self, name: str, success: bool, duration: float):
        if not self._config["enable_execution_log"]:
            return
        self._execution_stats.setdefault(name, []).append({
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "success": success,
            "duration": round(duration, 3),
        })
        # 只保留最近100条
        if len(self._execution_stats[name]) > 100:
            self._execution_stats[name] = self._execution_stats[name][-100:]

    def get_stats(self, name: str = None) -> dict:
        """获取执行统计"""
        if name:
            return {name: self._execution_stats.get(name, [])}
        return dict(self._execution_stats)

    # ═══ 配置 ═══

    def configure(self, **kwargs):
        """更新配置"""
        self._config.update(kwargs)

    def set_tool_enabled(self, name: str, enabled: bool):
        if name in self._tools:
            self._tools[name].enabled = enabled

    # ═══ OpenAI工具列表 ═══

    def get_openai_tools(self, enabled_only: bool = True) -> list[dict]:
        """获取所有工具的 OpenAI Function Calling 格式列表"""
        tools = self.list_enabled() if enabled_only else self.list_all()
        return [t.to_openai_tool() for t in tools]

    # ═══ 热加载 ═══

    def hot_reload_tool(self, name: str, new_function: Callable):
        """热更新工具函数，无需重启"""
        if name in self._tools:
            self._tools[name].function = new_function
            logger.info(f"[Registry] 热更新工具: {name}")
            return True
        return False

    def load_from_module(self, module):
        """从模块批量加载工具 (扫描 @tool 装饰器注册的)"""
        for attr_name in dir(module):
            attr = getattr(module, attr_name)
            if isinstance(attr, ToolDef):
                self.register(attr)
            elif callable(attr) and hasattr(attr, '_nexie_tool'):
                tool_def = attr._nexie_tool
                tool_def.function = attr
                self.register(tool_def)


# ═══ 全局单例 ═══

_registry: ToolRegistry = None


def get_registry() -> ToolRegistry:
    global _registry
    if _registry is None:
        _registry = ToolRegistry()
    return _registry


# ═══ 装饰器 ═══

def tool(name: str = None, description: str = "", category: str = "general",
         risk_level: str = "safe", timeout: int = 60, max_retries: int = 0,
         required_params: list = None):
    """@tool 装饰器 — 标记函数为可注册工具"""
    def decorator(func):
        tool_name = name or func.__name__
        # 从函数签名推断参数
        import inspect
        sig = inspect.signature(func)
        parameters = {}
        req_params = list(required_params) if required_params else []
        for pname, param in sig.parameters.items():
            if pname in ('self', 'cls'):
                continue
            ptype = "string"
            if param.annotation != inspect.Parameter.empty:
                anno = param.annotation
                if anno == int:
                    ptype = "integer"
                elif anno == float:
                    ptype = "number"
                elif anno == bool:
                    ptype = "boolean"
            parameters[pname] = {"type": ptype, "description": f"参数: {pname}"}

        tool_def = ToolDef(
            name=tool_name,
            description=description or (func.__doc__ or "").strip().split("\n")[0],
            parameters=parameters,
            function=func,
            category=category,
            risk_level=risk_level,
            timeout=timeout,
            max_retries=max_retries,
            required_params=req_params,
        )
        func._nexie_tool = tool_def
        return func
    return decorator
