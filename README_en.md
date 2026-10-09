# autoharmony 🎯

**HarmonyOS on-device UI testing for AI agents.** Let Claude Code / Cursor / Codex verify the ArkTS they wrote — on a real device, on their own.

> *An agent explores once; the script it saves runs forever.*

[![License: MIT](https://img.shields.io/github/license/minghaozhao-ctrl/autoHarmony)](https://github.com/minghaozhao-ctrl/autoHarmony/blob/main/LICENSE) [![Python 3.9+](https://img.shields.io/badge/python-3.9+-blue.svg)](https://www.python.org/downloads/) [![Platform: HarmonyOS](https://img.shields.io/badge/platform-HarmonyOS-red)]() [![Stars](https://img.shields.io/github/stars/minghaozhao-ctrl/autoHarmony)](https://github.com/minghaozhao-ctrl/autoHarmony/stargazers)

[中文](README.md) · [Quick start](#quick-start) · [Docs map](#documentation-map) · [Full comparison](docs/COMPARISON.md)

---

## AI writes your HarmonyOS code. Who runs it?

Claude Code, Cursor, Codex — they can write ArkTS all day. But verifying it on a real device is a different story: traditional HarmonyOS frameworks assume a human authoring test suites, and general-purpose agent automation treats HarmonyOS as a secondary target. `autoharmony` goes all-in on HarmonyOS — and on the agent loop.

`autoharmony` closes the loop. One CLI, one action per line, a deterministic verdict back:

```bash
$ autoharmony --json ui click-by-text "设置" --expect-route "SettingsPage"
{
  "status": "SUCCESS",
  "reason": "Action completed",
  "exit": 0,
  "log_path": "/Users/you/.hmuitest/logs/uitest-20260918.log"
}
```

With `--json`, human logs go to stderr and stdout holds JSON only — directly parseable.

Explore → verify → record → replay. The exploration becomes a regression script. Next code change: `script run`. No human test cases, no flaky utilities, no retyping the same flow.

**Agent writes code → Agent tests on real hardware → Script is saved → Next change is one command**

---

## See it in 30 seconds

### After every action: a deterministic verdict + a diff

The verdict says pass or fail; the diff says *exactly what happened* — no screenshots, no guessing:

```text
ACTION_VERDICT: SUCCESS | reason=路由发生变化

🧭 组件树变化  操作: click 设置  时间: 2026-09-18 14:05:22
📊 新增3 · 消失0 · 位置变化12 · 文本/状态/属性2 · 遮挡0 · 解除遮挡0 · 其它0
● 新增(3)  →  有信息量 1 条
    + Text '语言'  [56,820][1180,900]
● 位置变化(12)  →  合并 2 组同位移
    ▸ 位移(+0,-120)px · 8 节点 · 如 u=abc123 Column
PAGE_RESULT: CHANGES_DETECTED
```

---

## Quick start

**Requirements**: Python 3.9+, `hdc` in PATH, a HarmonyOS device (USB or network).

```bash
# Install (pick one)
pipx install git+https://github.com/minghaozhao-ctrl/autoHarmony.git   # recommended: isolated CLI install
pip install git+https://github.com/minghaozhao-ctrl/autoHarmony.git    # or into the current env

# From source (development / hacking)
git clone git@github.com:minghaozhao-ctrl/autoHarmony.git
cd autoHarmony && pip install -e .

# Verify
hdc list targets                                    # should print your device serial
export HMUITEST_BUNDLE=com.your.app                 # your app's bundleName (defaults to com.cmcc.DigitalHome)
autoharmony ui check-exist "设置"                    # check a widget exists (~0.6s)
autoharmony --json ui click-by-text "设置" --expect-route "SettingsPage"
```

That's it — you're ready to point an agent at your app. `aa` / `ui` actions / `script` / `device` support `--json` structured output; `tree dump --json` instead returns search results as a JSON array. The full command list lives in [docs/USAGE.md](docs/USAGE.md).

## The loop in 30 seconds

```bash
# 1. Agent (or human) explores the app — structured verdicts come back
$ autoharmony --json ui click-by-text "设置" --expect-route "SettingsPage"
{
  "status": "SUCCESS",
  "reason": "Action completed",
  "exit": 0,
  "log_path": "/Users/you/.hmuitest/logs/uitest-20260918.log"
}

# 2. Record the exploration as a reusable regression script
$ autoharmony script record start
$ autoharmony ui click-by-text "设置"
$ autoharmony ui click-by-text "关于"
$ autoharmony script record stop --output settings_test.json

# 3. After every code change: replay. No retyping, no human test case.
$ autoharmony --json script run settings_test.json
{
  "status": "SUCCESS",
  "reason": "2/2 steps passed",
  "exit": 0,
  "all_passed": true,
  "passed": 2,
  "failed": 0,
  "total": 2,
  "log_path": "/Users/you/.hmuitest/logs/uitest-20260918.log"
}
```

Failed a step? The verdict + tree diff tell the agent exactly which selector changed — `ui scroll-find`, `tree dump`, fix the JSON, rerun. Self-healing, not manual triage. See [docs/AGENT_GUIDE.md](docs/AGENT_GUIDE.md).

---

## The three advantages that matter

Most UI tools optimize for a human sitting at a terminal. An agent loop has three hard requirements — **deterministic, fast, self-explaining** — and `autoharmony` is built around them.

### 1. Deterministic by construction — never a coin flip

Every action is a CLI call driven by **selectors** (`text` / `id` / `type`) or coordinates, resolved against the live widget tree — not by a model looking at a screenshot. Same input, same verdict, every run.

- Exit code **0/1/2** on every command (0 pass, 1 fail, 2 usage error). `--json` adds structured `status`/`reason`/`exit`/`log_path`; the flag works before or after the subcommand (`tree` excepted — its `--json` means "search results as a JSON array").
- Two-layer click fallback: try an exact `text`/`hint` match, then fall back to a contains match + coordinate click when there's no exact match. Assertions are preserved through both paths.
- Stable node IDs (`uniqueId`) pair tree diffs correctly, so scroll/filter never report existing nodes as add+remove.

```bash
# Machine-readable, deterministic verdict — the agent's native language
autoharmony --json ui click-by-text "登录" --expect-route "HomePage"
```

### 2. Fast enough to live inside the loop

A regression loop is only useful if re-running it is cheaper than not running it. `autoharmony` talks to the device over a **pure-`hdc` path** — no daemon to bring up.

| Operation | Time (on-device, device-dependent) |
|---|---|
| `check-exist` | **~0.6s** |
| per widget-tree dump | **~0.7s** |
| `click-by-text` | **~2.6s** |
| `dismiss-dialogs` | **~2.3s** |

Read-only operations land in **sub-second**, full actions in **single-digit seconds** — fully local, zero required runtime dependencies. A 20-step regression run finishes while a vision model is still on step two.

### 3. It tells the agent *what went wrong*

Raw UI automation returns "clicked" — useless if the app crashed, a permission dialog stole the tap, or the button did nothing. After every action, `autoharmony` emits a one-line verdict with a reason:

```
ACTION_VERDICT: SUCCESS            | reason=路由发生变化
ACTION_VERDICT: NO_CHANGE          | reason=操作已执行但未检测到页面变化（可能被遮挡/控件不可点/已是目标状态）
ACTION_VERDICT: BLOCKED_BY_DIALOG  | reason=操作无变化且存在覆盖层弹窗[Dialog]: 确认
ACTION_VERDICT: PENDING_DIALOG     | reason=操作已产生变化，但留有未处理弹窗[Dialog]
ACTION_VERDICT: BACK_INEFFECTIVE   | reason=返回后页面无变化（可能已在栈底或返回被拦截）
ACTION_VERDICT: CRASHED            | reason=App 进程已退出或出现 faultlog
ACTION_VERDICT: RESTARTED          | reason=accessibilityId 骤降，App 内部重启（UI 已重建）
ACTION_VERDICT: ERROR              | reason=action_failed
```

The agent branches on the first token of that `ACTION_VERDICT:` line. Note the `--json` `status` only distinguishes `SUCCESS`/`FAILED`; the fine-grained states live in the text line:

| Status | Agent's move |
|---|---|
| `SUCCESS` | Continue |
| `BLOCKED_BY_DIALOG` | Auto-covered by `--auto-handle-dialog` |
| `PENDING_DIALOG` | Clear the leftover dialog (`ui dismiss-dialogs`), then re-check |
| `CRASHED` / `RESTARTED` | Stop, restore app, report crash |
| `ERROR` | Action failed — check the selector / device connection |
| `NO_CHANGE` / `BACK_INEFFECTIVE` | Rethink navigation |

And the recovery is built in, not bolted on:

- **Auto dialog handling** — `--auto-handle-dialog` / `ui dismiss-dialogs` recognize and clear permission / upgrade / overlay popups so a flow never wedges on a modal.
- **Crash detection** — CppCrash / JSCrash / AppFreeze caught automatically after every action.
- **Assertion polling** — `--expect-text "成功" --timeout 10` polls the tree for route/text/dialog/state assertions, so timing-sensitive checks don't flake (`--expect-no-change` is judged immediately).
- **Failure evidence, zero setup** — when a `ui` action fails, a screenshot + `hilog` slice are captured into `artifacts/`; a suspected crash adds `hidumper -e`, and the widget-tree dump is saved when available. The location comes back via `log_path` in `--json` and `📦 失败留证: …` on stderr. Disable with `--no-artifacts` (or `HMUITEST_ARTIFACTS=0`).
- **Full log trail, zero config** — every run's stdout+stderr (incl. tracebacks) appended to `~/.hmuitest/logs/uitest-YYYYMMDD.log`, sessions correlated via `run=<id>`, tree dumps archived per-day with pid and tied to actions via `[dump]`/`[time]`. Debug from the log instead of re-running.

---

## After every action: a diff, and a log

The verdict says pass or fail. These two say *exactly what happened* — no screenshots, no guessing.

### Automatic structural diff

Every `ui` action snapshots the widget tree before and after, then reports only what changed. The agent sees route shifts, new dialogs, and toggled text directly — not "clicked".

```text
🧭 组件树变化  操作: click 设置  时间: 2026-09-18 14:05:22
📊 新增3 · 消失0 · 位置变化12 · 文本/状态/属性2 · 遮挡0 · 解除遮挡0 · 其它0
● 新增(3)  →  有信息量 1 条
    + Text '语言'  [56,820][1180,900]
● 位置变化(12)  →  合并 2 组同位移
    ▸ 位移(+0,-120)px · 8 节点 · 如 u=abc123 Column
PAGE_RESULT: CHANGES_DETECTED
```

- **Typed changes** — added / removed / text / position / state / property / overlay appeared / occluded / revealed, each with a priority.
- **Route changes get their own report** — before/after navigation stack plus an overview of the new page.
- **Compact by design** — structural nodes folded, position changes merged by displacement, removed nodes carry no coordinates, repeats de-duplicated.
- **Paired by stable `uniqueId`** — scrolling or filtering never misreports an existing node as add + remove.
- **System-UI noise filtered** — status-bar clock / battery nodes are excluded from diffs.
- **Machine-readable tail** — when a structural diff is printed it ends with `PAGE_RESULT: ROUTE_CHANGED | CHANGES_DETECTED | NO_CHANGES`, so the agent branches without parsing prose.

### Full log trail, zero config

Every run appends its complete stdout+stderr — including tracebacks — to `~/.hmuitest/logs/uitest-YYYYMMDD.log`. Nothing to enable, no flag to remember. Terminal output is unchanged; the file just adds timestamps and correlation.

```text
===== autoharmony 会话开始 2026-09-18 18:08:12 | pid=48213 | run=a1b2c3d4 | cwd=... =====
[cmd]  autoharmony ui click-by-text 设置 --expect-route SettingsPage
[dev]  127.0.0.1:5555
[page] --expect-route=SettingsPage
[18:08:14.101] ACTION_VERDICT: SUCCESS | reason=路由发生变化
[18:08:14.120] [dump] tag=before device=127.0.0.1:5555 pid=48213 size=182340B src=/data/local/tmp/layout.json dst=~/.hmuitest/logs/dump/20260918/180813.900-before-pid48213.json
[18:08:14.910] [time] op=点击文本为 '设置' 的控件 elapsed_ms=2600 status=OK
[18:08:15.204] [exit] rc=0
[session] 结束 rc=0 elapsed_ms=3120
```

- **Correlated sessions** — every run gets a `run=<id>`; `[cmd]` / `[dev]` / `[page]` open the session, `[exit] rc=` and `[session]` close it.
- **Actions tied to artifacts** — each tree dump is archived to `dump/<YYYYMMDD>/` with its pid and a `[dump]` line, so "what did the screen look like at this step?" is answerable after the fact.
- **Timing and swallowed errors** — `[time]` records per-operation latency, `[err]` captures exceptions that were downgraded instead of vanishing.
- **The path comes back** — `--json` carries `log_path`, and a failed run prints the log file to stderr, so the agent knows where to look without guessing.
- **Debug by reading, not re-running** — reproduce a flaky verdict from the log; override paths with `HMUITEST_LOG_DIR` / `HMUITEST_LOG_FILE`, or disable with `HMUITEST_LOG=0`.

---

## How it compares

**vs. [agent-device](https://github.com/callstack/agent-device)** — the closest neighbor: an agent-native CLI that also covers HarmonyOS, but across 9 platforms:

| Where HarmonyOS depth shows | autoharmony | agent-device |
|---|---|---|
| Screen perception | ✅ widget-tree text dump (text/type/bounds) + semantic targeting | flat `@eN` ref snapshot |
| Dialogs / crashes | ✅ deterministic, folded into verdict | ⚠️ not claimed for HarmonyOS |
| Post-action verdict | ✅ eight states + exit 0/1/2 | snapshot diff after `--settle` |
| Route / state assertions | ✅ built-in polling | — |
| System-UI noise filtering | ✅ excluded from diffs | — |
| Business-layer access | ✅ JSON-RPC bridge | — (UI only) |
| Runtime | ✅ Python + `hdc`, no daemon | Node.js + daemon |
| Breadth (platforms / MCP / video / perf) | HarmonyOS only | ✅ 9 targets |

**vs. AI vision-driven automation (Midscene et al.)**: deterministic selectors vs multimodal screenshots — `autoharmony` wins on 100% determinism, sub-second speed, auditability, and no API key; vision wins when you need to *see* the screen like a human (colors, layout, visual fidelity).

**If your target platform is Android or iOS, use agent-device.** Full comparison (Chinese, with measured data): [docs/COMPARISON.md](docs/COMPARISON.md)

---

## Assertions

Every `ui` **action** command (the coordinate family — click/input/swipe/back — and the semantic variants) supports these, so verification is a flag, not a paragraph:

```bash
--expect-route "SettingsPage"       # route changed?
--expect-text "保存成功"             # text appeared?
--expect-gone "加载中"               # text disappeared?
--expect-dialog                      # a dialog popped up?
--expect-no-change                   # nothing changed (regression check, judged immediately)?
--expect-state "开关:checked=true"   # widget state check
--timeout 10                         # poll route/text/dialog/state up to N seconds
--auto-handle-dialog                 # dismiss blockers automatically
```

Other useful flags: `--index N` (`click-by-text` only — disambiguate multiple matches), `--fresh-before` (force a fresh baseline dump instead of reusing the cached one), `--no-recover` (disable auto-retry when blocked by a dialog), `--no-artifacts` (skip failure evidence).

---

## Batch scripts

One JSON file, N steps, single engine. Two accepted shapes: a full `{name, steps}` script (`params`/`expect` both honored), or the `[{cmd, args}]` recording emitted by `script record` (replays only `RECORD_ACTION_MAP` actions).

```json
{
  "name": "login_test",
  "steps": [
    {"action": "click_by_text", "params": {"text": "登录"}, "expect": {"route": "LoginPage"}},
    {"action": "input_by_text", "params": {"target": "手机号", "text": "13800138000"}},
    {"action": "input_by_text", "params": {"target": "密码", "text": "****"}},
    {"action": "click_by_text", "params": {"text": "确认"}, "expect": {"route": "MainPage"}}
  ]
}
```

```bash
autoharmony --json script run login_test.json
```

An optional `start` / `setup` field validates and recovers the pre-state (`route` / `text` / `max_backs` / `recover`) — see [docs/USAGE.md](docs/USAGE.md).

---

## Bridge framework

Need the agent to reach into your app's business logic? `autoharmony` ships a JSON-RPC TCP bridge — login/logout, user info, business data, whatever the app's TCP server implements:

```python
from bridge.tcp_bridge import TcpBridge

bridge = TcpBridge("your-device-id")

bridge.call("login", {"account": "13800138000"})
info = bridge.call("getUserInfo")                  # → {"nickname": "...", "vip": true}
devices = bridge.call("queryDevices", {"room": "客厅"})  # → {"devices": [...]}
bridge.call("logout")

bridge.close()
```

Bridge steps inside a batch script require fixed method signatures; copy `bridge/bridge_template.py` to extend your own bridge class and wire it in with `HMUITEST_BRIDGE_CLASS=your.module:YourBridge`. See [docs/BRIDGE.md](docs/BRIDGE.md) for the protocol and ArkTS server example.

---

## Install as an Agent skill

`autoharmony` is distributed as a standard agent skill — drop it into your coding agent, and it verifies its own HarmonyOS work:

```bash
git clone git@github.com:minghaozhao-ctrl/autoHarmony.git
cp autoHarmony/SKILL.md ~/.claude/skills/autoharmony/SKILL.md                  # Claude Code
cp autoHarmony/SKILL.md .cursor/rules/autoharmony.mdc                          # Cursor
cp autoHarmony/SKILL.md AGENTS.md                                               # Copilot / Codex / Goose
cp autoHarmony/SKILL.md .windsurfrules                                          # Windsurf
cp autoHarmony/SKILL.md .clinerules                                             # Cline
mkdir -p ~/.config/opencode/skills/autoharmony && cp autoHarmony/SKILL.md ~/.config/opencode/skills/autoharmony/SKILL.md   # opencode
```

Per-client setup, config snippets, and the explore → record → replay loop:
→ [docs/agent-guides/](docs/agent-guides/)

---

## FAQ

**Does it support emulators?**
Yes — any HarmonyOS device `hdc` can reach, emulators included (both real devices and emulators have measured runs; emulator dump ~1.9s).

**How do I configure my app's bundle name?**
`export HMUITEST_BUNDLE=com.your.app` (applies to all commands), or edit `FALLBACK_BUNDLE` in `utils/bundle.py`. Without it, the default is `com.cmcc.DigitalHome` (the host app used during development).

**How does this relate to Hypium / Appium?**
Not a replacement. Hypium is an on-device test framework — **humans** author the cases; `autoharmony` is an agent-driven CLI — the **agent** explores, records, and replays. They coexist: stable critical paths can still be promoted to Hypium/pytest suites.

**Does it need root or app instrumentation?**
No. It relies only on `hdc` + the system `uitest` API — zero intrusion into the app under test. The business Bridge is an optional enhancement, not a requirement.

**Windows / Linux?**
Yes — pure Python + `hdc`, no platform-specific code. Primary development and validation happen on macOS; Windows/Linux feedback is welcome.

**Why not screenshots + a vision model?**
Determinism, speed, auditability: vision depends on the model (same input may yield different verdicts), is slow (seconds of inference), and cannot detect process crashes. `autoharmony` turns the screen into text and judgment into code. When you do need visual checks (colors, layout fidelity), pair it with a vision tool.

**Is the business Bridge required?**
No. Pure UI actions (click / input / assertions) need nothing on the app side. Configure it only when the agent must reach business state directly (login session, device lists, …) — see [docs/BRIDGE.md](docs/BRIDGE.md).

**How do I wire it into CI?**
Exit codes work out of the box: 0 pass, 1 fail, 2 usage error. `autoharmony --json script run suite.json` runs the whole regression in one command, with directly parseable JSON on stdout.

---

## Documentation map

| Doc | Contents |
|---|---|
| [docs/USAGE.md](docs/USAGE.md) | Full command reference |
| [docs/AGENT_GUIDE.md](docs/AGENT_GUIDE.md) | Agent integration guide (explore → record → replay) |
| [docs/EXAMPLES.md](docs/EXAMPLES.md) | Real-world cases and workflows |
| [docs/BRIDGE.md](docs/BRIDGE.md) | JSON-RPC bridge protocol and ArkTS server example |
| [docs/REFERENCE.md](docs/REFERENCE.md) | Architecture and implementation details |
| [docs/COMPARISON.md](docs/COMPARISON.md) | Full comparison vs. agent-device / vision tools (Chinese) |
| [docs/PROOF_DUMP_SPEED.md](docs/PROOF_DUMP_SPEED.md) | Dump-speed measurement report |
| [docs/agent-guides/](docs/agent-guides/) | Per-client setup guides (Claude Code / Cursor / Codex / opencode, 8 clients) |
| [SKILL.md](SKILL.md) | Agent skill definition file |

## Contributing

Issues and PRs welcome — especially new engines, better verdicts, and real-world agent integrations.

- **Dev setup**: `git clone` then `pip install -e .` — edits take effect immediately.
- **Layout**: `autoharmony.py` is the CLI entry; `engines/` holds the core (hdc / semantic / diff / verdict / batch); `analyzers/` widget-tree analysis; `bridge/` business bridge; `utils/` helpers.
- **Before submitting**: on a real device, run `autoharmony --json ui click-by-text "设置"` to confirm the basic path works.
- **Zero-dependency constraint**: `requirements.txt` stays empty (stdlib only) — please don't introduce external packages.

---

## Star history

[![Star History Chart](https://api.star-history.com/svg?repos=minghaozhao-ctrl/autoHarmony&type=Date)](https://www.star-history.com/#minghaozhao-ctrl/autoHarmony&Date)

Found it useful? ⭐ Star it — it tells agents (and people) this tool works.

---

## License

MIT
