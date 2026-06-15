# Nexie — AI Coding Agent
# Copyright (c) 2026. All rights reserved.
# 本软件仅供个人学习与研究使用，禁止未经授权的商业用途、分发或修改后闭源。
# Licensed for personal, educational, and non-commercial use only.
"""
Nexie — 代码搜索工具
ripgrep全文搜索(正则/文件类型过滤/上下文) + 语义搜索(自然语言定位)
"""
import os
import re
import subprocess
import json
from pathlib import Path
from nexie.tool_registry import ToolDef, get_registry


def _find_rg() -> str | None:
    """查找ripgrep可执行文件"""
    # 检查PATH
    for path in os.environ.get("PATH", "").split(os.pathsep):
        rg_path = os.path.join(path, "rg.exe" if os.name == "nt" else "rg")
        if os.path.exists(rg_path):
            return rg_path
    # 检查嵌入式rg
    embedded = Path(__file__).parent.parent / "bin" / ("rg.exe" if os.name == "nt" else "rg")
    if embedded.exists():
        return str(embedded)
    return None


def _fallback_grep(pattern: str, path: str, glob_filter: str = "",
                   max_results: int = 200, context: int = 0,
                   ignore_case: bool = False) -> str:
    """Python原生搜索 — ripgrep不可用时的回退"""
    import fnmatch
    results = []
    search_dir = Path(path)

    if not search_dir.exists():
        return f"[错误] 目录不存在: {path}"

    flags = re.IGNORECASE if ignore_case else 0
    try:
        regex = re.compile(pattern, flags)
    except re.error as e:
        return f"[错误] 正则表达式无效: {e}"

    # 解析glob过滤
    glob_patterns = [g.strip() for g in glob_filter.split(",") if g.strip()]

    # 要跳过的大目录
    skip_dirs = {'.git', '__pycache__', 'node_modules', '.venv', 'venv', 'target',
                 'build', 'dist', '.next', '.nuxt', 'vendor', '.tox', '.eggs'}

    for root, dirs, files in os.walk(search_dir):
        # 跳过忽略目录
        dirs[:] = [d for d in dirs if d not in skip_dirs and not d.startswith('.git')]

        for fname in files:
            # Glob过滤
            if glob_patterns:
                if not any(fnmatch.fnmatch(fname, g) for g in glob_patterns):
                    continue

            # 跳过二进制/大文件
            if fname.endswith(('.pyc', '.pyo', '.dll', '.exe', '.so', '.o',
                               '.bin', '.pkl', '.db', '.sqlite', '.jpg', '.png',
                               '.gif', '.mp4', '.zip', '.tar', '.gz', '.7z')):
                continue

            fpath = os.path.join(root, fname)
            try:
                size = os.path.getsize(fpath)
                if size > 2 * 1024 * 1024:  # 跳过 >2MB
                    continue
                with open(fpath, 'r', encoding='utf-8', errors='replace') as f:
                    for lineno, line in enumerate(f, 1):
                        if regex.search(line):
                            results.append({
                                "file": fpath, "line": lineno,
                                "content": line.rstrip()[:200],
                            })
                            if len(results) >= max_results:
                                return _format_search_results(results, pattern, path,
                                                             max_results, len(results))
            except (PermissionError, OSError, UnicodeDecodeError):
                continue

    return _format_search_results(results, pattern, path, max_results, len(results))


def _format_search_results(results: list, pattern: str, path: str,
                           max_results: int, count: int) -> str:
    """格式化搜索结果"""
    if not results:
        return f"🔍 未找到匹配 '{pattern}' 的结果\n搜索目录: {path}"

    lines = [f"🔍 搜索: '{pattern}' | 目录: {path} | 找到 {count} 条结果"]
    lines.append("=" * 70)

    for r in results:
        lines.append(f"  {r['file']}:{r['line']}")
        lines.append(f"     {r['content']}")

    if count >= max_results:
        lines.append(f"\n⚠️ 结果已被截断，显示前 {max_results} 条。请缩小搜索范围。")
    return "\n".join(lines)


def search_code(pattern: str, path: str = ".", file_types: str = "",
                max_results: int = 200, context_lines: int = 0,
                ignore_case: bool = False, regex: bool = True,
                fixed_string: bool = False) -> str:
    """
    ripgrep全文搜索，支持正则、文件类型过滤、上下文展示
    自动fallback到Python原生搜索
    """
    search_path = Path(path)
    if not search_path.is_absolute():
        search_path = Path(os.getcwd()) / path
    search_path = search_path.resolve()

    if not search_path.exists():
        return f"[错误] 搜索目录不存在: {path}"

    rg = _find_rg()

    if rg and search_path.is_dir():
        # 使用ripgrep
        cmd = [rg, "--no-heading", "--line-number", "--color=never"]
        if not regex:
            cmd.append("--fixed-strings")
        if fixed_string:
            cmd.append("--fixed-strings")
        if ignore_case:
            cmd.append("--ignore-case")
        if context_lines > 0:
            cmd.extend(["-C", str(context_lines)])
        if max_results:
            cmd.extend(["-m", str(max_results)])

        # 文件类型过滤
        if file_types:
            for ft in file_types.split(","):
                ft = ft.strip().lstrip(".")
                if ft:
                    cmd.extend(["--type-add", f"custom:*.{ft}"])
                    cmd.extend(["-t", "custom"])

        cmd.extend([pattern, str(search_path)])

        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=30
            , creationflags=0x08000000)
            output = proc.stdout.strip()
            if not output:
                return f"🔍 未找到匹配 '{pattern}' 的结果\n搜索目录: {search_path}"
            lines = output.split("\n")
            count = len(lines)
            header = f"🔍 搜索: '{pattern}' | 目录: {search_path} | 找到 {count} 条"
            if file_types:
                header += f" | 文件类型: {file_types}"
            result = header + "\n" + "=" * 70 + "\n"
            result += "\n".join(lines[:max_results + (context_lines * max_results)])
            if count > max_results:
                result += f"\n\n⚠️ 结果已截断 ({count}→{max_results}条)，请缩小范围"
            return result
        except subprocess.TimeoutExpired:
            return _fallback_grep(pattern, str(search_path), file_types,
                                  max_results, context_lines, ignore_case)
        except FileNotFoundError:
            pass

    # Fallback: Python原生搜索
    if search_path.is_file():
        # 单文件搜索
        try:
            content = Path(search_path).read_text(encoding="utf-8", errors="replace")
            flags = re.IGNORECASE if ignore_case else 0
            pat = re.compile(pattern, flags) if regex else re.compile(re.escape(pattern), flags)
            results = []
            for i, line in enumerate(content.split("\n"), 1):
                if pat.search(line):
                    results.append({"file": str(search_path), "line": i, "content": line.rstrip()[:200]})
            return _format_search_results(results, pattern, str(search_path), max_results, len(results))
        except Exception as e:
            return f"[错误] 搜索失败: {e}"

    return _fallback_grep(pattern, str(search_path), file_types,
                          max_results, context_lines, ignore_case)


def search_semantic(query: str, path: str = ".", top_k: int = 10) -> str:
    """
    语义搜索 — 用自然语言描述来定位代码
    基于关键词分解+正则匹配的近似语义搜索
    (后续可升级为embedding向量搜索)
    """
    search_path = Path(path)
    if not search_path.is_absolute():
        search_path = Path(os.getcwd()) / path
    search_path = search_path.resolve()

    if not search_path.exists():
        return f"[错误] 目录不存在: {path}"

    # 将自然语言查询分解为关键词
    # 常见代码概念映射
    concept_keywords = {
        # 函数/方法
        "函数": [r"def\s+\w+", r"function\s+\w+", r"func\s+\w+", r"fn\s+\w+", r"sub\s+\w+"],
        "类": [r"class\s+\w+", r"struct\s+\w+", r"interface\s+\w+", r"enum\s+\w+"],
        "导入": [r"import\s+", r"from\s+\w+\s+import", r"require\(", r"#include"],
        "错误处理": [r"try\s*:", r"catch\s*\(", r"except\s+", r"error", r"panic!"],
        "测试": [r"def\s+test_", r"test\(.*\{", r"it\(.*\{", r"describe\(.*\{", r"@Test"],
        "配置": [r"\.env", r"config", r"settings", r"\.json", r"\.yaml", r"\.toml"],
        "API": [r"@app\.", r"@router\.", r"app\.(get|post|put|delete)", r"router\.", r"endpoint"],
        "数据库": [r"SELECT", r"INSERT", r"UPDATE", r"DELETE FROM", r"sql", r"mongo", r"db\."],
        "认证": [r"auth", r"login", r"logout", r"token", r"jwt", r"oauth", r"session"],
        "日志": [r"log", r"logger", r"logging", r"console\.log", r"println"],
        "异常": [r"raise\s+", r"throw\s+", r"Exception", r"Error"],
        "异步": [r"async\s+", r"await\s+", r"Promise", r"\.then\(", r"async/await"],
        "路由": [r"@route", r"router", r"path", r"url", r"endpoint"],
        "中间件": [r"middleware", r"interceptor", r"filter", r"before_"],
        "序列化": [r"json\.", r"serialize", r"parse", r"marshal", r"encode", r"decode"],
        "装饰器": [r"@\w+", r"decorator", r"wrapper"],
        "类型": [r"type\s+", r"interface\s+", r"enum\s+", r"typedef"],
    }

    # 解析查询中的概念
    keywords = []
    query_lower = query.lower()

    for concept, patterns in concept_keywords.items():
        if concept in query_lower:
            keywords.extend(patterns)

    # 如果没匹配到概念，直接用查询词
    if not keywords:
        # 提取英文标识符
        identifiers = re.findall(r'[a-zA-Z_][a-zA-Z0-9_]{2,}', query)
        keywords = [re.escape(w) for w in identifiers]

    if not keywords:
        # 直接用整个查询作为搜索词
        keywords = [re.escape(query)]

    # 用所有关键词做OR搜索
    combined = "|".join(keywords)

    results = []
    skip_dirs = {'.git', '__pycache__', 'node_modules', '.venv', 'venv',
                 'target', 'build', 'dist', '.next', 'vendor'}

    # 限制搜索范围，只搜源码文件
    code_exts = {'.py', '.js', '.ts', '.jsx', '.tsx', '.go', '.rs', '.java',
                 '.c', '.cpp', '.h', '.hpp', '.cs', '.rb', '.php', '.swift',
                 '.kt', '.scala', '.vue', '.svelte', '.toml', '.yaml', '.yml',
                 '.json', '.xml', '.md', '.sql', '.sh', '.bat', '.ps1'}

    try:
        pat = re.compile(combined, re.IGNORECASE)
    except re.error:
        return f"[错误] 无法解析查询生成搜索模式: {query}"

    for root, dirs, files in os.walk(search_path):
        dirs[:] = [d for d in dirs if d not in skip_dirs and not d.startswith('.')]

        if len(results) >= top_k * 3:
            break

        for fname in files:
            ext = os.path.splitext(fname)[1].lower()
            if ext not in code_exts:
                continue

            fpath = os.path.join(root, fname)
            try:
                size = os.path.getsize(fpath)
                if size > 500 * 1024:
                    continue
                with open(fpath, 'r', encoding='utf-8', errors='replace') as f:
                    content = f.read()
                    matches = pat.findall(content)
                    if matches:
                        # 计算相关性分数(匹配数量+文件大小)
                        score = min(len(matches) * 10, 100)
                        results.append({
                            "file": fpath, "matches": len(set(matches)),
                            "unique": list(set(matches))[:5], "score": score,
                        })
            except (PermissionError, OSError, UnicodeDecodeError):
                continue

    # 按相关性排序
    results.sort(key=lambda x: (-x["score"], -x["matches"]))

    if not results:
        return f"🔍 语义搜索 '{query}' 未找到相关代码\n搜索目录: {search_path}\n建议: 尝试更具体的功能关键词"

    lines = [f"🧠 语义搜索: '{query}' | 目录: {search_path} | Top {top_k}"]
    lines.append("=" * 70)
    for i, r in enumerate(results[:top_k]):
        rel_path = os.path.relpath(r["file"], search_path) if str(search_path) in r["file"] else r["file"]
        lines.append(f"\n{i+1}. 📄 {rel_path} (匹配 {r['matches']} 次)")
        for kw in r["unique"]:
            lines.append(f"     → {kw}")

    return "\n".join(lines)


def search_files_glob(pattern: str, path: str = ".", exclude_patterns: str = "",
                      max_results: int = 100) -> str:
    """Glob文件搜索(增强版)"""
    import fnmatch
    search_path = Path(path)
    if not search_path.is_absolute():
        search_path = Path(os.getcwd()) / path
    search_path = search_path.resolve()

    excludes = [e.strip() for e in exclude_patterns.split(",") if e.strip()]
    excludes.extend(["__pycache__", "*.pyc", ".git", "node_modules"])

    results = []
    try:
        for p in search_path.rglob(pattern):
            # 检查排除模式
            rel = str(p.relative_to(search_path))
            if any(fnmatch.fnmatch(rel, e) or e in str(p) for e in excludes):
                continue
            if p.is_file():
                size = p.stat().st_size
                results.append((str(p), size))
            elif p.is_dir():
                results.append((str(p) + "/", 0))

            if len(results) >= max_results:
                break
    except PermissionError:
        return f"[错误] 无权限搜索目录: {path}"

    if not results:
        return f"🔍 未找到匹配 '{pattern}' 的文件"

    lines = [f"📁 搜索: '{pattern}' | 找到 {len(results)} 项"]
    lines.append("=" * 60)
    for fpath, size in sorted(results):
        rel = os.path.relpath(fpath, str(search_path)) if str(search_path) in fpath else fpath
        if fpath.endswith("/"):
            lines.append(f"  📂 {rel}")
        else:
            s = f" ({size}B)" if size < 1024 else f" ({size/1024:.1f}KB)" if size < 1024*1024 else f" ({size/(1024*1024):.1f}MB)"
            lines.append(f"  📄 {rel}{s}")

    return "\n".join(lines)


# ═══════════════════════════════════════════
# 注册工具
# ═══════════════════════════════════════════

def register_search_tools():
    registry = get_registry()
    tools = [
        ToolDef("search_code", "ripgrep全文搜索，支持正则/文件类型过滤/上下文。自动fallback到Python原生搜索", {
            "pattern": {"type": "string", "description": "搜索模式(支持正则表达式)"},
            "path": {"type": "string", "description": "搜索目录，默认当前目录"},
            "file_types": {"type": "string", "description": "文件类型过滤，逗号分隔，如 '.py,.js,.ts'"},
            "max_results": {"type": "integer", "description": "最大结果数，默认200"},
            "context_lines": {"type": "integer", "description": "上下文行数，默认0"},
            "ignore_case": {"type": "boolean", "description": "忽略大小写"},
            "regex": {"type": "boolean", "description": "是否正则搜索(默认true)"},
            "fixed_string": {"type": "boolean", "description": "固定字符串搜索(非正则)"},
        }, search_code, category="search", risk_level="safe", required_params=["pattern"]),

        ToolDef("search_semantic", "语义搜索 — 用自然语言描述定位代码。如搜索'用户认证相关代码'", {
            "query": {"type": "string", "description": "自然语言查询，如'数据库连接的函数'、'处理登录的类'"},
            "path": {"type": "string", "description": "搜索目录，默认当前目录"},
            "top_k": {"type": "integer", "description": "返回前K个结果，默认10"},
        }, search_semantic, category="search", risk_level="safe", required_params=["query"]),

        ToolDef("search_files_glob", "按glob模式搜索文件(增强版，含文件大小)", {
            "pattern": {"type": "string", "description": "Glob模式，如 '**/*.py' 或 '*.json'"},
            "path": {"type": "string", "description": "搜索起始目录"},
            "exclude_patterns": {"type": "string", "description": "排除模式，逗号分隔"},
            "max_results": {"type": "integer", "description": "最大结果数，默认100"},
        }, search_files_glob, category="search", risk_level="safe", required_params=["pattern"]),
    ]
    registry.register_many(tools)
    return len(tools)

register_search_tools()
