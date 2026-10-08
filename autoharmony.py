#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""autoharmony — HarmonyOS UI 自动化测试统一 CLI

子命令分组（互斥 action 结构化，parse 时即校验）：
  app      应用桥接（TcpBridge 直连 App：导航/登录/设备查询）
  aa       Deep Link 显式启动（aa start）
  ui       UI 操作与断言（坐标/语义化，自动差异报告）
  tree     控件树分析（本地文件/远程 dump/差异比较）
  script   批量脚本执行（单进程复用 driver）
  device   跨会话设备占用声明（并行 Agent 安全共享设备）
  log      hilog / 故障日志抓取（防卡死：设备侧落盘 + 超时保护）

exit code：0 成功；1 动作/断言/裁决失败；2 命令行用法错误（CI / agent 友好）。
"""

import argparse
import json
import os
import sys

# 约定的退出码（对 Agent/CI 稳定）：
#   0 成功；1 动作/断言/裁决失败；2 命令行用法错误（argparse 默认）。
EXIT_OK = 0
EXIT_FAIL = 1

SKILL_DIR = os.path.dirname(os.path.abspath(__file__))
if SKILL_DIR not in sys.path:
    sys.path.insert(0, SKILL_DIR)

RECORD_FILE = os.path.join(SKILL_DIR, ".autoharmony_recording.json")

# 目标应用包名集中配置（env HMUITEST_BUNDLE 可覆盖，见 utils/bundle.py）
from utils.bundle import default_bundle  # noqa: E402


# ==================== JSON Output ====================

# json 模式下人类输出改走 stderr，真正的 JSON 只写这里（默认 stdout）。
# main() 启动时记下当前 stdout，再把 sys.stdout 指向 stderr。
_JSON_STDOUT = None


def _write_json(obj: dict) -> None:
    """把 JSON 写到真正的 stdout（json 模式下不受人类输出重定向影响）。"""
    out = _JSON_STDOUT or sys.stdout
    out.write(json.dumps(obj, ensure_ascii=False, indent=2) + "\n")
    try:
        out.flush()
    except Exception:
        pass


def _attach_log_path(result: dict) -> dict:
    """把当前日志文件路径注入结果（缺失时），便于 Agent 事后定位完整日志。"""
    if "log_path" not in result:
        try:
            from engines.logger import current_log_path
            path = current_log_path()
        except Exception:
            path = None
        if path:
            result["log_path"] = path
    return result


def _emit_json(result: dict, args=None):
    """Emit structured JSON output and exit."""
    if args and getattr(args, 'json_output', False):
        _attach_log_path(result)
        _write_json(result)
        sys.exit(result.get("exit", EXIT_OK if result.get("status") == "SUCCESS"
                            else EXIT_FAIL))
    return result


def _make_verdict(status: str, reason: str, **extra) -> dict:
    """Build a standard verdict dict."""
    d = {"status": status, "reason": reason,
         "exit": EXIT_OK if status == "SUCCESS" else EXIT_FAIL}
    d.update(extra)
    return d


def _capture_cmd_failure(device, reason: str):
    """独立命令（非裁决管线）的失败留证；绝不影响主流程。"""
    try:
        from engines.artifacts import capture_failure
        capture_failure(device, reason)
    except Exception:
        pass


def _record_step(cmd: str, args_list: list):
    """Append a step to the recording file if recording is active."""
    # 未设置 _record_cmd 的命令（暂不支持回放映射）不写空步骤，
    # 避免录制文件混入 {"cmd":"","args":[]} 空步骤污染回放
    if not cmd or not os.path.exists(RECORD_FILE):
        return
    try:
        with open(RECORD_FILE, 'r', encoding='utf-8') as f:
            steps = json.load(f)
    except Exception:
        steps = []
    steps.append({"cmd": cmd, "args": args_list})
    with open(RECORD_FILE, 'w', encoding='utf-8') as f:
        json.dump(steps, f, ensure_ascii=False, indent=2)


# ==================== Common Utilities ====================

def _detect_device(explicit):
    """设备 ID 检测（显式参数 → 环境变量 → hdc list targets）"""
    from utils.hdc import detect_device_id
    device_id = explicit or detect_device_id()
    if not device_id:
        print("❌ 未检测到 hdc 设备（hdc list targets 确认连接，或设 HARMONY_DEVICE_ID）")
        print("ACTION_VERDICT: ERROR | reason=未检测到 hdc 设备")
        sys.exit(1)
    return device_id


# ==================== app 子命令辅助 ====================

def _resolve_bridge_cls():
    """解析并校验桥接类：必须实现 app 语义方法（bridge/app_bridge.py 契约），
    否则给出机器可读 ERROR 裁决后退出，避免运行到深层才 AttributeError。
    """
    from bridge.tcp_bridge import resolve_bridge_class
    from bridge.app_bridge import missing_bridge_methods
    cls = resolve_bridge_class()
    missing = missing_bridge_methods(cls)
    if missing:
        print(f"❌ 桥接实现不完整：{cls.__name__} 缺少语义方法: {', '.join(missing)}")
        print("ACTION_VERDICT: ERROR | reason=bridge_methods_missing: "
              f"{','.join(missing)} | suggestion=设置 HMUITEST_BRIDGE_CLASS="
              "your_module:YourBridge（继承 TcpBridge 并实现这些方法，"
              "参考 bridge/bridge_template.py）")
        sys.exit(1)
    return cls


def _make_bridge(args, timeout=30):
    device_id = _detect_device(args.device)
    bridge_timeout = args.timeout if getattr(args, 'timeout', 0) > 0 else timeout
    return device_id, _resolve_bridge_cls()(device=device_id, timeout=bridge_timeout)


def _add_page_notice_arg(p):
    """给会改变页面的 app 命令追加：操作后是否检查页面弹窗（默认检查并提醒）"""
    p.add_argument('--no-page-notice', action='store_true',
                   help='操作后不检查页面弹窗（默认会检查，检测到弹窗时提醒）')


def _add_expect_text_arg(p):
    """给 app 桥接命令追加 --expect-text：断言页面（含覆盖层）存在该文本。

    锁屏/引导层等覆盖层不进路由栈，--expect-route 无法断言；此前每次只能
    tree dump --text 两步确认。此参数让 navigate/click-device 后一步完成
    「跳转 + 覆盖层文本断言」（如 --expect-text "请输入安全锁密码"）。
    """
    p.add_argument('--expect-text', action='append', default=[],
                   help='断言：页面（含锁屏等覆盖层）应存在包含该文本的控件（可重复）')


def _check_page_text_exists(device, texts, timeout: float = 5.0,
                            action_label: str = "") -> bool:
    """轮询断言页面文本存在（全量控件树匹配，覆盖层文本同样可见）。

    Returns:
        True=全部文本命中；False=超时仍缺失（已打印 FAILED 裁决）
    """
    import time as _time
    from engines.hdc_engine import HdcUITestEngine
    engine = HdcUITestEngine(device=device)
    deadline = _time.time() + max(timeout, 0.5)
    missing = list(texts)
    while True:
        analyzer = None
        try:
            analyzer = engine._dump_and_load("expect_text")
            if analyzer is not None:
                hit = set()
                for t in missing:
                    for w in analyzer.widgets:
                        if t in (w.get('text') or '') or t in (w.get('hint') or ''):
                            hit.add(t)
                            break
                missing = [t for t in missing if t not in hit]
                if not missing:
                    print(f"✅ 页面文本断言通过: {texts}")
                    return True
        finally:
            if analyzer is not None:
                analyzer.cleanup()
        if _time.time() >= deadline:
            print(f"❌ 页面缺少期望文本{f'（{action_label}后）' if action_label else ''}: "
                  f"{missing}")
            print("ACTION_VERDICT: FAILED | reason=expect_text_not_found | "
                  f"missing={missing} | "
                  "suggestion=tree dump --text 查看页面实际文本，确认覆盖层/页面是否符合预期")
            return False
        _time.sleep(1)


def _report_page_dialogs(device, action_label):
    """操作后检查页面覆盖层弹窗，有则打印提醒。

    只做检测与提醒（不点击，避免误点正向按钮的副作用）；
    返回 True=页面干净；False=检测到弹窗未处理。
    失败/无法 dump 时按"干净"处理（不因检测失败误判命令失败）。
    """
    from engines.hdc_engine import HdcUITestEngine
    from engines.verdict import find_overlays, top_overlay, overlay_summary
    analyzer = None
    engine = None
    try:
        engine = HdcUITestEngine(device=device)
        analyzer = engine._dump_and_load("page_notice")
    except Exception as ex:
        print(f"⚠️  页面弹窗检查跳过（{action_label}）: {ex}")
        return True
    try:
        if analyzer is None:
            print(f"⚠️  页面弹窗检查跳过（{action_label}，获取控件树失败）")
            return True
        overlays = find_overlays(analyzer.widgets, analyzer.get_screen_bounds())
        if not overlays:
            return True
        top = top_overlay(overlays)
        assert top is not None
        summary = overlay_summary(analyzer.widgets, top)
        print(f"🔔 {action_label}后检测到弹窗未处理: "
              f"[{top.get('type', '')}] {summary}")
        print("ACTION_VERDICT: BLOCKED_BY_DIALOG | reason=post_action_overlay | "
              "suggestion=追加 --auto-handle-dialog（click-device）自动处理，"
              "或 ui dismiss-dialogs 清理后重试")
        return False
    finally:
        if analyzer is not None:
            analyzer.cleanup()
        if engine is not None:
            try:
                engine.close()
            except Exception:
                pass


def _page_notice_or_exit(args, action_label):
    """页面改变类 app 命令的统一收尾：非 --no-page-notice 时检查弹窗并提醒；
    检测到弹窗未处理且用户未要求跳过检查时以失败退出（信号给 Agent/CI）。"""
    if getattr(args, 'no_page_notice', False):
        return
    device_id = _detect_device(args.device)
    if not _report_page_dialogs(device_id, action_label):
        sys.exit(1)


def _fail(msg):
    """统一失败出口：❌ 提示 + 结构化 ERROR 裁决（机器可读）+ exit 1。

    此前失败分支只打印一句中文提示、没有 ACTION_VERDICT 行，事后排障难以机读
    定位；统一到此后，任何命令级失败都会带 `ACTION_VERDICT: ERROR | reason=...`。
    """
    print(f"❌ {msg}")
    print(f"ACTION_VERDICT: ERROR | reason={msg}")
    sys.exit(1)


def _bridge_unreachable_exit(args, ex: Exception):
    """TcpBridge 不可达的统一兜底：结构化 BRIDGE_UNREACHABLE 裁决 + exit 1。

    此前 App 进程被系统回收时，app route/navigate 等桥接命令直接抛 Python
    Traceback 退出，违反工具自身恢复协议（应给出机器可读裁决 + 恢复建议）。
    这里自动 pidof 检查进程存活，区分两种情况给出精准 suggestion。
    """
    from utils.common import run_hdc_command
    device = getattr(args, 'device', None)
    alive = False
    try:
        cmd = ["hdc"]
        if device:
            cmd.extend(["-t", device])
        cmd.extend(["shell", "pidof", default_bundle()])
        ok, out = run_hdc_command(cmd, timeout=10)
        alive = bool(ok and (out or "").strip())
    except Exception:
        pass
    if not alive:
        reason = f"App 桥接不可达：进程未运行（pidof {default_bundle()} 为空）"
        suggestion = (f"app restart 重启应用后重试；或 "
                      f"hdc shell aa start -a EntryAbility -b {default_bundle()} 拉起")
    else:
        reason = ("App 桥接不可达：进程在运行但 TcpBridge(9999) 无响应"
                  "（可能刚重启未完成初始化）")
        suggestion = "等待 3s 后重试；仍失败则 app restart 重启应用"
    print(f"❌ {reason}（{type(ex).__name__}: {ex}）")
    print(f"ACTION_VERDICT: BRIDGE_UNREACHABLE | reason={reason} | "
          f"suggestion={suggestion}")
    sys.exit(1)


def _unexpected_error_exit(args, ex: Exception):
    """未预期异常的顶层兜底：traceback 只进日志，终端给机器可读 ERROR 裁决。

    工具承诺「任何失败都可机读」；未捕获异常直接抛 Python traceback 会破坏
    这一契约（agent 无法解析）。这里统一转成 `ACTION_VERDICT: ERROR`，并把
    完整堆栈写入会话日志供事后定位。
    """
    import traceback
    tb = traceback.format_exc()
    try:
        from engines.logger import log_line
        log_line("!!! 未预期异常：\n" + tb)
    except Exception:
        pass
    reason = f"未预期异常 {type(ex).__name__}: {ex}"
    suggestion = ("查看日志中的 traceback 定位；若为桥接方法缺失，"
                  "请设置 HMUITEST_BRIDGE_CLASS 指向实现完整的 TcpBridge 子类")
    if getattr(args, 'json_output', False):
        _write_json(_attach_log_path(
            _make_verdict("ERROR", reason, suggestion=suggestion)))
    else:
        print(f"❌ {reason}", file=sys.stderr)
        print(f"ACTION_VERDICT: ERROR | reason={reason} | suggestion={suggestion}")
    sys.exit(1)


def _build_expectations(args):
    """Build expectation dict from CLI args"""
    exp = {}
    if getattr(args, 'expect_route', None):
        exp['route'] = args.expect_route
    if getattr(args, 'expect_text', None):
        exp['text_exists'] = args.expect_text
    if getattr(args, 'expect_gone', None):
        exp['text_gone'] = args.expect_gone
    if getattr(args, 'expect_no_change', False):
        exp['no_change'] = True
    if getattr(args, 'expect_dialog', None) is not None:
        exp['dialog'] = args.expect_dialog
    if getattr(args, 'expect_state', None):
        exp['state'] = args.expect_state
    if getattr(args, 'timeout', 0):
        exp['timeout'] = args.timeout
    return exp if exp else None


def _add_device_arg(p):
    p.add_argument('--device', '-d', default=None,
                   help='Target device serial (auto-detect: env → session → hdc list targets)')


def _add_expect_args(p):
    """Common assertion args for UI action subcommands"""
    p.add_argument('--operation', default="",
                   help='Operation description (shown in diff report title)')
    p.add_argument('--expect-route', type=str,
                   help='Assert: route stack should contain this route (partial match)')
    p.add_argument('--expect-text', action='append', default=[],
                   help='Assert: widget with this text should exist (repeatable)')
    p.add_argument('--expect-gone', action='append', default=[],
                   help='Assert: widget with this text should NOT exist (repeatable)')
    p.add_argument('--expect-no-change', action='store_true',
                   help='Assert: widget tree should be unchanged after action')
    p.add_argument('--expect-dialog', type=str, nargs='?', const='Dialog',
                   help='Assert: dialog should appear after action')
    p.add_argument('--expect-state', action='append', default=[],
                   help='Assert: widget state ("text:attr=value", repeatable)')
    p.add_argument('--timeout', type=float, default=0,
                   help='Assertion polling timeout seconds (default 0 = no polling). '
                        'Note: in app navigate/login commands, >0 also sets the '
                        'bridge connect timeout (default 30s)')
    p.add_argument('--auto-handle-dialog', action='store_true',
                   help='Auto-dismiss overlay dialogs before/after action')
    p.add_argument('--auto-dialog-wait', type=float, default=0.0,
                   help='Auto-handle: wait N s before first dialog check')
    p.add_argument('--auto-dialog-grace', type=float, default=2.0,
                   help='Auto-handle: after clearing wait N s & recheck delayed dialog')
    p.add_argument('--auto-dialog-back', type=int, default=1,
                   help='Auto-handle: max system-back fallback presses')
    p.add_argument('--fresh-before', action='store_true',
                   help='Force fresh baseline dump before action')
    p.add_argument('--no-recover', action='store_true',
                   help='Disable auto-retry when blocked by dialog')


def _close_engine(engine):
    """Close engine (release resources if the engine has a close())"""
    if hasattr(engine, 'close'):
        engine.close()


def _resolve_pct(args, engine, *coords):
    """--pct 模式下把 0-1 比例坐标换算为像素（奇偶位对应屏幕宽/高）

    普通模式：四舍五入取整（兼容 type=float 解析的整数坐标）。
    """
    if not getattr(args, 'pct', False):
        return tuple(int(round(float(c))) for c in coords)
    w, h = engine._screen_wh()
    out = []
    for i, c in enumerate(coords):
        size = w if i % 2 == 0 else h
        out.append(int(round(float(c) * size)))
    return tuple(out)


def _run_ui_action(args, engine, action_fn):
    """Unified UI action flow: optional pre-dialog clear → action → optional post-dialog clear → assertion

    With auto-handle-dialog, assertion is deferred until after dialog clearing.
    """
    from engines.helpers import auto_handle_dialogs, check_expectations_with_polling

    expectations = _build_expectations(args)
    auto_dialog = getattr(args, 'auto_handle_dialog', False)

    if auto_dialog and expectations and expectations.get('no_change'):
        expectations.pop('no_change', None)

    if auto_dialog:
        auto_handle_dialogs(args.device, wait=args.auto_dialog_wait,
                            max_backs=args.auto_dialog_back)

    pass_expect = None if auto_dialog else expectations
    if auto_dialog and pass_expect is None:
        pass_expect = {'auto_dialog': True}
    ok = action_fn(pass_expect)

    # Record step if recording
    if ok:
        _record_step(getattr(args, '_record_cmd', ''), getattr(args, '_record_args', []))

    if not ok:
        # 兜底：引擎未走裁决管线/未留证时补一次
        if not getattr(engine, '_artifacts_captured', False):
            _capture_cmd_failure(args.device, "action failed")
            try:
                engine._artifacts_captured = True
            except Exception:
                pass
        verdict = _make_verdict("FAILED", "Action failed")
        _emit_json(verdict, args)
        return False

    if auto_dialog:
        auto_handle_dialogs(args.device, grace=args.auto_dialog_grace,
                            max_backs=args.auto_dialog_back)
        if expectations:
            ok = check_expectations_with_polling(
                expectations,
                dump_fn=lambda: engine._dump_and_load("dialog_poll"),
                cleanup_fn=lambda a: a.cleanup(),
                device=args.device)

    verdict = _make_verdict("SUCCESS" if ok else "FAILED",
                            "Action completed" if ok else "Assertions failed")
    _emit_json(verdict, args)
    return ok


def cmd_app_navigate(args):
    import time
    device_id, bridge = _make_bridge(args)
    try:
        try:
            params = json.loads(args.params) if args.params else None
        except ValueError:
            print(f"❌ --params 非法 JSON: {args.params}")
            print("ACTION_VERDICT: ERROR | reason=invalid_params_json")
            sys.exit(1)
        result = bridge.navigate(args.page, params)
        if not result.get("success"):
            _fail("页面跳转失败: %s" % result.get('message', '未知错误'))
        # 轮询验证路由跳转（默认 5s），慢加载页面不再误报
        timeout = args.timeout if args.timeout > 0 else 5
        deadline = time.time() + timeout
        while True:
            route = bridge.get_current_route()
            if route and route[-1] == args.page:
                print(f"✅ 页面跳转成功: {args.page}")
                break
            if time.time() >= deadline:
                current = route[-1] if route else "(空)"
                print(f"📄 路由栈: {' -> '.join(route) if route else '(空)'}")
                _fail("跳转未生效（等待 %ss），当前栈顶: %s，期望: %s"
                      % (timeout, current, args.page))
            time.sleep(1)
    finally:
        bridge.close()
    # 覆盖层文本断言（锁屏/引导层不进路由栈，--expect-text 兜底断言）
    if args.expect_text:
        if not _check_page_text_exists(device_id, args.expect_text,
                                       timeout=timeout, action_label="跳转"):
            sys.exit(1)
    _page_notice_or_exit(args, f"跳转 {args.page}")


def cmd_app_back(args):
    import time
    _, bridge = _make_bridge(args)
    try:
        before_route = bridge.get_current_route()
        bridge.navigate_back()
        # 轮询等待路由栈稳定（最多 5s，连续两次一致视为稳定），避免瞬时状态误判
        deadline = time.time() + 5
        stable_route = None
        last_route = object()  # 哨兵：与任何列表都不相等
        while time.time() < deadline:
            after_route = bridge.get_current_route()
            if after_route == last_route:
                stable_route = after_route
                break
            last_route = after_route
            time.sleep(1)
        if stable_route is None:
            stable_route = last_route if isinstance(last_route, list) else None

        if stable_route and before_route is not None \
                and stable_route != before_route:
            print(f"✅ 返回上一页: {before_route[-1]} ⟶ {stable_route[-1]}")
            print("ACTION_VERDICT: SUCCESS | reason=路由栈已回退")
            _page_notice_or_exit(args, "返回")
            return
        if not stable_route:
            print("❌ 返回后路由栈为空，App 可能已退出")
            print("ACTION_VERDICT: BACK_INEFFECTIVE | reason=App 已退出 | "
                  "suggestion=app restart 重启应用后重新导航")
            sys.exit(1)
        if before_route is not None and len(before_route) <= 1:
            print(f"⚠️ 已在路由栈底（{before_route[-1]}），返回无上级页面")
            print("ACTION_VERDICT: BACK_INEFFECTIVE | reason=已在路由栈底 | "
                  "suggestion=无需返回，或 app navigate 目标页")
            sys.exit(1)
        print("❌ 返回未生效：路由栈无变化（返回可能被拦截）")
        print("ACTION_VERDICT: BACK_INEFFECTIVE | reason=路由栈前后一致 | "
              "suggestion=app route 确认栈状态，或 app navigate 目标页")
        sys.exit(1)
    finally:
        bridge.close()


def cmd_app_restart(args):
    """重启应用：force-stop → 冷启动 → 可选重新导航（恢复协议最后一环）"""
    import time
    from utils.common import run_hdc_command
    device_id = _detect_device(args.device)

    def _shell(*parts, timeout=30):
        cmd = ["hdc", "-t", device_id, "shell"] + list(parts)
        return run_hdc_command(cmd, timeout)

    ok, out = _shell("aa", "force-stop", default_bundle())
    if not ok:
        _fail("停止应用失败: %s" % out)
    print(f"✅ 已停止应用: {default_bundle()}")
    time.sleep(1)

    ok, out = _shell("aa", "start", "-b", default_bundle(), "-a", "EntryAbility")
    if not ok or "error" in (out or "").lower():
        _fail("启动应用失败: %s" % out)
    print(f"✅ 已冷启动应用: {default_bundle()}/EntryAbility")
    _page_notice_or_exit(args, "冷启动")

    if args.page:
        time.sleep(3)  # 等待冷启动完成
        bridge = _resolve_bridge_cls()(device=device_id, timeout=30)
        try:
            result = bridge.navigate(args.page)
            if not result.get("success"):
                _fail("重启后导航失败: %s" % result.get('message', '未知错误'))
            deadline = time.time() + 8
            while time.time() < deadline:
                route = bridge.get_current_route()
                if route and route[-1] == args.page:
                    print(f"✅ 重启并导航成功: {args.page}")
                    _page_notice_or_exit(args, f"重启后跳转 {args.page}")
                    return
                time.sleep(1)
            _fail("重启后导航未生效，期望: %s" % args.page)
        finally:
            bridge.close()


def _is_logged_in(info: object) -> bool:
    """登录态判定：getUserInfo 返回 dict 且 success=true 或有用户标识字段。"""
    if not isinstance(info, dict):
        return False
    if info.get('success') is True:
        return True
    return bool(info.get('mobileNumber') or info.get('passId') or info.get('sessionId'))


def cmd_app_login(args):
    _, bridge = _make_bridge(args)
    try:
        # 预检查：已登录则拒绝（热切换账号会让 App 设备缓存脏读，页面状态错乱）
        try:
            info = bridge.get_user_info()
        except (ConnectionError, TimeoutError) as ex:
            # 桥接不可达：明确失败，不静默登录
            _bridge_unreachable_exit(args, ex)
            return
        except Exception:
            # 业务层拒绝（可能未登录以 error 返回）：视为未知登录态，放行登录
            info = None
        if _is_logged_in(info):
            current = info.get('mobileNumber') or info.get('passId') or '未知'
            print(f"❌ 当前已登录（{current}），请先执行 `app logout` 再登录其他账号")
            print("ACTION_VERDICT: ERROR | reason=already_logged_in | "
                  f"current={current} | suggestion=先 `app logout`；热切换账号会导致设备缓存脏读")
            sys.exit(1)
        result = bridge.login(args.phone, args.password)
        if result.get("success"):
            print(f"✅ 登录成功: {args.phone}")
            _page_notice_or_exit(args, "登录")
        else:
            _fail("登录失败: %s" % result.get('message', '未知错误'))
    finally:
        bridge.close()


def cmd_app_logout(args):
    _, bridge = _make_bridge(args)
    try:
        bridge.logout()
        print("✅ 退出登录成功")
        _page_notice_or_exit(args, "退出登录")
    finally:
        bridge.close()


def cmd_app_user_info(args):
    _, bridge = _make_bridge(args)
    try:
        result = bridge.get_user_info()
        if result:
            print("👤 用户信息:")
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            _fail("未获取到用户信息，可能未登录")
    finally:
        bridge.close()


def cmd_app_route(args):
    _, bridge = _make_bridge(args)
    try:
        route = bridge.get_current_route()
        print("📍 当前路由栈:")
        for i, r in enumerate(route, 1):
            print(f"   {i}. {r}")
    finally:
        bridge.close()


def cmd_app_wakeup(args):
    """唤醒屏幕，可选上滑解锁（复用引擎 _ensure_awake，含熄屏状态检查）。

    用于息屏后截图全黑/控件树为空的场景；登录页等 FLAG_SECURE 页面
    禁止截屏导致的黑图不是息屏，唤醒无效（screenshot 会自动诊断提示）。
    仅熄屏（State=0）时才执行唤醒序列：亮屏时提示无需唤醒、不盲滑
    （避免在应用页面内触发滚动/跳转）。唤醒后覆盖自动熄屏时间 60s。
    """
    from engines.hdc_engine import HdcUITestEngine
    device = _detect_device(args.device)
    engine = HdcUITestEngine(device=device)
    try:
        if engine._ensure_awake(unlock=args.unlock):
            print("✅ 屏幕已唤醒（60s 内不自动熄屏）"
                  + ("，已尝试上滑解锁" if args.unlock else ""))
        else:
            print("✅ 屏幕已亮，无需唤醒")
    finally:
        engine.close()


def _clean_device_name(name):
    """清洗 App 端返回的设备名称（兼容 JSON 数组/带方括号的异常数据）"""
    if not isinstance(name, str):
        return str(name) if name is not None else '未知'
    if name.startswith('['):
        try:
            parsed = json.loads(name)
            if isinstance(parsed, list):
                return '、'.join(str(x) for x in parsed) if parsed else '未知'
        except Exception:
            pass
        return name.strip('[]')
    return name


def cmd_app_devices(args):
    _, bridge = _make_bridge(args)
    try:
        result = bridge.query_devices(
            device_category=args.category,
            device_name=args.name,
            device_id=args.id,
            device_class=args.klass,
            device_status=args.status,
            platform_type=args.platform,
            device_type_id=args.type_id,
            is_shared=args.shared,
        )
        if not result.get("success"):
            _fail("查询设备失败: %s" % result.get('message', '未知错误'))
        devices = result.get("devices", [])
        total = result.get("totalCount", 0)
        print(f"✅ 查询成功，共 {total} 个设备:")
        for i, d in enumerate(devices, 1):
            name = _clean_device_name(d.get("deviceName", "未知"))
            cat = d.get('deviceCategory', '未知')
            if cat == 'security':
                ver = d.get('sdkVersion')
                if ver:
                    cat = f"security({ver})"
            share_tag = "[分享设备]" if d.get('isShared') else "[自有设备]"
            print(f"   {i}. [{d.get('deviceId', 'N/A')}] {name} ({cat}) {share_tag} - "
                  f"{d.get('statusDesc', '未知')}")
    finally:
        bridge.close()


def cmd_app_click_device(args):
    from engines.helpers import auto_handle_dialogs
    device_id, bridge = _make_bridge(args)
    try:
        result = bridge.click_device_card(args.name)
        if not result.get("success"):
            _fail("设备卡片点击失败: %s" % result.get('message', '未知错误'))
        print(f"✅ 设备卡片点击成功: {args.name}")
    finally:
        bridge.close()
    if args.auto_handle_dialog:
        auto_handle_dialogs(args.device)
    # 覆盖层文本断言（安全锁锁屏等覆盖层不进路由栈）
    if args.expect_text:
        if not _check_page_text_exists(device_id, args.expect_text,
                                       timeout=getattr(args, 'timeout', 0) or 5,
                                       action_label="点击设备"):
            sys.exit(1)
    _page_notice_or_exit(args, "点击设备卡片")


# ==================== aa subcommand ====================

def cmd_aa_start(args):
    from engines.semantic_engine import SemanticEngine
    engine = SemanticEngine(device=args.device,
                          history_dir=getattr(args, 'history_dir', None))
    try:
        params = json.loads(args.params) if args.params else None
    except ValueError:
        print(f"❌ --params 非法 JSON: {args.params}")
        print("ACTION_VERDICT: ERROR | reason=invalid_params_json")
        _close_engine(engine)
        sys.exit(1)
    args._record_cmd = "aa start"
    args._record_args = [args.uri or "", args.bundle or "",
                         args.ability or ""]

    def action(expectations):
        return engine.aa_start(
            uri=args.uri, action=args.action, bundle_name=args.bundle,
            ability_name=args.ability, module_name=args.module,
            params=params, operation=args.operation,
            expectations=expectations,
            fresh_before=args.fresh_before,
            auto_recover=not args.no_recover)

    ok = _run_ui_action(args, engine, action)
    _close_engine(engine)
    sys.exit(0 if ok else 1)


# ==================== ui subcommands (coordinate-based, HdcUITestEngine) ====================

def _hdc_engine(args):
    from engines.hdc_engine import HdcUITestEngine
    return HdcUITestEngine(device=args.device,
                           history_dir=getattr(args, 'history_dir', None))


def cmd_ui_click(args):
    engine = _hdc_engine(args)
    x, y = _resolve_pct(args, engine, args.x, args.y)
    args._record_cmd = "ui click"
    args._record_args = [str(x), str(y)]

    def action(expectations):
        return engine.click(x, y, args.operation, expectations=expectations,
                            fresh_before=args.fresh_before,
                            auto_recover=not args.no_recover,
                            uinput=getattr(args, 'uinput', False))

    ok = _run_ui_action(args, engine, action)
    sys.exit(0 if ok else 1)


def cmd_ui_click_sequence(args):
    """序列点击：一条 shell 批量 uinput 注入，跳过逐步 diff，仅末尾校验"""
    engine = _hdc_engine(args)
    raw_points = []
    for part in args.points.split(';'):
        part = part.strip()
        if not part:
            continue
        nums = part.split(',')
        if len(nums) != 2:
            _fail(f"坐标格式错误: '{part}'（应为 x,y，多坐标用 ';' 分隔）")
        try:
            raw_points.append((float(nums[0].strip()), float(nums[1].strip())))
        except ValueError:
            _fail(f"坐标格式错误: '{part}'（应为 x,y 数值）")
    if getattr(args, 'pct', False):
        w, h = engine._screen_wh()
        points = [(int(round(px * w)), int(round(py * h)))
                  for px, py in raw_points]
    else:
        points = [(int(round(px)), int(round(py))) for px, py in raw_points]
    if not points:
        _fail("坐标序列为空（示例: \"219,2092;660,2092;1100,2092\"）")
    args._record_cmd = "ui click-sequence"
    args._record_args = [args.points, str(args.interval)]

    def action(expectations):
        return engine.click_sequence(points, interval=args.interval,
                                     operation=args.operation,
                                     expectations=expectations,
                                     fresh_before=args.fresh_before)

    ok = _run_ui_action(args, engine, action)
    sys.exit(0 if ok else 1)


def cmd_ui_double_click(args):
    engine = _hdc_engine(args)
    x, y = _resolve_pct(args, engine, args.x, args.y)
    args._record_cmd = "ui double-click"
    args._record_args = [str(x), str(y)]

    def action(expectations):
        return engine.double_click(x, y, args.operation, expectations=expectations,
                                   fresh_before=args.fresh_before,
                                   auto_recover=not args.no_recover)

    ok = _run_ui_action(args, engine, action)
    sys.exit(0 if ok else 1)


def cmd_ui_long_click(args):
    engine = _hdc_engine(args)
    x, y = _resolve_pct(args, engine, args.x, args.y)
    args._record_cmd = "ui long-click"
    args._record_args = [str(x), str(y)]

    def action(expectations):
        return engine.long_click(x, y, args.operation, expectations=expectations,
                                 fresh_before=args.fresh_before,
                                 auto_recover=not args.no_recover)

    ok = _run_ui_action(args, engine, action)
    sys.exit(0 if ok else 1)


def cmd_ui_swipe(args):
    engine = _hdc_engine(args)
    x1, y1, x2, y2 = _resolve_pct(args, engine, args.x1, args.y1, args.x2, args.y2)
    args._record_cmd = "ui swipe"
    args._record_args = [str(x1), str(y1), str(x2), str(y2)]

    def action(expectations):
        return engine.swipe(x1, y1, x2, y2, args.operation,
                             expectations=expectations,
                             fresh_before=args.fresh_before,
                             auto_recover=not args.no_recover)

    ok = _run_ui_action(args, engine, action)
    sys.exit(0 if ok else 1)


def cmd_ui_input(args):
    engine = _hdc_engine(args)
    args._record_cmd = "ui input"
    args._record_args = [args.text] + (["--clear"] if args.clear else [])

    def action(expectations):
        return engine.text_input(args.text, args.operation, expectations=expectations,
                                 fresh_before=args.fresh_before,
                                 auto_recover=not args.no_recover,
                                 clear_first=args.clear)

    ok = _run_ui_action(args, engine, action)
    if not ok and not args.clear:
        # 实测事故复盘：隐藏输入框残留字符与新文本拼接（或无获焦目标注入
        # 无效）时，表现均为 NO_CHANGE/校验失败——提示用 --clear 或坐标点击
        print("💡 提示: 输入未生效或校验失败，可能原因："
              "① 目标框无焦点（如锁屏自绘键盘需用 ui click 坐标点击）；"
              "② 隐藏输入框有残留字符与新文本拼接——加 --clear 先清空再输入")
    sys.exit(0 if ok else 1)


def cmd_ui_back(args):
    engine = _hdc_engine(args)
    args._record_cmd = "ui back"
    args._record_args = []

    def action(expectations):
        return engine.key_back(args.operation, expectations=expectations,
                               fresh_before=args.fresh_before,
                               auto_recover=not args.no_recover)

    ok = _run_ui_action(args, engine, action)
    sys.exit(0 if ok else 1)


# ==================== ui subcommands (semantic, SemanticEngine) ====================

def _semantic_engine(args):
    from engines.semantic_engine import SemanticEngine
    return SemanticEngine(device=args.device,
                        history_dir=getattr(args, 'history_dir', None))


def cmd_ui_click_by_text(args):
    engine = _semantic_engine(args)
    args._record_cmd = "ui click-by-text"
    args._record_args = [args.text]

    def action(expectations):
        return engine.click_by_text(args.text, args.operation, expectations=expectations,
                                    index=args.index,
                                    fresh_before=args.fresh_before,
                                    auto_recover=not args.no_recover,
                                    exact=getattr(args, 'exact', False))

    ok = _run_ui_action(args, engine, action)
    _close_engine(engine)
    sys.exit(0 if ok else 1)


def cmd_ui_click_by_id(args):
    engine = _semantic_engine(args)
    args._record_cmd = "ui click-by-id"
    args._record_args = [args.key]

    def action(expectations):
        return engine.click_by_id(args.key, args.operation, expectations=expectations,
                                  fresh_before=args.fresh_before,
                                  auto_recover=not args.no_recover)

    ok = _run_ui_action(args, engine, action)
    _close_engine(engine)
    sys.exit(0 if ok else 1)


def cmd_ui_click_by_type(args):
    engine = _semantic_engine(args)

    def action(expectations):
        return engine.click_by_type(args.type, args.operation, expectations=expectations,
                                    fresh_before=args.fresh_before,
                                    auto_recover=not args.no_recover)

    ok = _run_ui_action(args, engine, action)
    _close_engine(engine)
    sys.exit(0 if ok else 1)


def cmd_ui_double_click_by_text(args):
    engine = _semantic_engine(args)

    def action(expectations):
        return engine.double_click_by_text(args.text, args.operation, expectations=expectations,
                                           fresh_before=args.fresh_before,
                                           auto_recover=not args.no_recover)

    ok = _run_ui_action(args, engine, action)
    _close_engine(engine)
    sys.exit(0 if ok else 1)


def cmd_ui_long_click_by_text(args):
    engine = _semantic_engine(args)
    args._record_cmd = "ui long-click-by-text"
    args._record_args = [args.text]

    def action(expectations):
        return engine.long_click_by_text(args.text, args.operation, expectations=expectations,
                                         fresh_before=args.fresh_before,
                                         auto_recover=not args.no_recover)

    ok = _run_ui_action(args, engine, action)
    _close_engine(engine)
    sys.exit(0 if ok else 1)


def cmd_ui_input_by_text(args):
    engine = _semantic_engine(args)

    def action(expectations):
        return engine.input_by_text(args.target, args.text, args.operation,
                                     expectations=expectations,
                                     fresh_before=args.fresh_before,
                                     auto_recover=not args.no_recover)

    ok = _run_ui_action(args, engine, action)
    _close_engine(engine)
    sys.exit(0 if ok else 1)


def cmd_ui_input_by_type(args):
    engine = _semantic_engine(args)

    def action(expectations):
        return engine.input_by_type(args.type, args.text, args.operation,
                                     expectations=expectations,
                                     fresh_before=args.fresh_before,
                                     auto_recover=not args.no_recover)

    ok = _run_ui_action(args, engine, action)
    _close_engine(engine)
    sys.exit(0 if ok else 1)


def cmd_ui_swipe_direction(args):
    engine = _semantic_engine(args)

    def action(expectations):
        return engine.swipe_direction(args.direction, args.distance, args.operation,
                                        expectations=expectations,
                                        fresh_before=args.fresh_before,
                                        auto_recover=not args.no_recover)

    ok = _run_ui_action(args, engine, action)
    _close_engine(engine)
    sys.exit(0 if ok else 1)


# ==================== ui subcommands (check/screenshot, no history) ====================

def _diagnose_dark_screenshot(device, path):
    """截图全黑时的自诊断：区分「登录页禁止截屏(FLAG_SECURE)」与「息屏」。

    引擎层 screenshot 已对全黑做过一次「熄屏→唤醒+解锁→重截」自恢复，
    走到这里说明重截后仍黑：登录页禁止截屏时黑图是预期（系统 FLAG_SECURE），
    唤醒屏幕无用，应改用 tree dump / app route 获取页面信息；息屏则建议 app wakeup。
    """
    from utils.common import is_dark_image
    if not is_dark_image(path):
        return
    route_top = None
    try:
        from bridge.tcp_bridge import TcpBridge
        bridge = TcpBridge(device=device, timeout=5)
        try:
            route = bridge.get_current_route()
            if route:
                route_top = route[-1]
        finally:
            bridge.close()
    except Exception:
        pass
    if route_top == 'LoginPage':
        print("⚠️  截图全黑：当前在 LoginPage，登录页禁止截屏（FLAG_SECURE），"
              "黑图是预期行为")
        print("DIAGNOSE: login_page_no_capture | "
              "suggestion=改用 tree dump / app route 获取页面信息，无需唤醒屏幕")
    else:
        print("⚠️  截图全黑：可能息屏或禁止截屏页面"
              f"（当前路由栈顶: {route_top or '未知'}）")
        print("DIAGNOSE: dark_screenshot | "
              "suggestion=先 `app wakeup` 唤醒屏幕重试；"
              "若在登录页/安全页则黑图为预期（禁止截屏），改用 tree dump")


def cmd_ui_screenshot(args):
    engine = _semantic_engine(args)
    ok = engine.screenshot(args.path)
    _close_engine(engine)
    if ok:
        _diagnose_dark_screenshot(_detect_device(args.device), args.path)
    sys.exit(0 if ok else 1)


def cmd_ui_snapshot(args):
    """页面快照：同时保存当前页面截图(PNG)与控件树(JSON)"""
    import shutil

    base = args.path.rstrip('/')
    png_path = base + '.png'
    json_path = base + '.json'

    engine = _semantic_engine(args)
    try:
        # 1. 截图
        ok_png = engine.screenshot(png_path)

        # 2. 控件树 JSON（走 hdc 快路径 dump，比 dump_layout -a 约快 5x）
        ok_json = False
        analyzer = engine._dump_and_load("snapshot")
        if analyzer is not None:
            try:
                shutil.copyfile(analyzer.json_file, json_path)
                ok_json = True
            except OSError as e:
                print(f"❌ 保存控件树 JSON 失败: {e}")
            analyzer.cleanup()
        else:
            print("❌ 获取控件树失败")
    finally:
        _close_engine(engine)

    if ok_png and ok_json:
        print(f"✅ 快照完成:\n  截图: {png_path}\n  控件树: {json_path}")
        sys.exit(0)
    print("ACTION_VERDICT: ERROR | reason=snapshot_failed")
    sys.exit(1)


def cmd_ui_check_dialog(args):
    engine = _semantic_engine(args)
    ok = engine.check_dialog(args.type)
    _close_engine(engine)
    sys.exit(0 if ok else 1)


def cmd_ui_toggle_state(args):
    """Read/assert Toggle state (by --x/--y coordinate or --index)"""
    engine = _semantic_engine(args)
    expect = None
    if getattr(args, 'expect', None) is not None:
        expect = str(args.expect).lower() in ('true', '1', 'on', 'checked')
    x = y = None
    if args.x is not None and args.y is not None:
        x, y = _resolve_pct(args, engine, args.x, args.y)
    elif (args.x is not None or args.y is not None) and args.index is None:
        # 只传 --x 或只传 --y：半缺参数，明确报错而非静默降级为列出全部
        print("❌ 坐标参数不完整：--x 与 --y 须成对提供（或改用 --index）")
        _close_engine(engine)
        sys.exit(1)
    ok = engine.toggle_state(x=x, y=y, index=args.index,
                             expect_checked=expect)
    _close_engine(engine)
    sys.exit(0 if ok else 1)


def cmd_ui_picker_set(args):
    """Scroll a wheel/picker column to target value"""
    engine = _semantic_engine(args)
    ok = engine.picker_set(args.column_x, args.value, max_iter=args.max_iter)
    _close_engine(engine)
    sys.exit(0 if ok else 1)


def cmd_ui_check_exist(args):
    engine = _semantic_engine(args)
    ok = engine.check_component(text=args.text, key=args.id, widget_type=args.type)
    _close_engine(engine)
    sys.exit(0 if ok else 1)


def cmd_ui_find(args):
    engine = _semantic_engine(args)
    ok = engine.find_and_print(text=args.text, key=args.id, widget_type=args.type)
    _close_engine(engine)
    sys.exit(0 if ok else 1)


def cmd_ui_scroll_find(args):
    engine = _semantic_engine(args)
    # 不录制：scroll-find 参数复杂（direction/max_swipes 等）暂无回放映射，
    # 录制后回放只会"跳过"提示——不进录制文件保持产物干净
    ok = engine.scroll_find(args.text, max_swipes=args.swipes)
    if not ok:
        _capture_cmd_failure(args.device, "scroll_find not found: %s" % args.text)
    verdict = _make_verdict("SUCCESS" if ok else "FAILED",
                            f"Found '{args.text}'" if ok else f"'{args.text}' not found after {args.swipes} swipes")
    _emit_json(verdict, args)
    _close_engine(engine)
    sys.exit(0 if ok else 1)


def cmd_ui_collect(args):
    """滚动收集去重计数：数列表项数类验证（如 AI 事件 18 开关 vs 副标题 /16）"""
    import re as _re
    try:
        _re.compile(args.regex)
    except _re.error as e:
        print(f"❌ 非法正则表达式 '{args.regex}': {e}")
        print("ACTION_VERDICT: ERROR | reason=invalid_regex | "
              "suggestion=检查 regex 语法（如转义特殊字符）")
        sys.exit(2)
    engine = _semantic_engine(args)
    items = engine.collect_texts(args.regex, max_swipes=args.swipes)
    _close_engine(engine)
    if not items:
        print(f"❌ 未收集到匹配 '{args.regex}' 的文本")
        print("ACTION_VERDICT: FAILED | reason=collect_empty | "
              "suggestion=检查 regex 是否匹配页面文本，或确认列表非空")
        sys.exit(1)
    print(f"✅ 收集到 {len(items)} 项（滚动 {args.swipes} 屏内去重）:")
    for i, t in enumerate(items, 1):
        print(f"   {i}. {t}")
    print(f"COLLECT_RESULT: count={len(items)}")
    sys.exit(0)


def cmd_ui_dismiss_dialogs(args):
    """Standalone dialog dismiss: click known confirm buttons to close overlays"""
    from engines.helpers import auto_handle_dialogs
    from engines.hdc_engine import HdcUITestEngine
    from engines.verdict import find_overlays, top_overlay, overlay_summary
    auto_handle_dialogs(args.device, max_rounds=args.rounds,
                        wait=args.wait, grace=args.grace, max_backs=args.back)
    # 权威校验：无论是否点过，一律以“当前是否仍有覆盖层”为准（避免误报成功）
    engine = HdcUITestEngine(device=args.device)
    try:
        analyzer = engine._dump_and_load("dismiss_verify")
    finally:
        engine.close()
    if analyzer is None:
        print("❌ Failed to get widget tree")
        print("ACTION_VERDICT: ERROR | reason=failed_to_get_widget_tree")
        _capture_cmd_failure(args.device, "dismiss-dialogs: failed to get widget tree")
        sys.exit(1)
    overlays = find_overlays(analyzer.widgets, analyzer.get_screen_bounds())
    analyzer.cleanup()
    if overlays:
        top = top_overlay(overlays)
        assert top is not None
        print(f"❌ 弹窗仍未关闭: [{top.get('type', '')}] "
              f"{overlay_summary(analyzer.widgets, top)}")
        print("ACTION_VERDICT: BLOCKED_BY_DIALOG | reason=still_blocked | "
              "suggestion=use 'tree dump' to inspect, then click manually")
        _capture_cmd_failure(args.device, "dismiss-dialogs: overlay still present")
        sys.exit(1)
    print("✅ 弹窗均已关闭")
    print("ACTION_VERDICT: SUCCESS | reason=dialogs dismissed")
    sys.exit(0)


def cmd_ui_wait_for(args):
    """Wait for text to appear/disappear or Toggle state (standalone, not bound to action)"""
    import time
    from engines.helpers import _point_in_bounds

    use_toggle = args.toggle_x is not None and args.toggle_y is not None
    if not use_toggle and not args.text:
        print("❌ 需要 --text 或 --toggle-x/--toggle-y 指定等待目标")
        sys.exit(1)
    expect_checked = None
    if args.checked is not None:
        expect_checked = str(args.checked).lower() in ('true', '1', 'on', 'checked')
    if use_toggle and expect_checked is None:
        print("❌ Toggle 模式需要 --checked true/false")
        sys.exit(1)
    target_desc = (f"Toggle ({args.toggle_x},{args.toggle_y}) checked="
                   f"{'true' if expect_checked else 'false'}"
                   if use_toggle else f"text '{args.text}'")

    deadline = time.time() + args.timeout
    interval = max(args.interval, 0.5)
    # Use engine hdc fast-path dump (was WidgetTreeAnalyzer.dump_layout -a slow path, ~5x)
    engine = _hdc_engine(args)
    tx = ty = 0
    if use_toggle:
        tx, ty = _resolve_pct(args, engine, args.toggle_x, args.toggle_y)
    try:
        while True:
            done = False
            detail = ''
            analyzer = engine._dump_and_load("wait_for")
            if analyzer is not None:
                if use_toggle:
                    toggles = [w for w in analyzer.widgets
                               if (w.get('type', '') or '').lower()
                               in ('toggle', 'switch')]
                    hit = [w for w in toggles
                           if _point_in_bounds(w, tx, ty)]
                    if hit:
                        checked = (hit[0].get('checked', '')
                                   or '').lower() == 'true'
                        done = (checked == expect_checked)
                        detail = f"checked={'true' if checked else 'false'}"
                    else:
                        detail = 'toggle not found at coordinate'
                else:
                    matched = [w for w in analyzer.widgets
                               if args.text.lower() in (w.get('text', '') or '').lower()
                               or args.text.lower() in (w.get('hint', '') or '').lower()]
                    found = bool(matched)
                    done = (not found) if args.gone else found
                    detail = 'present' if found else 'not present'
                analyzer.cleanup()

            if done:
                print(f"✅ Wait satisfied: {target_desc} ({detail})")
                print(f"ACTION_VERDICT: SUCCESS | reason=target reached ({detail})")
                sys.exit(0)

            if time.time() >= deadline:
                print(f"❌ Wait timeout ({args.timeout}s): {target_desc} "
                      f"({detail or 'unknown'})")
                print(f"ACTION_VERDICT: NO_CHANGE | reason=waited {args.timeout}s timeout | "
                      "suggestion=check if page is loading or target is correct")
                _capture_cmd_failure(
                    args.device,
                    "wait_for timeout (%ss): %s" % (args.timeout, target_desc))
                sys.exit(1)
            time.sleep(interval)
    finally:
        _close_engine(engine)


# ==================== log subcommands ====================

def cmd_log_grab(args):
    """抓取 hilog / 故障日志（防卡死：设备侧落盘 + 超时保护 + 部分输出保留）"""
    from utils import log_grab

    if getattr(args, 'list_fault', False):
        ok, out = log_grab.list_faults(args.device, args.bundle)
        print(out or "(空)")
        sys.exit(0 if ok else 1)

    if getattr(args, 'fault', False):
        ok, msg, lines = log_grab.print_fault(
            args.device, args.bundle,
            n=args.fault_n, out_file=args.output)
        print(msg)
        if not args.output:
            for ln in lines:
                print(ln)
        sys.exit(0 if ok else 1)

    ok, msg, lines = log_grab.grab_hilog(
        device=args.device, pid=args.pid, bundle=args.bundle,
        tags=(args.tag.split(',') if args.tag else None),
        level=args.level, log_type=args.log_type, regex=args.regex,
        tail=args.tail, head=args.head, out_file=args.output,
        local_filter=args.filter, timeout=args.timeout)
    print(msg)
    if not args.output:
        show = args.show or 60
        for ln in lines[-show:]:
            print(ln)
        if len(lines) > show:
            print(f"...（共 {len(lines)} 行，仅显示末尾 {show} 行；用 -o 保存全部）")
    sys.exit(0 if ok else 1)


# ==================== tree subcommands ====================

def _analysis_mode_param(args):
    """Convert analysis options to (mode, param)"""
    if args.list_types:
        return 'list-types', None
    if args.type:
        return 'type', args.type
    if args.text:
        return 'text', args.text
    if args.id:
        return 'id', args.id
    if args.clickable:
        return 'clickable', None
    if args.input:
        return 'input', None
    if args.detail is not None:
        return 'detail', str(args.detail)
    return None, None


def _add_analysis_args(p):
    """Widget tree analysis options (shared by tree show / tree dump)"""
    p.add_argument('--type', '-t', help='Search widgets by type')
    p.add_argument('--text', '-T', help='Search widgets by text (contains match)')
    p.add_argument('--id', '-i', help='Search widgets by ID')
    p.add_argument('--clickable', '-c', action='store_true', help='Search clickable widgets')
    p.add_argument('--input', '-I', action='store_true', help='Search input widgets')
    p.add_argument('--list-types', '-l', action='store_true', help='List all widget types')
    p.add_argument('--detail', '-D', type=int, help='Show detail for widget at index')
    p.add_argument('--json', action='store_true',
                   help='Output search results as JSON array')
    p.add_argument('--no-filter', action='store_true',
                   help='Disable top-page filter (keep overlapped pages & system windows)')


def cmd_tree_show(args):
    from analyzers.widget_tree import WidgetTreeAnalyzer
    if not os.path.exists(args.file):
        print(f"❌ File not found: {args.file}")
        print(f"ACTION_VERDICT: ERROR | reason=file_not_found: {args.file}")
        sys.exit(1)
    analyzer = WidgetTreeAnalyzer(args.file)
    if not analyzer.load_tree(filter_top=not args.no_filter):
        print("ACTION_VERDICT: ERROR | reason=invalid_tree: 文件非合法控件树 JSON")
        sys.exit(1)
    mode, param = _analysis_mode_param(args)
    analyzer._dispatch_analysis(mode, param, as_json=args.json)


def cmd_tree_dump(args):
    # Use engine hdc fast-path dump (was dump_and_analyze -a slow path, ~5x)
    engine = _hdc_engine(args)
    try:
        analyzer = engine._dump_and_load("remote_dump")
        if analyzer is None:
            print("ACTION_VERDICT: ERROR | reason=dump_failed: 获取控件树失败")
            sys.exit(1)
        if args.no_filter:
            # 重载完整原始树（_dump_and_load 默认已过滤到当前页面）
            analyzer.load_tree(filter_top=False)
        # 打印落盘 JSON 路径（归档副本优先）——产物定位入口。
        # 无归档（HMUITEST_LOG=0）时不打印：临时文件 cleanup 后即失效，打印死路径误导
        dst = getattr(analyzer, '_archived_path', None)
        if dst:
            print(f"📄 控件树 JSON: {dst}")
        mode, param = _analysis_mode_param(args)
        analyzer._dispatch_analysis(mode, param, as_json=args.json)
        analyzer.cleanup()
    finally:
        _close_engine(engine)


def cmd_tree_diff(args):
    from analyzers.widget_tree import WidgetTreeAnalyzer
    from engines.diff_engine import WidgetTreeDiff
    for f in (args.file1, args.file2):
        if not os.path.exists(f):
            print(f"❌ File not found: {f}")
            print(f"ACTION_VERDICT: ERROR | reason=file_not_found: {f}")
            sys.exit(1)
    analyzer1 = WidgetTreeAnalyzer(args.file1)
    analyzer2 = WidgetTreeAnalyzer(args.file2)
    if not analyzer1.load_tree() or not analyzer2.load_tree():
        print("ACTION_VERDICT: ERROR | reason=invalid_tree: 文件非合法控件树 JSON")
        sys.exit(1)
    after_route = WidgetTreeDiff.get_current_route(args.device)
    diff = WidgetTreeDiff()
    report = diff.compare(analyzer1.widgets, analyzer2.widgets, args.operation,
                          after_route=after_route, after_analyzer=analyzer2)
    report.print()


def cmd_tree_auto(args):
    """Auto-diff: dump current tree and compare with last baseline"""
    from engines.diff_engine import WidgetTreeDiff, AutoDiffManager
    manager = AutoDiffManager(getattr(args, 'history_dir', None),
                              device=getattr(args, 'device', None))
    previous_widgets = manager.load_history()

    # Use engine hdc fast-path dump (was dump_layout -a slow path, ~5x)
    engine = _hdc_engine(args)
    try:
        analyzer = engine._dump_and_load("auto_diff")
        if analyzer is None:
            print("ACTION_VERDICT: ERROR | reason=dump_failed: 获取控件树失败")
            sys.exit(1)
        print("✅ Widget tree captured")
        print(f"📁 Temp file: {analyzer.json_file}")
        print()

        if previous_widgets:
            diff = WidgetTreeDiff()
            before_route = manager.load_route_history()
            after_route = WidgetTreeDiff.get_current_route(args.device)
            report = diff.compare(previous_widgets, analyzer.widgets, args.operation,
                                  before_route=before_route, after_route=after_route,
                                  after_analyzer=analyzer)
            report.print()
        else:
            print("ℹ️  First run, saved current tree as baseline")
            print()

        current_route = WidgetTreeDiff.get_current_route(args.device)
        manager.save_history(analyzer.widgets, route=current_route)
        analyzer.cleanup()
    finally:
        _close_engine(engine)


# ==================== script subcommand ====================

def cmd_script_run(args):
    from engines.batch_runner import BatchRunner
    if not os.path.exists(args.file):
        verdict = _make_verdict("FAILED", f"Script file not found: {args.file}")
        _emit_json(verdict, args)
        print(f"❌ Script file not found: {args.file}")
        print(f"ACTION_VERDICT: ERROR | reason=script_file_not_found: {args.file}")
        sys.exit(1)
    with open(args.file, 'r', encoding='utf-8') as f:
        try:
            script = json.load(f)
        except ValueError:
            print(f"❌ 脚本文件非合法 JSON: {args.file}")
            print(f"ACTION_VERDICT: ERROR | reason=invalid_script_json: {args.file}")
            sys.exit(1)
    runner = BatchRunner(device=args.device, history_dir=getattr(args, 'history_dir', None))
    result = runner.run(script)
    verdict = _make_verdict("SUCCESS" if result['all_passed'] else "FAILED",
                            f"{result.get('passed', 0)}/{result.get('total', 0)} steps passed",
                            all_passed=result['all_passed'],
                            passed=result.get('passed', 0),
                            failed=result.get('failed', 0),
                            total=result.get('total', 0))
    _emit_json(verdict, args)
    sys.exit(0 if result['all_passed'] else 1)


def cmd_script_record(args):
    if args.record_action == 'start':
        with open(RECORD_FILE, 'w', encoding='utf-8') as f:
            json.dump([], f)
        verdict = _make_verdict("SUCCESS", "Recording started",
                                record_file=RECORD_FILE)
        _emit_json(verdict, args)
        print(f"🔴 Recording started → {RECORD_FILE}")
    elif args.record_action == 'stop':
        if not os.path.exists(RECORD_FILE):
            verdict = _make_verdict("FAILED", "No recording in progress")
            _emit_json(verdict, args)
            sys.exit(1)
        with open(RECORD_FILE, 'r', encoding='utf-8') as f:
            steps = json.load(f)
        output = args.output or "recorded_script.json"
        with open(output, 'w', encoding='utf-8') as f:
            json.dump(steps, f, ensure_ascii=False, indent=2)
        os.remove(RECORD_FILE)
        verdict = _make_verdict("SUCCESS",
                                f"Recording saved: {len(steps)} steps",
                                output=output, steps=len(steps))
        _emit_json(verdict, args)
        print(f"✅ Recording saved: {len(steps)} steps → {output}")
    elif args.record_action == 'status':
        active = os.path.exists(RECORD_FILE)
        steps = 0
        if active:
            with open(RECORD_FILE, 'r', encoding='utf-8') as f:
                steps = len(json.load(f))
        verdict = _make_verdict("SUCCESS",
                                "Recording active" if active else "Not recording",
                                recording=active, steps=steps)
        _emit_json(verdict, args)
        print(f"✅ {'Recording active' if active else 'Not recording'}"
              + (f"（{steps} steps）" if active else ""))


# ==================== device subcommand ====================

def _device_lock_module():
    from utils import device_lock
    return device_lock


def cmd_device_status(args):
    lock = _device_lock_module()
    me = lock.resolve_session_id()
    claims = lock.list_claims()
    if getattr(args, 'json_output', False):
        _write_json(_attach_log_path({
            "session": me,
            "locked": lock.lock_enabled(),
            "claim_dir": lock.claim_dir(),
            "claims": claims,
        }))
        sys.exit(0)
    print(f"会话: {me or '(设备锁已关闭)'}")
    print(f"声明目录: {lock.claim_dir()}")
    if not claims:
        print("当前无设备占用")
        return
    for c in claims:
        state = "活跃" if c["live"] else "过期"
        mine = "  ← 本会话" if me and c["session"] == me else ""
        print(f"  • {c['device']}  会话={c['session']}  {state}  "
              f"心跳 {c['age_s']}s 前  pid={c['pid']}{mine}")


def cmd_device_release(args):
    lock = _device_lock_module()
    me = lock.resolve_session_id()
    if args.release_all:
        device, session, stale_only = None, None, False
    elif args.stale:
        device, session, stale_only = args.device, None, True
    elif args.device or args.session:
        device, session, stale_only = args.device, args.session, False
    else:
        device, session, stale_only = None, me, False
    if not (args.release_all or args.stale or args.device or args.session) and not me:
        verdict = _make_verdict("FAILED", "设备锁已关闭，且未指定 --device/--session")
        _emit_json(verdict, args)
        print("无本会话声明可释放（设备锁已关闭）")
        sys.exit(1)
    removed = lock.release(device=device, session=session, stale_only=stale_only)
    verdict = _make_verdict("SUCCESS",
                            f"released {len(removed)} claim(s)",
                            released=len(removed),
                            claims=[{"device": c.get("device"),
                                     "session": c.get("session")} for c in removed])
    _emit_json(verdict, args)
    if not removed:
        print("没有匹配的设备声明")
        return
    for c in removed:
        print(f"✅ 释放 {c.get('device')}（会话 {c.get('session')}）")


# ==================== parser ====================

def build_parser():
    parser = argparse.ArgumentParser(
        prog='autoharmony',
        description='HarmonyOS UI Automation Testing CLI: '
                    'UI actions & assertions, widget tree analysis, batch scripts.')
    parser.add_argument('--json', dest='json_output', action='store_true',
                        help='Output structured JSON (for AI agents)')
    parser.add_argument('--no-artifacts', dest='no_artifacts', action='store_true',
                        help='Disable failure evidence capture '
                             '(screenshot/hilog/dump; default on)')
    parser.add_argument('--device-wait', type=float, default=None, metavar='SEC',
                        help='Wait up to SEC seconds for a device held by another session')
    parser.add_argument('--device-takeover', action='store_true',
                        help='Forcefully take over a device held by another live session')
    parser.add_argument('--no-device-lock', action='store_true',
                        help='Disable session device-claim locking for this command')
    parser.add_argument('--fast', action='store_true',
                        help='Fast mode: settle 0.1s + silent diff/page summary '
                             '(for batch regression runs)')
    sub = parser.add_subparsers(dest='command', metavar='<command>', required=True)

    # ---------- app ----------
    app = sub.add_parser('app', help='应用桥接（TcpBridge 直连 App）')
    app_sub = app.add_subparsers(dest='subcommand', metavar='<action>', required=True)

    p = app_sub.add_parser('navigate', help='跳转到指定页面（须在 route_map.json 注册）')
    p.add_argument('page', help='目标页面名，如 MainPage')
    p.add_argument('--params', default=None,
                   help='路由参数（JSON 格式，如 \'{"sn":"xxx","mac":"yy"}\'）')
    p.add_argument('--timeout', type=float, default=0,
                   help='路由跳转轮询超时秒数（默认 5）')
    _add_expect_text_arg(p)
    _add_page_notice_arg(p)
    _add_device_arg(p)
    p.set_defaults(func=cmd_app_navigate)

    p = app_sub.add_parser('back', help='返回上一页（自动验证路由栈回退）')
    _add_page_notice_arg(p)
    _add_device_arg(p)
    p.set_defaults(func=cmd_app_back)

    p = app_sub.add_parser('restart', help='重启应用（force-stop → 冷启动 → 可选重导航）')
    p.add_argument('page', nargs='?', default=None,
                   help='重启后重新导航的目标页面（可选）')
    _add_page_notice_arg(p)
    _add_device_arg(p)
    p.set_defaults(func=cmd_app_restart)

    p = app_sub.add_parser('login', help='登录')
    p.add_argument('phone', help='手机号')
    p.add_argument('--password', default=None, help='密码（可选）')
    p.add_argument('--timeout', type=float, default=0, help='连接超时秒数（默认 30）')
    _add_page_notice_arg(p)
    _add_device_arg(p)
    p.set_defaults(func=cmd_app_login)

    p = app_sub.add_parser('logout', help='退出登录')
    _add_page_notice_arg(p)
    _add_device_arg(p)
    p.set_defaults(func=cmd_app_logout)

    p = app_sub.add_parser('user-info', help='获取当前用户信息')
    _add_device_arg(p)
    p.set_defaults(func=cmd_app_user_info)

    p = app_sub.add_parser('route', help='获取当前路由栈')
    _add_device_arg(p)
    p.set_defaults(func=cmd_app_route)

    p = app_sub.add_parser('wakeup', help='唤醒屏幕（wakeup+常亮，可选 --unlock 上滑解锁）')
    p.add_argument('--unlock', action='store_true', help='唤醒后尝试上滑解锁')
    _add_device_arg(p)
    p.set_defaults(func=cmd_app_wakeup)

    p = app_sub.add_parser('devices', help='查询设备列表（支持过滤组合）')
    p.add_argument('--category', default=None,
                   help='设备大类：all 所有(默认) / security 安防 / iot IoT')
    p.add_argument('--name', default=None, help='设备名称（模糊匹配）')
    p.add_argument('--id', default=None, help='设备ID（精确匹配）')
    p.add_argument('--class', dest='klass', type=int, default=None,
                   help='安防细分（仅 ZDK 2.0）：1摄像头 2门锁 3猫眼 4门铃 5音箱 7一体机 11 3D光感')
    p.add_argument('--status', type=int, default=None,
                   help='在线状态：0离线 1在线 2推流中(仅安防)')
    p.add_argument('--platform', default=None,
                   help='业务平台：home 看家 / shop 商铺 / country 乡村 / community 社区')
    p.add_argument('--type-id', default=None, help='设备类型ID（精确匹配）')
    p.add_argument('--shared', dest='shared', action='store_true', default=None,
                   help='仅返回分享设备（他人分享给当前账号，isShared=true）')
    p.add_argument('--no-shared', dest='shared', action='store_false',
                   help='仅返回自有设备（非分享，isShared=false）')
    _add_device_arg(p)
    p.set_defaults(func=cmd_app_devices)

    p = app_sub.add_parser('click-device', help='点击设备卡片跳转管控页')
    p.add_argument('name', help='设备名称（如 客厅摄像头）')
    p.add_argument('--auto-handle-dialog', action='store_true',
                   help='点击后自动处理权限/云存/引导弹窗')
    p.add_argument('--timeout', type=float, default=0,
                   help='文本断言轮询超时秒数（默认 5，配合 --expect-text）')
    _add_expect_text_arg(p)
    _add_page_notice_arg(p)
    _add_device_arg(p)
    p.set_defaults(func=cmd_app_click_device)

    # ---------- aa ----------
    aa = sub.add_parser('aa', help='Deep Link explicit launch (aa start)')
    aa_sub = aa.add_subparsers(dest='subcommand', metavar='<action>', required=True)

    p = aa_sub.add_parser('start', help='Launch app via Deep Link URI')
    p.add_argument('uri', help='Deep Link URI')
    p.add_argument('--bundle', default=default_bundle(),
                   help='bundleName (default $HMUITEST_BUNDLE 或内置默认)')
    p.add_argument('--ability', default=None, help='abilityName')
    p.add_argument('--module', default=None, help='moduleName')
    p.add_argument('--action', default='ohos.want.action.viewData', help='Want action')
    p.add_argument('--params', default=None,
                   help='Extra params JSON')
    p.add_argument('--history-dir', default=None,
                   help='Base dir for diff baseline; actual files go to a per-device '
                        'subdir (default $TMPDIR/hmuitest-history/<device>; '
                        'override base with env HMUITEST_HISTORY_DIR)')
    _add_expect_args(p)
    _add_device_arg(p)
    p.set_defaults(func=cmd_aa_start)

    # ---------- ui ----------
    ui = sub.add_parser('ui', help='UI actions & assertions (auto diff report)')
    ui_sub = ui.add_subparsers(dest='subcommand', metavar='<action>', required=True)

    def add_history(p):
        p.add_argument('--history-dir', default=None,
                       help='Base dir for diff baseline; actual files go to a per-device '
                        'subdir (default $TMPDIR/hmuitest-history/<device>; '
                        'override base with env HMUITEST_HISTORY_DIR)')

    # Coordinate-based (HdcUITestEngine)
    p = ui_sub.add_parser('click', help='Click at coordinates')
    p.add_argument('x', type=float, help='X coordinate (px, or 0-1 ratio with --pct)')
    p.add_argument('y', type=float, help='Y coordinate (px, or 0-1 ratio with --pct)')
    p.add_argument('--pct', action='store_true',
                   help='Interpret x/y as 0-1 screen ratios (resolution-independent)')
    p.add_argument('--uinput', action='store_true',
                   help='快速模式：uinput 直接注入触摸（跳过 uitest 服务开销），'
                        '适合连续批量点击')
    add_history(p)
    _add_expect_args(p)
    _add_device_arg(p)
    p.set_defaults(func=cmd_ui_click)

    p = ui_sub.add_parser(
        'click-sequence',
        help='Batch click sequence: "x1,y1;x2,y2;..." (one shell, uinput inject, '
             'skip per-step diff, verify at end only)')
    p.add_argument('points', help='坐标序列，格式 "x1,y1;x2,y2;..."（--pct 时为 0-1 比例）')
    p.add_argument('--interval', type=float, default=0.2,
                   help='相邻点击间隔秒数（默认 0.2）')
    p.add_argument('--pct', action='store_true',
                   help='Interpret all coords as 0-1 screen ratios')
    add_history(p)
    _add_expect_args(p)
    _add_device_arg(p)
    p.set_defaults(func=cmd_ui_click_sequence)

    p = ui_sub.add_parser('double-click', help='Double-click at coordinates')
    p.add_argument('x', type=float, help='X coordinate (px, or 0-1 ratio with --pct)')
    p.add_argument('y', type=float, help='Y coordinate (px, or 0-1 ratio with --pct)')
    p.add_argument('--pct', action='store_true',
                   help='Interpret x/y as 0-1 screen ratios (resolution-independent)')
    add_history(p)
    _add_expect_args(p)
    _add_device_arg(p)
    p.set_defaults(func=cmd_ui_double_click)

    p = ui_sub.add_parser('long-click', help='Long-click at coordinates')
    p.add_argument('x', type=float, help='X coordinate (px, or 0-1 ratio with --pct)')
    p.add_argument('y', type=float, help='Y coordinate (px, or 0-1 ratio with --pct)')
    p.add_argument('--pct', action='store_true',
                   help='Interpret x/y as 0-1 screen ratios (resolution-independent)')
    add_history(p)
    _add_expect_args(p)
    _add_device_arg(p)
    p.set_defaults(func=cmd_ui_long_click)

    p = ui_sub.add_parser('swipe', help='Swipe between coordinates')
    p.add_argument('x1', type=float, help='Start X (px, or 0-1 ratio with --pct)')
    p.add_argument('y1', type=float, help='Start Y (px, or 0-1 ratio with --pct)')
    p.add_argument('x2', type=float, help='End X (px, or 0-1 ratio with --pct)')
    p.add_argument('y2', type=float, help='End Y (px, or 0-1 ratio with --pct)')
    p.add_argument('--pct', action='store_true',
                   help='Interpret all coords as 0-1 screen ratios')
    add_history(p)
    _add_expect_args(p)
    _add_device_arg(p)
    p.set_defaults(func=cmd_ui_swipe)

    p = ui_sub.add_parser('input', help='Input text to focused field')
    p.add_argument('text', help='Text to input')
    p.add_argument('--clear', action='store_true',
                   help='输入前先清空当前焦点输入框（DEL×6 + Ctrl+A 全选 + DEL），'
                        '防止隐藏输入框残留字符与新文本拼接（密码框必用）')
    add_history(p)
    _add_expect_args(p)
    _add_device_arg(p)
    p.set_defaults(func=cmd_ui_input)

    p = ui_sub.add_parser('back', help='Press back key')
    add_history(p)
    _add_expect_args(p)
    _add_device_arg(p)
    p.set_defaults(func=cmd_ui_back)

    # Semantic (SemanticEngine)
    p = ui_sub.add_parser('click-by-text', help='Click widget by text (fuzzy match on exact fail)')
    p.add_argument('text', help='Widget text')
    p.add_argument('--index', type=int, default=None,
                   help='Click Nth match (default 0)')
    p.add_argument('--exact', action='store_true',
                   help='Only exact text match (no contains fallback)')
    add_history(p)
    _add_expect_args(p)
    _add_device_arg(p)
    p.set_defaults(func=cmd_ui_click_by_text)

    p = ui_sub.add_parser('click-by-id', help='Click widget by key/id')
    p.add_argument('key', help='Widget key or id')
    add_history(p)
    _add_expect_args(p)
    _add_device_arg(p)
    p.set_defaults(func=cmd_ui_click_by_id)

    p = ui_sub.add_parser('click-by-type', help='Click first widget of type')
    p.add_argument('type', help='Widget type, e.g. Button')
    add_history(p)
    _add_expect_args(p)
    _add_device_arg(p)
    p.set_defaults(func=cmd_ui_click_by_type)

    p = ui_sub.add_parser('double-click-by-text', help='Double-click widget by text')
    p.add_argument('text', help='Widget text')
    add_history(p)
    _add_expect_args(p)
    _add_device_arg(p)
    p.set_defaults(func=cmd_ui_double_click_by_text)

    p = ui_sub.add_parser('long-click-by-text', help='Long-click widget by text')
    p.add_argument('text', help='Widget text')
    add_history(p)
    _add_expect_args(p)
    _add_device_arg(p)
    p.set_defaults(func=cmd_ui_long_click_by_text)

    p = ui_sub.add_parser('input-by-text', help='Input to field by text')
    p.add_argument('target', help='Target field text/hint')
    p.add_argument('text', help='Text to input')
    add_history(p)
    _add_expect_args(p)
    _add_device_arg(p)
    p.set_defaults(func=cmd_ui_input_by_text)

    p = ui_sub.add_parser('input-by-type', help='Input to field by type')
    p.add_argument('type', help='Target field type, e.g. TextInput')
    p.add_argument('text', help='Text to input')
    add_history(p)
    _add_expect_args(p)
    _add_device_arg(p)
    p.set_defaults(func=cmd_ui_input_by_type)

    p = ui_sub.add_parser('swipe-direction', help='Swipe in direction')
    p.add_argument('direction', choices=['UP', 'DOWN', 'LEFT', 'RIGHT'], help='Swipe direction')
    p.add_argument('--distance', type=int, default=60, help='Swipe distance (default 60)')
    add_history(p)
    _add_expect_args(p)
    _add_device_arg(p)
    p.set_defaults(func=cmd_ui_swipe_direction)

    # Check/screenshot (no history)
    p = ui_sub.add_parser('screenshot', help='Save screenshot to local file')
    p.add_argument('path', help='Local save path')
    _add_device_arg(p)
    p.set_defaults(func=cmd_ui_screenshot)

    p = ui_sub.add_parser(
        'snapshot',
        help='页面快照：一次性保存当前页面截图(PNG) + 控件树(JSON)',
        description='取证用页面快照：指定输出基础路径，自动生成两个文件——'
                    '<path>.png（当前屏幕截图）与 <path>.json（当前页面控件树）。'
                    '例如 "%(prog)s /tmp/page" 会生成 /tmp/page.png 和 /tmp/page.json。')
    p.add_argument('path', help='输出基础路径（自动补 .png 与 .json），例如 /tmp/page')
    _add_device_arg(p)
    p.set_defaults(func=cmd_ui_snapshot)

    p = ui_sub.add_parser('check-dialog', help='Check if dialog exists')
    p.add_argument('type', nargs='?', const='Dialog', default='Dialog',
                   help='Dialog type (default Dialog)')
    _add_device_arg(p)
    p.set_defaults(func=cmd_ui_check_dialog)

    p = ui_sub.add_parser('toggle-state',
                          help='Read/assert Toggle state (by --x/--y or --index)')
    p.add_argument('--x', type=float, default=None,
                   help='X coordinate of the toggle (px, or 0-1 with --pct)')
    p.add_argument('--y', type=float, default=None,
                   help='Y coordinate of the toggle (px, or 0-1 with --pct)')
    p.add_argument('--pct', action='store_true',
                   help='Interpret --x/--y as 0-1 screen ratios')
    p.add_argument('--index', type=int, default=None,
                   help='Nth toggle on page (0-based); omit all to list every toggle')
    p.add_argument('--expect', default=None,
                   help='Assert state: true/false (omit to just read)')
    _add_device_arg(p)
    p.set_defaults(func=cmd_ui_toggle_state)
    p = ui_sub.add_parser('picker-set',
                          help='Scroll a wheel/picker column to target value')
    p.add_argument('--column-x', type=int, required=True,
                   help='Horizontal center X of the picker column')
    p.add_argument('--value', required=True, help='Target value (e.g. 16)')
    p.add_argument('--max-iter', type=int, default=10,
                   help='Max dump/scroll iterations (default 10)')
    _add_device_arg(p)
    p.set_defaults(func=cmd_ui_picker_set)
    p = ui_sub.add_parser('check-exist', help='Check if widget exists')
    p.add_argument('--text', default=None, help='Match by text (contains)')
    p.add_argument('--id', default=None, help='Match by key/id')
    p.add_argument('--type', default=None, help='Match by type')
    _add_device_arg(p)
    p.set_defaults(func=cmd_ui_check_exist)

    p = ui_sub.add_parser('find', help='Find widget and print info')
    p.add_argument('--text', default=None, help='Match by text (contains)')
    p.add_argument('--id', default=None, help='Match by key/id')
    p.add_argument('--type', default=None, help='Match by type')
    _add_device_arg(p)
    p.set_defaults(func=cmd_ui_find)

    p = ui_sub.add_parser('scroll-find', help='Smart scroll to find widget')
    p.add_argument('text', help='Target text (contains match)')
    p.add_argument('--swipes', type=int, default=8, help='Max scroll pages (default 8)')
    _add_device_arg(p)
    p.set_defaults(func=cmd_ui_scroll_find)

    p = ui_sub.add_parser('collect', help='滚动收集去重计数（数列表项数类验证）')
    p.add_argument('--regex', required=True, help='文本匹配正则（text/hint）')
    p.add_argument('--swipes', type=int, default=6, help='最大滚动屏数（默认 6）')
    _add_device_arg(p)
    p.set_defaults(func=cmd_ui_collect)

    p = ui_sub.add_parser('dismiss-dialogs', help='Dismiss overlay dialogs')
    p.add_argument('--rounds', type=int, default=3, help='Max dismiss rounds (default 3)')
    p.add_argument('--wait', type=float, default=0.0,
                   help='Wait N s before first check (let delayed dialog appear)')
    p.add_argument('--grace', type=float, default=2.0,
                   help='After clear, wait N s & recheck delayed dialog (default 2.0)')
    p.add_argument('--back', type=int, default=1,
                   help='Max system-back fallback presses (default 1)')
    _add_device_arg(p)
    p.set_defaults(func=cmd_ui_dismiss_dialogs)

    p = ui_sub.add_parser('wait-for', help='Wait for text or Toggle state')
    p.add_argument('text', nargs='?', default=None, help='Target text (contains match)')
    p.add_argument('--gone', action='store_true', help='Wait for text to disappear')
    p.add_argument('--toggle-x', type=float, default=None,
                   help='Toggle X coordinate (enables toggle mode; px or 0-1 with --pct)')
    p.add_argument('--toggle-y', type=float, default=None,
                   help='Toggle Y coordinate (enables toggle mode; px or 0-1 with --pct)')
    p.add_argument('--pct', action='store_true',
                   help='Interpret --toggle-x/--toggle-y as 0-1 screen ratios')
    p.add_argument('--checked', default=None,
                   help='Expected toggle state: true/false (toggle mode)')
    p.add_argument('--timeout', type=float, default=10, help='Timeout seconds (default 10)')
    p.add_argument('--interval', type=float, default=1, help='Poll interval seconds (default 1)')
    _add_device_arg(p)
    p.set_defaults(func=cmd_ui_wait_for)

    # ---------- log ----------
    log = sub.add_parser('log', help='hilog / fault log grab (anti-hang)')
    log_sub = log.add_subparsers(dest='subcommand', metavar='<action>', required=True)
    p = log_sub.add_parser(
        'grab',
        help='Grab hilog buffer logs (device-side file + fetch; anti-hang)')
    p.add_argument('--pid', type=int, default=None, help='Filter by PID')
    p.add_argument('--bundle', default=None,
                   help='Resolve PID by bundle name '
                        '(default: $HMUITEST_BUNDLE or %s)' % default_bundle())
    p.add_argument('--tag', default=None, help='Tag filter (comma separated, max 10)')
    p.add_argument('--level', default=None, help='Level filter: D/I/W/E/F (comma ok)')
    p.add_argument('--type', dest='log_type', default=None,
                   help='Log type: app/core/init (comma ok)')
    p.add_argument('--regex', default=None, help='Device-side content regex (-e)')
    p.add_argument('--filter', default=None, help='Local regex filter (post-fetch)')
    p.add_argument('--tail', type=int, default=None, help='Only last N lines (device -z)')
    p.add_argument('--head', type=int, default=None, help='Only first N lines (device -a)')
    p.add_argument('--show', type=int, default=60,
                   help='Show last N lines on stdout when no -o (default 60)')
    p.add_argument('-o', '--output', default=None, help='Save all lines to local file')
    p.add_argument('--timeout', type=int, default=25, help='hdc timeout seconds (default 25)')
    p.add_argument('--fault', action='store_true',
                   help='Grab fault log instead (hidumper -e --print)')
    p.add_argument('--list-fault', action='store_true',
                   help='List fault records instead (hidumper -e --list)')
    p.add_argument('--fault-n', type=int, default=1, help='Fault records count (default 1)')
    _add_device_arg(p)
    p.set_defaults(func=cmd_log_grab)

    # ---------- tree ----------
    tree = sub.add_parser('tree', help='Widget tree analysis')
    tree_sub = tree.add_subparsers(dest='subcommand', metavar='<action>', required=True)

    p = tree_sub.add_parser('show', help='Analyze local widget tree JSON file')
    p.add_argument('file', help='Widget tree JSON file path')
    _add_analysis_args(p)
    p.set_defaults(func=cmd_tree_show)

    p = tree_sub.add_parser('dump', help='Dump widget tree from device and analyze')
    _add_device_arg(p)
    _add_analysis_args(p)
    p.set_defaults(func=cmd_tree_dump)

    p = tree_sub.add_parser('diff', help='Compare two widget tree files')
    p.add_argument('file1', help='Before widget tree file')
    p.add_argument('file2', help='After widget tree file')
    p.add_argument('--operation', default="", help='Operation description')
    _add_device_arg(p)
    p.set_defaults(func=cmd_tree_diff)

    p = tree_sub.add_parser('auto', help='Auto-diff: dump and compare with last baseline')
    p.add_argument('--operation', default="", help='Operation description')
    p.add_argument('--history-dir', default=None,
                   help='Base dir for history baseline; actual files go to a per-device '
                        'subdir (default $TMPDIR/hmuitest-history/<device>; '
                        'override base with env HMUITEST_HISTORY_DIR)')
    _add_device_arg(p)
    p.set_defaults(func=cmd_tree_auto)

    # ---------- script ----------
    script = sub.add_parser('script', help='Batch script execution')
    script_sub = script.add_subparsers(dest='subcommand', metavar='<action>', required=True)

    p = script_sub.add_parser('run', help='Run test script file')
    p.add_argument('file', help='Script JSON file path')
    p.add_argument('--history-dir', default=None,
                   help='Base dir for history baseline; actual files go to a per-device '
                        'subdir (default $TMPDIR/hmuitest-history/<device>; '
                        'override base with env HMUITEST_HISTORY_DIR)')
    _add_device_arg(p)
    p.set_defaults(func=cmd_script_run)

    p = script_sub.add_parser('record', help='Record UI actions as reusable script')
    p.add_argument('record_action', choices=['start', 'stop', 'status'],
                   help='start: begin recording, stop: save & stop, status: check')
    p.add_argument('--output', '-o', default=None,
                   help='Output script file (default: recorded_script.json)')
    p.set_defaults(func=cmd_script_record)

    # ---------- device ----------
    device = sub.add_parser('device', help='Device claims across sessions (parallel agents)')
    device_sub = device.add_subparsers(dest='subcommand', metavar='<action>', required=True)

    p = device_sub.add_parser('status', help='List device claims (which session holds which device)')
    p.set_defaults(func=cmd_device_status)

    p = device_sub.add_parser('release', help='Release device claims')
    p.add_argument('--device', '-d', default=None, help='Only release this device')
    p.add_argument('--session', default=None, help='Only release claims of this session')
    p.add_argument('--stale', action='store_true',
                   help='Only release claims whose heartbeat has expired')
    p.add_argument('--all', dest='release_all', action='store_true',
                   help='Release every claim regardless of session')
    p.set_defaults(func=cmd_device_release)

    return parser


def main():
    try:
        from engines.logger import init_logger
        _LOG_FILE = init_logger("autoharmony")
    except Exception:
        _LOG_FILE = None
    parser = build_parser()
    # 让 --json 可放在子命令前或后。tree 例外：其 --json 另有「搜索结果
    # 数组」语义（tree show/dump 的子命令参数），保持原样。
    argv = sys.argv[1:]
    # 顶层开关（--no-artifacts/--device-takeover/--no-device-lock）允许写在任意
    # 位置（含子命令之后）：统一提到子命令之前，避免 argparse 报 unrecognized arguments。
    _anywhere_flags = ('--no-artifacts', '--device-takeover', '--no-device-lock', '--fast')
    _hoisted = [a for a in argv if a in _anywhere_flags]
    if _hoisted:
        argv = _hoisted + [a for a in argv if a not in _anywhere_flags]
    json_anywhere = False
    if '--json' in argv:
        _commands = {'aa', 'ui', 'tree', 'script', 'device', 'app', 'log'}
        _cmd = next((a for a in argv if a in _commands), '')
        if _cmd != 'tree':
            argv = [a for a in argv if a != '--json']
            json_anywhere = True
    args = parser.parse_args(argv)
    if json_anywhere:
        args.json_output = True
    # json 模式：人类输出转 stderr，JSON 独占 stdout（_JSON_STDOUT 留原句柄）
    global _JSON_STDOUT
    _JSON_STDOUT = sys.stdout
    if getattr(args, 'json_output', False):
        sys.stdout = sys.stderr

    if getattr(args, 'no_artifacts', False):
        try:
            from engines.artifacts import disable as _disable_artifacts
            _disable_artifacts()
        except Exception:
            pass

    if getattr(args, 'fast', False):
        os.environ['HMUITEST_FAST'] = '1'

    try:
        from utils.device_lock import configure as _configure_device_lock
        _configure_device_lock(
            takeover=True if getattr(args, 'device_takeover', False) else None,
            wait_s=getattr(args, 'device_wait', None),
            enabled=False if getattr(args, 'no_device_lock', False) else None)
    except Exception:
        pass

    # 统一解析目标设备并在会话内登记占用。引擎多靠 self.device 直接工作（未必
    # 走 detect_device_id），因此这里解析后回注 args.device，既保证确定性指向，
    # 又保证占用锁生效。`device` 子命令自身不参与。
    if getattr(args, 'command', None) != 'device' and hasattr(args, 'device'):
        try:
            from utils.hdc import claim_device, detect_device_raw
            json_mode = getattr(args, 'json_output', False)
            target = args.device or detect_device_raw()
            if target:
                blocked = claim_device(target, quiet=json_mode)
                if blocked is not None:
                    if json_mode:
                        verdict = _make_verdict(
                            "DEVICE_IN_USE",
                            f"device {target} held by session {blocked.owner_session}",
                            device=target,
                            owner_session=blocked.owner_session,
                            owner_pid=blocked.owner_pid,
                            suggestion="retry with --device-wait or --device-takeover")
                        _write_json(_attach_log_path(verdict))
                    sys.exit(1)
                args.device = target
        except SystemExit:
            raise
        except Exception:
            pass

    if hasattr(args, 'func'):
        try:
            args.func(args)
        except (ConnectionError, BrokenPipeError, TimeoutError) as ex:
            # TcpBridge 不可达（进程消失/端口无响应）：结构化裁决兜底，
            # 不再抛 Python Traceback（违反工具自身恢复协议）
            _bridge_unreachable_exit(args, ex)
        except SystemExit:
            raise
        except Exception as ex:  # 顶层兜底：任何未预期异常 → 结构化 ERROR 裁决
            from bridge.tcp_bridge import JSONRPCError
            if isinstance(ex, JSONRPCError):
                _bridge_unreachable_exit(args, ex)
            else:
                _unexpected_error_exit(args, ex)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == '__main__':
    main()
