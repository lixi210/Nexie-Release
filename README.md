# Nexie — AI 编程智能体

[![Version](https://img.shields.io/badge/version-v1.1.1-blue)](https://github.com/lixi210/Nexie-Release/releases)
[![Python](https://img.shields.io/badge/python-3.11-green)](https://www.python.org/)
[![Platform](https://img.shields.io/badge/platform-Windows%2010%2F11-lightgrey)](.)
[![License](https://img.shields.io/badge/license-MIT-yellow)](LICENSE)

**Nexie**（奈西，读音 `/ˈneksi/`，中文昵称：力袭，寓意"努力逆袭"）是一个 AI 驱动的桌面编程智能体，基于 **DeepSeek V4 Pro / MiMo 视觉大模型**，能够理解自然语言指令并自动执行编程任务——代码生成、调试、文件管理、Git 操作、数据库查询、系统控制等。

---

## ✨ 核心能力

| 能力模块 | 说明 |
|---------|------|
| 🧠 **AI 编程** | 代码生成、修改、重构、调试，支持 40+ 种编程语言 |
| 🔀 **Git 全覆盖** | 19 个 Git 命令（status/add/commit/push/pull/branch/merge/rebase/cherry-pick/tag/clone/remote/init/create_pr），自动生成 commit message |
| 🗄️ **多数据库** | SQLite / MySQL / PostgreSQL / MongoDB 查询、表结构分析、数据导出 |
| 🌐 **网络能力** | GET/POST/PUT/DELETE/PATCH + 网页抓取 + 文件下载 + 自定义请求头 |
| 📦 **包管理** | npm / yarn / pnpm / pip / cargo / go mod 自动检测与操作 |
| 📁 **文件管理** | 智能文件搜索、读写、批量处理、diff 对比 |
| 🖥️ **桌面自动化** | 鼠标键盘控制、截图、窗口管理、剪贴板操作 |
| ⚡ **命令行** | 一键执行终端命令，自动分析和修复错误 |
| 🔍 **代码审查** | 代码质量分析、安全扫描、性能建议 |

---

## 🏗️ 技术架构

```
┌─────────────────────────────────────────────────────────┐
│                     三通道交互层                          │
│  ┌──────────┐   ┌──────────────┐   ┌──────────────┐    │
│  │ PC 桌面  │   │ 手机 App     │   │ 微信机器人   │    │
│  │ Tkinter  │   │ WebSocket    │   │ iLink API    │    │
│  └────┬─────┘   └──────┬───────┘   └──────┬───────┘    │
├───────┼────────────────┼──────────────────┼────────────┤
│       │           AI 核心调度层                        │
│  ┌────┴────────────────┴──────────────────┴────────┐   │
│  │  AgentCore — 状态机 · 消息处理 · 工具执行 · 回滚 │   │
│  └────┬──────────────────────────────────────┬─────┘   │
│       │          工具注册中心                  │         │
│  ┌────┴────┐  ┌──────────┐  ┌───────────────┐│         │
│  │ 传统工具│  │ 搜索工具 │  │ 自定义扩展... ││         │
│  └─────────┘  └──────────┘  └───────────────┘│         │
├───────────────────────────────────────────────────────┤
│                   基础设施层                            │
│  ┌──────────┐ ┌───────────┐ ┌──────────────┐          │
│  │ API 韧性 │ │ 四层记忆  │ │ 权限系统     │          │
│  │ 限流重试 │ │ L1~L4     │ │ 三级风险控制 │          │
│  └──────────┘ └───────────┘ └──────────────┘          │
└─────────────────────────────────────────────────────────┘
```

### 关键技术栈

- **AI SDK**: OpenAI Python SDK（兼容 DeepSeek API）
- **GUI**: Tkinter + ttk（原生 Windows 暗色主题）
- **系统托盘**: pystray + PIL
- **WebSocket**: websockets（asyncio）
- **加密**: AES-256-GCM（cryptography）
- **桌面自动化**: pyautogui + mss + pyperclip
- **打包**: PyInstaller（单文件 ~31MB）

### 线程模型

| 线程 | 职责 |
|------|------|
| 主线程 | Tkinter 事件循环（仅 UI） |
| Worker 线程 | TaskQueue → AgentCore 任务处理 |
| WebSocket 线程 | asyncio 事件循环（手机通信） |
| HTTP 服务线程 | 文件上传下载服务 |
| 心跳线程 | 心跳监控 |
| 微信轮询线程 | iLink 消息轮询 |

所有跨线程通信通过 `queue.Queue + root.after` 保证线程安全。

### API 韧性体系

- **限流**: 滑动窗口 30次/分钟 + 500次/小时，最小间隔 500ms
- **密钥池**: 支持多密钥轮询，连续失败自动冷却 300s
- **退避重试**: 指数退避 2s→120s，5 次重试，30% 随机抖动
- **SSE 心跳**: 45s 无数据触发重连

---

## 📦 项目结构

```
Nexie/
├── main.py              # 入口：DPI、互斥锁、暗色标题栏
├── app_gui.py           # Tkinter GUI：聊天界面、权限弹窗、系统托盘
├── agent_core.py        # AI 核心调度：状态机、消息处理、工具执行
├── client.py            # API 客户端：DeepSeek + MiMo、流式 SSE、重试
├── tools.py             # 传统工具集：桌面控制、文件操作、剪贴板
├── memory_manager.py    # 知识图谱记忆：实体 + 关系 + 事实 + 会话历史
├── communication.py     # WebSocket 服务器：手机端双向通信 (端口 9527)
├── mobile_bridge.py     # 手机桥接层：消息路由、权限代理
├── wechat_bot.py        # 微信机器人：iLink API 登录、轮询、消息处理
├── http_upload.py       # HTTP 文件服务：手机端上传下载 (端口 9528)
├── relay_server.py      # 公网中继：跨网通信服务器 (部署到 VPS)
├── qr_manager.py        # 二维码生成：手机扫码配对
├── config_gui.py        # 配置界面：API 密钥、模型选择
└── nexie/               # 核心引擎包
    ├── tool_registry.py     # 插件式工具注册中心（装饰器 + 单例）
    ├── stability.py         # 稳定性模块：心跳、超时执行器
    ├── memory_layers.py     # 四层记忆：L1缓存/L2核心/L3压缩/L4归档
    ├── api_resilience.py    # API 韧性：限流、拆分、退避、密钥池
    ├── permission_system.py # 权限控制：三级风险、黑名单
    ├── search_tools.py      # 搜索工具：ripgrep + 语义搜索
    ├── task_queue.py        # FIFO 任务队列：UI/Worker 线程分离
    ├── session_memory.py    # 长期记忆引擎：规则驱动、零 API 成本
    ├── budget.py            # Token 预算追踪
    └── workspace.py         # 工作空间持久化
```

---

## 🚀 快速开始

### 桌面端（Windows 10/11）

1. 下载 `Nexie.zip`（约 29MB）
2. 解压到任意文件夹
3. 双击运行 `Nexie.exe`
4. 配置 DeepSeek API 密钥即可使用

**系统要求**: 8GB+ 内存，Windows 10/11

### 手机端（Android 8.0+）

1. 下载 `Nexie-mobile.zip`
2. 通过 WebSocket 与桌面端配对（扫码连接）
3. 支持局域网直连、手机热点、公网中继三种模式

### 从源码运行

```bash
git clone https://github.com/lixi210/Nexie-Release.git
cd Nexie-Release/nexie_src
pip install -r requirements.txt
python main.py
```

---

## 🔗 三通道交互

| 通道 | 协议 | 端口 | 适用场景 |
|------|------|------|---------|
| PC 桌面 | Tkinter GUI | — | 主力编程环境 |
| 手机 App | WebSocket | 9527 | 移动办公 |
| 微信机器人 | iLink API (HTTPS) | — | 轻量交互 |

---

## 📄 文档

- [技术架构文档](docs/Nexie_技术架构文档.txt) — 完整架构说明

---

> ⚠️ 注意：部分杀毒软件可能误报 PyInstaller 打包的程序，请添加信任。Nexie 不会收集用户数据，所有 API 调用直连 DeepSeek 官方。
