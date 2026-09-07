---
name: hmuitest
description: >-
  HarmonyOS UI 自动化测试工具链：UI 操作（点击/滑动/输入/返回）、断言验证、
  控件树分析/diff、崩溃检测、批量脚本执行、操作录制，全部命令支持 --json
  结构化输出供 AI Agent 消费。Use when 需要执行 HarmonyOS UI 自动化测试、
  功能验收、页面状态验证、跑回归脚本、生成/录制可复用测试脚本 时。
license: MIT
compatibility: linux, macos, windows
metadata:
  device: HarmonyOS
  language: python
  entry: hmuitest.py
---

# hmuitest

统一 CLI 入口 `hmuitest.py`，一条命令完成 UI 操作 + 自动差异报告 + 断言。
所有命令退出码 0/1（0=通过），追加 `--json` 输出结构化 verdict 供 agent 解析。

> 交互流程：每次 `ui` 操作后先读 `ACTION_VERDICT` 与差异报告再执行下一步，
> 禁止不看反馈连续盲操作。同一操作失败 3 次即停止报告，不试第 4 次。

## Quick start

```bash
UITEST="hmuitest.py"

# Deep Link 启动应用（--bundle 必填）
python3 $UITEST aa start "myapp://page" --bundle com.example.app

# 语义化点击 + 断言路由变化
python3 $UITEST ui click-by-text "设置" --expect-route "SettingsPage"

# Agent 模式：结构化输出
python3 $UITEST ui click-by-text "设置" --json
```

设备检测：自动走 env `HARMONY_DEVICE_ID` → session → `hdc list targets`，可 `--device <ID>` 指定。

## 核心工作流

### 探索（首次）
逐命令执行 UI 操作，每步用 `--expect-*` 断言验证，成功后 `ui screenshot` 留证。

### 录制（沉淀可复用脚本）
```bash
python3 $UITEST script record start
python3 $UITEST ui click-by-text "设置"
python3 $UITEST script record stop --output regression.json
```

### 回归（改代码后）
```bash
python3 $UITEST script run regression.json --json
# → {"all_passed": true, "steps": N, "failed": 0}
```
某步失败时：`tree dump` 查当前控件 → 改脚本目标文本 → 重跑。

## 裁决决策树

每条 `ui` 操作输出一行 `ACTION_VERDICT: <状态> | reason=… | suggestion=…`，按此消费：

| 状态 | 含义 | 应对 |
|------|------|------|
| `SUCCESS` | 操作+断言通过 | 继续下一步 |
| `BLOCKED_BY_DIALOG` | 弹窗挡住 | `ui dismiss-dialogs` 后重试一次 |
| `CRASHED` / `RESTARTED` | 进程崩溃 | 重启应用后重试 |
| `NO_CHANGE` | UI 无变化 | `tree dump` 复核（`--expect-no-change` 时是预期，继续） |
| `BACK_INEFFECTIVE` | 返回无效 | 改用目标页导航 |

## Examples

- 用户说"帮我测一下登录流程" → `script record start` → 依次 `click-by-text "登录"` / `input-by-text` / 断言路由 → `script record stop` 生成脚本
- 用户说"检查设置页打开对不对" → `aa start <uri> --bundle <bundleName>` → `ui click-by-text "设置" --expect-route "SettingsPage" --expect-text "语言"`
- 用户说"跑一遍回归" → 找到已有 `*.json` 脚本 → `script run <file> --json`

## Troubleshooting

| 问题 | 处理 |
|------|------|
| 操作全失败且 verdict 为 `NO_CHANGE` | `tree dump --overview` 看页面真实状态，确认目标文本/坐标是否变化 |
| `ui click-by-text` 点错（多匹配） | 追加 `--index N`，或改用 `click-by-id` |
| 图像按钮点不到 | `ui find --type Image` 查 key/id，优先 `click-by-id`，避免坐标 |
| 脚本某步失败 | 详见 `docs/AGENT_GUIDE.md` 的"自愈脚本"章节 |

## References

按需读取，读哪个由当前任务决定：

- **命令大全与参数**（需要精确命令/参数/断言选项时）→ [docs/USAGE.md](docs/USAGE.md)
- **AI Agent 集成**（写 agent 工具定义、解析 verdict、错误恢复时）→ [docs/AGENT_GUIDE.md](docs/AGENT_GUIDE.md)
- **Bridge 扩展**（需要和 App 通信、自定义业务方法时）→ [docs/BRIDGE.md](docs/BRIDGE.md)