# Nexie — AI Coding Agent
# Copyright (c) 2026. All rights reserved.
# 本软件仅供个人学习与研究使用，禁止未经授权的商业用途、分发或修改后闭源。
# Licensed for personal, educational, and non-commercial use only.
"""
Nexie — AI Coding Agent
插件式工具系统，自我进化，全能工程师

模块化架构：
- nexie.tool_registry: 插件式工具注册中心
- nexie.stability: 稳定性模块(心跳/超时/后台/清理/内存)
- nexie.search_tools: ripgrep搜索+语义搜索
- nexie.memory_layers: 四层记忆架构(L1/L2/L3/L4)
- nexie.api_resilience: 全链路防400/429体系
- nexie.permission_system: 权限控制系统
- nexie.workspace: 工作空间管理
- nexie.task_queue: FIFO任务队列(UI线程分离)
- nexie.session_memory: 长期记忆引擎
"""

__codename__ = "Nexie"

import sys
import os
import logging
from pathlib import Path
from datetime import datetime

# ═══════════════════════════════════════════
# 统一数据目录 — 所有模块的唯一入口（必须在日志初始化之前定义）
# ═══════════════════════════════════════════

# 项目根目录（源码位置，nexie/__init__.py 的上上级 = Nexie/）
_PROJECT_ROOT = Path(__file__).parent.parent.resolve()

# 数据目录缓存
_data_dir_cache: Path = None


def get_data_dir() -> Path:
    """
    获取 Nexie 数据目录（唯一入口，所有模块必须通过此函数获取）。

    优先级：
    1. 环境变量 NEXIE_DATA_DIR（用户显式指定）
    2. %APPDATA%/Nexie/（Windows 标准用户数据目录，持久化且不污染程序目录）

    自动创建目录及子目录（patches, logs, l1_cache, l2_core_memory, l4_archive, workspace）
    """
    global _data_dir_cache
    if _data_dir_cache is not None:
        return _data_dir_cache

    # 1. 环境变量优先
    env_dir = os.environ.get("NEXIE_DATA_DIR", "").strip()
    if env_dir:
        _data_dir_cache = Path(env_dir)
    else:
        # 2. Windows 标准用户数据目录（%APPDATA%/Nexie）
        appdata = os.environ.get("APPDATA", "")
        if appdata:
            _data_dir_cache = Path(appdata) / "Nexie"
        else:
            # 极端回退
            _data_dir_cache = Path.home() / "Nexie_data"

    # 确保目录结构存在
    _data_dir_cache.mkdir(parents=True, exist_ok=True)
    for sub in ["patches", "logs", "l1_cache", "l2_core_memory", "l4_archive", "workspace"]:
        (_data_dir_cache / sub).mkdir(parents=True, exist_ok=True)

    # ═══ 自动迁移旧数据 ═══
    _migrate_legacy_data(_data_dir_cache)

    return _data_dir_cache


def _migrate_legacy_data(target: Path):
    """将旧位置的 Nexie_data 迁移到新位置（仅迁移一次）"""
    import shutil as _shutil

    # 标记文件：迁移过就不再重复
    migrated_flag = target / ".migrated"
    if migrated_flag.exists():
        return

    # 旧位置候选列表
    legacy_candidates = []
    # 1. exe 旁边的 Nexie_data（打包模式）
    if getattr(sys, 'frozen', False):
        try:
            exe_dir = Path(sys.executable).parent
            legacy_candidates.append(exe_dir / "Nexie_data")
        except Exception:
            pass
    # 2. 项目根目录的 Nexie_data（开发模式）
    legacy_candidates.append(_PROJECT_ROOT / "Nexie_data")
    # 3. 桌面
    try:
        legacy_candidates.append(Path.home() / "Desktop" / "Nexie_data")
    except Exception:
        pass

    for legacy in legacy_candidates:
        if not legacy.exists() or legacy.resolve() == target.resolve():
            continue
        try:
            # 迁移 patches、.env、workspace.json 等关键文件
            for item in legacy.iterdir():
                if item.name == ".migrated":
                    continue
                dest = target / item.name
                if item.is_dir() and not dest.exists():
                    _shutil.copytree(item, dest)
                    logger.info(f"[迁移] {item.name}/ → {target}")
                elif item.is_file() and not dest.exists():
                    _shutil.copy2(item, dest)
                    logger.info(f"[迁移] {item.name} → {target}")
        except Exception as e:
            logger.debug(f"[迁移] 跳过 {legacy}: {e}")

    # 标记已迁移
    try:
        migrated_flag.write_text(datetime.now().isoformat())
    except Exception:
        pass


def get_project_root() -> Path:
    """获取项目根目录（源码位置）"""
    return _PROJECT_ROOT


def reset_data_dir_cache():
    """重置数据目录缓存（用于测试或重新定位）"""
    global _data_dir_cache
    _data_dir_cache = None


# ═══ 集中日志配置：控制台 + 按日期滚动文件 ═══
def _setup_logging():
    """配置日志系统：控制台输出(INFO) + 文件记录(DEBUG,按日期滚动)"""
    from logging.handlers import RotatingFileHandler

    # 日志目录：使用统一数据目录
    try:
        log_dir = get_data_dir() / "logs"
    except Exception:
        log_dir = Path("Nexie_data") / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    today = datetime.now().strftime("%Y-%m-%d")
    log_path = log_dir / f"nexie_{today}.log"

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    fmt = logging.Formatter(
        "%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # 控制台 handler (INFO级别)
    console = logging.StreamHandler()
    console.setLevel(logging.INFO)
    console.setFormatter(fmt)
    root.addHandler(console)

    # 文件 handler (DEBUG级别, 追加模式, 1MB轮转, 保留5个备份)
    try:
        file_handler = RotatingFileHandler(
            str(log_path), mode='a', maxBytes=1048576,
            backupCount=5, encoding='utf-8'
        )
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(fmt)
        root.addHandler(file_handler)
    except Exception:
        pass  # 文件日志不可用时不影响程序运行

_setup_logging()

logger = logging.getLogger("Nexie")

# ═══ 自动加载所有工具模块 ═══

# 导入注册中心
from nexie.tool_registry import ToolRegistry, ToolDef, get_registry, tool

# 导入稳定性模块
from nexie.stability import (
    get_heartbeat, get_executor, get_bg_manager, get_memory_monitor,
    HeartbeatMonitor, TimeoutExecutor, BackgroundTaskManager, MemoryMonitor,
    cleanup_all_processes,
)

# 导入四层记忆架构
from nexie.memory_layers import (
    get_memory_layers, L1TempCache, L2CoreMemory,
    L3AutoCompressor, L4ColdArchive, MemoryLayerManager,
    estimate_tokens, count_message_tokens,
)

# 导入API韧性系统
from nexie.api_resilience import (
    get_api_resilience, RateLimiter, InputSplitter,
    ExponentialBackoff, APIKeyPool, APIResilienceManager,
)

# 导入权限控制系统
from nexie.permission_system import (
    get_permission, PermissionController,
)

# 导入工作空间管理
from nexie.workspace import (
    get_workspace, Workspace,
)


def init_nexie(enable_all: bool = True) -> ToolRegistry:
    """
    初始化Nexie系统
    - 加载所有工具模块
    - 启动稳定性监控
    - 返回工具注册中心
    """
    registry = get_registry()

    # 导入工具模块(触发自动注册) — 仅保留核心，其余走run_command省Token
    tool_modules = [
        "nexie.search_tools",
    ]

    loaded = []
    for module_name in tool_modules:
        try:
            __import__(module_name)
            loaded.append(module_name)
        except Exception as e:
            logger.warning(f"加载 {module_name} 失败: {e}")

    # 启动稳定性监控
    heartbeat = get_heartbeat()
    heartbeat.start()

    memory_monitor = get_memory_monitor()
    # 注册缓存清理回调
    memory_monitor.register_cache_manager(lambda: __clean_caches())

    logger.info(f"Nexie 初始化完成 | {len(registry.list_all())} 工具已注册")
    logger.info(f"已加载模块: {', '.join(loaded)}")

    return registry


def __clean_caches():
    """清理各种缓存"""
    import gc
    gc.collect()


def get_tool_summary() -> str:
    """获取工具摘要信息"""
    registry = get_registry()
    tools = registry.list_all()
    categories = registry.get_categories()

    lines = [f"🔧 Nexie | {len(tools)} 工具 | {len(categories)} 类别"]
    lines.append("=" * 60)

    for cat in sorted(categories):
        cat_tools = registry.list_by_category(cat)
        lines.append(f"\n📂 {cat} ({len(cat_tools)}):")
        for t in cat_tools:
            risk_icon = {"safe": "🟢", "moderate": "🟡", "high": "🟠", "critical": "🔴"}.get(t.risk_level, "⚪")
            enabled = "" if t.enabled else " [禁用]"
            lines.append(f"  {risk_icon} {t.name}: {t.description[:60]}{enabled}")

    return "\n".join(lines)


# 自动初始化
_registry = None


def auto_init():
    """自动初始化 — 在导入时调用"""
    global _registry
    try:
        _registry = init_nexie()
    except Exception as e:
        logger.error(f"自动初始化失败: {e}")


# 不自动执行，由主程序显式调用 init_nexie()
