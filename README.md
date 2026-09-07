# hmuitest 🎯

> **Testing toolchain for AI agents.** Agent explores once, script reused forever.

`hmuitest` is a CLI-first HarmonyOS UI testing tool built for AI agents. The agent autonomously explores your app, generates reusable test scripts, and reruns them after every code change — zero human involvement.

```
Agent writes code → Agent tests autonomously → Script saved → Next change: just rerun
```

[中文文档](README_zh.md)

## Why?

Because AI agents shouldn't need a human to write test cases. `hmuitest` gives agents:

- **One command = one action** — click, swipe, input, assert. Each returns exit code 0/1, agent knows instantly pass or fail
- **Structured output** — `--json` flag on every command, agent parses verdict directly, no regex needed
- **Auto diff reports** — every action snapshots widget tree before/after, agent sees exactly what changed
- **Script recording** — agent explores once, actions recorded as reusable JSON scripts
- **Verdict layer** — crash/dialog/route changes auto-detected, one-line `ACTION_VERDICT: SUCCESS | reason=...` for agent consumption
- **Batch scripts** — 20 test steps in one JSON file, single driver connection, fast
- **Bridge framework** — agent can call app-side business methods via JSON-RPC

## How It Works

```
┌──────────────┐     CLI      ┌──────────────┐     hdc     ┌──────────────┐
│  AI Agent    │ ──────────── │  hmuitest    │ ─────────── │  Device      │
│  (Claude/    │  exit 0/1    │  (Python)    │  fport      │  (HarmonyOS) │
│   GPT/etc)   │  JSON out    │              │             │              │
└──────────────┘              └──────────────┘             └──────────────┘
      │                              │
      │    script.json               │
      └──────── saved ───────────────┘
            (reused next time)
```

## Quick Start

```bash
git clone https://github.com/qkdndqxkr5-ctrl/hmuitest.git
cd hmuitest
pip install -r requirements.txt
hdc list targets  # verify device connected
```

### Agent workflow

```bash
# Step 1: Agent explores — each command returns structured verdict
python3 hmuitest.py ui click-by-text "设置" --expect-route "SettingsPage" --json
# → {"status": "SUCCESS", "reason": "route changed", "route": "SettingsPage", "exit": 0}

# Step 2: Agent saves exploration as reusable script
python3 hmuitest.py script record start
python3 hmuitest.py ui click-by-text "设置"
python3 hmuitest.py ui scroll-find "关于"
python3 hmuitest.py ui click-by-text "关于"
python3 hmuitest.py script record stop --output settings_test.json

# Step 3: Next code change — agent reruns script
python3 hmuitest.py script run settings_test.json --json
# → {"all_passed": true, "steps": 3, "failed": 0}
```

### Human workflow (still works)

```bash
python3 hmuitest.py ui click 540 550 --expect-route "SettingsPage"
python3 hmuitest.py tree dump --overview
python3 hmuitest.py tree auto --operation "Open settings"
```

## Commands

| Command | What it does | Agent-friendly |
|---------|-------------|:---:|
| `ui click X Y` | Click coordinates | `--json` |
| `ui click-by-text "text"` | Click widget by text | `--json` |
| `ui click-by-id "key"` | Click widget by ID | `--json` |
| `ui swipe X1 Y1 X2 Y2` | Swipe gesture | `--json` |
| `ui input "text"` | Type into focused field | `--json` |
| `ui input-by-text "target" "text"` | Type into specific field | `--json` |
| `ui back` | Press back key | `--json` |
| `ui scroll-find "text"` | Scroll until text found | `--json` |
| `ui wait-for "text"` | Wait for text to appear | `--json` |
| `ui screenshot path.png` | Save screenshot | `--json` |
| `ui dismiss-dialogs` | Auto-dismiss overlays | `--json` |
| `tree dump` | Dump widget tree | `--json` |
| `tree diff f1.json f2.json` | Compare two trees | `--json` |
| `tree auto` | Auto-diff vs baseline | `--json` |
| `aa start URI` | Launch via Deep Link | `--json` |
| `script run file.json` | Run batch script | `--json` |
| `script record start/stop` | Record agent actions | `--json` |

## Structured Output

Every command supports `--json` for agent consumption:

```bash
$ python3 hmuitest.py ui click-by-text "登录" --expect-route "LoginPage" --json
{
  "status": "SUCCESS",
  "reason": "route changed to LoginPage",
  "action": "click_by_text",
  "target": "登录",
  "route_before": "HomePage",
  "route_after": "LoginPage",
  "exit": 0
}
```

```bash
$ python3 hmuitest.py ui click-by-text "不存在" --json
{
  "status": "NOT_FOUND",
  "reason": "no widget with text '不存在'",
  "action": "click_by_text",
  "target": "不存在",
  "exit": 1
}
```

## Verdict System

Every action produces a machine-readable verdict line:

```
ACTION_VERDICT: SUCCESS | reason=route changed to LoginPage | suggestion=none
ACTION_VERDICT: NO_CHANGE | reason=no widget tree change | suggestion=check if element exists
ACTION_VERDICT: BLOCKED_BY_DIALOG | reason=dialog '确认' detected | suggestion=dismiss dialog first
ACTION_VERDICT: CRASHED | reason=process died after action | suggestion=check crash logs
```

Agent can parse this directly — no need to interpret human-readable output.

## Batch Scripts

```json
[
  {"cmd": "ui click-by-text", "args": ["登录"], "expect": {"route": "LoginPage"}},
  {"cmd": "ui input-by-text", "args": ["手机号", "13800138000"]},
  {"cmd": "ui input-by-text", "args": ["密码", "****"]},
  {"cmd": "ui click-by-text", "args": ["确认"], "expect": {"route": "MainPage"}},
  {"cmd": "ui screenshot", "args": ["/tmp/logged_in.png"]}
]
```

```bash
python3 hmuitest.py script run login_test.json --json
```

## Agent Integration

See [docs/AGENT_GUIDE.md](docs/AGENT_GUIDE.md) for:
- How to integrate with Claude / GPT / custom agents
- Structured output parsing
- Script recording workflow
- Error recovery patterns

## Bridge Framework

Agent can call app-side business methods:

```python
from bridge.tcp_bridge import TcpBridge

bridge = TcpBridge(device="your-device-id")
result = bridge.call("navigate", {"route": "MainPage"})
bridge.close()
```

See [docs/BRIDGE.md](docs/BRIDGE.md) for full extension guide.

## Requirements

- Python 3.9+
- `hdc` in PATH
- Device connected via USB or network

## Contributing

Contributions welcome! Open an issue or submit a PR.

## License

MIT
