# hmuitest 🎯

> **HarmonyOS UI testing that doesn't suck.**

[中文文档](README_zh.md)

Point, click, assert, done. One CLI to rule them all — UI automation, widget tree diffing, crash detection, and batch scripts for HarmonyOS apps. No boilerplate, no flaky tests, no wasted afternoons.

## Why?

Because writing HarmonyOS UI tests shouldn't feel like assembling IKEA furniture with the instructions missing. `hmuitest` gives you:

- **One-liner UI actions** — click, swipe, input, assert. Each command returns exit code 0/1 so you can chain them in CI without thinking
- **Auto diff reports** — every action snapshots the widget tree before/after and tells you *exactly* what changed (route shifts, new dialogs, text toggles)
- **Semantic targeting** — don't know coordinates? Click by text, by ID, by type. `click-by-text "Settings"` just works
- **Smart assertions** — `--expect-route`, `--expect-text`, `--expect-dialog` — verify any UI state with a flag, not a paragraph of code
- **Widget tree inspector** — dump, search, diff. Like Chrome DevTools but for your HarmonyOS app
- **Crash detector** — catches CppCrash, JSCrash, AppFreeze automatically after actions
- **Batch scripts** — run 20 test steps in one shot, single driver connection, fast

## Quick Start

```bash
# Install
git clone https://github.com/YOUR_USERNAME/hmuitest.git
cd hmuitest
pip install -r requirements.txt

# Make sure hdc is connected
hdc list targets

# Click a button and assert the route changed
python3 hmuitest.py ui click 540 550 --expect-route "SettingsPage" --operation "Open settings"

# Click by text (no coordinates needed!)
python3 hmuitest.py ui click-by-text "设置" --expect-route "SettingsPage"

# Dump and inspect widget tree
python3 hmuitest.py tree dump --overview

# Run a batch test script
python3 hmuitest.py script run tests.json
```

## Commands

| Command | What it does | Example |
|---------|-------------|---------|
| `ui click X Y` | Click coordinates | `ui click 540 550` |
| `ui click-by-text "文本"` | Click widget by text | `ui click-by-text "设置" --expect-route "Settings"` |
| `ui click-by-id "key"` | Click widget by ID | `ui click-by-id "btn_submit"` |
| `ui swipe X1 Y1 X2 Y2` | Swipe gesture | `ui swipe 500 1500 500 500` |
| `ui input "text"` | Type into focused field | `ui input "hello"` |
| `ui input-by-text "目标" "text"` | Type into specific field | `ui input-by-text "搜索框" "query"` |
| `ui back` | Press back key | `ui back` |
| `ui scroll-find "文本"` | Scroll until text found | `ui scroll-find "关于我们"` |
| `ui wait-for "文本"` | Wait for text to appear | `ui wait-for "加载完成" --timeout 15` |
| `ui screenshot path.png` | Save screenshot | `ui screenshot /tmp/screen.png` |
| `ui dismiss-dialogs` | Auto-dismiss overlays | `ui dismiss-dialogs` |
| `tree dump` | Dump widget tree from device | `tree dump --overview` |
| `tree diff f1.json f2.json` | Compare two trees | `tree diff before.json after.json` |
| `tree auto` | Auto-diff vs last baseline | `tree auto` |
| `aa start URI` | Launch via Deep Link | `aa start "myapp://page" --bundle com.example.app` |
| `script run file.json` | Run batch script | `script run regression.json` |

## Assertions

Every `ui` action supports these flags:

```bash
# Route changed?
--expect-route "SettingsPage"

# Text appeared?
--expect-text "保存成功"

# Text disappeared?
--expect-gone "加载中"

# Dialog popped up?
--expect-dialog "Dialog"

# Nothing changed (regression check)?
--expect-no-change

# Widget state check?
--expect-state "开关:checked=true"

# Retry until timeout?
--expect-text "完成" --timeout 10

# Auto-handle dialogs blocking the view?
--auto-handle-dialog
```

## Batch Scripts

Run multiple steps with a single driver connection:

```json
[
  {"cmd": "ui click-by-text", "args": ["登录"]},
  {"cmd": "ui input-by-text", "args": ["手机号", "13800138000"]},
  {"cmd": "ui input-by-text", "args": ["密码", "****"]},
  {"cmd": "ui click-by-text", "args": ["确认登录"], "expect": {"route": "MainPage"}},
  {"cmd": "ui screenshot", "args": ["/tmp/logged_in.png"]}
]
```

```bash
python3 hmuitest.py script run login_test.json
```

## Bridge Framework

`hmuitest` comes with a generic TCP bridge for communicating with your HarmonyOS app. Register your own business methods:

```python
from bridge.tcp_bridge import TcpBridge

bridge = TcpBridge(device="your-device-id")

# Register your app's methods
def handle_navigate(params):
    # Your navigation logic here
    return {"success": True}

bridge.register("navigate", handle_navigate)

# Or just call remote methods directly
result = bridge.call("navigate", {"route": "MainPage"})
print(result)

bridge.close()
```

See [docs/BRIDGE.md](docs/BRIDGE.md) for the full bridge extension guide.

## Requirements

- Python 3.9+
- `hdc` (HarmonyOS Device Connector) in PATH
- Device connected via USB or network

## Contributing

Contributions welcome! Open an issue or submit a PR.

## License

MIT
