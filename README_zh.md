# hmuitest 🎯

> **为 AI Agent 而生的鸿蒙 UI 测试工具链。** Agent 探索一次，脚本复用 forever。

`hmuitest` 是一个 CLI 优先的 HarmonyOS UI 测试工具，专为 AI Agent 设计。Agent 自主探索你的应用，生成可复用的测试脚本，之后每次代码变更直接跑脚本——零人工介入。

```
Agent 写代码 → Agent 自主测试 → 脚本沉淀 → 改代码后直接回归
```

## 为什么做这个？

因为 AI Agent 不该需要人来写测试用例。`hmuitest` 给 Agent：

- **一行命令 = 一个操作** — 点击、滑动、输入、断言。每条返回 exit code 0/1，Agent 秒懂 pass/fail
- **结构化输出** — 每个命令支持 `--json`，Agent 直接解析 verdict，不需要正则
- **自动 diff 报告** — 每次操作自动快照控件树前后状态，Agent 看到到底变了啥
- **录制功能** — Agent 探索一次，操作自动录制为可复用 JSON 脚本
- **裁决层** — 崩溃/弹窗/路由变化自动检测，一行 `ACTION_VERDICT: SUCCESS | reason=...` 供 Agent 消费
- **批量脚本** — 20 步测试一个 JSON 文件搞定，单 driver 连接，快
- **Bridge 框架** — Agent 可以通过 JSON-RPC 调用 App 的业务方法

## 工作流

```
┌──────────────┐     CLI      ┌──────────────┐     hdc     ┌──────────────┐
│  AI Agent    │ ──────────── │  hmuitest    │ ─────────── │  Device      │
│  (Claude/    │  exit 0/1    │  (Python)    │  fport      │  (HarmonyOS) │
│   GPT/etc)   │  JSON out    │              │             │              │
└──────────────┘              └──────────────┘             └──────────────┘
      │                              │
      │    script.json               │
      └──────── saved ───────────────┘
            (复用下次)
```

## 快速上手

```bash
git clone https://github.com/qkdndqxkr5-ctrl/hmuitest.git
cd hmuitest
pip install -r requirements.txt
hdc list targets  # 确认设备已连接
```

### Agent 工作流

```bash
# 第一步：Agent 探索 — 每个命令返回结构化 verdict
python3 hmuitest.py ui click-by-text "设置" --expect-route "SettingsPage" --json
# → {"status": "SUCCESS", "reason": "route changed", "exit": 0}

# 第二步：Agent 保存探索为可复用脚本
python3 hmuitest.py script record start
python3 hmuitest.py ui click-by-text "设置"
python3 hmuitest.py ui scroll-find "关于"
python3 hmuitest.py script record stop --output settings_test.json

# 第三步：代码变更后，Agent 直接跑脚本
python3 hmuitest.py script run settings_test.json --json
# → {"all_passed": true, "steps": 2, "failed": 0}
```

### 人类工作流（同样可用）

```bash
python3 hmuitest.py ui click 540 550 --expect-route "SettingsPage"
python3 hmuitest.py tree dump --overview
python3 hmuitest.py tree auto --operation "打开设置"
```

## 命令速查

| 命令 | 干啥的 | Agent 友好 |
|------|--------|:---:|
| `ui click X Y` | 点坐标 | `--json` |
| `ui click-by-text "文本"` | 按文字点击 | `--json` |
| `ui click-by-id "key"` | 按 ID 点击 | `--json` |
| `ui swipe X1 Y1 X2 Y2` | 滑动 | `--json` |
| `ui input "text"` | 输入文字 | `--json` |
| `ui input-by-text "目标" "text"` | 往指定输入框输入 | `--json` |
| `ui back` | 返回键 | `--json` |
| `ui scroll-find "文本"` | 滚动查找 | `--json` |
| `ui wait-for "文本"` | 等文字出现 | `--json` |
| `ui screenshot path.png` | 截图 | `--json` |
| `ui dismiss-dialogs` | 关弹窗 | `--json` |
| `tree dump` | dump 控件树 | `--json` |
| `tree diff f1.json f2.json` | 对比两棵树 | `--json` |
| `tree auto` | 自动和上次基线 diff | `--json` |
| `aa start URI` | Deep Link 启动 | `--json` |
| `script run file.json` | 跑批量脚本 | `--json` |
| `script record start/stop` | 录制操作 | `--json` |

## 结构化输出

```bash
$ python3 hmuitest.py ui click-by-text "登录" --json
{
  "status": "SUCCESS",
  "reason": "route changed to LoginPage",
  "action": "click_by_text",
  "target": "登录",
  "exit": 0
}
```

## 裁决系统

每个操作产生机器可读的 verdict：

```
ACTION_VERDICT: SUCCESS | reason=route changed to LoginPage
ACTION_VERDICT: NO_CHANGE | reason=no widget tree change
ACTION_VERDICT: BLOCKED_BY_DIALOG | reason=dialog detected
ACTION_VERDICT: CRASHED | reason=process died
```

Agent 直接解析，不需要人看。

## 断言参数

每个 `ui` 操作都支持：

```bash
--expect-route "SettingsPage"     # 路由变化
--expect-text "保存成功"           # 文字出现
--expect-gone "加载中"             # 文字消失
--expect-dialog                   # 弹窗出现
--expect-no-change                # 无变化（回归检查）
--timeout 10                      # 超时轮询
--auto-handle-dialog              # 自动处理弹窗
```

## 批量脚本

```json
[
  {"cmd": "ui click-by-text", "args": ["登录"]},
  {"cmd": "ui input-by-text", "args": ["手机号", "13800138000"]},
  {"cmd": "ui click-by-text", "args": ["确认"], "expect": {"route": "MainPage"}}
]
```

```bash
python3 hmuitest.py script run login_test.json --json
```

## Agent 集成

详见 [docs/AGENT_GUIDE.md](docs/AGENT_GUIDE.md)：
- 如何与 Claude / GPT / 自定义 Agent 集成
- 结构化输出解析
- 录制工作流
- 错误恢复模式

## Bridge 框架

Agent 可以通过 TCP bridge 调用 App 的业务方法：

```python
from bridge.tcp_bridge import TcpBridge
bridge = TcpBridge(device="your-device-id")
result = bridge.call("navigate", {"route": "MainPage"})
bridge.close()
```

扩展指南见 [docs/BRIDGE.md](docs/BRIDGE.md)。

## 环境要求

- Python 3.9+
- `hdc` 在 PATH 中
- 设备通过 USB 或网络连接

## 贡献

欢迎提 issue 或 PR！

## License

MIT
