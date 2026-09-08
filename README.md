# autoharmony 🎯

> **The test toolchain your AI agent writes for itself.**
> *Agent explores once, script reused forever.*

[![License: MIT](https://img.shields.io/github/license/qkdndqxkr5-ctrl/autoHarmony)]()
[![Python 3.9+](https://img.shields.io/badge/python-3.9+-blue)]()
[![Stars](https://img.shields.io/github/stars/qkdndqxkr5-ctrl/autoHarmony)]()
![Platform: HarmonyOS](https://img.shields.io/badge/platform-HarmonyOS-red)

[中文文档](README_zh.md) · [Agent Guide](docs/AGENT_GUIDE.md) · [Usage](docs/USAGE.md) · [Bridge](docs/BRIDGE.md)

---

## The problem, in one line

> AI writes your HarmonyOS code.
> Who tests it?
> **`autoharmony` lets the agent test its own work — and remembers how.**

You (or an agent) explore the app once. Every action is a one-line CLI command that returns a yes/no verdict. The exploration is recorded as a reusable JSON script. Next time the code changes, just rerun the script. No human test cases. No flaky utilities. No retyping the same flow ever again.

**Agent writes code → Agent tests autonomously → Script saved → Next change: just rerun**

---

## Demo

<!-- TODO: record terminal demo with asciinema and place gif here
     Suggested: 1) script record start  2) a few ui clicks with --json
     3) script record stop --output demo.json  4) script run demo.json
     Put the .gif in assets/demo.gif and uncomment the line below.
![autoharmony demo](assets/demo.gif)
-->

```bash
# Agent (or human) explores the app — structured verdict comes back
$ autoharmony ui click-by-text "设置" --expect-route "SettingsPage" --json
{"status": "SUCCESS", "reason": "route changed to SettingsPage", "exit": 0}

# The same flow, saved and rerun forever
$ autoharmony script record start
$ autoharmony ui click-by-text "设置"
$ autoharmony ui click-by-text "关于"
$ autoharmony script record stop --output settings_test.json
$ autoharmony script run settings_test.json
{"all_passed": true, "steps": 2, "failed": 0}
```

---

## Quick Start

> **Write & run your first test in under five minutes.**

```bash
pip install autoharmony          # or: git clone + pip install .
hdc list targets              # verify a device is connected

autoharmony ui click-by-text "设置" --expect-route "SettingsPage" --json
```

That's it. You're ready to point an agent at your app.

---

## Why autoharmony?

Two different questions need two different answers.

### vs. HarmonyOS testing today

| | **autoharmony** | HMNextAuto | Hypium (official) |
|---|---|---|---|
| **Made for** | AI Agents | Humans writing scripts | Humans writing test suites |
| **First test** | Agent explores in <1 min | Write tests by hand | Build a test project |
| **Re-run after code change** | `script run test.json` | Re-run library code | Full rebuild + runner |
| **Agent-readable output** | `--json` verdict, exit 0/1 | Human report | Human report |
| **Exploration → regression** | Built-in recorder | No | No |
| **Crash detection** | Built-in | — | — |
| **Auto dialog handling** | Built-in | — | — |
| **Business-layer bridge** | JSON-RPC | — | — |
| **Boilerplate** | Zero (one-liners) | Some | A lot |

### vs. AI vision-driven automation (Midscene et al.)

| | **autoharmony** | Midscene.js |
|---|---|---|
| **Trigger** | Deterministic selectors (text / id / type) | Multimodal VLM reading screenshots |
| **Determinism** | 100% — same input, same verdict, every run | Probabilistic — model-dependent |
| **Speed** | Milliseconds per action, fully local | Seconds per action (model inference) |
| **Auditability** | Every action → widget-tree diff + verdict line | Replay depends on current model state |
| **Runtime deps** | None | VLM API key or self-hosted model |
| **Crash detection** | ✅ process-level | ❌ visual models can't see it |
| **Dialog handling** | ✅ deterministic | ⚠️ probabilistic |
| **Business-layer access** | ✅ JSON-RPC | ❌ UI only |

**The honest split:** if your agent needs to *see* the UI like a human — verify colors, layout, visual polish — use a vision-driven tool. But a regression loop must be **deterministic and fast**: 100 identical runs must give 100 identical verdicts, in milliseconds, with zero flakiness. That's the half of the loop vision models will always be bad at. `autoharmony` is the fast, repeatable, auditable half — and it's the only one built for HarmonyOS.

We're not here to replace Hypium either — it's a solid library. We're here for the **agent loop**: explore → verify → record → replay. That loop needs a CLI, structured output, and instant re-runs. Nobody else has it.

---

## Features for the agent loop

- **One command = one action** — click, swipe, input, assert. Always returns exit code 0/1.
- **`--json` on everything** — agents parse verdicts directly, zero regex.
- **Auto diff reports** — every action snapshots the widget tree before/after, so the agent sees exactly what changed (route shifts, dialogs, toggled text).
- **Script recorder** — exploration becomes a replayable regression script. This is the whole point.
- **Verdict layer** — crash, stuck-dialog, and route-change auto-detected, summarized in one line:
  ```
  ACTION_VERDICT: SUCCESS          | reason=route changed to LoginPage
  ACTION_VERDICT: BLOCKED_BY_DIALOG| reason=dialog '确认' detected
  ACTION_VERDICT: CRASHED          | reason=process died after action
  ```
- **Auto dialog handling** — `ui dismiss-dialogs` / `--auto-handle-dialog` recognize and clear permission/upgrade/overlay popups so the flow never gets stuck.
- **Smart scroll-find** — `ui scroll-find "text"` scrolls until the target appears — no fragile coordinates, no manual swipes.
- **Widget tree inspector** — `tree dump`, `tree search`, `tree diff`, `tree auto`. DevTools for your app.
- **Crash detector** — CppCrash / JSCrash / AppFreeze caught automatically after actions.
- **Bridge framework** — agents can call app-side business methods over JSON-RPC.

---

## Command reference

| Command | What it does | Agent-friendly |
|---------|--------------|:---:|
| `ui click X Y` | Click coordinates | `--json` |
| `ui click-by-text "text"` | Click widget by text | `--json` |
| `ui click-by-id "key"` | Click widget by ID | `--json` |
| `ui swipe x1 y1 x2 y2` | Swipe gesture | `--json` |
| `ui input "text"` | Type into focused field | `--json` |
| `ui input-by-text "fld" "txt"` | Type into a specific field | `--json` |
| `ui back` | Press back | `--json` |
| `ui scroll-find "text"` | Scroll until found | `--json` |
| `ui wait-for "text"` | Wait for text to appear | `--json` |
| `ui screenshot path.png` | Save screenshot | `--json` |
| `ui dismiss-dialogs` | Clear overlay dialogs | `--json` |
| `tree dump` | Dump widget tree | `--json` |
| `tree diff a.json b.json` | Compare two trees | `--json` |
| `tree auto` | Diff vs last baseline | `--json` |
| `aa start "uri://page" --bundle pkg` | Deep-link launch | `--json` |
| `script run file.json` | Replay test script | `--json` |
| `script record start/stop` | Record agent exploration | `--json` |

Full usage in [docs/USAGE.md](docs/USAGE.md). Agent integration patterns in [docs/AGENT_GUIDE.md](docs/AGENT_GUIDE.md).

---

## Assertions

Every `ui` action supports these, so verification is a flag, not a paragraph:

```bash
--expect-route "SettingsPage"       # route changed?
--expect-text "保存成功"             # text appeared?
--expect-gone "加载中"               # text disappeared?
--expect-dialog                      # a dialog popped up?
--expect-no-change                   # nothing changed (regression)?
--expect-state "开关:checked=true"   # widget state check
--timeout 10                         # poll up to N seconds
--auto-handle-dialog                 # dismiss blockers automatically
```

---

## Batch scripts

One JSON file, N steps, single driver connection:

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

## Bridge framework

Need the agent to reach into your app's business logic? `autoharmony` ships a JSON-RPC TCP bridge — login/logout, user info, business data, whatever the app registers:

```python
from bridge.tcp_bridge import TcpBridge

bridge = TcpBridge("your-device-id")

bridge.call("login", {"account": "13800138000"})
info = bridge.call("getUserInfo")                  # → {"nickname": "...", "vip": true}
devices = bridge.call("queryDevices", {"room": "客厅"})  # → {"devices": [...]}
bridge.call("logout")

bridge.close()
```

The UI shows a "logged in" page; the session and underlying state are verified in one call.

See [docs/BRIDGE.md](docs/BRIDGE.md) for the protocol and ArkTS server example.

---

## Install as an Agent skill

`autoharmony` is distributed as a standard agent skill — drop it into your coding agent, and it verifies its own HarmonyOS work:

```bash
git clone git@github.com:qkdndqxkr5-ctrl/autoHarmony.git
cp autoHarmony/SKILL.md ~/.claude/skills/autoharmony/SKILL.md                  # Claude Code
cp autoHarmony/SKILL.md .cursor/rules/autoharmony.mdc                          # Cursor
cp autoHarmony/SKILL.md AGENTS.md                                               # Copilot / Codex / Goose
cp autoHarmony/SKILL.md .windsurfrules                                          # Windsurf
cp autoHarmony/SKILL.md .clinerules                                             # Cline
mkdir -p ~/.config/opencode/skills/autoharmony && cp autoHarmony/SKILL.md ~/.config/opencode/skills/autoharmony/SKILL.md   # opencode
```

Per-client setup, config snippets, and the explore → record → replay loop:
→ [docs/agent-guides/](docs/agent-guides/)

## Requirements

- Python 3.9+
- `hdc` (HarmonyOS Device Connector) in PATH
- A HarmonyOS device over USB or network
- Optional: `pip install autoharmony[semantic]` — semantic commands like `click-by-text` / `click-by-id` need `hypium`

## Contributing

Issues and PRs welcome — especially new engines, better verdicts, and real-world agent integrations. See [CONTRIBUTING](docs/USAGE.md) basics.

## Star history

[![Star History Chart](https://api.star-history.com/svg?repos=qkdndqxkr5-ctrl/autoHarmony&type=Date)](https://www.star-history.com/#qkdndqxkr5-ctrl/autoHarmony&Date)

Found it useful? ⭐ Star it — it tells agents (and people) this tool works.

---

## License

MIT