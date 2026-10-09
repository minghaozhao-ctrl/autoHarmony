# autoharmony 🎯

**给 AI Agent 用的 HarmonyOS 真机 UI 测试工具。** 让 Claude Code / Cursor / Codex 写完的鸿蒙代码，自己上真机验证、自己跑回归。

> *Agent 探索一次，脚本永久复用。*

[![License: MIT](https://img.shields.io/github/license/minghaozhao-ctrl/autoHarmony)](https://github.com/minghaozhao-ctrl/autoHarmony/blob/main/LICENSE) [![Python 3.9+](https://img.shields.io/badge/python-3.9+-blue.svg)](https://www.python.org/downloads/) [![Platform: HarmonyOS](https://img.shields.io/badge/platform-HarmonyOS-red)]() [![Stars](https://img.shields.io/github/stars/minghaozhao-ctrl/autoHarmony)](https://github.com/minghaozhao-ctrl/autoHarmony/stargazers)

[English](README_en.md) · [快速上手](#快速上手) · [文档地图](#文档地图) · [完整对比](docs/COMPARISON.md)

---

## AI 帮你写鸿蒙代码。谁来跑？

Claude Code、Cursor、Codex 们可以一天写满 ArkTS。但要在真机上验证就是另一回事：传统鸿蒙框架默认由人编写测试套件，通用 Agent 自动化又把 HarmonyOS 当成次要目标。`autoharmony` 只押 HarmonyOS——也只押 Agent 闭环。

`autoharmony` 把闭环合上。一个 CLI、一行一个动作、返回确定性 verdict：

```bash
$ autoharmony --json ui click-by-text "设置" --expect-route "SettingsPage"
{
  "status": "SUCCESS",
  "reason": "Action completed",
  "exit": 0,
  "log_path": "/Users/you/.hmuitest/logs/uitest-20260918.log"
}
```

`--json` 时人类日志走 stderr，stdout 只有 JSON，可直接解析。

探索 → 验证 → 录制 → 重放。探索过程自动沉淀为回归脚本。下次代码变更，`script run` 一条命令。不需要人写用例、不需要 flaky 工具、不需要把同一流程再输一遍。

**Agent 写代码 → Agent 真机自主测试 → 脚本沉淀 → 改代码后一键回归**

---

## 30 秒看效果

### 每个动作之后：确定性 verdict + 差异报告

verdict 告诉你成/败，diff 告诉你**到底发生了什么**——不用截图、不用猜：

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

## 快速上手

**前置要求**：Python 3.9+、`hdc` 在 PATH、一台 HarmonyOS 设备（USB 或网络连接）。

```bash
# 安装（三选一）
pipx install git+https://github.com/minghaozhao-ctrl/autoHarmony.git   # 推荐：隔离安装 CLI 工具
pip install git+https://github.com/minghaozhao-ctrl/autoHarmony.git    # 或装进当前环境

# 源码方式（开发 / 魔改）
git clone git@github.com:minghaozhao-ctrl/autoHarmony.git
cd autoHarmony && pip install -e .

# 验证
hdc list targets                                    # 应输出设备序列号
export HMUITEST_BUNDLE=com.your.app                 # 你的 App 包名（未设置时默认 com.cmcc.DigitalHome）
autoharmony ui check-exist "设置"                    # 检查控件存在（~0.6s）
autoharmony --json ui click-by-text "设置" --expect-route "SettingsPage"
```

完事——直接可以把 Agent 指向你的 App。`aa` / `ui` 动作 / `script` / `device` 等命令支持 `--json` 结构化输出；`tree dump` 的 `--json` 输出搜索结果数组。完整命令清单见 [docs/USAGE.md](docs/USAGE.md)。

## 三十秒看懂闭环

```bash
# 1. Agent（或人）探索 App —— 结构化 verdict 直接返回
$ autoharmony --json ui click-by-text "设置" --expect-route "SettingsPage"
{
  "status": "SUCCESS",
  "reason": "Action completed",
  "exit": 0,
  "log_path": "/Users/you/.hmuitest/logs/uitest-20260918.log"
}

# 2. 把探索过程录成可复用回归脚本
$ autoharmony script record start
$ autoharmony ui click-by-text "设置"
$ autoharmony ui click-by-text "关于"
$ autoharmony script record stop --output settings_test.json

# 3. 每次改完代码：重放。不用重输、不用人写用例
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

某步失败了？verdict + 控件树 diff 会告诉 Agent 哪个选择器变了——`ui scroll-find`、`tree dump`、改 JSON、重跑。脚本自愈，而不是人工排查。详见 [docs/AGENT_GUIDE.md](docs/AGENT_GUIDE.md)。

---

## 真正重要的三个优势

大多数 UI 工具服务的是坐在终端前的人。而 Agent 闭环有三个硬指标——**确定性、快、会解释**——`autoharmony` 就是围绕这三点做的。

### 1. 结构上确定性——从不掷骰子

每个动作都是一条 CLI 命令，由**选择器**（`text` / `id` / `type`）或坐标驱动，在实时控件树上解析——不是模型看截图。同样的输入，永远同样的判定。

- 每条命令返回退出码 **0/1/2**（0=通过，1=失败，2=用法错误）。`--json` 额外输出结构化 `status`/`reason`/`exit`/`log_path`；位置随意（子命令前或后均可），`tree` 除外——它的 `--json` 另有「搜索结果数组」语义。
- 两层点击兜底：先按精确 `text`/`hint` 匹配；无精确匹配时回退到包含匹配 + 坐标点击。断言在两条路径上均保留。
- 稳定节点 ID（`uniqueId`）让 diff 正确配对，滚动/过滤不会把存量节点误报为增删。

```bash
# 机器可读、确定性 verdict —— Agent 的母语
autoharmony --json ui click-by-text "登录" --expect-route "HomePage"
```

### 2. 快到能塞进循环里

只有“重跑比不跑更划算”，回归循环才有意义。`autoharmony` 走**纯 `hdc` 直连**，没有 daemon 启动开销。

| 操作 | 耗时（真机实测，随设备而异） |
|---|---|
| `check-exist` | **~0.6s** |
| 单次控件树 dump | **~0.7s** |
| `click-by-text` | **~2.6s** |
| `dismiss-dialogs` | **~2.3s** |

只读操作**亚秒级**，完整动作**个位数秒级**，纯本地、零必需运行时依赖。20 步回归跑完时，视觉模型可能还在第二步。

### 3. 出问题它会告诉 Agent 哪里错了

原始 UI 自动化只返回“已点击”——可如果 App 崩了、权限弹窗抢了这次点击、按钮根本没反应，这毫无意义。每个动作后，`autoharmony` 输出一行带原因的 verdict：

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

Agent 按这行 `ACTION_VERDICT:` 的首个 token 分支。注意 `--json` 里的 `status` 只区分 `SUCCESS`/`FAILED`，上面这些细分状态在文本行里：

| 状态 | Agent 动作 |
|---|---|
| `SUCCESS` | 继续 |
| `BLOCKED_BY_DIALOG` | `--auto-handle-dialog` 自动兜底 |
| `PENDING_DIALOG` | 处理残留弹窗（`ui dismiss-dialogs`）后复核 |
| `CRASHED` / `RESTARTED` | 停止、恢复 App、上报崩溃 |
| `ERROR` | 动作执行失败，检查选择器/设备连接 |
| `NO_CHANGE` / `BACK_INEFFECTIVE` | 重新考虑导航 |

恢复能力是内建的，不是补丁：

- **弹窗自动处理** — `--auto-handle-dialog` / `ui dismiss-dialogs` 识别并点掉权限/升级/覆盖层弹窗，流程不会被弹窗卡死。
- **崩溃检测** — 每个动作后自动抓 CppCrash / JSCrash / AppFreeze。
- **断言轮询** — `--expect-text "成功" --timeout 10` 对 route/text/dialog/state 断言持续轮询控件树，时序敏感断言不再 flake（`--expect-no-change` 为即时判定）。
- **失败必留证** — `ui` 动作失败时自动截屏 + 抓 `hilog` 切片到 `artifacts/`；疑似崩溃时附带 `hidumper -e`，能取到当前控件树时一并存 dump。位置通过 `--json` 的 `log_path` 与 stderr 的 `📦 失败留证: …` 交还。`--no-artifacts`（或 `HMUITEST_ARTIFACTS=0`）可关闭。
- **全量日志留痕，零配置** — 每次运行 stdout+stderr（含 traceback）自动追加到 `~/.hmuitest/logs/uitest-YYYYMMDD.log`；会话用 `run=<id>` 关联，dump 按天归档带 pid、与操作用 `[dump]`/`[time]` 关联。事后看日志即可定位，不必重跑。

---

## 每个动作之后：一份差异，一份日志

verdict 告诉你成/败；这两样告诉你**到底发生了什么**——不用截图、不用猜。

### 自动差异比较

每个 `ui` 动作前后各快照一次控件树，只报告变化的部分。路由跳转、新弹窗、文字切换，Agent 直接看到——而不是一句“已点击”。

```text
🧭 组件树变化  操作: click 设置  时间: 2026-09-18 14:05:22
📊 新增3 · 消失0 · 位置变化12 · 文本/状态/属性2 · 遮挡0 · 解除遮挡0 · 其它0
● 新增(3)  →  有信息量 1 条
    + Text '语言'  [56,820][1180,900]
● 位置变化(12)  →  合并 2 组同位移
    ▸ 位移(+0,-120)px · 8 节点 · 如 u=abc123 Column
PAGE_RESULT: CHANGES_DETECTED
```

- **变化分类** — 新增 / 消失 / 文本 / 位置 / 状态 / 属性 / 覆盖层出现 / 遮挡 / 解除遮挡，各自带优先级。
- **路由变化单独成报** — 前后导航栈 + 新页面内容概览。
- **天生精简** — 折叠结构节点、同位移合并、消失节点不给坐标、重复上报去重。
- **按稳定 `uniqueId` 配对** — 滚动或过滤不会把存量节点误报为“删一个 + 加一个”。
- **系统 UI 噪声过滤** — 状态栏时钟/电量等系统 UI 节点不计入 diff。
- **机器可读结尾** — 打印结构 diff 时末行为 `PAGE_RESULT: ROUTE_CHANGED | CHANGES_DETECTED | NO_CHANGES`，Agent 无需解析散文即可分支。

### 全量日志留痕，零配置

每次运行把完整 stdout+stderr（含 traceback）追加到 `~/.hmuitest/logs/uitest-YYYYMMDD.log`。不用开启、不用记参数。终端输出原样不变，文件里多了时间戳和关联标识。

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

- **会话关联** — 每次运行分配 `run=<id>`；`[cmd]` / `[dev]` / `[page]` 开启会话，`[exit] rc=` 和 `[session]` 收尾。
- **操作与产物绑定** — 每次控件树 dump 归档到 `dump/<YYYYMMDD>/`，带 pid 和一行 `[dump]`，“这一步当时界面长啥样”事后可查。
- **耗时与吞掉的异常** — `[time]` 记录每个操作耗时，`[err]` 捕获被降级处理、否则会消失的异常。
- **路径会交还给你** — `--json` 带上 `log_path`，失败时 stderr 还会打印日志文件路径，Agent 不用猜在哪。
- **靠读日志定位，而不是反复重跑** — 日志目录用 `HMUITEST_LOG_DIR` / `HMUITEST_LOG_FILE` 覆盖，`HMUITEST_LOG=0` 关闭。

---

## 对比

**vs. [agent-device](https://github.com/callstack/agent-device)**（最接近的邻居——同样 Agent 原生、也覆盖 HarmonyOS，但横跨 9 类平台）：

| HarmonyOS 上真正见分晓处 | autoharmony | agent-device |
|---|---|---|
| 感知界面 | ✅ 控件树文本 dump（文本/类型/坐标）+ 语义定位 | 扁平 `@eN` ref 快照 |
| 弹窗 / 崩溃 | ✅ 确定性并入 verdict | ⚠️ 未宣称支持 |
| 动作后裁决 | ✅ 八态 + exit 0/1/2 | `--settle` 后给快照 diff |
| 路由 / 状态断言 | ✅ 内置轮询 | —（有谓词，无路由断言） |
| 系统 UI 噪声过滤 | ✅ 不进 diff | — |
| 业务层访问 | ✅ JSON-RPC bridge | —（仅界面） |
| 运行时 | ✅ Python + `hdc`，无 daemon | Node.js + daemon |
| 广度（平台数 / MCP / 录像 / 性能） | 只做 HarmonyOS | ✅ 9 类平台 |

**vs. AI 视觉驱动方案（Midscene 等）**：确定性选择器 vs 多模态看截图——autoharmony 赢在 100% 确定性、亚秒级、可审计、无 API key；视觉方案赢在"像人一样看界面"（颜色、布局、视觉还原度）。

**如果目标平台是 Android / iOS，请用 agent-device。** 完整对比（含实测数据与各自适合谁）→ [docs/COMPARISON.md](docs/COMPARISON.md)

---

## 断言参数

每个 `ui` **动作**命令（坐标族 click/input/swipe/back + 语义化变体）都支持这些 flag，验证是一个参数而不是一段代码：

```bash
--expect-route "SettingsPage"       # 路由变了？
--expect-text "保存成功"             # 文字出现了？
--expect-gone "加载中"               # 文字消失了？
--expect-dialog                      # 弹窗弹出来了？
--expect-no-change                   # 啥都没变（回归检查，即时判定）？
--expect-state "开关:checked=true"   # 控件状态检查
--timeout 10                         # 对 route/text/dialog/state 轮询最多 N 秒
--auto-handle-dialog                 # 自动关挡路的弹窗
```

其他常用参数：`--index N`（仅 `click-by-text`，多匹配时消歧）、`--fresh-before`（强制重新取前置基线，不复用缓存）、`--no-recover`（关闭被弹窗阻塞时的自动重试）、`--no-artifacts`（失败时不抓留证）。

---

## 批量脚本

一个 JSON 文件、N 步操作、单引擎驱动。支持两种写法：`{name, steps}` 完整脚本（`params`/`expect` 均生效），或 `script record` 产出的 `[{cmd, args}]` 录制清单（仅回放 `RECORD_ACTION_MAP` 支持的动作）。

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

可选 `start` / `setup` 字段做前置状态校验与恢复（`route` / `text` / `max_backs` / `recover`），详见 [docs/USAGE.md](docs/USAGE.md)。

---

## Bridge 框架

需要 Agent 触达 App 业务逻辑？`autoharmony` 自带 JSON-RPC TCP bridge——登录登出、用户信息、业务数据，App 的 TCP server 实现了什么就能调什么：

```python
from bridge.tcp_bridge import TcpBridge

bridge = TcpBridge("your-device-id")

bridge.call("login", {"account": "13800138000"})
info = bridge.call("getUserInfo")                       # → {"nickname": "星河", "vip": true}
devices = bridge.call("queryDevices", {"room": "客厅"}) # → {"devices": [...]}
bridge.call("logout")

bridge.close()
```

批量脚本里的桥接步骤需要固定方法签名；复制 `bridge/bridge_template.py` 扩展你自己的桥接类，用 `HMUITEST_BRIDGE_CLASS=your.module:YourBridge` 接入。协议与 ArkTS 服务端示例见 [docs/BRIDGE.md](docs/BRIDGE.md)。

---

## 安装为你的 Agent 的 skill

`autoharmony` 以标准 Agent skill 分发——丢给你的编码 Agent，它就会自己验证自己写的鸿蒙代码：

```bash
git clone git@github.com:minghaozhao-ctrl/autoHarmony.git
cp autoHarmony/SKILL.md ~/.claude/skills/autoharmony/SKILL.md   # Claude Code
cp autoHarmony/SKILL.md .cursor/rules/autoharmony.mdc           # Cursor
cp autoHarmony/SKILL.md AGENTS.md                                # Copilot / Codex / Goose
cp autoHarmony/SKILL.md .windsurfrules                           # Windsurf
cp autoHarmony/SKILL.md .clinerules                              # Cline
mkdir -p ~/.config/opencode/skills/autoharmony && cp autoHarmony/SKILL.md ~/.config/opencode/skills/autoharmony/SKILL.md   # opencode
```

各客户端的配置细节、片段、探索 → 录制 → 重放循环：
→ [docs/agent-guides/](docs/agent-guides/)

---

## FAQ

**支持模拟器吗？**
支持——任何 `hdc` 能连上的 HarmonyOS 设备，包括模拟器（真机与模拟器均有实测记录，模拟器 dump 约 1.9s）。

**App 包名怎么配？**
`export HMUITEST_BUNDLE=com.your.app`（所有命令生效），或改 `utils/bundle.py` 的 `FALLBACK_BUNDLE`。未设置时默认 `com.cmcc.DigitalHome`（开发时的宿主 App）。

**和 Hypium / Appium 什么关系？**
不是替代。Hypium 是设备端测试框架——由**人**编写用例；`autoharmony` 是 Agent 驱动的 CLI——由 **Agent** 探索、录制、重放。两者可以共存：稳定后的关键路径仍可沉淀为 Hypium/pytest 用例。

**需要 root 或给 App 插桩吗？**
不需要。只依赖 `hdc` + 系统自带 `uitest` API，对被测 App 零侵入。业务 Bridge 是可选增强，不是必需。

**Windows / Linux 能用吗？**
能——纯 Python + `hdc`，无平台特定代码。主要开发与验证环境是 macOS，Windows/Linux 欢迎反馈。

**为什么不做截图 + 视觉模型？**
确定性、速度、可审计性：视觉方案依赖模型（同样输入可能不同判定）、慢（秒级推理）、无法检测进程崩溃。`autoharmony` 把界面变成文本，把判定变成代码。需要视觉检查（颜色/布局还原度）时，可搭配视觉方案使用。

**业务 Bridge 是必须的吗？**
不是。纯 UI 操作（点击/输入/断言）不需要任何 App 侧配合。只有当你想让 Agent 直达业务状态（登录态、设备列表等）时才需要配置，见 [docs/BRIDGE.md](docs/BRIDGE.md)。

**怎么接 CI？**
exit code 天然可用：0=通过、1=失败、2=用法错误。`autoharmony --json script run suite.json` 一条命令跑完整回归，stdout 是可直接解析的 JSON。

---

## 文档地图

| 文档 | 内容 |
|---|---|
| [docs/USAGE.md](docs/USAGE.md) | 完整命令清单与参数 |
| [docs/AGENT_GUIDE.md](docs/AGENT_GUIDE.md) | Agent 集成指南（探索 → 录制 → 重放） |
| [docs/EXAMPLES.md](docs/EXAMPLES.md) | 真实用例与工作流 |
| [docs/BRIDGE.md](docs/BRIDGE.md) | JSON-RPC bridge 协议与 ArkTS 服务端示例 |
| [docs/REFERENCE.md](docs/REFERENCE.md) | 架构与实现细节 |
| [docs/COMPARISON.md](docs/COMPARISON.md) | 与 agent-device / 视觉方案的完整对比 |
| [docs/PROOF_DUMP_SPEED.md](docs/PROOF_DUMP_SPEED.md) | dump 速度实测报告 |
| [docs/agent-guides/](docs/agent-guides/) | 各 AI 客户端接入指南（Claude Code / Cursor / Codex / opencode 等 8 个） |
| [SKILL.md](SKILL.md) | Agent skill 定义文件 |

## 贡献

欢迎提 issue 和 PR——尤其是新引擎、更聪明的裁决、真实的 Agent 集成案例。

- **开发环境**：`git clone` 后 `pip install -e .`，改代码立即生效。
- **代码结构**：`autoharmony.py` 主入口；`engines/` 核心引擎（hdc / 语义 / diff / 裁决 / 批量）；`analyzers/` 控件树分析；`bridge/` 业务桥接；`utils/` 工具层。
- **提交前**：在真机上跑一遍 `autoharmony --json ui click-by-text "设置"` 确认基础链路正常。
- **无第三方依赖约束**：`requirements.txt` 保持零依赖（只用标准库），新功能请勿引入外部包。

---

## Star history

[![Star History Chart](https://api.star-history.com/svg?repos=minghaozhao-ctrl/autoHarmony&type=Date)](https://www.star-history.com/#minghaozhao-ctrl/autoHarmony&Date)

好用的话 ⭐ Star 一下——让 Agent 们（和人）都知道这个工具靠谱。

---

## License

MIT
