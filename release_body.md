# 🚀 Nexie v1.1.2 — 源码开放 & 文档润色

> 本次更新开放完整源码，并基于技术架构文档重新润色项目介绍。

---

## 🆕 更新内容

- 📂 **完整源码开放** — `nexie_src/` 目录包含全部 Python 源代码，可直接从源码运行
- 📦 **源码压缩包** — `NexieSource.zip` 方便一键下载全部源码
- 📖 **技术架构文档** — `docs/Nexie_技术架构文档.txt` 详细说明项目结构、启动流程、核心架构、API 韧性体系等
- ✨ **README 全面润色** — 基于技术文档重新组织，补充架构图、线程模型、API 韧性等核心亮点

---

## 📦 下载

| 包 | 大小 | 说明 |
|----|------|------|
| `Nexie.zip` | ~29 MB | Windows 桌面端完整包 |
| `Nexie-mobile.zip` | ~25 MB | 移动端桥梁模块 |
| `NexieSource.zip` | ~39 MB | 完整源码（含依赖 DLL） |

---

## 🏗️ 技术亮点

- **AI 模型**: DeepSeek V4 Pro（主）/ MiMo（视觉）
- **三通道交互**: PC 桌面 Tkinter + 手机 WebSocket + 微信 iLink
- **API 韧性**: 滑动窗口限流 + 密钥池轮询 + 指数退避重试
- **四层记忆**: L1 缓存 → L2 核心 → L3 压缩 → L4 归档
- **权限系统**: 三级风险控制 + 黑名单 + 管理员提升

**🔗 项目地址：** [github.com/lixi210/Nexie-Release](https://github.com/lixi210/Nexie-Release)
