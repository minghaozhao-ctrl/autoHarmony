# autoharmony 🎯

> **你的 AI Agent 给自己写的测试工具链。**
> *Agent 探索一次，脚本复用 forever。*

[![License: MIT](https://img.shields.io/github/license/qkdndqxkr5-ctrl/autoHarmony)]()
[![Python 3.9+](https://img.shields.io/badge/python-3.9+-blue)]()
[![Stars](https://img.shields.io/github/stars/qkdndqxkr5-ctrl/autoHarmony)]()
![Platform: HarmonyOS](https://img.shields.io/badge/platform-HarmonyOS-red)

[English](README.md) · [Agent Guide](docs/AGENT_GUIDE.md) · [用法](docs/USAGE.md) · [Bridge](docs/BRIDGE.md)

---

## 痛点，一句话说清

> AI 帮你写 HarmonyOS 代码。
> 谁来测？
> **`autoharmony` 让 Agent 自己测自己的代码——并且记住怎么测。**

你（或 Agent）探索一次 App。每个操作都是一条一行 CLI 命令，返回确定性的 pass/fail。探索过程被录制成可复用的 JSON 脚本。下次代码变更，直接重跑脚本。不需要人写用例、不需要 flaky 工具、不需要把同一流程再输一遍。

**Agent 写代码 → Agent 自主测试 → 脚本沉淀 → 改代码后一键回归**

---

## Demo

<!-- TODO: 用 asciinema 录制终端演示，放 assets/demo.gif 并取消下行注释
![autoharmony demo](assets/demo.gif)
-->

```bash
# Agent（或人）探索 App —— 结构化 verdict 直接返回
$ autoharmony ui click-by-text "设置" --expect-route "SettingsPage" --json
{"status": "SUCCESS", "reason": "route changed to SettingsPage", "exit": 0}

# 同样的流程，保存下来，永续复用
$ autoharmony script record start
$ autoharmony ui click-by-text "设置"
$ autoharmony ui click-by-text "关于"
$ autoharmony script record stop --output settings_test.json
$ autoharmony script run settings_test.json
{"all_passed": true, "steps": 2, "failed": 0}
```

---

## 快速上手

> **五分钟写出并跑通你的第一个测试。**

```bash
pip install autoharmony          # 或：git clone + pip install .
hdc list targets              # 确认设备已连接

autoharmony ui click-by-text "设置" --expect-route "SettingsPage" --json
```

完事。直接可以把 Agent 指向你的 App。

---

## 为什么选 autoharmony？

两个不同的问题，两个不同的答案。

### vs. 现在的 HarmonyOS 测试方案

| | **autoharmony** | HMNextAuto | Hypium（官方） |
|---|---|---|---|
| **为谁设计** | AI Agent | 人写脚本 | 人写测试套件 |
| **第一个测试** | Agent 探索 <1 分钟 | 手写用例 | 搭测试工程 |
| **代码变更后重跑** | `script run test.json` | 重跑库代码 | 全量重建 + runner |
| **Agent 可读输出** | `--json` verdict，exit 0/1 | 人看的报告 | 人看的报告 |
| **探索 → 回归** | 内置录制 | 无 | 无 |
| **崩溃检测** | 内置 | — | — |
| **弹窗自动处理** | 内置 | — | — |
| **业务层 Bridge** | JSON-RPC | — | — |
| **样板代码** | 零（一行命令） | 有一些 | 一大堆 |

### vs. AI 视觉驱动方案（Midscene 等）

| | **autoharmony** | Midscene.js |
|---|---|---|
| **动作触发** | 确定性选择器（text / id / type） | 多模态模型看截图 |
| **确定性** | 100% — 同样的输入，永远同样的判定 | 概率性 — 依赖模型 |
| **速度** | 毫秒级，纯本地 | 秒级（模型推理） |
| **可审计性** | 每个操作 → 控件树 diff + verdict | 重放结果取决于模型当时状态 |
| **运行时依赖** | 无 | VLM API key 或自托管模型 |
| **崩溃检测** | ✅ 进程级 | ❌ 视觉模型看不到进程 |
| **弹窗处理** | ✅ 确定性 | ⚠️ 概率性 |
| **业务层访问** | ✅ JSON-RPC | ❌ 只能操作界面 |

**实话实说：** 如果你的 Agent 需要像人一样*看*界面——验证颜色、布局、视觉还原度——那用视觉驱动方案。但回归测试必须**确定且快**：100 次同样的运行必须给出 100 次同样的判定，毫秒级完成，零抖动。这一半，视觉模型永远做不好。`autoharmony` 就是那「快、可重复、可审计」的一半——而且它还是唯一为 HarmonyOS 而生的。

我们也不是要替代 Hypium——它是好库。我们做的是**Agent 闭环**：探索 → 验证 → 录制 → 重放。这个闭环需要 CLI、结构化输出、即时重跑。这条路上没有别人。

---

## Agent 闭环的特性

- **一行命令 = 一个操作** — 点击、滑动、输入、断言。永远返回 exit code 0/1。
- **全命令 `--json`** — Agent 直接解析 verdict，不用正则。
- **自动 diff 报告** — 每个操作自动快照控件树前后状态，Agent 看到到底变了啥（路由跳转、弹窗、文字切换）。
- **脚本录制** — 探索变成可重放的回归脚本。这是整个工具的意义所在。
- **裁决层** — 崩溃、弹窗卡死、路由变化自动识别，一行总结：
  ```
  ACTION_VERDICT: SUCCESS           | reason=route changed to LoginPage
  ACTION_VERDICT: BLOCKED_BY_DIALOG | reason=dialog '确认' detected
  ACTION_VERDICT: CRASHED           | reason=process died after action
  ```
- **弹窗自动处理** — `ui dismiss-dialogs` / `--auto-handle-dialog` 识别并点掉权限/升级/覆盖层弹窗，流程永远不被卡住。
- **智能滚动查找** — `ui scroll-find "text"` 自动往下滚到目标出现，不需要脆弱坐标，不需要手写滑动。
- **控件树检查器** — `tree dump`、`tree diff`、`tree auto`。给你的 App 配的 DevTools。
- **崩溃检测** — 自动抓 CppCrash / JSCrash / AppFreeze。
- **Bridge 框架** — Agent 通过 JSON-RPC 调 App 业务方法。

---

## 命令速查

| 命令 | 干啥的 | Agent 友好 |
|------|--------|:---:|
| `ui click X Y` | 点坐标 | `--json` |
| `ui click-by-text "text"` | 按文字点击 | `--json` |
| `ui click-by-id "key"` | 按 ID 点击 | `--json` |
| `ui swipe x1 y1 x2 y2` | 滑动 | `--json` |
| `ui input "text"` | 输入文字 | `--json` |
| `ui input-by-text "fld" "txt"` | 往指定输入框输入 | `--json` |
| `ui back` | 返回键 | `--json` |
| `ui scroll-find "text"` | 滚动查找 | `--json` |
| `ui wait-for "text"` | 等文字出现 | `--json` |
| `ui screenshot path.png` | 截图 | `--json` |
| `ui dismiss-dialogs` | 清弹窗 | `--json` |
| `tree dump` | dump 控件树 | `--json` |
| `tree diff a.json b.json` | 对比两棵树 | `--json` |
| `tree auto` | 与上次基线 diff | `--json` |
| `aa start "uri://page" --bundle pkg` | Deep Link 启动 | `--json` |
| `script run file.json` | 重放测试脚本 | `--json` |
| `script record start/stop` | 录制 Agent 探索 | `--json` |

完整用法见 [docs/USAGE.md](docs/USAGE.md)，Agent 集成模式见 [docs/AGENT_GUIDE.md](docs/AGENT_GUIDE.md)。

---

## 断言参数

每个 `ui` 操作都支持这些 flag，验证是一个参数而不是一段代码：

```bash
--expect-route "SettingsPage"       # 路由变了？
--expect-text "保存成功"             # 文字出现了？
--expect-gone "加载中"               # 文字消失了？
--expect-dialog                      # 弹窗弹出来了？
--expect-no-change                   # 啥都没变（回归检查）？
--expect-state "开关:checked=true"   # 控件状态检查
--timeout 10                         # 轮询最多 N 秒
--auto-handle-dialog                 # 自动关挡路的弹窗
```

---

## 批量脚本

一个 JSON 文件、N 步操作、单 driver 连接：

```json
[
  {"cmd": "ui click-by-text", "args": ["登录"],  "expect": {"route": "LoginPage"}},
  {"cmd": "ui input-by-text", "args": ["手机号", "13800138000"]},
  {"cmd": "ui input-by-text", "args": ["密码",   "****"]},
  {"cmd": "ui click-by-text", "args": ["确认"],  "expect": {"route": "MainPage"}},
  {"cmd": "ui screenshot",    "args": ["/tmp/logged_in.png"]}
]
```

```bash
autoharmony script run login_test.json --json
```

---

## Bridge 框架

需要 Agent 触达 App 业务逻辑？`autoharmony` 自带 JSON-RPC TCP bridge——登录登出、用户信息、业务数据，App 注册了什么就能调什么：

```python
from bridge.tcp_bridge import TcpBridge

bridge = TcpBridge("your-device-id")

bridge.call("login", {"account": "13800138000"})
info = bridge.call("getUserInfo")                       # → {"nickname": "星河", "vip": true}
devices = bridge.call("queryDevices", {"room": "客厅"}) # → {"devices": [...]}
bridge.call("logout")

bridge.close()
```

协议与 ArkTS 服务端示例见 [docs/BRIDGE.md](docs/BRIDGE.md)。

---

## 安装为你的 Agent 的 skill

`autoharmony` 以标准 Agent skill 分发——丢给你的编码 Agent，它就会自己验证自己写的鸿蒙代码：

```bash
git clone git@github.com:qkdndqxkr5-ctrl/autoHarmony.git
cp autoHarmony/SKILL.md ~/.claude/skills/autoharmony/SKILL.md   # Claude Code
cp autoHarmony/SKILL.md .cursor/rules/autoharmony.mdc           # Cursor
cp autoHarmony/SKILL.md AGENTS.md                                # Copilot / Codex / Goose
cp autoHarmony/SKILL.md .windsurfrules                           # Windsurf
cp autoHarmony/SKILL.md .clinerules                              # Cline
```

各客户端的配置细节、片段、探索 → 录制 → 重放循环：
→ [docs/agent-guides/](docs/agent-guides/)

## 环境要求

- Python 3.9+
- `hdc`（HarmonyOS Device Connector）在 PATH 中
- 一台 HarmonyOS 设备（USB 或网络连接）

## 贡献

欢迎提 issue 和 PR——尤其是新引擎、更聪明的裁决、真实的 Agent 集成案例。

---

## Star history

[![Star History Chart](https://api.star-history.com/svg?repos=qkdndqxkr5-ctrl/autoHarmony&type=Date)](https://www.star-history.com/#qkdndqxkr5-ctrl/autoHarmony&Date)

好用的话 ⭐ Star 一下——让 Agent 们（和人）都知道这个工具靠谱。

---

## License

MIT