# Nexie 自检验收文档

生成日期: 2026-06-05

## 一、四层记忆架构 (L1-L4)

### L1 临时缓存
- **代码位置**: `nexie/memory_layers.py` → `L1TempCache` 类
- **测试方式**:
  1. 启动Nexie，执行 `read_file(path="大文件路径")` 或任何返回超大内容的工具
  2. 超过8000字符的结果会自动落地到 `Nexie_data/l1_cache/` 目录（.txt.gz压缩格式）
  3. 上下文只显示摘要索引（头部+尾部预览）
  4. 使用 `list_cached` 查看缓存列表，使用 `get_cached(key, offset, limit)` 按需加载片段
  5. 验证：检查 `Nexie_data/l1_cache/index.json` 中是否有缓存记录
- **关键逻辑**:
  - `CACHE_THRESHOLD = 8000` 字符触发
  - `SUMMARY_MAX_LEN = 500` 摘要最大长度
  - 24小时过期自动清理
  - 最多200个缓存文件

### L2 常驻核心记忆
- **代码位置**: `nexie/memory_layers.py` → `L2CoreMemory` 类
- **测试方式**:
  1. 使用 `remember_core(content="测试需求", mem_type="requirement", importance=8)` 添加记忆
  2. 使用 `search(query="测试")` 搜索记忆
  3. 使用 `list_by_type(mem_type="requirement")` 按类型列出
  4. 重启Nexie后，之前添加的记忆仍然存在（持久化到JSON文件）
  5. 验证：检查 `Nexie_data/l2_core_memory/core_memory.json` 文件
- **关键逻辑**:
  - 6种记忆类型: requirement/modification/config/error/fact/reference
  - importance≥7的条目始终注入system prompt，不会被裁剪
  - 支持去重、标签、全文搜索
  - `build_injection_prompt()` 生成L2上下文注入文本

### L3 自动压缩
- **代码位置**: `nexie/memory_layers.py` → `L3AutoCompressor` 类
- **测试方式**:
  1. 进行大量对话使上下文接近上限（默认96K tokens）
  2. 当Token数超过87%阈值（约83,520 tokens）时自动触发压缩
  3. 使用 `get_token_stats(messages)` 查看当前Token统计
  4. 使用 `get_compress_count()` 查看已压缩次数
  5. 验证：压缩后保留system消息、第一条user、最近4轮对话，中间生成摘要
- **关键逻辑**:
  - `CONTEXT_LIMIT_TOKENS = 96000`
  - `COMPRESS_THRESHOLD = 0.87`
  - `MIN_KEEP_ROUNDS = 4`
  - 保留代码相关内容不省略

### L4 冷归档存储
- **代码位置**: `nexie/memory_layers.py` → `L4ColdArchive` 类
- **测试方式**:
  1. 使用 `archive_content(content="大量内容", source="test", content_type="conversation", tags=["测试"])` 归档
  2. 使用 `search(query="测试")` 关键词检索归档
  3. 使用 `load_chunk(archive_id, chunk=0)` 按分片加载
  4. 使用 `load_full(archive_id)` 加载全部
  5. 验证：检查 `Nexie_data/l4_archive/` 目录是否有压缩存储文件和分片
- **关键逻辑**:
  - `CHUNK_SIZE = 3000` 字符/分片, 重叠300字符
  - `MAX_ARCHIVE_SIZE_MB = 500` 自动清理最旧条目
  - gzip压缩存储
  - 自动关键词提取（TF-based）

## 二、全链路防400/429报错体系

### ① 请求频率限流
- **代码位置**: `nexie/api_resilience.py` → `RateLimiter` 类
- **测试方式**:
  1. 快速连续发送API请求
  2. 观察日志中是否出现"限流等待"信息
  3. 使用 `get_stats()` 查看限流统计
- **关键逻辑**:
  - 默认30次/分钟, 500次/小时
  - 最小间隔500ms
  - 滑动窗口实现

### ② 超长入参自动拆分
- **代码位置**: `nexie/api_resilience.py` → `InputSplitter` 类
- **测试方式**:
  1. 发送超大工具参数（>50000字符）
  2. `should_split_tool_params()` 返回True
  3. `split_tool_params()` 自动拆分为多段
  4. `split_messages_for_request()` 拆分超大消息列表
- **关键逻辑**:
  - `MAX_TOOL_PARAM_CHARS = 50000`
  - `MAX_MESSAGE_CHARS = 80000`
  - 按换行边界智能分割

### ③ 指数退避自动重试
- **代码位置**: `nexie/api_resilience.py` → `ExponentialBackoff` 类
- **测试方式**:
  1. 模拟API返回429/500/502/503错误
  2. 观察是否自动重试（最多5次）
  3. 重试间隔是否指数增长（2s→4s→8s→16s→32s）
  4. 检查 `Nexie_data/logs/retry_errors.log` 重试日志
- **关键逻辑**:
  - `BASE_DELAY = 2.0s`
  - `MAX_DELAY = 120s`
  - `MAX_RETRIES = 5`
  - 30%随机抖动防惊群
  - client.py中已集成: 429/401/403/402自动切换密钥+退避重试

### ④ 多API密钥池轮询
- **代码位置**: `nexie/api_resilience.py` → `APIKeyPool` 类
- **测试方式**:
  1. 在 `Nexie_data/.env` 配置多个密钥:
     ```
     DEEPSEEK_API_KEY=sk-aaa
     DEEPSEEK_API_KEY_2=sk-bbb
     DEEPSEEK_API_KEY_3=sk-ccc
     ```
  2. 启动Nexie，密钥池自动加载
  3. 模拟密钥失败（429/401/402），观察是否自动切换到备用密钥
  4. 使用 `get_available_key_count()` 查看可用密钥数
  5. 连续失败3次的密钥进入5分钟冷却期
- **关键逻辑**:
  - 支持 DEEPSEEK_API_KEY / DEEPSEEK_API_KEY_2..10
  - 支持 MIMO_API_KEY / MIMO_API_KEY_2..10
  - 冷却时间300秒
  - 轮询策略

## 三、权限控制系统

- **代码位置**: `nexie/permission_system.py` → `PermissionController` 类
- **测试方式**:
  1. 执行 `rm -rf /` → 应被拦截
  2. 执行 `del /f /s C:\*` → 应被拦截
  3. 执行 `format C:` → 应被拦截
  4. 执行 `shutdown /s` → 应被拦截
  5. 执行常规命令 `dir` / `ls` / `cat file.txt` → 应放行
  6. 读写任意用户文件 → 应放行
  7. 使用 `get_block_report()` 查看拦截统计
- **关键逻辑**:
  - 18条命令黑名单正则
  - 10条路径黑名单（仅拦截删除操作）
  - 全盘读写默认放开

## 四、命名与打包

- **命名**: 所有源码、标题栏、系统托盘、spec文件均使用 "Nexie" 无版本标注
- **打包**: `build.py` 支持 `--onefile --noconsole` 模式
  - 测试方式: `python build.py onefile`
  - 产物: `dist/Nexie.exe` (单文件)
  - 运行后自动生成 `Nexie_data/` 文件夹

## 五、集成验证清单

| 功能 | 代码位置 | 验收方法 |
|------|---------|---------|
| L1缓存工具结果 | agent_core.py:_exec_single_tool | 执行返回大内容的工具，观察L1落地 |
| L2记忆注入 | agent_core.py:__init__ | system prompt中应包含L2记忆 |
| L3自动压缩 | agent_core.py:process_message | 大量对话后观察压缩触发 |
| L4会话归档 | agent_core.py:end_session | 长会话结束后检查归档 |
| 限流控制 | client.py:chat | 高频请求时观察等待 |
| 密钥池轮询 | client.py:_switch_key | 密钥失败时自动切换 |
| 退避重试 | client.py:_stream_chat | 429/5xx时观察重试 |
| 权限拦截 | agent_core.py:_exec_single_tool | 执行黑名单命令被拦截 |
| 无版本号 | 全项目 | grep无"v5.0"残留 |

## 六、关键文件清单

```
Nexie/
├── main.py                    # 入口 (无版本)
├── agent_core.py              # 核心引擎 (集成四层记忆+权限)
├── client.py                  # API客户端 (集成密钥池+限流+重试)
├── tools.py                   # 工具集
├── app_gui.py                 # GUI界面
├── memory_manager.py          # 基础记忆管理
├── Nexie.spec                 # PyInstaller单文件配置
├── build.py                   # 构建脚本
├── SELF_TEST.md               # 本文档
├── nexie/
│   ├── __init__.py            # 导出所有模块
│   ├── memory_layers.py       # ★ L1+L2+L3+L4 四层记忆
│   ├── api_resilience.py      # ★ 限流+拆分+重试+密钥池
│   ├── permission_system.py   # ★ 权限控制
│   ├── stability.py           # 稳定性模块
│   ├── self_evolution.py      # 自我进化
│   ├── tool_registry.py       # 工具注册
│   ├── ... (其他工具模块)
└── Nexie_data/                # 运行时数据 (自动生成)
    ├── l1_cache/              # L1缓存
    ├── l2_core_memory/        # L2核心记忆
    ├── l4_archive/            # L4归档
    ├── logs/                  # 日志(含重试错误日志)
    └── .env                   # API密钥配置
```
