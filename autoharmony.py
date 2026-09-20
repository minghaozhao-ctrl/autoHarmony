#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""autoharmony — HarmonyOS UI Automation Testing CLI

Subcommands (mutually exclusive):
  aa       Deep Link explicit launch (aa start)
  ui       UI actions & assertions (with auto diff report)
  tree     Widget tree analysis (local file / remote dump / diff)
  script   Batch script execution (single-process driver reuse)

Exit codes: 0 success, 1 failure (rule/assertion/action), 2 usage error
(bad args, e.g. unknown command or misplaced flag) — CI / agent friendly.
"""

import argparse
import json
import os
import sys

# 约定的退出码（对 Agent/CI 稳定）：
#   0 成功；1 动作/断言/裁决失败；2 命令行用法错误（argparse 默认）。
EXIT_OK = 0
EXIT_FAIL = 1
EXIT_USAGE = 2

SKILL_DIR = os.path.dirname(os.path.abspath(__file__))
if SKILL_DIR not in sys.path:
    sys.path.insert(0, SKILL_DIR)

RECORD_FILE = os.path.join(SKILL_DIR, ".autoharmony_recording.json")


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
    if not os.path.exists(RECORD_FILE):
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
    """Device ID detection (explicit arg → env var → hdc list targets)"""
    from utils.hdc import detect_device_id
    device_id = explicit or detect_device_id()
    if not device_id:
        sys.exit(1)
    return device_id


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
                   help='Assertion polling timeout seconds (default 0 = no polling)')
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


def _run_ui_action(args, engine, action_fn):
    """Unified UI action flow: optional pre-dialog clear → action → optional post-dialog clear → assertion

    With auto-handle-dialog, assertion is deferred until after dialog clearing.
    """
    from engines.engines import auto_handle_dialogs, check_expectations_with_polling

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


# ==================== aa subcommand ====================

def cmd_aa_start(args):
    from engines.engines import SemanticEngine
    engine = SemanticEngine(device=args.device,
                          history_dir=getattr(args, 'history_dir', None))
    params = json.loads(args.params) if args.params else None
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
    from engines.engines import HdcUITestEngine
    return HdcUITestEngine(device=args.device,
                           history_dir=getattr(args, 'history_dir', None))


def cmd_ui_click(args):
    engine = _hdc_engine(args)
    args._record_cmd = "ui click"
    args._record_args = [str(args.x), str(args.y)]

    def action(expectations):
        return engine.click(args.x, args.y, args.operation, expectations=expectations,
                            fresh_before=args.fresh_before,
                            auto_recover=not args.no_recover)

    ok = _run_ui_action(args, engine, action)
    sys.exit(0 if ok else 1)


def cmd_ui_double_click(args):
    engine = _hdc_engine(args)

    def action(expectations):
        return engine.double_click(args.x, args.y, args.operation, expectations=expectations,
                                   fresh_before=args.fresh_before,
                                   auto_recover=not args.no_recover)

    ok = _run_ui_action(args, engine, action)
    sys.exit(0 if ok else 1)


def cmd_ui_long_click(args):
    engine = _hdc_engine(args)

    def action(expectations):
        return engine.long_click(args.x, args.y, args.operation, expectations=expectations,
                                 fresh_before=args.fresh_before,
                                 auto_recover=not args.no_recover)

    ok = _run_ui_action(args, engine, action)
    sys.exit(0 if ok else 1)


def cmd_ui_swipe(args):
    engine = _hdc_engine(args)
    args._record_cmd = "ui swipe"
    args._record_args = [str(args.x1), str(args.y1), str(args.x2), str(args.y2)]

    def action(expectations):
        return engine.swipe(args.x1, args.y1, args.x2, args.y2, args.operation,
                             expectations=expectations,
                             fresh_before=args.fresh_before,
                             auto_recover=not args.no_recover)

    ok = _run_ui_action(args, engine, action)
    sys.exit(0 if ok else 1)


def cmd_ui_input(args):
    engine = _hdc_engine(args)
    args._record_cmd = "ui input"
    args._record_args = [args.text]

    def action(expectations):
        return engine.text_input(args.text, args.operation, expectations=expectations,
                                 fresh_before=args.fresh_before,
                                 auto_recover=not args.no_recover)

    ok = _run_ui_action(args, engine, action)
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
    from engines.engines import SemanticEngine
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
                                    auto_recover=not args.no_recover)

    ok = _run_ui_action(args, engine, action)
    _close_engine(engine)
    sys.exit(0 if ok else 1)


def cmd_ui_click_by_id(args):
    engine = _semantic_engine(args)

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

def cmd_ui_screenshot(args):
    engine = _semantic_engine(args)
    ok = engine.screenshot(args.path)
    _close_engine(engine)
    sys.exit(0 if ok else 1)


def cmd_ui_check_dialog(args):
    engine = _semantic_engine(args)
    ok = engine.check_dialog(args.type)
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
    args._record_cmd = "ui scroll-find"
    args._record_args = [args.text]
    ok = engine.scroll_find(args.text, max_swipes=args.swipes)
    if not ok:
        _capture_cmd_failure(args.device, "scroll_find not found: %s" % args.text)
    verdict = _make_verdict("SUCCESS" if ok else "FAILED",
                            f"Found '{args.text}'" if ok else f"'{args.text}' not found after {args.swipes} swipes")
    _emit_json(verdict, args)
    _close_engine(engine)
    sys.exit(0 if ok else 1)


def cmd_ui_dismiss_dialogs(args):
    """Standalone dialog dismiss: click known confirm buttons to close overlays"""
    from engines.engines import auto_handle_dialogs, HdcUITestEngine
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
    """Wait for text to appear/disappear (standalone, not bound to action)"""
    import time

    deadline = time.time() + args.timeout
    interval = max(args.interval, 0.5)
    # Use engine hdc fast-path dump (was WidgetTreeAnalyzer.dump_layout -a slow path, ~5x)
    engine = _hdc_engine(args)
    try:
        while True:
            analyzer = engine._dump_and_load("wait_for")
            found = False
            if analyzer is not None:
                matched = [w for w in analyzer.widgets
                           if args.text.lower() in (w.get('text', '') or '').lower()
                           or args.text.lower() in (w.get('hint', '') or '').lower()]
                found = bool(matched)
                analyzer.cleanup()

            if args.gone:
                if not found:
                    print(f"✅ Text '{args.text}' has disappeared")
                    print("ACTION_VERDICT: SUCCESS | reason=target text disappeared")
                    sys.exit(0)
            else:
                if found:
                    print(f"✅ Text '{args.text}' has appeared")
                    print("ACTION_VERDICT: SUCCESS | reason=target text appeared")
                    sys.exit(0)

            if time.time() >= deadline:
                state = "still not present" if not args.gone else "still present"
                print(f"❌ Wait timeout ({args.timeout}s): text '{args.text}' {state}")
                print(f"ACTION_VERDICT: NO_CHANGE | reason=waited {args.timeout}s timeout | "
                      "suggestion=check if page is loading or text is correct")
                _capture_cmd_failure(
                    args.device,
                    "wait_for timeout (%ss): %s" % (args.timeout, args.text))
                sys.exit(1)
            time.sleep(interval)
    finally:
        _close_engine(engine)


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
    return 'overview', None


def _add_analysis_args(p):
    """Widget tree analysis options (shared by tree show / tree dump)"""
    p.add_argument('--overview', '-o', action='store_true', help='Show overview (default)')
    p.add_argument('--type', '-t', help='Search widgets by type')
    p.add_argument('--text', '-T', help='Search widgets by text (contains match)')
    p.add_argument('--id', '-i', help='Search widgets by ID')
    p.add_argument('--clickable', '-c', action='store_true', help='Search clickable widgets')
    p.add_argument('--input', '-I', action='store_true', help='Search input widgets')
    p.add_argument('--list-types', '-l', action='store_true', help='List all widget types')
    p.add_argument('--detail', '-D', type=int, help='Show detail for widget at index')
    p.add_argument('--json', action='store_true',
                   help='Output search results as JSON array')
    p.add_argument('--rw', type=int, default=None,
                   help='Overview grid width (chars, default auto-fill)')
    p.add_argument('--rh', type=int, default=110,
                   help='Overview grid height limit (lines, default 110)')


def cmd_tree_show(args):
    from analyzers.widget_tree import WidgetTreeAnalyzer
    if not os.path.exists(args.file):
        print(f"❌ File not found: {args.file}")
        print(f"ACTION_VERDICT: ERROR | reason=file_not_found: {args.file}")
        sys.exit(1)
    analyzer = WidgetTreeAnalyzer(args.file)
    if not analyzer.load_tree():
        sys.exit(1)
    mode, param = _analysis_mode_param(args)
    rw = args.rw if args.rw else 120
    analyzer._dispatch_analysis(mode, param, rw, args.rh, as_json=args.json)


def cmd_tree_dump(args):
    # Use engine hdc fast-path dump (was dump_and_analyze -a slow path, ~5x)
    engine = _hdc_engine(args)
    try:
        analyzer = engine._dump_and_load("remote_dump")
        if analyzer is None:
            sys.exit(1)
        mode, param = _analysis_mode_param(args)
        rw = args.rw if args.rw else 120
        analyzer._dispatch_analysis(mode, param, rw, args.rh, as_json=args.json)
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
    from engines.engines import BatchRunner
    if not os.path.exists(args.file):
        verdict = _make_verdict("FAILED", f"Script file not found: {args.file}")
        _emit_json(verdict, args)
        print(f"❌ Script file not found: {args.file}")
        print(f"ACTION_VERDICT: ERROR | reason=script_file_not_found: {args.file}")
        sys.exit(1)
    with open(args.file, 'r', encoding='utf-8') as f:
        script = json.load(f)
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


# ==================== device subcommand ====================

def _device_lock_module():
    try:
        from utils import device_lock
    except ImportError:
        import device_lock  # skill 侧扁平导入
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
    sub = parser.add_subparsers(dest='command', metavar='<command>', required=True)

    # ---------- aa ----------
    aa = sub.add_parser('aa', help='Deep Link explicit launch (aa start)')
    aa_sub = aa.add_subparsers(dest='subcommand', metavar='<action>', required=True)

    p = aa_sub.add_parser('start', help='Launch app via Deep Link URI')
    p.add_argument('uri', help='Deep Link URI')
    p.add_argument('--bundle', required=True, help='bundleName (required)')
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
    p.add_argument('x', type=int, help='X coordinate')
    p.add_argument('y', type=int, help='Y coordinate')
    add_history(p)
    _add_expect_args(p)
    _add_device_arg(p)
    p.set_defaults(func=cmd_ui_click)

    p = ui_sub.add_parser('double-click', help='Double-click at coordinates')
    p.add_argument('x', type=int, help='X coordinate')
    p.add_argument('y', type=int, help='Y coordinate')
    add_history(p)
    _add_expect_args(p)
    _add_device_arg(p)
    p.set_defaults(func=cmd_ui_double_click)

    p = ui_sub.add_parser('long-click', help='Long-click at coordinates')
    p.add_argument('x', type=int, help='X coordinate')
    p.add_argument('y', type=int, help='Y coordinate')
    add_history(p)
    _add_expect_args(p)
    _add_device_arg(p)
    p.set_defaults(func=cmd_ui_long_click)

    p = ui_sub.add_parser('swipe', help='Swipe between coordinates')
    p.add_argument('x1', type=int, help='Start X')
    p.add_argument('y1', type=int, help='Start Y')
    p.add_argument('x2', type=int, help='End X')
    p.add_argument('y2', type=int, help='End Y')
    add_history(p)
    _add_expect_args(p)
    _add_device_arg(p)
    p.set_defaults(func=cmd_ui_swipe)

    p = ui_sub.add_parser('input', help='Input text to focused field')
    p.add_argument('text', help='Text to input')
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

    p = ui_sub.add_parser('check-dialog', help='Check if dialog exists')
    p.add_argument('type', nargs='?', const='Dialog', default='Dialog',
                   help='Dialog type (default Dialog)')
    _add_device_arg(p)
    p.set_defaults(func=cmd_ui_check_dialog)

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

    p = ui_sub.add_parser('wait-for', help='Wait for text to appear/disappear')
    p.add_argument('text', help='Target text (contains match)')
    p.add_argument('--gone', action='store_true', help='Wait for text to disappear')
    p.add_argument('--timeout', type=float, default=10, help='Timeout seconds (default 10)')
    p.add_argument('--interval', type=float, default=1, help='Poll interval seconds (default 1)')
    _add_device_arg(p)
    p.set_defaults(func=cmd_ui_wait_for)

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
    json_anywhere = False
    if '--json' in argv:
        _commands = {'aa', 'ui', 'tree', 'script', 'device'}
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
        args.func(args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == '__main__':
    main()
