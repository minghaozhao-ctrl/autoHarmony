---
name: hmuitest
description: HarmonyOS UI 自动化测试工具：统一 CLI 入口，HDC UI 操作（自动差异报告）、断言验证、批量脚本、控件树分析、AI Agent 集成。Use when 执行 HarmonyOS UI 自动化测试、页面验证、功能验收，或需要从自然语言用例生成稳定测试脚本时。
argument-hint: '输入测试场景或脚本文件路径，例如：点击按钮 / 验证页面变化 / script run ./cases.json。'
user-invocable: true
---

# HarmonyOS 自动化测试工具

## 概述

所有功能通过统一入口 `hmuitest.py` 提供，子命令结构互斥（parse 时即校验）：

| 子命令 | 核心能力 | 典型场景 |
|--------|---------|---------|
| `aa` | Deep Link 显式启动（aa start） | 跳转指定 URI |
| `ui` | 点击/滑动/输入/返回 + `--expect-*` 断言，操作后自动差异报告 | 执行 UI 操作并验证 |
| `tree` | 概览、搜索、差异比较（本地文件 / 远程 dump） | 分析 UI 状态、验证变化 |
| `script` | 批量脚本执行 / 录制操作 | 跑完整测试流程、录制可复用脚本 |

**核心优势**：一条命令完成操作+差异报告+页面状态摘要；`--expect-*` 让 exit code 直接反映 pass/fail；`--json` 输出结构化结果供 AI Agent 解析。

## AI 行为约束（强制，防盲走）

每次 `ui` 操作后会自动输出两层反馈，AI 必须按以下规则消费，**禁止不看反馈连续盲操作**：

**自动反馈（引擎产出，零参数）**：
- `ACTION_VERDICT: <状态> | reason=原因 | suggestion=下一步`：每次操作的一行机器可读结论（最高优先级，消费规则见下）
- 差异报告 + `PAGE_RESULT:` 标记（CHANGES_DETECTED / NO_CHANGES / ROUTE_CHANGED）
- 断言失败时打印**实际值 vs 期望值**对照与页面现有文本样本

**裁决纠错闭环（意外情况标准应对，禁止盲目重试）**：
1. `SUCCESS` → 继续下一步
2. `BLOCKED_BY_DIALOG` → 按 suggestion 点击弹窗按钮，或 `ui dismiss-dialogs` 清理后**重试一次**
3. `BACK_INEFFECTIVE` → 确认路由栈状态；需回退改用其他导航方式
4. `CRASHED` / `RESTARTED` → 恢复应用后重试
5. `NO_CHANGE` → `tree dump` 复核实际状态再决定（`--expect-no-change` 时是预期，继续）

**强制规则**：
1. **状态确认门槛**：每步操作后，先读 `ACTION_VERDICT` 与状态摘要，非 SUCCESS 且不在上述闭环内 → 禁止执行下一步
2. **复核义务**：见到 `NO_CHANGE`（且本意是改 UI）→ 先复核再继续
3. **三振止损**：同一操作连续失败 3 次 → 停止并报告，禁止试第 4 次

## 环境要求

- Python 3.9+
- `hdc` 在 PATH 中
- 设备已连接（`hdc list targets` 可查）

## Quick start

```bash
UITEST="${SKILL_DIR:-.}/hmuitest.py"

# Deep Link 启动应用
python3 $UITEST aa start "myapp://page" --bundle com.example.app

# HDC 点击操作（自动返回差异报告）
python3 $UITEST ui click 540 550 --operation "点击按钮"

# 语义化点击 + 断言验证
python3 $UITEST ui click-by-text "设置" --expect-route "SettingsPage" --operation "点击设置"

# AI Agent 模式（结构化输出）
python3 $UITEST ui click-by-text "设置" --json

# 录制操作序列
python3 $UITEST script record start
python3 $UITEST ui click-by-text "设置"
python3 $UITEST ui scroll-find "关于"
python3 $UITEST script record stop --output test.json

# 批量执行
python3 $UITEST script run test.json
```

## 决策树

| 任务 | 推荐路径 |
|------|----------|
| 跳转指定 URI / Deep Link | `aa start <URI> --bundle <bundleName>` |
| 单次 UI 操作验证（点/滑/输入） | `ui click` 等坐标操作 |
| 语义化操作（按文本/ID 点击） | `ui click-by-text` 等 |
| 操作后精确验证（路由/文本/弹窗/状态） | 任意 `ui` 操作追加 `--expect-*` |
| 多步用例一键执行 | `script run cases.json` |
| 检查弹窗/控件是否存在 | `ui check-dialog` / `ui check-exist` |
| 长列表滚动查找 | `ui scroll-find` |
| 被弹窗挡住 / 清理弹窗 | `ui dismiss-dialogs` |
| 等待加载/异步文本出现或消失 | `ui wait-for` |
| 差异对比、回归验证 | `tree auto` 或 `tree diff` |
| AI Agent 自动化测试 | 所有命令追加 `--json` |

## 子命令速查

### ui — UI 操作 + 断言

**坐标操作**：`click` / `double-click` / `long-click` / `swipe` / `input` / `back`

**语义化操作**：`click-by-text` / `click-by-id` / `click-by-type` / `double-click-by-text` / `long-click-by-text` / `input-by-text` / `input-by-type` / `swipe-direction`

**辅助工具**：`screenshot` / `check-dialog` / `check-exist` / `find` / `scroll-find` / `dismiss-dialogs` / `wait-for`

### tree — 控件树分析

- `tree show <file>` — 分析本地 JSON 文件
- `tree dump` — 从设备 dump 并分析
- `tree diff <f1> <f2>` — 对比两个控件树
- `tree auto` — 自动与上次基线 diff

### script — 批量脚本

- `script run <file.json>` — 执行批量脚本
- `script record start/stop` — 录制操作序列

### aa — Deep Link 启动

- `aa start <URI> --bundle <bundleName>` — 通过 Deep Link 启动应用

## 断言参数

每个 `ui` 操作都支持：

```bash
--expect-route "RouteName"       # 路由变化
--expect-text "文本"              # 文字出现
--expect-gone "文本"              # 文字消失
--expect-dialog                  # 弹窗出现
--expect-no-change               # 无变化（回归检查）
--expect-state "组件:attr=value"  # 控件状态
--timeout 10                     # 超时轮询（秒）
--auto-handle-dialog             # 自动处理弹窗
```

## 输出格式

```bash
# 默认：人类可读
python3 hmuitest.py ui click-by-text "设置"
# ACTION_VERDICT: SUCCESS | reason=route changed to SettingsPage

# Agent 可解析 JSON
python3 hmuitest.py ui click-by-text "设置" --json
# {"status": "SUCCESS", "reason": "route changed to SettingsPage", "exit": 0}
```

## 项目结构

```
hmuitest/
├── hmuitest.py           # CLI 入口
├── engines/
│   ├── engines.py        # 操作引擎（HdcUITestEngine / HypiumEngine）
│   ├── diff_engine.py    # 控件树差异比较
│   ├── assertions.py     # 断言库
│   └── verdict.py        # 统一裁决层
├── analyzers/
│   ├── widget_tree.py    # 控件树分析器
│   └── crash_detector.py # 崩溃检测
├── utils/
│   ├── hdc.py            # hdc 工具函数
│   └── common.py         # 通用工具
├── bridge/
│   └── tcp_bridge.py     # TCP Bridge（JSON-RPC 2.0）
└── docs/
    ├── AGENT_GUIDE.md    # AI Agent 集成指南
    ├── BRIDGE.md         # Bridge 扩展指南
    └── USAGE.md          # 完整使用指南
```
