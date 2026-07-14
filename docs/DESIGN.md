# AI Token 桌面统计工具设计方案

> 日期：2026-07-13  
> 当前阶段：DESIGN  
> 目标平台：Windows  
> 首期数据源：Codex、Claude Code

## 1. 项目目标

开发一个本地运行的桌面 Token 统计工具，通过读取 Codex 和 Claude Code 的本地使用日志，展示实时及历史 Token 消耗。

工具以透明桌面悬浮窗为主要交互形态，支持任意拖动、调整透明度、置顶、锁定位置、手动隐藏和托盘恢复。默认只提取用量字段，不保存提示词、回复正文和工具输出。

## 2. 选定方案

采用“本地日志适配器 + 统一事件模型 + SQLite 聚合 + 桌面悬浮窗”的架构。

第一版直接读取本机已有日志，不代理网络请求，也不修改 Codex 或 Claude Code 的配置。每个工具由独立 Adapter 负责解析，解析结果统一转换为 `UsageEvent`，写入本地 SQLite。界面只查询聚合数据，不直接处理原始日志。

```text
Codex sessions/*.jsonl ──> CodexAdapter ──┐
                                          ├─> UsageEvent ─> SQLite ─> 悬浮窗/详情面板
Claude projects/*.jsonl ─> ClaudeAdapter ─┘
                         └> stats-cache.json（历史校验）
```

## 3. 本机验证结果

以下结论来自 2026-07-13 对当前机器的只读检查。日志属于产品内部存储格式，不应视为官方稳定接口。

### 3.1 Codex

日志目录：

```text
%USERPROFILE%\.codex\sessions\YYYY\MM\DD\rollout-*.jsonl
```

本机抽样记录版本：

```text
cli_version = 0.144.1
```

Token 事件：

```text
type = event_msg
payload.type = token_count
```

可用字段：

```text
payload.info.total_token_usage:
  input_tokens
  cached_input_tokens
  output_tokens
  reasoning_output_tokens
  total_tokens

payload.info.last_token_usage:
  input_tokens
  cached_input_tokens
  output_tokens
  reasoning_output_tokens
  total_tokens

payload.rate_limits.primary:
  used_percent
  window_minutes
  resets_at
```

`total_token_usage` 是会话内递增的累计值，不能把每条事件直接相加。

统计规则：

- 全量扫描：每个会话使用最后一条有效 `total_token_usage`。
- 实时增量：计算相邻两条 `total_token_usage` 的非负差值。
- 如果累计值发生回退或重置，将当前值视为新的统计区段起点。
- `last_token_usage` 用于辅助核对增量，不作为唯一恢复依据。
- 日期归属按 Token 事件自身时间戳确定，不能把跨日会话全部计入会话开始日。

Codex 日志目前没有验证到可靠的费用字段。订阅额度与 API 按量账单不是同一概念，因此第一版只展示 Token、缓存和额度窗口，不推算货币费用。

### 3.2 Claude Code

项目会话日志：

```text
%USERPROFILE%\.claude\projects\<project>\*.jsonl
```

本机抽样记录版本：

```text
version = 2.1.206
```

助手消息中的用量字段：

```text
type = assistant
message:
  id
  model
  usage:
    input_tokens
    output_tokens
    cache_creation_input_tokens
    cache_read_input_tokens
    server_tool_use
    service_tier
```

Claude Code 会针对同一个模型响应写入多条 `assistant` 记录。抽样中，同一 `message.id` 的多条记录具有相同 Usage，如果逐行累加会严重重复统计。

统计规则：

- 主去重键：`sessionId + message.id`。
- `requestId` 作为辅助去重及诊断字段。
- 同一去重键只写入一条 Usage。
- 如果相同去重键出现不同 Usage，记录解析告警并保留 Token 更完整的一条。
- `isSidechain` 数据默认计入总消耗，同时保留字段供界面区分主会话与子任务。

Claude Code 聚合缓存：

```text
%USERPROFILE%\.claude\stats-cache.json
```

可用聚合数据包括：

```text
dailyActivity
dailyModelTokens
modelUsage
totalSessions
totalMessages
longestSession
firstSessionDate
hourCounts
```

其中 `modelUsage` 包含：

```text
inputTokens
outputTokens
cacheReadInputTokens
cacheCreationInputTokens
webSearchRequests
costUSD
contextWindow
maxOutputTokens
```

`stats-cache.json` 可能延迟更新，因此只用于历史初始化、费用参考和结果校验，实时统计仍以 `projects/**/*.jsonl` 为准。

## 4. 统一数据模型

```text
UsageEvent
  id
  source                  codex | claude_code
  source_version
  session_id
  request_id
  message_id
  project_path
  model
  event_time
  input_tokens
  cached_input_tokens
  cache_creation_tokens
  output_tokens
  reasoning_tokens
  total_tokens
  cost_usd
  is_sidechain
  accuracy                exact | parsed | estimated
  source_file
  source_offset
  created_at
```

设计约束：

- Token 字段统一使用非负整数。
- 不适用或来源中不存在的字段保存为 `NULL`，不伪造为零。
- `cost_usd` 只接收来源明确提供的值，不默认按公开 API 单价推算。
- 数据库内保存规范化项目标识；界面展示时可只显示项目目录名。
- `source_file + source_offset` 用于断点续读和诊断，不用于界面公开展示。

## 5. 核心模块

### 5.1 Collector Manager

- 发现 Codex、Claude Code 日志目录。
- 启动全量扫描和实时监听。
- 管理 Adapter 版本、状态及错误。
- 使用文件监听及时刷新，并通过定时轮询补偿漏报事件。

### 5.2 CodexAdapter

- 识别 `rollout-*.jsonl`。
- 提取 `session_meta`、`turn_context` 和 `token_count`。
- 处理累计值、增量、跨日和重置。
- 提取额度百分比及重置时间。

### 5.3 ClaudeAdapter

- 识别 `projects/**/*.jsonl`。
- 提取 `assistant.message.usage`。
- 按 `sessionId + message.id` 去重。
- 读取 `stats-cache.json` 进行聚合校验。

### 5.4 Storage

- 使用 SQLite 保存事件、扫描游标、聚合结果和解析告警。
- 记录每个文件的大小、最后修改时间和最后读取偏移量。
- 文件截断或替换时自动重新建立游标，依靠唯一键避免重复入库。

### 5.5 Aggregator

- 按小时、日、周、月聚合。
- 按来源、模型、项目和会话聚合。
- 分别统计输入、缓存、缓存创建、输出和推理 Token。
- 计算缓存占比和最近一小时消耗速度。

### 5.6 Desktop UI

- 悬浮窗：显示 Codex、Claude Code 和今日合计。
- 详情面板：显示趋势、来源占比、模型占比和项目排行。
- 托盘菜单：显示/隐藏、锁定、置顶、开机启动、退出。
- 设置面板：透明度、刷新频率、日志目录和隐私选项。

## 6. 悬浮窗交互

第一版必须支持：

- 无边框透明窗口。
- 任意拖动并保存位置。
- 20%～100% 透明度调整。
- 始终置顶。
- 锁定位置。
- 手动隐藏和托盘恢复。
- 双击展开详情。
- 记住多显示器位置。

后续可增加：

- 鼠标穿透。
- 全局显示/隐藏快捷键。
- 屏幕边缘吸附。
- 鼠标离开后自动降低透明度。
- 全屏应用运行时自动隐藏。
- Token 或额度阈值通知。

## 7. 技术栈

| 用途 | 选择 | 原因 |
|---|---|---|
| 桌面框架 | Electron | 透明窗口、置顶、透明度、托盘和鼠标穿透能力完整，首版实现风险低 |
| 前端 | Vue 3 + TypeScript | 适合轻量状态界面和设置面板，类型约束清晰 |
| 本地数据库 | SQLite | 单机使用、无需部署，便于增量写入和聚合查询 |
| 图表 | ECharts | 支持日趋势、来源占比和项目排行 |
| 数据采集 | Node.js Adapter | 能直接与 Electron 主进程整合并进行文件增量读取 |
| 打包 | electron-builder | 生成 Windows 安装版和便携版 |

## 8. 隐私与安全边界

- 所有采集、计算和存储默认在本机完成。
- 不上传原始日志和统计数据。
- 不保存提示词、回复正文、工具参数、工具输出和文件内容。
- 日志行只在解析期间存在于内存，提取 Usage 后立即丢弃正文。
- 界面默认隐藏完整项目路径，只显示项目名。
- 数据导出必须由用户主动触发。
- 不读取 `auth.json`、`.credentials.json`、API Key 或登录凭据。

## 9. 兼容性与容错

Codex 和 Claude Code 的本地日志格式都可能随版本变化，因此：

- 每个 Adapter 必须检测来源版本。
- 所有字段使用安全读取，单条异常不能中断整个扫描。
- 未识别格式进入解析告警，不猜测字段含义。
- 保存少量不含正文的结构摘要用于诊断。
- 为当前验证版本建立脱敏 Fixture 和单元测试。
- 界面显示各数据源状态：正常、延迟、格式变化、目录不存在。

## 10. MVP 范围

第一阶段只完成：

1. 自动发现 Codex 和 Claude Code 默认日志目录。
2. 扫描历史 JSONL 并建立 SQLite 索引。
3. 正确处理 Codex 累计值和 Claude Code 重复消息。
4. 实时监听新增日志。
5. 展示今日 Codex、Claude Code 和合计 Token。
6. 展示输入、缓存、输出、推理分类。
7. 实现透明悬浮窗、拖动、置顶、隐藏和托盘。
8. 提供七天趋势和项目排行。
9. 不采集或存储任何对话正文。

第一阶段不做：

- 不代理 Codex 或 Claude Code 网络请求。
- 不接入云端账号或团队管理后台。
- 不支持 Cursor 等其他 AI 工具。
- 不根据公开 API 单价估算 Codex 订阅费用。
- 不做跨设备同步。
- 不上传遥测数据。

## 11. 验收标准

- 对同一批固定日志重复扫描两次，数据库总 Token 不发生变化。
- Claude Code 同一 `message.id` 的重复行只统计一次。
- Codex 多条累计事件不会被直接求和。
- 新日志写入后，悬浮窗在设定刷新周期内更新。
- 重启应用后从断点继续读取，不重复入库。
- 删除数据库重新扫描后，聚合结果与首次扫描一致。
- 日志包含损坏行时，其余正常行仍能成功统计。
- 数据库及导出结果中不存在提示词和回复正文。
- 隐藏、透明度、置顶和窗口位置在重启后保持。

## 12. 后续阶段

下一阶段进入 SCAFFOLD：初始化 Electron、Vue 3、TypeScript、SQLite 和测试框架，建立 Adapter 接口及脱敏日志 Fixture，再优先实现 Claude Code Adapter，随后实现 Codex Adapter。
