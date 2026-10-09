# 对比：autoharmony vs. 其他 Agent 自动化方案

> 本文是 [README](../README.md) 对比章节的完整版。对比只针对 **HarmonyOS 平台**——其他平台上各工具差异很大，不在讨论范围。

## 定位差异，一句话

- **autoharmony**：为 Agent 闭环而生的 **HarmonyOS 专用** UI 测试工具——确定性 verdict、内置断言、业务桥接、零依赖。
- **agent-device**：面向 Agent 的**跨平台**设备自动化 CLI（callstack 出品，9 类平台）——宽面工具，HarmonyOS 是目标之一。
- **Midscene.js**：**AI 视觉驱动**的 Web/移动自动化——多模态模型看截图决策，跨端通用。

三者不是替代关系，是不同取舍。

---

## vs. agent-device

[agent-device](https://github.com/callstack/agent-device) 是最接近的邻居——同样是面向 Agent 原生的 CLI，也覆盖 HarmonyOS（通过 `hdc` + ArkUI `uitest`），且横跨众多平台。它是强大的宽面工具：九类平台、MCP + 类型化 Node.js API、worktree 会话、设备云、丰富证据。`autoharmony` 押的是更窄的一注——只做 HarmonyOS——并把深度都花在这里。

### HarmonyOS 深度——真正见分晓处

| | autoharmony | agent-device（HarmonyOS） |
|---|---|---|
| **感知界面** | ✅ 控件树文本 dump（文本/类型/坐标）+ 语义定位，全本地 | 扁平无障碍快照 / `@eN` ref |
| **滚动查找屏外元素** | ✅ `ui scroll-find "text"` | —（先 scroll 再重新快照） |
| **弹窗/告警处理** | ✅ 确定性、自动 | ⚠️ HarmonyOS 未宣称支持 |
| **崩溃检测** | ✅ CppCrash / JSCrash / AppFreeze → verdict | ⚠️ HarmonyOS 未宣称支持 |
| **动作后裁决** | ✅ 八态 + exit 0/1/2 | `--settle` 后给快照 diff |
| **路由/状态/无变化断言** | ✅ 内置轮询 | —（有谓词，无路由断言） |
| **覆盖层/遮挡感知** | ✅ 分层渲染 + diff 的遮挡/解除遮挡分类 | — |
| **系统 UI 噪声过滤** | ✅ 状态栏时钟/电量不进 diff | — |
| **失败自动留证** | ✅ 截图 + `hilog`（+ 崩溃时 faultlog）本地、零配置 | ⚠️ artifact 包（视频 / 日志 / trace） |
| **业务层访问** | ✅ JSON-RPC bridge | —（仅界面） |
| **运行时** | ✅ 只需 Python + `hdc`——无 daemon、无依赖，~0.6s / ~2.6s | Node.js 22.12+ + daemon |
| **并行 Agent** | ✅ 会话级设备占用锁（无设备云） | ✅ worktree 会话、设备锁、设备云 |

### 广度——agent-device 走得更远的地方（多在 HarmonyOS 之外）

| | autoharmony | agent-device |
|---|---|---|
| **控件树 diff** | ✅ `uniqueId` 配对、精简 | ✅ `diff snapshot` |
| **点击 / 输入 / 等待** | ✅ | ✅ |
| **截图 / 录像** | ✅ 截图 | ✅ 截图 + 真机录像 |
| **应用生命周期** | 仅 Deep Link | ✅ `open` / `close` / `install` |
| **键盘控制** | — | ✅ `keyboard enter` / `dismiss` |
| **手势** | swipe + 方向 | ✅ `swipe` / `pan` / `fling` |
| **设备应用日志** | — | ✅ `logs`（`hilog`） |
| **内存 / 性能** | — | ✅ HarmonyOS 上有 RSS |
| **MCP server / Node.js API** | — | ✅ |
| **平台** | 只做 HarmonyOS | 9 类目标 |

### 实测差异（HarmonyOS 真机，2026-10 实测）

| 场景 | autoharmony | agent-device |
|---|---|---|
| 切 tab | 8.5s / 26 行 / 1979 字符，`ACTION_VERDICT: PENDING_DIALOG` + reason + suggestion + 留证 | `--settle` 5.1s / 37 行 / 819 字符，原始 diff 需自行解读 |
| 进设备页 + 返回 | 7.9s / 68 行 / 3451 字符，两次 `SUCCESS` + 路由栈 + 控件明细 | 9.7s / 112 行 / 2760 字符，无 verdict，靠文本推断 |
| 单动作裸执行 | ~2.6s（含前后快照 + 裁决） | 0.5s（无验证）；`--settle` 5–7s；`--verify` 3.4s（证据仅 JSON） |
| 单次快照 | ~0.7s | 1.4–1.6s（工具自警告 p95 1555ms） |

> agent-device 的 HarmonyOS 覆盖范围以其 [HarmonyOS command boundary](https://oss.callstack.com/agent-device/docs/commands) 为准；具体设备请跑 `agent-device capabilities --platform harmonyos` 获取权威列表。

### 各自适合谁

**用 autoharmony**：确定且快的 HarmonyOS 回归闭环——控件树文本 dump、`scroll-find`、弹窗自动清、崩溃并入 verdict、内置路由/状态断言、业务 bridge 一调即达，整条链路 Python + `hdc`。

**用 agent-device**：需要广度——九类平台、安装应用、键盘控制、设备日志、MCP/Node 工具、设备云、真机录像、性能采样。

**适用范围说明**：在 Android 和 iOS 上，agent-device 的后端成熟且完整：XCTest 与 Android snapshot helper、多点手势（`pan` / `pinch` / `rotate` / `transform` / `drag`）、`alert`、剪贴板、键盘、push、`logcat` / `xctrace` / Simpleperf / Perfetto 性能分析、React DevTools、录像，以及 `.apk` / `.aab` / `.app` / `.ipa` 安装。`autoharmony` 在这两个平台上完全不能跑。**如果目标平台是 Android 或 iOS，请用 agent-device。**

---

## vs. AI 视觉驱动方案（Midscene 等）

| | autoharmony | Midscene.js |
|---|---|---|
| **动作触发** | 确定性选择器（text / id / type） | 多模态模型看截图 |
| **确定性** | 100% — 同样的输入，永远同样的判定 | 概率性 — 依赖模型 |
| **速度** | 亚秒级，纯本地 | 秒级（模型推理） |
| **可审计性** | 每个操作 → 控件树 diff + verdict | 重放结果取决于模型当时状态 |
| **运行时依赖** | 无 | VLM API key 或自托管模型 |
| **崩溃检测** | ✅ 进程级 | ❌ 视觉模型看不到进程 |
| **弹窗处理** | ✅ 确定性 | ⚠️ 概率性 |
| **业务层访问** | ✅ JSON-RPC | ❌ 只能操作界面 |

**取舍**：需要像人一样*看*界面（颜色、布局、视觉还原度）——用视觉驱动方案。需要**确定且快**的回归判定——用 autoharmony。两者可以互补：视觉方案做探索期的视觉检查，autoharmony 做回归期的确定性验证。

---

## 实测记录

以上对比数据来自 2026-10 在 HarmonyOS 真机（Mate 60 Pro 级设备）上对同一 App 的对照实测。测试方法：同一设备、同一 App、串行执行，分别记录耗时、输出体积与结论质量。原始记录见 [PROOF_DUMP_SPEED.md](PROOF_DUMP_SPEED.md)。
