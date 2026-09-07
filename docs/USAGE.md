# Usage Guide

## Installation

```bash
git clone https://github.com/YOUR_USERNAME/hmuitest.git
cd hmuitest
pip install -r requirements.txt
```

Make sure `hdc` is in your PATH and a device is connected:

```bash
hdc list targets
```

## UI Actions

### Coordinate-based (HdcUITestEngine)

```bash
# Click
python3 hmuitest.py ui click 540 550

# Double click
python3 hmuitest.py ui double-click 540 550

# Long click
python3 hmuitest.py ui long-click 540 550

# Swipe
python3 hmuitest.py ui swipe 500 1500 500 500

# Input text to focused field
python3 hmuitest.py ui input "hello world"

# Press back
python3 hmuitest.py ui back
```

### Semantic (HypiumEngine)

```bash
# Click by text (fuzzy match)
python3 hmuitest.py ui click-by-text "设置"

# Click by text, 2nd match
python3 hmuitest.py ui click-by-text "确定" --index 1

# Click by widget ID
python3 hmuitest.py ui click-by-id "btn_submit"

# Click by type (first Button)
python3 hmuitest.py ui click-by-type "Button"

# Double click by text
python3 hmuitest.py ui double-click-by-text "图片"

# Long click by text
python3 hmuitest.py ui long-click-by-text "菜单项"

# Input by target field text
python3 hmuitest.py ui input-by-text "搜索框" "harmonyos"

# Input by field type
python3 hmuitest.py ui input-by-type "TextInput" "hello"

# Swipe in direction
python3 hmuitest.py ui swipe-direction UP
python3 hmuitest.py ui swipe-direction DOWN --distance 100
```

### Smart Operations

```bash
# Scroll to find a widget
python3 hmuitest.py ui scroll-find "关于我们"

# Wait for text to appear
python3 hmuitest.py ui wait-for "加载完成" --timeout 15

# Wait for text to disappear
python3 hmuitest.py ui wait-for "加载中" --gone

# Dismiss overlay dialogs
python3 hmuitest.py ui dismiss-dialogs
```

### Utilities

```bash
# Screenshot
python3 hmuitest.py ui screenshot /tmp/screen.png

# Check dialog exists
python3 hmuitest.py ui check-dialog

# Check component exists
python3 hmuitest.py ui check-exist --text "确定"

# Find and print widget info
python3 hmuitest.py ui find --text "设置"
```

## Assertions

Every `ui` action supports assertion flags:

```bash
# Route stack check
python3 hmuitest.py ui click-by-text "设置" --expect-route "SettingsPage"

# Text existence
python3 hmuitest.py ui click-by-text "保存" --expect-text "保存成功"

# Text absence
python3 hmuitest.py ui click-by-text "删除" --expect-gone "确认删除"

# Dialog detection
python3 hmuitest.py ui click-by-text "退出" --expect-dialog

# No change (regression)
python3 hmuitest.py ui click 10 10 --expect-no-change

# Widget state
python3 hmuitest.py ui click-by-text "开关" --expect-state "开关:checked=true"

# Polling with timeout
python3 hmuitest.py ui click-by-text "刷新" --expect-text "更新完成" --timeout 10

# Auto-handle blocking dialogs
python3 hmuitest.py ui click 540 550 --auto-handle-dialog --expect-text "成功"
```

## Widget Tree Analysis

```bash
# Analyze local file
python3 hmuitest.py tree show tree.json --overview

# Search by type
python3 hmuitest.py tree show tree.json --type Button

# Search by text
python3 hmuitest.py tree show tree.json --text "设置"

# Search by ID
python3 hmuitest.py tree show tree.json --id "btn_submit"

# List all types
python3 hmuitest.py tree show tree.json --list-types

# Show detail for widget at index
python3 hmuitest.py tree show tree.json --detail 5

# JSON output
python3 hmuitest.py tree show tree.json --text "设置" --json

# Dump from device
python3 hmuitest.py tree dump --overview

# Diff two files
python3 hmuitest.py tree diff before.json after.json --operation "Open settings"

# Auto-diff (dump + compare with last baseline)
python3 hmuitest.py tree auto --operation "Click button"
```

## Deep Link Launch

```bash
python3 hmuitest.py aa start "myapp://page/settings" \
  --bundle com.example.app \
  --ability com.example.app.MainAbility \
  --expect-route "SettingsPage"
```

## Batch Scripts

Create a JSON file with test steps:

```json
[
  {"cmd": "ui click-by-text", "args": ["登录"]},
  {"cmd": "ui input-by-text", "args": ["手机号", "13800138000"]},
  {"cmd": "ui input-by-text", "args": ["密码", "****"]},
  {
    "cmd": "ui click-by-text",
    "args": ["确认登录"],
    "expect": {"route": "MainPage"}
  },
  {"cmd": "ui screenshot", "args": ["/tmp/logged_in.png"]}
]
```

Run it:

```bash
python3 hmuitest.py script run login_test.json
```

## Device Selection

```bash
# Auto-detect (env HARMONY_DEVICE_ID → session → hdc list targets)
python3 hmuitest.py tree dump

# Explicit device
python3 hmuitest.py tree dump --device DEVICE_SERIAL
python3 hmuitest.py tree dump -d DEVICE_SERIAL
```

## CI Integration

Since all commands exit with code 0 (success) or 1 (failure), you can use them directly in CI:

```bash
#!/bin/bash
set -e

python3 hmuitest.py aa start "myapp://home" --bundle com.example.app
python3 hmuitest.py ui click-by-text "设置" --expect-route "SettingsPage"
python3 hmuitest.py ui scroll-find "关于" --expect-text "版本 1.0"
python3 hmuitest.py tree auto --operation "Regression check"

echo "All tests passed!"
```

## Troubleshooting

**No device detected**
```bash
hdc list targets          # Check connected devices
hdc tconn <ip>:<port>     # Connect network device
```

**Bridge connection failed**
```bash
# Make sure your app is running and the bridge server is initialized
python3 hmuitest.py ui click 540 550  # Will show clear error message
```

**Widget tree empty**
```bash
# Try forcing a fresh dump
python3 hmuitest.py tree dump --fresh-before
```
