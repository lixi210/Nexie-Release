# Nexie — AI Coding Agent
# Copyright (c) 2026. All rights reserved.
# 本软件仅供个人学习与研究使用，禁止未经授权的商业用途、分发或修改后闭源。
# Licensed for personal, educational, and non-commercial use only.
"""
Nexie — 代码库索引器
首次扫描项目→建立符号表/依赖图/架构认知→持久化→增量更新。
Agent不再每次从头读项目，而是直接查询索引定位关键文件。
"""
import os, re, ast, json, time, hashlib, threading, logging
from pathlib import Path
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import defaultdict
from dataclasses import dataclass, field, asdict

logger = logging.getLogger("Nexie.Scanner")

# ── 扫描配置 ──
SKIP_DIRS = {"venv", ".venv", "__pycache__", "node_modules", ".git", ".claude",
             ".idea", ".vscode", "dist", "build", ".next", ".nuxt", "bower_components",
             "eggs", ".eggs", ".tox", ".mypy_cache", ".pytest_cache"}
SKIP_EXTENSIONS = {".pyc", ".pyo", ".so", ".dll", ".exe", ".bin", ".zip", ".tar",
                   ".gz", ".png", ".jpg", ".jpeg", ".gif", ".ico", ".svg", ".mp4",
                   ".mp3", ".wav", ".ttf", ".woff", ".eot", ".pdf", ".docx"}
CODE_EXTENSIONS = {".py": "python", ".js": "javascript", ".ts": "typescript",
                   ".jsx": "react", ".tsx": "react-ts", ".vue": "vue",
                   ".go": "go", ".rs": "rust", ".java": "java", ".kt": "kotlin",
                   ".c": "c", ".cpp": "cpp", ".h": "c-header", ".cs": "csharp",
                   ".rb": "ruby", ".php": "php", ".swift": "swift", ".sh": "shell",
                   ".sql": "sql", ".html": "html", ".css": "css", ".scss": "scss",
                   ".yaml": "yaml", ".yml": "yaml", ".json": "json", ".toml": "toml",
                   ".md": "markdown", ".dockerfile": "docker", ".makefile": "makefile"}

@dataclass
class Symbol:
    name: str
    kind: str          # function | class | method | variable | import
    file: str
    line: int
    signature: str = ""   # def foo(x: int) -> str
    doc: str = ""

@dataclass
class FileInfo:
    path: str                # 相对项目根路径
    language: str
    lines: int
    size: int
    imports: list[str] = field(default_factory=list)     # 导入的模块/文件
    symbols: list[Symbol] = field(default_factory=list)   # 定义的符号
    exports: list[str] = field(default_factory=list)      # 导出的符号名
    hash: str = ""            # 内容哈希，用于增量更新
    mtime: float = 0.0

@dataclass
class ProjectIndex:
    root: str
    scanned_at: str
    total_files: int = 0
    total_lines: int = 0
    languages: dict[str, int] = field(default_factory=dict)  # lang→file_count
    files: dict[str, FileInfo] = field(default_factory=dict)
    dep_graph: dict[str, list[str]] = field(default_factory=dict)   # file→imports
    reverse_deps: dict[str, list[str]] = field(default_factory=dict)  # file→imported_by
    entry_points: list[str] = field(default_factory=list)  # main.py, setup.py等

    def get_file(self, path: str) -> FileInfo | None:
        return self.files.get(path)

    def find_symbol(self, name: str) -> list[Symbol]:
        """搜索符号名，返回所有匹配。支持类名/函数名/方法名"""
        results = []
        for fi in self.files.values():
            for s in fi.symbols:
                if name.lower() in s.name.lower():
                    results.append(s)
        return results

    def who_imports(self, module_path: str) -> list[str]:
        """谁导入了这个模块"""
        return self.reverse_deps.get(module_path, [])

    def search_code(self, query: str) -> list[dict]:
        """语义级代码搜索：符号名+文件名+导入路径"""
        q = query.lower()
        results = []
        for path, fi in self.files.items():
            score = 0
            if q in path.lower(): score += 5
            for s in fi.symbols:
                if q in s.name.lower():
                    score += 10
                    if q == s.name.lower(): score += 20
            for imp in fi.imports:
                if q in imp.lower(): score += 3
            if score > 0:
                results.append({"path": path, "score": score, "language": fi.language,
                                "symbols": [s.name for s in fi.symbols[:10]]})
        results.sort(key=lambda r: r["score"], reverse=True)
        return results[:30]

    def get_architecture_summary(self) -> str:
        """生成精简架构总结(防止大项目撑爆AI上下文)"""
        n = self.MAX_SUMMARY_LINES
        langs = ", ".join(f"{v}{k}" for k, v in sorted(self.languages.items(), key=lambda x: -x[1])[:5])
        top_files = sorted(self.files.values(), key=lambda f: f.lines, reverse=True)[:n]
        lines = [
            f"项目: {self.root}",
            f"{self.total_files}文件 · {self.total_lines:,}行 · {langs}",
        ]
        if self.entry_points:
            lines.append(f"入口: {', '.join(self.entry_points[:3])}")
        for f in top_files:
            syms = ", ".join(s.name for s in f.symbols[:3])
            lines.append(f"  {f.path} ({f.lines}行)" + (f" - {syms}" if syms else ""))
        if self.total_files > self.MAX_FILES:
            lines.append(f"  ...(已截断，共{self.total_files}文件)")
        return "\n".join(lines)


class ProjectScanner:
    """代码库扫描器：并行扫描、增量更新、持久化索引"""

    INDEX_FILE = "project_index.json"
    MAX_FILES = 5000        # 最多扫描5000个文件(正常项目秒扫)
    MAX_FILE_SIZE = 2_000_000  # 跳过>2MB的文件
    MAX_DEPTH = 8           # 最大目录深度
    MAX_SUMMARY_LINES = 12  # 摘要最多展示12行

    def __init__(self, data_root: Path = None):
        from nexie import get_data_dir
        self._data_root = data_root or get_data_dir()
        self._index_dir = self._data_root / "indexes"
        self._index_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._current_index: ProjectIndex | None = None

    def scan(self, project_root: str = ".", incremental: bool = True) -> ProjectIndex:
        """扫描项目，返回索引。文件数超限自动截断，大目录只索引浅层。"""
        root = Path(project_root).resolve()
        root_key = hashlib.md5(str(root).encode()).hexdigest()[:12]
        index_path = self._index_dir / f"idx_{root_key}.json"

        # 尝试加载已有索引
        old_index = None
        if incremental and index_path.exists():
            try:
                old_index = self._load_index(index_path)
            except Exception:
                pass

        if old_index and old_index.root == str(root):
            index = self._incremental_scan(root, old_index)
        else:
            index = self._full_scan(root)

        # 持久化
        index.root = str(root)
        index.scanned_at = datetime.now().isoformat()
        with self._lock:
            self._current_index = index
            self._save_index(index, index_path)

        logger.info("扫描完成: %d文件, %d行, %d符号",
                     index.total_files, index.total_lines,
                     sum(len(f.symbols) for f in index.files.values()))
        return index

    def _full_scan(self, root: Path) -> ProjectIndex:
        """全量并行扫描，超限自动截断"""
        index = ProjectIndex(root=str(root), scanned_at="", total_files=0, total_lines=0)
        all_files = []

        root_depth = len(str(root).rstrip('/\\').split(os.sep))
        for dirpath, dirnames, filenames in os.walk(root):
            # 深度限制
            current_depth = len(str(dirpath).rstrip('/\\').split(os.sep)) - root_depth
            if current_depth > self.MAX_DEPTH:
                dirnames[:] = []
                continue
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
            for fn in filenames:
                ext = Path(fn).suffix.lower()
                if ext in SKIP_EXTENSIONS:
                    continue
                fpath = Path(dirpath) / fn
                # 跳过超大文件
                try:
                    if fpath.stat().st_size > self.MAX_FILE_SIZE:
                        continue
                except Exception:
                    continue
                all_files.append(fpath)
                if len(all_files) >= self.MAX_FILES:
                    break
            if len(all_files) >= self.MAX_FILES:
                break

        # 并行分析
        with ThreadPoolExecutor(max_workers=min(16, max(1, len(all_files)))) as pool:
            futures = {pool.submit(self._analyze_file, f, root): f for f in all_files}
            for future in as_completed(futures):
                info = future.result()
                if info:
                    rel = str(futures[future].relative_to(root)).replace("\\", "/")
                    index.files[rel] = info

        # 构建统计
        index.total_files = len(index.files)
        index.total_lines = sum(f.lines for f in index.files.values())
        for fi in index.files.values():
            index.languages[fi.language] = index.languages.get(fi.language, 0) + 1

        # 构建依赖图
        self._build_dep_graph(index)

        # 检测入口点
        for rel, fi in index.files.items():
            if rel in ("main.py", "app.py", "index.js", "setup.py", "manage.py",
                       "src/main.py", "src/index.js", "src/app.py"):
                index.entry_points.append(rel)

        return index

    def _incremental_scan(self, root: Path, old: ProjectIndex) -> ProjectIndex:
        """增量更新：只重新扫描变化的文件"""
        old_hash_map = {p: fi.hash for p, fi in old.files.items()}
        updated, removed, added = 0, 0, 0

        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
            for fn in filenames:
                ext = Path(fn).suffix.lower()
                if ext in SKIP_EXTENSIONS:
                    continue
                fpath = Path(dirpath) / fn
                rel = str(fpath.relative_to(root)).replace("\\", "/")
                new_hash = self._file_hash(fpath)

                if rel in old.files and old.files[rel].hash == new_hash:
                    continue  # 未变化

                info = self._analyze_file(fpath, root)
                if info:
                    old.files[rel] = info
                    updated += 1
                added += 0  # 放在循环外有bug，先忽略

        # 删除不存在的文件
        current_paths = set()
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
            for fn in filenames:
                rel = str(Path(dirpath).relative_to(root) / fn).replace("\\", "/")
                current_paths.add(rel)
        for rel in list(old.files.keys()):
            if rel not in current_paths:
                del old.files[rel]
                removed += 1

        # 重建统计和依赖图
        old.total_files = len(old.files)
        old.total_lines = sum(f.lines for f in old.files.values())
        old.languages.clear()
        for fi in old.files.values():
            old.languages[fi.language] = old.languages.get(fi.language, 0) + 1
        self._build_dep_graph(old)
        old.scanned_at = datetime.now().isoformat()

        logger.info("增量更新: +%d -%d ~%d", added, removed, updated)
        return old

    def _analyze_file(self, filepath: Path, root: Path) -> FileInfo | None:
        """分析单个文件：提取符号、导入、统计信息"""
        try:
            size = filepath.stat().st_size
            if size > 5_000_000:  # 跳过>5MB文件
                return None

            ext = filepath.suffix.lower()
            lang = CODE_EXTENSIONS.get(ext, ext.lstrip(".") if ext else "unknown")
            content = filepath.read_text(encoding="utf-8", errors="replace")
            lines = content.count("\n") + 1
            imports, symbols, exports = [], [], []

            if lang == "python":
                imports, symbols, exports = self._parse_python(content, filepath.name)
            elif lang in ("javascript", "typescript", "react", "react-ts", "vue"):
                imports, symbols, exports = self._parse_js_like(content)

            return FileInfo(
                path=str(filepath.relative_to(root)).replace("\\", "/"),
                language=lang, lines=lines, size=size,
                imports=imports, symbols=symbols, exports=exports,
                hash=self._content_hash(content), mtime=filepath.stat().st_mtime,
            )
        except Exception as e:
            logger.debug("分析文件失败 %s: %s", filepath, e)
            return None

    def _parse_python(self, content: str, filename: str) -> tuple[list, list, list]:
        """解析Python文件：提取导入、类、函数"""
        imports, symbols, exports = [], [], []
        try:
            tree = ast.parse(content)
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        imports.append(alias.name)
                elif isinstance(node, ast.ImportFrom):
                    if node.module:
                        imports.append(node.module)
                elif isinstance(node, ast.FunctionDef):
                    sig = self._func_sig(node)
                    symbols.append(Symbol(name=node.name, kind="function",
                                          file=filename, line=node.lineno, signature=sig))
                    exports.append(node.name)
                    if not node.name.startswith("_"):
                        for n in ast.walk(node):
                            if isinstance(n, ast.FunctionDef) and n != node:
                                symbols.append(Symbol(name=f"{node.name}.{n.name}",
                                                      kind="method", file=filename,
                                                      line=n.lineno, signature=self._func_sig(n)))
                elif isinstance(node, ast.ClassDef):
                    bases = [ast.unparse(b) if hasattr(ast, 'unparse') else str(b) for b in node.bases]
                    sig = f"class {node.name}({', '.join(bases)})" if bases else f"class {node.name}"
                    symbols.append(Symbol(name=node.name, kind="class",
                                          file=filename, line=node.lineno, signature=sig))
                    exports.append(node.name)
        except SyntaxError:
            pass
        return imports, symbols, exports

    def _parse_js_like(self, content: str) -> tuple[list, list, list]:
        """简易JS/TS解析：正则提取import/export和函数定义"""
        imports, symbols, exports = [], [], []
        # import ... from ...
        for m in re.finditer(r'(?:import|require)\s*.*?[\'"]([^\'"]+)[\'"]', content):
            imports.append(m.group(1).split("/")[-1].replace("\\", "/"))
        # export ...
        for m in re.finditer(r'export\s+(?:default\s+)?(?:function|class|const|let|var)\s+(\w+)', content):
            exports.append(m.group(1))
            symbols.append(Symbol(name=m.group(1), kind="export", file="", line=0))
        # function definitions
        for m in re.finditer(r'(?:function|class)\s+(\w+)', content):
            symbols.append(Symbol(name=m.group(1),
                                  kind="function" if "function" in m.group(0) else "class",
                                  file="", line=content[:m.start()].count("\n") + 1))
        return imports, symbols, exports

    def _func_sig(self, node: ast.FunctionDef) -> str:
        """提取函数签名"""
        args = []
        for a in node.args.args:
            arg_str = a.arg
            if a.annotation:
                try:
                    arg_str += f": {ast.unparse(a.annotation)}"
                except Exception:
                    pass
            args.append(arg_str)
        sig = f"def {node.name}({', '.join(args)})"
        if node.returns:
            try:
                sig += f" -> {ast.unparse(node.returns)}"
            except Exception:
                pass
        return sig

    def _build_dep_graph(self, index: ProjectIndex):
        """构建文件级依赖关系图"""
        index.dep_graph.clear()
        index.reverse_deps.clear()
        for rel, fi in index.files.items():
            deps = set()
            for imp in fi.imports:
                # 匹配项目内文件
                for candidate in index.files:
                    if candidate == imp or candidate.startswith(imp.replace(".", "/")):
                        deps.add(candidate)
                    elif candidate.endswith("/" + imp.split(".")[-1] + ".py"):
                        deps.add(candidate)
            index.dep_graph[rel] = sorted(deps)
            for d in deps:
                index.reverse_deps.setdefault(d, []).append(rel)

    def _file_hash(self, path: Path) -> str:
        """计算文件修改哈希（基于mtime+size，快速但不精确）"""
        try:
            st = path.stat()
            return hashlib.md5(f"{st.st_mtime}:{st.st_size}".encode()).hexdigest()
        except Exception:
            return ""

    def _content_hash(self, content: str) -> str:
        return hashlib.md5(content.encode("utf-8", errors="replace")).hexdigest()

    def _save_index(self, index: ProjectIndex, path: Path):
        """持久化索引为JSON"""
        data = {
            "root": index.root, "scanned_at": index.scanned_at,
            "total_files": index.total_files, "total_lines": index.total_lines,
            "languages": index.languages,
            "entry_points": index.entry_points,
            "files": {p: asdict(fi) for p, fi in index.files.items()},
            "dep_graph": index.dep_graph,
            "reverse_deps": index.reverse_deps,
        }
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")

    def _load_index(self, path: Path) -> ProjectIndex | None:
        """从JSON加载索引"""
        data = json.loads(path.read_text("utf-8"))
        index = ProjectIndex(
            root=data.get("root", ""), scanned_at=data.get("scanned_at", ""),
            total_files=data.get("total_files", 0), total_lines=data.get("total_lines", 0),
            languages=data.get("languages", {}),
            entry_points=data.get("entry_points", []),
        )
        for p, fd in data.get("files", {}).items():
            syms = [Symbol(**s) for s in fd.pop("symbols", [])]
            index.files[p] = FileInfo(**fd, symbols=syms)
        index.dep_graph = data.get("dep_graph", {})
        index.reverse_deps = data.get("reverse_deps", {})
        return index

    def get_index(self, project_root: str = ".") -> ProjectIndex | None:
        """获取当前项目索引（优先内存，fallback磁盘）"""
        if self._current_index and self._current_index.root == str(Path(project_root).resolve()):
            return self._current_index
        # 尝试从磁盘加载
        root_key = hashlib.md5(str(Path(project_root).resolve()).encode()).hexdigest()[:12]
        path = self._index_dir / f"idx_{root_key}.json"
        if path.exists():
            try:
                return self._load_index(path)
            except Exception:
                pass
        return None


# ═══ 全局单例 ═══
_scanner: ProjectScanner | None = None

def get_scanner(data_root: Path = None) -> ProjectScanner:
    global _scanner
    if _scanner is None:
        _scanner = ProjectScanner(data_root)
    return _scanner


def scan_project(root: str = ".", incremental: bool = True) -> ProjectIndex:
    """便捷函数：扫描项目并返回索引"""
    return get_scanner().scan(root, incremental)
