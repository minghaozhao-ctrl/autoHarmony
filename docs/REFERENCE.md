# REFERENCE

> 命令参数、注意事项、常见问题详解。所有命令入口：`UITEST="${SKILL_DIR:-.}/autoharmony.py"`。

---

## 1. 应用桥接（`app`，通过 hdc fport + TCP 直连 App）

> 所有 `app` 命令通过 TcpBridge 直连设备上的 App，执行即返回结果，无需切换目录。

### 1.1 页面导航

```bash
python3 $UITEST app navigate "MainPage"
python3 $UITEST app navigate "SomePage" --params '{"key":"value"}'
python3 $UITEST app back
python3 $UITEST app route
# 重启应用（force-stop → 冷启动，可选传目标页参数直接回到目标页）
python3 $UITEST app restart
python3 $UITEST app restart "MainPage"
# 覆盖层文本断言（锁屏/引导层不进路由栈，--expect-text 兜底断言）
python3 $UITEST app navigate "DevicePreferencePage" --expect-text "安全锁" --timeout 8
```

**路径格式**：仅支持页面名格式（如 `MobileSmartTalkPage`、`Settings`），页面需已在 route_map.json 中注册。

**路由轮询**：`app navigate` 跳转后自动轮询路由栈验证（默认 5s，可用 `--timeout N` 调整），慢加载页面不再误报失败。

**`--expect-text` 覆盖层断言**：路由栈无法感知的覆盖层（安全锁锁屏、全屏引导层等），用 `--expect-text "文本"` 断言其出现（可多次指定）。断言通过 dump 控件树轮询匹配 text/hint（包含匹配），失败 exit 1 并打印裁决。**适用**：锁屏、隐私弹层、自定义覆盖层；**不适用**：路由内页面文本（那是 `--expect-route` 的事）。

**back 校验**：`app back` 前后对比路由栈（轮询至栈稳定，最多 5s），返回无效时 exit 1 并输出 `ACTION_VERDICT: BACK_INEFFECTIVE`（已在栈底/返回被拦截/App 已退出三种情况分别提示）。

**restart 场景**：App 崩溃（`CRASHED`）/ 内部重启（`RESTARTED`）/ 需要干净状态时使用；冷启动后需重新登录态检查（`app user-info`）。

**页面状态检查（自动，异常才展开）**：`app navigate` / `click-device` / `back` / `restart` / `login` / `logout` 等页面改变类命令结束后，自动 dump 一次检查页面实况，**正常时无额外输出**（导航命令保持精简），检测到异常才展开：

| 检查项 | 级别 | 行为 |
|--------|------|------|
| 弹窗未处理 | 硬异常 | 打印弹窗摘要 + `ACTION_VERDICT: BLOCKED_BY_DIALOG`，exit 1 |
| 页面白屏（控件数 < 6） | 硬异常 | 打印警告 + `ACTION_VERDICT: FAILED reason=white_screen`，exit 1 |
| 会话失效（被踢到登录页） | 硬异常 | 打印警告 + `ACTION_VERDICT: FAILED reason=session_expired`，exit 1 |
| 加载指示器 | 软提醒 | 仅打印 ⏳ 提示（不改变退出码） |
| 页面错误文案 | 软提醒 | 仅打印 ⚠️ 提示（不改变退出码） |

`--no-page-notice` 可跳过整个检查。踢线检查在 logout/restart/login/导航目标为登录页等"处于登录页是预期"的场景自动跳过，不会误报。

### 1.2 设备操作

```bash
# 查询设备列表（支持过滤）
python3 $UITEST app devices --category security
# 点击设备卡片跳转管控页
python3 $UITEST app click-device "客厅摄像头"
# 点击后自动处理权限/云存/引导弹窗
python3 $UITEST app click-device "客厅摄像头" --auto-handle-dialog
# 点击后断言覆盖层文本（如安全锁锁屏提示），一步完成跳转+断言
python3 $UITEST app click-device "camera1" --expect-text "请输入密码" --timeout 8
```

**设备过滤选项**（配合 `app devices`，可组合使用）：

| 选项 | 说明 | 可选值 |
|------|------|--------|
| `--category` | 设备大类 | `all`所有(默认), `security`安防, `iot` IoT |
| `--class` | 安防细分类型 | `0`未知, `1`摄像头, `2`门锁, `3`猫眼, `4`门铃, `5`音箱, `7`一体机, `11` 3D光感 |
| `--status` | 在线状态 | `0`离线, `1`在线, `2`推流中(仅安防) |
| `--platform` | 业务平台 | `home`看家, `shop`商铺, `country`乡村, `community`社区 |
| `--name` | 设备名称 | 模糊匹配 |
| `--id` | 设备ID | 精确匹配 |
| `--type-id` | 设备类型ID | 精确匹配 |
| `--shared` | 仅返回**分享设备**（他人分享给当前账号，`isShared=true`） | 无值开关 |
| `--no-shared` | 仅返回**自有设备**（非分享，`isShared=false`） | 无值开关 |

> **分享标记**：`app devices` 输出中每个设备名后会带 `[分享设备]` / `[自有设备]` 标签（对应返回值 `isShared`），便于区分他人分享的设备与账号自有绑定的设备。示例：`1. [8155582000000135] 20小湃摄像头 (security(2.0)) [分享设备] - 在线`。

**⚠️ 注意**：
- 安防设备包含 ZDK（视洞 2.0）与 SD（视洞 1.0），两者都归 `security` 类别，输出中以 `sdkVersion` 字段区分：`"1.0"`=SD、`"2.0"`=ZDK；列表显示为 `security(1.0)` / `security(2.0)`
- `--class` 仅对 **ZDK 设备**（视洞 2.0）有效，SD 设备无 macClass 字段，无法按类型过滤
- ZDK 设备的 macClass 值：1=摄像头, 2=门锁, 3=猫眼, 4=门铃, 5=音箱, 7=一体机, 11=3D光感
- `--status 2`（推流中）仅安防设备有，IoT 设备只有 0/1
- IoT 列表中 `mainCategoryId==12`（移动看家）的条目会被自动归为安防（SD/ZDK），不出现在 `--category iot` 结果中
- 组合示例：`--category security --class 1 --status 1` 获取在线安防摄像头

**⚠️ 重要规则**：
1. 测试设备管控页时，**必须**用 `app click-device` 跳转
2. 测试设备设置页时，**必须**先跳转到管控页，再点击设置按钮

### 1.3 用户认证

> ⚠️ **登录前必须先询问用户获取测试手机号**
> 登录后必须用 `app user-info` 验证状态；测试结束后必须执行 `app logout`

```bash
python3 $UITEST app login "用户提供的手机号"   # 登录
python3 $UITEST app login "手机号" --password "密码"  # 带密码登录
python3 $UITEST app user-info                    # 验证登录状态
python3 $UITEST app logout                       # 退出登录
```

### 1.4 通过 Deep Link 跳转指定 URI（`aa start`）

> 通过 `aa start` 显式启动应用并传递 URI（如 `cmcc://digitalhome/speakingTest`），
> App 内部由 CRouter 拦截 `cmcc://` 前缀的 URI，按 `cmcc.json` 映射表路由到具体页面。

```bash
# 跳转指定 URI
python3 $UITEST aa start "cmcc://digitalhome/speakingTest" \
  --ability "EntryAbility" --module "entry"

# 携带额外参数（自动映射为 --pb/--pi/--ps，页面在 onCreate/onNewWant 中通过 want.parameters 读取）
python3 $UITEST aa start "cmcc://digitalhome/speakingTest" \
  --ability "EntryAbility" --module "entry" \
  --params '{"deviceId":"xxx","autoStart":true}'
```

**参数**：

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `<uri>` | Deep Link URI（位置参数，必填） | 无 |
| `--ability` | abilityName | 无 |
| `--module` | moduleName | 无 |
| `--action` | Want action | `ohos.want.action.viewData` |
| `--bundle` | bundleName | `com.cmcc.DigitalHome` |
| `--params` | 额外参数 JSON（bool→`--pb`、int→`--pi`、str→`--ps`） | 无 |

**注意事项**：
- 目标 Ability 的 `exported` 必须为 `true`，否则报 `10103001 Failed to verify the visibility`
- 能力名/模块名通过 `hdc shell bm dump -n com.cmcc.DigitalHome` 查看
- 支持 `--expect-*` 断言与差异报告（执行后自动 dump 控件树对比）

---

## 2. UI 操作工具（`ui`，自动返回差异报告）

### 2.0 自动反馈机制（每次操作自动执行，零参数）

所有改变 UI 的操作（点击/滑动/输入/返回/aa start）执行后自动输出：

1. **差异报告**（原有）：变化明细 + `PAGE_RESULT:` 标记（§3.4）
2. **页面状态摘要** `CURRENT_PAGE_STATE`：
   - 📍 路由栈（最后 3 级，仅页面跳转时打）
   - 💬 Toast 文本（瞬时反馈，如"保存成功"/"密码错误"）
   - 🪟 弹窗类型 + 弹窗内标题/按钮文字
   - 🔘 状态控件实际值（Toggle/Switch/Checkbox/Radio/Slider 的 开/关/选中）
   - ⌨️ 输入框实际内容（可发现"旧值未删干净导致新旧混合"）
   - ⚠️ 警告：白屏（控件数异常少）/ 加载中 / 被踢下线（路由栈顶为登录页）/ 页面内错误文案
3. **分级输出**：同页面连续小操作只打**增量**（`delta`，仅变化的项），页面跳转打**全量**
4. **软失败信号**：操作已执行但页面无变化时，输出 `ACTION_VERDICT: NO_CHANGE`（exit 1），AI 必须复核后再继续（`--expect-no-change` 时视为通过）
5. **异步生效保护**：页面存在状态控件且操作无变化时，自动延迟 1.5s 复查一次（防 Toggle 异步生效被误判失败后重试导致双翻转）
6. **断言失败对照**：`--expect-text` 失败时打印页面现有文本样本；`--expect-gone` 失败时打印实际残留值
7. **统一裁决** `ACTION_VERDICT:`：每次操作输出一行机器可读结论（见下表），非 SUCCESS 时 exit 1
8. **归档 JSON 路径** `📄 控件树 JSON: <路径>`：操作后引擎已 dump 一次（差异报告用），
   直接给出本次 dump 的归档路径（`~/.hmuitest/logs/dump/<YYYYMMDD>/`），复核/精析
   用 jq/grep 解析该文件即可，**无需再跑一次 `tree dump`**（`--fast` 时静默）

**ACTION_VERDICT 状态与 AI 消费规则**：

| 状态 | 含义 | AI 应对 |
|------|------|---------|
| `SUCCESS` | 操作生效（路由/控件树变化，或命中预期） | 继续下一步 |
| `NO_CHANGE` | 已执行但页面无变化（附 🎯 目标命中控件提示） | 解析操作后输出的 JSON 复核再决定；`--expect-no-change` 时视为通过 |
| `CLICK_ON_DISABLED` | 点击命中 `enabled=false`/`clickable=false` 控件，**未执行点击** | 目标被禁用：先确认是否处于锁定/加载态，勿盲目重试 |
| `BLOCKED_BY_DIALOG` | 无变化且被覆盖层弹窗挡住（reason 含弹窗文本，suggestion 含可点按钮） | 按 suggestion 点击按钮，或 `ui dismiss-dialogs` 清理后重试一次 |
| `PENDING_DIALOG` | 检测到变化但弹窗仍悬停（操作可能只点到弹窗内元素） | 确认/处理弹窗按钮（`ui dismiss-dialogs`）后复核操作结果是否真正生效 |
| `BACK_INEFFECTIVE` | 返回未生效（栈底/被拦截/已退出） | `app route` 确认栈；需回退则 `app navigate` 目标页 |
| `CRASHED` | App 进程退出/faultlog | `app restart <目标页>`（位置参数，可选重导航）后重试 |
| `RESTARTED` | App 内部重启（accessibilityId 骤降，UI 重建） | 页面状态已重置：重新登录检查 + `app navigate` 目标页 |
| `BRIDGE_UNREACHABLE` | `app` 桥接命令连不上 TcpBridge(9999)（进程消失或未就绪） | 按 suggestion：`app restart` 恢复 / `aa start` 拉起 / 等 3s 重试 |
| `ERROR` | 动作执行异常/前置条件失败（如未检测到设备） | 按 reason 定位：设备未连接/操作异常，修复后重试 |
| `FAILED` | 断言/校验失败（exit 1） | 按页面现有文本样本与期望值对照排查 |

**CLICK_ON_DISABLED 预检说明**：坐标/double/long click 在执行前会判定命中目标（面积最小命中控件沿祖先链）的可用性——链上存在 `enabled=false` 判 disabled，无可点祖先判 not_clickable。禁用时**不注入点击**直接硬失败（exit 1），并打印命中控件 type/text/clickable/enabled 与禁用链，避免"点 6 次全无效只报 NO_CHANGE"的静默失效。典型场景：安全锁 30 分钟锁定期键盘全部禁用。

**性能**：摘要复用 after-dump 内存数据，零额外 dump；操作后固定 0.4s 稳定窗（Toast/动画）；路由栈同流程内只查 1 次（合并了原重复查询，常规操作净耗时 ≈ 0）；归档 JSON 路径复用同一次 dump 的产物（零额外开销）。

### 2.1 坐标操作 + 差异比较

```bash
python3 $UITEST ui click 540 550 --operation "点击登录"
# 快速模式：uinput 直接注入触摸（跳过 uitest 服务开销），适合连续批量点击
python3 $UITEST ui click 660 2092 --uinput --operation "快速点击数字键"
# 序列点击：一条 shell 批量 uinput 注入，跳过逐步 diff，仅末尾校验+断言
python3 $UITEST ui click-sequence "219,2092;660,2092;1100,2092" --interval 0.2 \
  --expect-text "已错误输入密码5次" --timeout 10 --operation "连点触发锁定"
python3 $UITEST ui double-click 500 500 --operation "双击图片"
python3 $UITEST ui long-click 500 500 --operation "长按删除"
python3 $UITEST ui swipe 540 1500 540 500 --operation "向下滚动列表"
python3 $UITEST ui input "Hello" --operation "输入搜索关键词"
# 输入前先清空焦点框（防止残留字符拼接，密码框必用）
python3 $UITEST ui input "888888" --clear --operation "输入安全锁密码"
python3 $UITEST ui back --operation "返回上一页"
```

**路由变化自动识别**：当页面路由发生变化时，自动输出新旧路由栈对比。

**`--clear` 清空注入**：隐藏输入框（如安全锁 PasswordInput 的透明 TextInput）有残留字符时，`ui input` 会与之**拼接**导致校验误报（如残留 `123` + 输入 `888888` = `123888`）。`--clear` 先执行 DEL×6 + Ctrl+A 全选 + DEL 清空再注入。失败且未加 `--clear` 时会打印 💡 提示。**局限**：目标框无焦点时（如锁屏自绘键盘场景）任何文本注入都无效，需用 `ui click` 坐标点击数字键。

**`click-sequence` 序列点击**：大量重复点击（如制造 5 次密码错误锁定 = 5 轮×12 次点击）时，逐点走 diff 管线会超时。序列点击在**一条 hdc shell 内批量 `uinput -T -c` 注入**（uinput 无 uitest 的并发锁，批量有效），跳过逐步 diff，仅末尾做一次 dump 校验 + `--expect-*` 断言。实测 60 个点击点 37s 完成（逐点方式 120s 超时）。

**`--pct` 比例坐标（换分辨率不废脚本）**：`ui click` / `double-click` / `long-click` / `swipe` / `click-sequence` / `toggle-state` / `wait-for` 支持 `--pct`，坐标按 **0-1 屏幕比例**解释（奇偶位分别对应宽/高）：

```bash
python3 $UITEST ui click 0.5 0.86 --pct --operation "点击屏幕中部偏下"
python3 $UITEST ui swipe 0.5 0.77 0.5 0.59 --pct --operation "上滑"
python3 $UITEST ui toggle-state --x 0.864 --y 0.143 --pct --expect true
```

**`--fast` 全局快速模式（批量回归）**：`--fast` 可加在任意位置，效果：稳定窗 0.4s→0.1s + 静默 diff/页面摘要/归档 JSON 路径（仅保留 `ACTION_VERDICT`）。实测输出 144 行→3 行、单步 4.47s→3.15s。适合大批量用例回归；调试时不要加。也可用环境变量 `HMUITEST_FAST=1`。
>
> **注意区分**：`ui click` 的快速注入选项是 `--uinput`（uinput 直接触摸，跳过 uitest 服务开销），与全局 `--fast`（静默输出）是两个开关。旧写法 `ui click --fast` 会被解析为全局快速模式（uinput 注入不生效），请改用 `--uinput`。

### 2.2 语义化操作（按文本/ID/类型）

> 基于控件树（widget tree）按文本/ID/类型匹配定位。精确匹配失败后自动 fallback 到文本包含+坐标点击。

```bash
# 按文本点击
python3 $UITEST ui click-by-text "设置" --operation "点击设置按钮"
# 按 key 点击（需 App 侧控件设 .key('xxx')）
python3 $UITEST ui click-by-id "login_btn" --operation "点击登录按钮"
# 按类型点击
python3 $UITEST ui click-by-type "Button" --operation "点击第一个按钮"

# 双击 / 长按
python3 $UITEST ui double-click-by-text "图片" --operation "双击图片"
python3 $UITEST ui long-click-by-text "删除" --operation "长按删除"

# 定向输入（指定目标输入框，不依赖当前焦点）
python3 $UITEST ui input-by-text "手机号" "13800138000" --operation "输入手机号"
python3 $UITEST ui input-by-type "TextInput" "13800138000" --operation "输入手机号"

# 方向滑动
python3 $UITEST ui swipe-direction DOWN --distance 60 --operation "向下滑动"

# 精确匹配（禁用包含匹配 fallback，避免误触同前缀控件）
python3 $UITEST ui click-by-text "关闭" --exact --operation "精确点击关闭按钮"

# 读取/断言 Toggle（开关）状态（无需自己 dump+python 解析）
python3 $UITEST ui toggle-state                            # 列出页面全部 Toggle（checked/bounds/邻近标签）
python3 $UITEST ui toggle-state --x 1089 --y 389           # 按坐标读取
python3 $UITEST ui toggle-state --index 0 --expect true    # 断言第 0 个 Toggle 为开（不符 exit 1）

# 滚轮（Picker）列滚动到目标值（自动滑动+校验，替代手工试错 swipe）
python3 $UITEST ui picker-set --column-x 195 --value 16    # 把 x≈195 的小时列滚到 16
```

> **候选排序**：`click-by-text` 包含匹配命中多个控件时，按「最短文本 + 最小面积」排序并打印候选列表（修复 '关闭' 误匹配 '关闭推送时间段' 行容器、'云端分析' 误触列表行容器等实测误触）。需要严格匹配时用 `--exact`。
>
> **`toggle-state` 定位**：`--x/--y` 为**命中 bounds** 的 Toggle（与 click 坐标一致）；`--index` 为控件树顺序第 N 个；邻近标签通过同行左侧文本自动关联（如 `label=手机消息推送通知`）。

### 2.3 检查与查找（不执行操作，精简单行输出）

```bash
# 检查弹窗是否存在
python3 $UITEST ui check-dialog "CustomDialog"
# 检查控件是否存在
python3 $UITEST ui check-exist --text "登录"
python3 $UITEST ui check-exist --type "Button" --text "确认"
# 查找控件并打印信息
python3 $UITEST ui find --text "设置"
python3 $UITEST ui find --type "TextInput"
# 长列表智能滚动查找（当前屏轮询 + 逐屏滚动，到底自动停止）
python3 $UITEST ui scroll-find "移动侦测" --swipes 8
# 清理覆盖层弹窗（点击已知确认按钮，随调随清；被弹窗挡住时的标准纠错手段）
python3 $UITEST ui dismiss-dialogs
# 等待文本出现/消失（独立等待，不绑定操作；适合加载/异步场景）
python3 $UITEST ui wait-for "加载完成" --timeout 10
python3 $UITEST ui wait-for "加载中" --gone --timeout 15
# 等待 Toggle 状态（替代 sleep 等保存完成/开关生效）
python3 $UITEST ui wait-for --toggle-x 1089 --toggle-y 389 --checked true --timeout 8
# 截图
python3 $UITEST ui screenshot screenshot.png
```

**退出码**：操作成功/检查存在退出 0，失败/不存在退出 1，可用 `$?` 判断。

### 2.4 断言验证（`--expect-*`，可与任何 `ui` 操作命令组合）

> 断言结果直接反映到 exit code（0=pass / 1=fail），适合 CI 判定。

```bash
# 路由断言：操作后路由栈应包含指定路由
python3 $UITEST ui click-by-text "设置" --expect-route "SettingsPage" --operation "点击设置"
# 文本断言：应存在 / 应消失（均可多次指定）
python3 $UITEST ui click-by-text "登录" --expect-text "首页" --expect-gone "登录" --operation "登录"
# 期望无变化（如点击禁用按钮）
python3 $UITEST ui click-by-text "禁用按钮" --expect-no-change --operation "点击禁用按钮"
# 期望弹窗出现（默认 Dialog，可指定类型）
python3 $UITEST ui click-by-text "删除" --expect-dialog --operation "触发删除弹窗"
# 控件状态断言（如开关/复选框状态）
python3 $UITEST ui click-by-text "状态灯" --expect-state "状态灯:checked=true" --operation "开启状态灯"
```

| 参数 | 说明 |
|------|------|
| `--expect-route <路由>` | 操作后路由栈应包含指定路由（部分匹配） |
| `--expect-text <文本>` | 操作后应存在包含该文本的控件（可多次指定） |
| `--expect-gone <文本>` | 操作后应不存在包含该文本的控件（可多次指定） |
| `--expect-no-change` | 操作后控件树应无变化 |
| `--expect-dialog [类型]` | 操作后应出现指定类型弹窗（默认 Dialog） |
| `--expect-state <文本:属性=值>` | 控件状态断言，如 `状态灯:checked=true`（沿自身及父容器链匹配属性，可多次指定） |
| `--timeout <秒>` | 断言轮询超时（默认 0 不轮询）：断言失败后每 1s 重新获取页面状态复查，直到通过或超时。适合网络请求/动画导致的慢加载页面。注意 `no_change` 依赖操作瞬间的 diff，不参与轮询 |
| `--fresh-before` | 强制重新 dump 前置基准（默认复用上次 after-state，路由一致时跳过 dump，批量用例省约一半 dump） |
| `--no-recover` | 禁用弹窗自愈重试（默认裁决为 BLOCKED_BY_DIALOG 时自动清理弹窗并重试一次） |

### 2.5 弹窗自动处理（`--auto-handle-dialog`）

> 进管控页/设置页/点击操作时可能弹权限、云存、引导等弹窗，命令追加 `--auto-handle-dialog` 自动处理。

```bash
python3 $UITEST app click-device "客厅摄像头" --auto-handle-dialog
python3 $UITEST ui click-by-id "AFComponent_AFUniControl_Setting" --auto-handle-dialog --expect-route "DevicePreferencePage"
```

**行为**：操作前后各执行一轮（最多 3 轮）覆盖层弹窗检测——dump 控件树 → 找 Dialog/Sheet/Popup 容器 → 点击常见关闭按钮（**负向/跳过优先**：跳过/我知道了/取消等，正向仅作兜底，尽量无副作用关闭）→ 循环直到无弹窗。

**注意**：
- 只处理覆盖层弹窗（Overlay 类型），**App 级隐私政策弹窗（页面级，非 overlay）仍需显式 `ui click-by-text "同意"`**
- 断言自动延后到弹窗清理之后执行，避免弹窗遮挡导致误报
- 与 `--expect-no-change` 语义冲突，组合时忽略 no_change

### 2.6 批量脚本（`script run`）

> 单进程复用引擎 + 复用上一步 after-state 作为下一步 before-state，多步用例一键执行。

```bash
python3 $UITEST script run cases.json
```

**脚本 JSON 格式**：

```json
{
  "name": "登录流程",
  "steps": [
    {
      "action": "navigate",
      "params": {"page": "LoginPage"},
      "desc": "导航到登录页",
      "expect": {"timeout": 8}
    },
    {
      "action": "click_by_text",
      "params": {"text": "登录"},
      "desc": "点击登录",
      "expect": {"route": "MainPage", "text_exists": ["首页"], "timeout": 5},
      "stop_on_fail": true
    }
  ]
}
```

| 步骤字段 | 说明 |
|---------|------|
| `action` | 操作类型，见下表 |
| `params` | 操作参数 |
| `desc` | 步骤描述（显示在报告标题） |
| `expect` | 断言字典，键同 §2.4（route/text_exists/text_gone/no_change/dialog/state），另可加 `timeout`（秒）启用断言轮询 |
| `stop_on_fail` | 该步失败后是否停止整个脚本（默认 false 继续执行） |

**支持的 action**（语义引擎，坐标族与语义族通吃——SemanticEngine 继承坐标引擎全部能力）：

| action | params |
|--------|--------|
| `click` / `double_click` / `long_click` | `{"x": 540, "y": 550}`（坐标 px；click 另可加 `"uinput": true` 快速注入） |
| `click_sequence` | `{"points": [[219,2092],[660,2092]], "interval": 0.2}` |
| `swipe` | `{"x1": 540, "y1": 1500, "x2": 540, "y2": 500}` |
| `text_input` | `{"text": "..."}`（另可加 `"clear": true` 先清空再输入） |
| `key_back` | 无 |
| `click_by_text` / `click_by_id` / `click_by_type` | `{"text": "..."}` / `{"key": "..."}` / `{"widget_type": "..."}` |
| `double_click_by_text` / `long_click_by_text` | `{"text": "..."}` |
| `input_by_text` / `input_by_type` | `{"target": "...", "text": "..."}` / `{"widget_type": "...", "text": "..."}` |
| `swipe_direction` | `{"direction": "DOWN", "distance": 60}` |
| `go_back` | 无 |
| `screenshot` | `{"path": "shot.png"}` |
| `check_component` | `{"text": "...", "key": "...", "type": "..."}` — `text` 为**包含匹配**（含 hint）；`key`/`type` 为**精确匹配**（须与控件完整 id/type 一致） |
| `check_dialog` | `{"type": "Dialog"}` |
| `aa_start` | `{"uri": "cmcc://digitalhome/speakingTest", "action": "...", "bundle_name": "...", "ability_name": "...", "module_name": "...", "params": {...}}` — 通过 deep link 启动应用，`uri` 必填，其余可选（缺省同 §1.4） |

**桥接步骤**（通过 TcpBridge 直连 App，可与 UI 步骤混合编排）：

| action | params | 说明 |
|--------|--------|------|
| `navigate` | `{"page": "X", "nav_params": {...}}` | 跳转页面；默认自动断言路由已跳转（轮询 5s，可被 expect 覆盖） |
| `navigate_back` | 无 | 返回上一页 |
| `login` | `{"phone": "138..."}` | 登录 |
| `logout` | 无 | 退出登录 |
| `get_user_info` / `get_route` | 无 | 查询用户信息 / 当前路由栈 |
| `query_devices` | `{"device_category": "security", ...}` | 查询设备列表（过滤键同 §1.2，含 `is_shared` 布尔：true 仅分享设备 / false 仅自有设备） |
| `click_device_card` | `{"name": "客厅摄像头"}` | 点击设备卡片跳转管控页 |

桥接步骤的 `expect` 同样支持 `timeout` 轮询，但不支持 `no_change`；改变 UI 的桥接步骤（navigate/login 等）执行后会自动刷新差异比较基准。

**退出码**：全部通过 0，存在失败 1。

---

## 3. 控件树分析（`tree`）

### 3.1 分析本地文件（`tree show`）

适用场景：保存控件树文件供后续分析、对比历史文件、调试排查。

```bash
hdc shell uitest dumpLayout -a -p /data/local/tmp/layout.json
hdc file recv /data/local/tmp/layout.json layout.json
python3 $UITEST tree show layout.json --text "登录"
```

### 3.2 远程 dump 并分析（`tree dump`）

```bash
python3 $UITEST tree dump                    # dump + 落盘 JSON 路径
python3 $UITEST tree dump --text "登录" --json
```

> **输出**：`📄 控件树 JSON: <归档路径>`（产物定位入口，
> 归档于 `~/.hmuitest/logs/dump/<YYYYMMDD>/`）；指定搜索选项时追加分析结果。
> 未指定搜索模式时仅输出 JSON 路径（精确分析用 jq/grep 解析 JSON 文件）。

**控件树 JSON 格式**（dumpLayout 输出，递归结构）：

```json
{
  "attributes": {"type": "Root", "bounds": "[0,0][1080,2400]"},
  "children": [
    {
      "attributes": {"type": "Text", "text": "标题", "bounds": "[100,100][300,150]"},
      "children": []
    }
  ]
}
```

- 每个节点含 `attributes`（`type`/`bounds`/`text`/`id`/`clickable` 等）和 `children` 列表
- `bounds` 格式 `[left,top][right,bottom]`
- 构造回归测试夹具时按此结构手写

### 3.3 分析选项（`tree show` / `tree dump` 共用）

> **默认过滤顶层页面**：dumpLayout 默认合并所有窗口，且 HarmonyOS Navigation 栈中
> 被覆盖的下层页面（如事件页 `NavDestinationMode.DIALOG` 覆盖的设置页）仍留在控件树中
> （visible=true），导致 dump 混层。`tree show` / `tree dump` 默认只保留**当前可见页面**
> （App 窗口内 DFS 最后一个 NavDestination 及其祖先链）+ **弹窗/覆盖层**（不在任何
> NavDestination 内的节点），剔除被覆盖的下层页面与系统窗口（桌面/状态栏）。
> 需要完整原始树时加 `--no-filter`。

| 选项 | 说明 | 示例 |
|------|------|------|
| `--type <类型>` | 按类型搜索 | `--type Button` |
| `--text <文本>` | 按文本搜索（包含匹配） | `--text 登录` |
| `--id <ID>` | 按ID搜索 | `--id login` |
| `--clickable` | 搜索可点击控件 | `--clickable` |
| `--input` | 搜索输入框 | `--input` |
| `--list-types` | 列出所有控件类型 | `--list-types` |
| `--detail <索引>` | 显示控件详情 | `--detail 1` |
| `--json` | 搜索结果以 JSON 数组输出（配合搜索选项） | `--text 登录 --json` |
| `--no-filter` | 关闭顶层页面过滤（保留被覆盖页面与系统窗口） | `--text 设置 --no-filter` |

### 3.4 差异比较（`tree diff` / `tree auto`）

```bash
# 比较两个控件树文件
python3 $UITEST tree diff layout1.json layout2.json --operation "点击按钮"
# 自动差异：dump 当前控件树与上次基准比较（每次执行后保存新基准）
python3 $UITEST tree auto --operation "点击按钮"
```

**`PAGE_RESULT` 机器可读标记**：diff 报告末尾输出，供 agent 程序化判断：

| 标记 | 含义 |
|------|------|
| `PAGE_RESULT: CHANGES_DETECTED` | 检测到控件变化 |
| `PAGE_RESULT: NO_CHANGES` | 无变化 |
| `PAGE_RESULT: ROUTE_CHANGED` | 页面路由发生变化 |

---

## 4. 截图工具

> ⚠️ **截图仅用于留证，判断 UI 状态必须通过控件树分析**

```bash
python3 $UITEST ui screenshot screenshot.png
```

---

## 5. 日志抓取（`log grab`，防卡死）

> 设备侧重定向到文件 + hdc 超时 + 部分输出保留，杜绝 `hdc shell "hilog ..." | grep` 挂死。
> 注意：始终用 `hdc shell "hilog ..."` 而非 `hdc hilog ...`（后者过滤选项不生效）。

```bash
# 按包名抓应用日志（tail 截断，stdout 显示末尾 60 行）
python3 $UITEST log grab --bundle com.cmcc.DigitalHome --tail 2000
# 按 tag/级别过滤；正则匹配日志内容（设备侧 -e）
python3 $UITEST log grab --bundle com.cmcc.DigitalHome --tag "A00000,HPSTaskManager" --level E
python3 $UITEST log grab --bundle com.cmcc.DigitalHome --regex "setWechatPushSwitch" --tail 500
# 按 PID；保存全部到本地文件
python3 $UITEST log grab --pid 51862 -o /tmp/app.log --tail 5000
# 本地二次过滤（全量拉取后 Python 正则，适合中文/多条件）
python3 $UITEST log grab --bundle com.cmcc.DigitalHome --tail 3000 --filter "推送|Push"

# 故障日志（崩溃堆栈；hidumper -e，shell 无权限直读 /data/log/faultlog）
python3 $UITEST log grab --list-fault                            # 列出异常退出记录
python3 $UITEST log grab --fault --fault-n 1 -o /tmp/crash.txt   # 抓最近 1 条完整堆栈
```

| 参数 | 说明 |
|------|------|
| `--bundle <包名>` | 按包名解析 PID（pidof，最多取 5 个） |
| `--pid <PID>` | 直接按 PID 过滤 |
| `--tag <tag1,tag2>` | tag 过滤（逗号分隔，最多 10 个） |
| `--level <D/I/W/E/F>` | 级别过滤（逗号多值） |
| `--type <app/core/init>` | 日志类型过滤 |
| `--regex <expr>` | 设备侧内容正则（`-e`，不匹配 tag） |
| `--filter <expr>` | 本地二次过滤（拉取后 Python 正则） |
| `--tail` / `--head <N>` | 只取尾部/头部 N 行（设备侧 `-z`/`-a`；与 `-x` 互斥，工具自动处理） |
| `-o <文件>` | 保存全部行到本地文件 |
| `--show <N>` | 无 `-o` 时 stdout 显示末尾 N 行（默认 60） |
| `--timeout <秒>` | hdc 调用超时（默认 25）；超时仍保留已落盘的部分内容 |
| `--fault` / `--list-fault` | 抓故障日志 / 列出故障记录（`--fault-n` 指定条数） |

**实测坑**：`hilog -x` 与 `-z`/`-a` 互斥（报 `Mutlti commands can't be used in combination`）；`-z N` 与 `-t app` 组合可能返回多于 N 行，工具已做本地兜底截断。

## 6. 注意事项

1. **按钮点击规则**：三次无反应即标记失败
2. **反馈消费规则**：每次操作后必须读 `CURRENT_PAGE_STATE` 摘要与 `PAGE_RESULT:` 标记；`ACTION_VERDICT: NO_CHANGE`（exit 1）出现时先复核再继续（详见 SKILL.md「AI 行为约束」）
3. **文件路径限制**：设备端临时文件用 `/data/local/tmp/`；本地拉取路径须用真实路径（macOS `/tmp` 是符号链接，hdc 不跟随）
4. **日志保存**：自动保存 hilog，测试结束后提取 error.log 和 app.log，最后汇报日志路径
5. **文件操作限制**：测试执行过程中不能删除文件

---

## 7. 常见问题

### 7.1 账号被踢下线
- **识别**：`app user-info` 返回未登录、UI 跳转登录页、网络请求返回 401
- **解决**：退出登录后重新登录

### 7.2 错误：uitest-api does not allow calling concurrently
- **原因**：异步接口未加 await，或多进程执行；**一条 `hdc shell` 内多次调用 uitest 也会触发并发锁**（如 `uitest uiInput keyEvent 2055; uitest uiInput keyEvent 2055`），且第二次起静默失效不报错
- **解决**：每个 uitest 调用单独一条 `hdc shell`；批量注入用 `uinput`（`hdc shell uinput -T -c x y`）——uinput 无并发锁，一条 shell 内用 `;` 串联多次有效（`click-sequence` 即基于此）

### 7.3 app 桥接命令报 BRIDGE_UNREACHABLE
- **识别**：`ACTION_VERDICT: BRIDGE_UNREACHABLE | reason=进程未运行/TcpBridge(9999) 无响应 | suggestion=...`（结构化输出，无 Python traceback）
- **进程未运行**（模拟器资源回收/崩溃）：`app restart` 恢复（登录态通常保持）
- **进程在运行但无响应**（刚拉起未就绪）：等 3s 重试一次即可

### 7.4 错误：does not exist on current UI!
- **原因**：查找到控件后界面已变化
- **解决**：重新执行，确保控件存在

### 7.5 导航失败
- 检查页面路径格式、确认页面已在 route_map.json 中注册、确认应用正常运行

### 7.6 命令参数报错
- 子命令结构互斥（如 `ui click-by-text` 与 `ui click` 不可混用），parse 时即校验
- 查看子命令帮助定位可用参数：`python3 $UITEST <command> <action> --help`

## 8. 跨会话设备占用（`device`，并行 agent 防互拆）

多会话/多 agent 共享同一设备时，通过声明目录互斥，防止并行 hdc 操作互相干扰：

```bash
python3 $UITEST device status                # 查看当前占用（支持 --json）
```

- **无 release 接口**：锁的目的是防互拆，提供释放会让其他会话先释放再操作、绕过防护。被占用时 AI 应**停止等待**，放弃本操作或询问用户；过期声明（心跳超 TTL，默认 180s）会被后续 claim 自动接替
- 默认 `enforce` 策略：被其他活跃会话占用时停止并报错（`--device-wait SEC` 显式指定时才等待）
- `off` 策略（`HMUITEST_DEVICE_LOCK=off`）= 完全关闭：不登记也不拦截
- 环境变量：`HMUITEST_SESSION_ID`（显式会话名）/ `HMUITEST_CLAIM_TTL`（心跳过期，默认 180s）/ `HMUITEST_CLAIM_DIR`（声明目录）
