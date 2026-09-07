#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""hmuitest — HarmonyOS UI Automation Testing CLI

Subcommands (mutually exclusive):
  aa       Deep Link explicit launch (aa start)
  ui       UI actions & assertions (with auto diff report)
  tree     Widget tree analysis (local file / remote dump / diff)
  script   Batch script execution (single-process driver reuse)

Exit code: 0 success, 1 failure (CI / agent friendly).
"""

import argparse
import json
import os
import sys

SKILL_DIR = os.path.dirname(os.path.abspath(__file__))
if SKILL_DIR not in sys.path:
    sys.path.insert(0, SKILL_DIR)

RECORD_FILE = os.path.join(SKILL_DIR, ".hmuitest_recording.json")


# ==================== JSON Output ====================

def _emit_json(result: dict, args=None):
    """Emit structured JSON output and exit."""
    if args and getattr(args, 'json_output', False):
        print(json.dumps(result, ensure_ascii=False, indent=2))
        sys.exit(result.get("exit", 0 if result.get("status") == "SUCCESS" else 1))
    return result


def _make_verdict(status: str, reason: str, **extra) -> dict:
    """Build a standard verdict dict."""
    d = {"status": status, "reason": reason, "exit": 0 if status == "SUCCESS" else 1}
    d.update(extra)
    return d


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
    p.add_argument('--fresh-before', action='store_true',
                   help='Force fresh baseline dump before action')
    p.add_argument('--no-recover', action='store_true',
                   help='Disable auto-retry when blocked by dialog')


def _close_engine(engine):
    """Close engine (release HypiumEngine driver connection)"""
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
        auto_handle_dialogs(args.device)

    pass_expect = None if auto_dialog else expectations
    ok = action_fn(pass_expect)

    # Record step if recording
    if ok:
        _record_step(getattr(args, '_record_cmd', ''), getattr(args, '_record_args', []))

    if not ok:
        verdict = _make_verdict("FAILED", "Action failed")
        _emit_json(verdict, args)
        return False

    if auto_dialog:
        auto_handle_dialogs(args.device)
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
    from engines.engines import HypiumEngine
    engine = HypiumEngine(device=args.device,
                          history_dir=getattr(args, 'history_dir', None))
    params = json.loads(args.params) if args.params else None

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


# ==================== ui subcommands (semantic, HypiumEngine) ====================

def _hypium_engine(args):
    from engines.engines import HypiumEngine
    return HypiumEngine(device=args.device,
                        history_dir=getattr(args, 'history_dir', None))


def cmd_ui_click_by_text(args):
    engine = _hypium_engine(args)
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
    engine = _hypium_engine(args)

    def action(expectations):
        return engine.click_by_id(args.key, args.operation, expectations=expectations,
                                  fresh_before=args.fresh_before,
                                  auto_recover=not args.no_recover)

    ok = _run_ui_action(args, engine, action)
    _close_engine(engine)
    sys.exit(0 if ok else 1)


def cmd_ui_click_by_type(args):
    engine = _hypium_engine(args)

    def action(expectations):
        return engine.click_by_type(args.type, args.operation, expectations=expectations,
                                    fresh_before=args.fresh_before,
                                    auto_recover=not args.no_recover)

    ok = _run_ui_action(args, engine, action)
    _close_engine(engine)
    sys.exit(0 if ok else 1)


def cmd_ui_double_click_by_text(args):
    engine = _hypium_engine(args)

    def action(expectations):
        return engine.double_click_by_text(args.text, args.operation, expectations=expectations,
                                           fresh_before=args.fresh_before,
                                           auto_recover=not args.no_recover)

    ok = _run_ui_action(args, engine, action)
    _close_engine(engine)
    sys.exit(0 if ok else 1)


def cmd_ui_long_click_by_text(args):
    engine = _hypium_engine(args)

    def action(expectations):
        return engine.long_click_by_text(args.text, args.operation, expectations=expectations,
                                         fresh_before=args.fresh_before,
                                         auto_recover=not args.no_recover)

    ok = _run_ui_action(args, engine, action)
    _close_engine(engine)
    sys.exit(0 if ok else 1)


def cmd_ui_input_by_text(args):
    engine = _hypium_engine(args)

    def action(expectations):
        return engine.input_by_text(args.target, args.text, args.operation,
                                     expectations=expectations,
                                     fresh_before=args.fresh_before,
                                     auto_recover=not args.no_recover)

    ok = _run_ui_action(args, engine, action)
    _close_engine(engine)
    sys.exit(0 if ok else 1)


def cmd_ui_input_by_type(args):
    engine = _hypium_engine(args)

    def action(expectations):
        return engine.input_by_type(args.type, args.text, args.operation,
                                     expectations=expectations,
                                     fresh_before=args.fresh_before,
                                     auto_recover=not args.no_recover)

    ok = _run_ui_action(args, engine, action)
    _close_engine(engine)
    sys.exit(0 if ok else 1)


def cmd_ui_swipe_direction(args):
    engine = _hypium_engine(args)

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
    engine = _hypium_engine(args)
    ok = engine.screenshot(args.path)
    _close_engine(engine)
    sys.exit(0 if ok else 1)


def cmd_ui_check_dialog(args):
    engine = _hypium_engine(args)
    ok = engine.check_dialog(args.type)
    _close_engine(engine)
    sys.exit(0 if ok else 1)


def cmd_ui_check_exist(args):
    engine = _hypium_engine(args)
    ok = engine.check_component(text=args.text, key=args.id, widget_type=args.type)
    _close_engine(engine)
    sys.exit(0 if ok else 1)


def cmd_ui_find(args):
    engine = _hypium_engine(args)
    ok = engine.find_and_print(text=args.text, key=args.id, widget_type=args.type)
    _close_engine(engine)
    sys.exit(0 if ok else 1)


def cmd_ui_scroll_find(args):
    engine = _hypium_engine(args)
    args._record_cmd = "ui scroll-find"
    args._record_args = [args.text]
    ok = engine.scroll_find(args.text, max_swipes=args.swipes)
    verdict = _make_verdict("SUCCESS" if ok else "FAILED",
                            f"Found '{args.text}'" if ok else f"'{args.text}' not found after {args.swipes} swipes")
    _emit_json(verdict, args)
    _close_engine(engine)
    sys.exit(0 if ok else 1)


def cmd_ui_dismiss_dialogs(args):
    """Standalone dialog dismiss: click known confirm buttons to close overlays"""
    from engines.engines import auto_handle_dialogs
    handled = auto_handle_dialogs(args.device, max_rounds=args.rounds)
    if handled:
        print("ACTION_VERDICT: SUCCESS | reason=dialogs dismissed")
        sys.exit(0)
    from engines.verdict import find_overlays
    from analyzers.widget_tree import WidgetTreeAnalyzer
    analyzer = WidgetTreeAnalyzer(json_file="dismiss_check.json", device=args.device)
    success, msg = analyzer.dump_layout()
    if not success:
        print(f"❌ Failed to get widget tree: {msg}")
        sys.exit(1)
    if not analyzer.load_tree():
        analyzer.cleanup()
        sys.exit(1)
    overlays = find_overlays(analyzer.widgets, analyzer.get_screen_bounds())
    analyzer.cleanup()
    if overlays:
        print(f"❌ Unrecognized overlay dialog (no known confirm button): "
              f"{overlays[-1].get('type', '')}")
        print("ACTION_VERDICT: BLOCKED_BY_DIALOG | reason=unknown overlay | "
              "suggestion=use 'tree dump' to inspect, then click manually")
        sys.exit(1)
    print("ℹ️  No dialogs to dismiss")
    print("ACTION_VERDICT: SUCCESS | reason=no dialogs")
    sys.exit(0)


def cmd_ui_wait_for(args):
    """Wait for text to appear/disappear (standalone, not bound to action)"""
    import time
    from analyzers.widget_tree import WidgetTreeAnalyzer

    deadline = time.time() + args.timeout
    interval = max(args.interval, 0.5)
    while True:
        analyzer = WidgetTreeAnalyzer(json_file="wait_for.json", device=args.device)
        success, _msg = analyzer.dump_layout()
        found = False
        if success and analyzer.load_tree():
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
            sys.exit(1)
        time.sleep(interval)


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
        sys.exit(1)
    analyzer = WidgetTreeAnalyzer(args.file)
    if not analyzer.load_tree():
        sys.exit(1)
    mode, param = _analysis_mode_param(args)
    rw = args.rw if args.rw else 120
    analyzer._dispatch_analysis(mode, param, rw, args.rh, as_json=args.json)


def cmd_tree_dump(args):
    from analyzers.widget_tree import WidgetTreeAnalyzer
    analyzer = WidgetTreeAnalyzer(json_file="remote_dump.json", device=args.device)
    mode, param = _analysis_mode_param(args)
    rw = args.rw if args.rw else 120
    success = analyzer.dump_and_analyze(mode=mode, search_param=param,
                                         rw=rw, rh=args.rh, as_json=args.json)
    if not success:
        sys.exit(1)


def cmd_tree_diff(args):
    from analyzers.widget_tree import WidgetTreeAnalyzer
    from engines.diff_engine import WidgetTreeDiff
    for f in (args.file1, args.file2):
        if not os.path.exists(f):
            print(f"❌ File not found: {f}")
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
    from analyzers.widget_tree import WidgetTreeAnalyzer
    from engines.diff_engine import WidgetTreeDiff, AutoDiffManager
    manager = AutoDiffManager(args.history_dir)
    previous_widgets = manager.load_history()

    analyzer = WidgetTreeAnalyzer(json_file="auto_diff.json", device=args.device)
    success, msg = analyzer.dump_layout()
    if not success:
        print(f"❌ Failed to get widget tree: {msg}")
        sys.exit(1)
    print("✅ Widget tree captured")
    print(f"📁 Temp file: {analyzer.json_file}")
    print()

    if not analyzer.load_tree():
        analyzer.cleanup()
        sys.exit(1)

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


# ==================== script subcommand ====================

def cmd_script_run(args):
    from engines.engines import BatchRunner
    if not os.path.exists(args.file):
        verdict = _make_verdict("FAILED", f"Script file not found: {args.file}")
        _emit_json(verdict, args)
        print(f"❌ Script file not found: {args.file}")
        sys.exit(1)
    with open(args.file, 'r', encoding='utf-8') as f:
        script = json.load(f)
    runner = BatchRunner(device=args.device, history_dir=args.history_dir)
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


# ==================== parser ====================

def build_parser():
    parser = argparse.ArgumentParser(
        prog='hmuitest',
        description='HarmonyOS UI Automation Testing CLI: '
                    'UI actions & assertions, widget tree analysis, batch scripts.')
    parser.add_argument('--json', dest='json_output', action='store_true',
                        help='Output structured JSON (for AI agents)')
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
                   help='Diff history directory (default: system temp)')
    _add_expect_args(p)
    _add_device_arg(p)
    p.set_defaults(func=cmd_aa_start)

    # ---------- ui ----------
    ui = sub.add_parser('ui', help='UI actions & assertions (auto diff report)')
    ui_sub = ui.add_subparsers(dest='subcommand', metavar='<action>', required=True)

    def add_history(p):
        p.add_argument('--history-dir', default=None,
                       help='Diff history directory (default: system temp)')

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

    # Semantic (HypiumEngine)
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
                   help='History directory (default: system temp)')
    _add_device_arg(p)
    p.set_defaults(func=cmd_tree_auto)

    # ---------- script ----------
    script = sub.add_parser('script', help='Batch script execution')
    script_sub = script.add_subparsers(dest='subcommand', metavar='<action>', required=True)

    p = script_sub.add_parser('run', help='Run test script file')
    p.add_argument('file', help='Script JSON file path')
    p.add_argument('--history-dir', default=None,
                   help='History directory (default: system temp)')
    _add_device_arg(p)
    p.set_defaults(func=cmd_script_run)

    p = script_sub.add_parser('record', help='Record UI actions as reusable script')
    p.add_argument('record_action', choices=['start', 'stop', 'status'],
                   help='start: begin recording, stop: save & stop, status: check')
    p.add_argument('--output', '-o', default=None,
                   help='Output script file (default: recorded_script.json)')
    p.set_defaults(func=cmd_script_record)

    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()

    if hasattr(args, 'func'):
        args.func(args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == '__main__':
    main()
