# AI Token 统计悬浮窗

本地运行的 Windows 桌面小工具，扫描 Codex 和 Claude Code 的本地日志，用一个圆形透明悬浮表盘实时展示今日 Token 用量。零强制第三方依赖，单文件实现。

## 功能特性

- 圆形透明悬浮窗，外环按 Claude Code / Codex 用量占比分色，圆心显示合计 Token
- 支持深色、浅色、霓虹、极简四套主题
- 拖动定位、透明度调节（20%～100%）、置顶、锁定位置，重启后自动恢复
- 双击表盘立即刷新，右键菜单可调整主题、透明度、刷新间隔
- 可选托盘图标（需安装 `pystray` + `pillow`），最小化后仍可从托盘恢复
- 提供 `--nogui` 命令行模式，支持文本和 JSON 输出，便于脚本集成
- 只提取 Token 用量字段，不保存对话正文、工具参数或凭据

## 环境要求

- Windows 10/11
- Python 3.9+（已在 3.12 上验证）
- 无强制第三方依赖；托盘功能需要 `pip install pystray pillow`

## 快速开始

### 方式一：双击启动（推荐，无需打开 VSCode）

双击项目根目录下的 `启动.bat`，会用 `pythonw` 静默启动悬浮窗（不弹黑色命令行窗口）。

### 方式二：命令行启动

```bash
python token_stats.py                # 启动悬浮窗
python token_stats.py --nogui        # CLI 模式，打印今日统计
python token_stats.py --nogui --days 7
python token_stats.py --nogui --json # JSON 输出，便于脚本消费
```

可选参数：

| 参数 | 说明 |
|---|---|
| `--nogui` | 命令行模式，不启动悬浮窗 |
| `--days N` | 统计天数（含今日），仅 CLI 模式生效 |
| `--json` | JSON 输出，须配合 `--nogui` 使用 |
| `--claude-dir <path>` | 覆盖 Claude Code 日志目录（默认 `~/.claude/projects`） |
| `--codex-dir <path>` | 覆盖 Codex 日志目录（默认 `~/.codex/sessions`） |

## 悬浮窗操作

| 操作 | 效果 |
|---|---|
| 左键拖动圆盘 | 移动窗口（锁定后无效，仅圆盘可见区域可触发） |
| 双击圆盘 | 立即刷新 |
| 右键圆盘 | 打开菜单：置顶 / 锁定位置 / 主题 / 透明度 / 刷新间隔 / 隐藏到托盘（需 pystray）/ 退出 |

窗口位置、透明度、锁定状态和主题保存在配置文件中，重启后自动恢复。

## 配置文件

首次运行会在用户目录生成配置文件：

```text
%USERPROFILE%\.token_stats_config.json
```

包含窗口位置、透明度、置顶、锁定、刷新间隔、主题、日志目录等字段，均带范围校验，非法值自动回退为默认值。

## 统计规则

- **Claude Code**：按 `sessionId + message.id` 去重，避免同一条助手回复的多条日志重复计入；同键出现不同 Usage 时保留 Token 更完整的一条。
- **Codex**：`total_token_usage` 是会话内递增累计值，仅在同一文件内取相邻两条记录的非负差分；累计值发生回退时视为新统计区段起点。
- 日期归属按事件时间戳的本地时区日期计算。
- 完整规则和数据模型见 [`docs/DESIGN.md`](docs/DESIGN.md)。

## 隐私边界

- 所有采集、计算均在本机完成，不上传任何数据。
- 只提取用量字段（token 数量），日志正文解析后立即丢弃，不落盘保存对话内容、工具参数或凭据。

## 测试

```bash
python -m unittest discover -s tests -v
```

覆盖配置校验、Claude 去重、Codex 累计差分和损坏日志容错等场景。

## 打包为独立 exe

```bash
pip install pyinstaller
pyinstaller -F -w --name TokenStats token_stats.py
```

生成的可执行文件体积约 15–20MB，无需安装 Python 环境即可运行。

## 项目结构

| 路径 | 职责 |
|---|---|
| `token_stats.py` | 配置、Codex/Claude Code 日志扫描、CLI 输出和 Tkinter 悬浮窗 |
| `启动.bat` | 双击静默启动悬浮窗（`pythonw`，无黑窗口） |
| `tests/test_token_stats.py` | 配置校验、Claude 去重、Codex 累计差分与损坏日志容错测试 |
| `docs/DESIGN.md` | 完整产品目标、数据规则、隐私边界和验收标准 |
| `PROJECT_MAP.md` | 当前实现范围与开发约束速览 |

当前代码是 `docs/DESIGN.md` 完整产品方案（Electron + Vue 3 + SQLite）的轻量单文件替代实现，可直接运行使用。
