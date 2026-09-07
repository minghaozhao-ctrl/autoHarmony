# hmuitest 🎯

> **HarmonyOS UI 测试，就该这么简单。**

点一下、断言一下、搞定。一个 CLI 搞定 UI 自动化、控件树 diff、崩溃检测、批量脚本——不用写样板代码，不用和 flaky test 较劲，不用浪费一整个下午。

## 为什么做这个？

因为写 HarmonyOS UI 测试不该像拼没有说明书的宜家家具。`hmuitest` 给你：

- **一行命令搞定 UI 操作** — 点击、滑动、输入、断言。每条命令返回 exit code 0/1，CI 里直接用
- **自动 diff 报告** — 每次操作自动快照控件树前后状态，告诉你*到底变了啥*（路由跳转、弹窗出现、文字切换）
- **语义化定位** — 不知道坐标？按文字点、按 ID 点、按类型点。`click-by-text "设置"` 直接搞定
- **智能断言** — `--expect-route`、`--expect-text`、`--expect-dialog` — 一个参数验证任意 UI 状态，不用写一堆代码
- **控件树检查器** — dump、搜索、diff。像 Chrome DevTools，但是给 HarmonyOS 用的
- **崩溃检测** — CppCrash、JSCrash、AppFreeze，操作后自动抓
- **批量脚本** — 20 步测试一次跑完，单 driver 连接，快

## 快速上手

```bash
# 安装
git clone https://github.com/YOUR_USERNAME/hmuitest.git
cd hmuitest
pip install -r requirements.txt

# 确保 hdc 已连接
hdc list targets

# 点击按钮并断言路由变了
python3 hmuitest.py ui click 540 550 --expect-route "SettingsPage" --operation "打开设置"

# 按文字点击（不需要坐标！）
python3 hmuitest.py ui click-by-text "设置" --expect-route "SettingsPage"

# dump 并检查控件树
python3 hmuitest.py tree dump --overview

# 跑批量测试脚本
python3 hmuitest.py script run tests.json
```

## 命令速查

| 命令 | 干啥的 | 例子 |
|------|--------|------|
| `ui click X Y` | 点坐标 | `ui click 540 550` |
| `ui click-by-text "文本"` | 按文字点击 | `ui click-by-text "设置" --expect-route "Settings"` |
| `ui click-by-id "key"` | 按 ID 点击 | `ui click-by-id "btn_submit"` |
| `ui swipe X1 Y1 X2 Y2` | 滑动 | `ui swipe 500 1500 500 500` |
| `ui input "text"` | 输入文字 | `ui input "hello"` |
| `ui input-by-text "目标" "text"` | 往指定输入框输入 | `ui input-by-text "搜索框" "query"` |
| `ui back` | 返回键 | `ui back` |
| `ui scroll-find "文本"` | 滚动查找 | `ui scroll-find "关于我们"` |
| `ui wait-for "文本"` | 等文字出现 | `ui wait-for "加载完成" --timeout 15` |
| `ui screenshot path.png` | 截图 | `ui screenshot /tmp/screen.png` |
| `ui dismiss-dialogs` | 关弹窗 | `ui dismiss-dialogs` |
| `tree dump` | dump 控件树 | `tree dump --overview` |
| `tree diff f1.json f2.json` | 对比两棵树 | `tree diff before.json after.json` |
| `tree auto` | 自动和上次基线 diff | `tree auto` |
| `aa start URI` | Deep Link 启动 | `aa start "myapp://page" --bundle com.example.app` |
| `script run file.json` | 跑批量脚本 | `script run regression.json` |

## 断言

每个 `ui` 操作都支持这些参数：

```bash
# 路由变了？
--expect-route "SettingsPage"

# 文字出现了？
--expect-text "保存成功"

# 文字消失了？
--expect-gone "加载中"

# 弹窗弹出来了？
--expect-dialog "Dialog"

# 啥都没变（回归检查）？
--expect-no-change

# 控件状态？
--expect-state "开关:checked=true"

# 超时轮询？
--expect-text "完成" --timeout 10

# 自动处理挡住视线的弹窗？
--auto-handle-dialog
```

## 批量脚本

一个 driver 连接跑多步：

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

## Bridge 框架

`hmuitest` 自带通用 TCP bridge，用来和你的 HarmonyOS App 通信。注册你自己的业务方法：

```python
from bridge.tcp_bridge import TcpBridge

bridge = TcpBridge(device="your-device-id")

# 注册 App 的方法
def handle_navigate(params):
    return {"success": True}

bridge.register("navigate", handle_navigate)

# 或者直接调远程方法
result = bridge.call("navigate", {"route": "MainPage"})
bridge.close()
```

扩展指南见 [docs/BRIDGE.md](docs/BRIDGE.md)。

## 环境要求

- Python 3.9+
- `hdc`（HarmonyOS Device Connector）在 PATH 里
- 设备通过 USB 或网络连接

## 贡献

欢迎提 issue 或 PR！

## License

MIT
