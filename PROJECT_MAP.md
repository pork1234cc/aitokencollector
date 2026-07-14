# 项目地图

## 当前实现

本项目当前采用零强制第三方依赖的单文件 Python 实现。`docs/DESIGN.md` 记录的是完整桌面产品方案，当前代码属于可直接运行的轻量版本。

| 路径 | 职责 |
|---|---|
| `token_stats.py` | 配置、Codex/Claude Code 日志扫描、CLI 输出和 Tkinter 悬浮窗 |
| `tests/test_token_stats.py` | 配置校验、Claude 去重、Codex 累计差分与损坏日志容错测试 |
| `docs/DESIGN.md` | 完整产品目标、数据规则、隐私边界和验收标准 |

## 开发约束

- 只提取 Token 用量字段，不保存对话正文、工具参数或凭据。
- 日志格式解析必须容忍缺失字段、损坏 JSON 行和未知结构。
- 修改去重或累计差分规则时，必须同步更新回归测试。
- 运行测试：`python -m unittest discover -s tests -v`。
