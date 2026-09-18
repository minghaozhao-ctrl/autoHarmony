#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""UI 操作引擎

HdcUITestEngine: 基于 hdc shell uitest 的坐标操作（自动差异比较）
HypiumEngine: 基于 hypium BY 选择器的语义化操作（自动差异比较）
"""
import os
import sys
import signal
import time

# 抑制 xdevice 控制台输出（必须放在任何 hypium 导入之前）
sys.log_mode = "no_console"

import tempfile
from typing import List, Optional, Callable

from analyzers.widget_tree import WidgetTreeAnalyzer
from engines.diff_engine import WidgetTreeDiff, AutoDiffManager, ChangeReport
from analyzers.crash_detector import CrashDetector
from utils.common import run_hdc_command
from engines.verdict import ActionPipeline
from engines.logger import archive_dump


def _import_hypium():
    """Optional dependency guard: hypium powers semantic commands (click-by-text etc.)."""
    try:
        from hypium import BY, UiDriver
    except ImportError:
        print("❌ 缺少可选依赖 hypium：语义化命令需要它。")
        print("   安装: pip install autoharmony[semantic]   (或本仓库: pip install -r requirements.txt)")
        raise SystemExit(1)
    return BY, UiDriver


class _OperationTimeout(Exception):
    """操作超时异常"""


def _record_page_path(analyzer, device) -> None:
    """从 dump 的窗口节点属性提取 pagePath，登记到路由缓存（纯 hdc 零开销）。

    app 前台时 dumpLayout 的窗口根节点带 pagePath（如 pages/LaunchPage）；
    桌面/非 app 窗口为空。供 WidgetTreeDiff.get_current_route 回退使用。
    同时把本次 dump 的本地 JSON 归档进日志目录（复用产物，零额外 dump）。
    """
    try:
        archive_dump(getattr(analyzer, '_temp_file', None) or None, "layout")
    except Exception:
        pass
    try:
        for w in analyzer.widgets:
            pp = (w.get('attributes') or {}).get('pagePath')
            if pp:
                WidgetTreeDiff.record_page_path(device, pp)
                return
    except Exception:
        pass


def _guard_timeout(seconds: int):
    """给函数加整体超时兜底（SIGALRM，仅 Unix）"""
    def decorator(fn: Callable):
        def wrapper(*args, **kwargs):
            def handler(signum, frame):
                raise _OperationTimeout(f"操作超时 ({seconds}s)，已强制中断")
            old_handler = signal.signal(signal.SIGALRM, handler)
            signal.alarm(seconds)
            try:
                return fn(*args, **kwargs)
            except _OperationTimeout as e:
                print(f"❌ {e}")
                return False
            finally:
                signal.alarm(0)
                signal.signal(signal.SIGALRM, old_handler)
        return wrapper
    return decorator


# 可轮询重试的断言键（no_change 依赖操作瞬间的 diff 报告，不参与轮询）
POLLABLE_KEYS = {'route', 'text_exists', 'text_gone', 'dialog', 'state'}
# 需要控件树数据的断言键
WIDGET_KEYS = {'text_exists', 'text_gone', 'dialog', 'state'}


def check_expectations_with_polling(expectations: dict,
                                    report=None,
                                    initial_widgets: Optional[List[dict]] = None,
                                    dump_fn: Optional[Callable] = None,
                                    cleanup_fn: Optional[Callable] = None,
                                    device: Optional[str] = None) -> bool:
    """断言检查（支持失败后轮询重试）

    首轮用 initial_widgets + report 检查全部断言；失败且 expectations 含
    timeout>0 时，每隔 1s 重新获取页面状态、只复查可轮询断言（route/文本/弹窗），
    直到通过或超时。route-only 断言只查路由栈，不做控件树 dump。

    Args:
        expectations: 期望字典（route/text_exists/text_gone/no_change/dialog），
            可含 timeout 键（秒，默认 0 不轮询）
        report: 差异报告（no_change 断言依赖，轮询时不复查）
        initial_widgets: 首轮检查用的控件列表（route-only 断言可传 None）
        dump_fn: 重新获取控件树的无参函数，返回 analyzer（文本/弹窗轮询必需）
        cleanup_fn: 清理 dump_fn 产物（接收 analyzer 参数）
        device: 设备 ID（获取路由栈）

    Returns:
        是否全部通过
    """
    from assertions import AssertionChecker

    timeout = expectations.get('timeout', 0) or 0
    needs_widgets = any(k in expectations for k in WIDGET_KEYS)

    def _check(exp: dict, widgets: List[dict], cur_report) -> List:
        route = WidgetTreeDiff.get_current_route(device)
        checker = AssertionChecker(widgets, route=route, change_report=cur_report)
        return checker.check_all(exp)

    widgets = initial_widgets
    if widgets is None and needs_widgets:
        if dump_fn is None:
            print("❌ 文本/弹窗断言需要 dump_fn 支持轮询")
            return False
        analyzer = dump_fn()
        if analyzer is None:
            return False
        widgets = analyzer.widgets
        if cleanup_fn:
            cleanup_fn(analyzer)

    results = _check(expectations, widgets or [], report)
    if all(r.passed for r in results) or timeout <= 0:
        return AssertionChecker.print_results(results)

    pollable = {k: v for k, v in expectations.items() if k in POLLABLE_KEYS}
    if not pollable:
        print("ℹ️  断言不含可轮询项（仅 no_change），不重试")
        return AssertionChecker.print_results(results)

    print(f"⏳ 断言未通过，轮询等待（每 1s 复查，{timeout}s 超时）...")
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(min(1.0, max(deadline - time.time(), 0.1)))
        if needs_widgets:
            analyzer = dump_fn()
            if analyzer is None:
                continue
            cur_widgets = analyzer.widgets
            if cleanup_fn:
                cleanup_fn(analyzer)
        else:
            cur_widgets = []
        results = _check(pollable, cur_widgets, None)
        if all(r.passed for r in results):
            return AssertionChecker.print_results(results)
    return AssertionChecker.print_results(results)


# ==================== 当前页面状态摘要 ====================

# 需要反馈实际状态的控件类型（开关/勾选/单选/滑块）
STATE_TYPE_LABELS = {
    'toggle': '开关', 'switch': '开关', 'checkbox': '勾选', 'radio': '单选',
    'slider': '滑块',
}
INPUT_TYPE_SET = {'textinput', 'textarea', 'search', 'richeditor', 'textfield'}
# 页面内错误文案关键词（Text 控件命中即提示 AI 注意）
ERROR_TEXT_KEYWORDS = ('失败', '错误', '异常', '不正确', '超时', '无效', '请先')
# 白屏判定：控件总数低于该值疑似白屏/未渲染完成
WHITE_SCREEN_MIN_WIDGETS = 6


def _state_value_text(w: dict) -> str:
    """把控件状态属性转成可读文本（checked/selected/indeterminate 等）"""
    checked = w.get('checked', '')
    if checked in ('true', 'false'):
        return '开' if checked == 'true' else '关'
    selected = w.get('selected', '')
    if selected in ('true', 'false'):
        return '选中' if selected == 'true' else '未选中'
    return ''


def _nearest_label(widgets: List[dict], w: dict, max_dist: int = 200) -> str:
    """为控件找就近文本标签（左侧或上方相邻的 Text，距离最近者）

    用于把纯状态控件（如 Toggle 无文本）与页面上的文字标签关联起来。
    """
    b = WidgetTreeDiff._parse_bounds(w.get('bounds', ''))
    if not b:
        return ''
    cx, cy = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2
    best, best_d = '', None
    for t in widgets:
        if t.get('type', '').lower() != 'text':
            continue
        txt = (t.get('text', '') or '').strip()
        if not txt or len(txt) > 30:
            continue
        tb = WidgetTreeDiff._parse_bounds(t.get('bounds', ''))
        if not tb:
            continue
        t_cy = (tb[1] + tb[3]) / 2
        # 高度带必须与控制中心接近（上下可重叠，容忍 25px）
        if abs(t_cy - cy) > (max(b[3] - b[1], 40) / 2 + 25):
            continue
        # 必须是控件的左侧或上方邻接区域
        if not (tb[2] <= b[0] + 5 or tb[3] <= b[1] + 5):
            continue
        if tb[2] <= b[0] + 5:
            d = abs(b[0] - tb[2])
        else:
            d = abs(b[1] - tb[3])
        if d > max_dist:
            continue
        if best_d is None or d < best_d:
            best_d, best = d, txt
    return best


def _in_subtree(widgets: List[dict], idx: int, root_idx: int) -> bool:
    """判断 idx 节点是否在 root_idx 子树内（沿 parent_index 向上追溯）"""
    cur = widgets[idx].get('parent_index')
    guard = 0
    while cur is not None and guard < 60:
        if cur == root_idx:
            return True
        if not (0 <= cur < len(widgets)):
            return False
        cur = widgets[cur].get('parent_index')
        guard += 1
    return False


def _overlay_texts(widgets: List[dict], overlay_widget: dict,
                   max_items: int = 8) -> List[str]:
    """收集覆盖层子树内可读文本（弹窗标题/正文/按钮文字，去重保序）"""
    ov_idx = None
    for i, w in enumerate(widgets):
        if w is overlay_widget:
            ov_idx = i
            break
    if ov_idx is None:
        return []
    texts: List[str] = []
    for i, w in enumerate(widgets):
        if i == ov_idx:
            continue
        if not _in_subtree(widgets, i, ov_idx):
            continue
        t = (w.get('text') or '').strip()
        if t and len(t) <= 40 and t not in texts:
            texts.append(t)
            if len(texts) >= max_items:
                break
    return texts


def _compute_page_signature(widgets: List[dict]) -> tuple:
    """从 widget 列表提取页面签名：标题栏区域(y 100~320)的 Text 内容集合"""
    import re
    sig = []
    for w in widgets:
        if w.get('type') == 'Text':
            m = re.findall(r'\d+', str(w.get('bounds', '')))
            if len(m) >= 2 and 100 <= int(m[1]) < 320:
                sig.append(w.get('text', ''))
    return tuple(sorted(sig))


def _collect_state_items(widgets: List[dict], max_items: int = 10) -> List[tuple]:
    """收集状态控件项：[(key, line, value)]，key 用于增量对比"""
    items = []
    for w in widgets:
        wt = (w.get('type', '') or '').lower()
        if wt not in STATE_TYPE_LABELS:
            continue
        val = _state_value_text(w)
        if not val:
            continue
        label = _nearest_label(widgets, w)
        key = (wt, label or w.get('bounds', ''))
        name = f"{label} ({w.get('type')})" if label else w.get('type', wt)
        items.append((key, f"  🔘 {name}: {val}", val))
        if len(items) >= max_items:
            break
    return items


def _collect_input_items(widgets: List[dict], max_items: int = 10) -> List[tuple]:
    """收集输入框实际内容项：[(key, line, value)]，空框显示占位提示"""
    items = []
    for w in widgets:
        wt = (w.get('type', '') or '').lower()
        if wt not in INPUT_TYPE_SET:
            continue
        text = (w.get('text', '') or '').strip()
        hint = (w.get('hint', '') or '').strip()
        if not text and not hint:
            continue
        label = _nearest_label(widgets, w)
        label_str = f"「{label}」" if label else ""
        if text:
            val = text
        else:
            val = f"(空 — 占位提示: {hint})"
        key = (wt, label or hint or w.get('bounds', ''))
        items.append((key, f"  ⌨️ {w.get('type')}{label_str}: \"{val}\"", val))
        if len(items) >= max_items:
            break
    return items


def _delta_changed_lines(after_items: List[tuple],
                         before_items: List[tuple],
                         max_items: int) -> List[str]:
    """增量对比：只返回值变化的项（含新出现的项）"""
    before_map = {k: v for k, _, v in before_items}
    lines = []
    for key, line, val in after_items:
        if before_map.get(key) != val:
            lines.append(line)
            if len(lines) >= max_items:
                break
    return lines


def print_page_state_summary(analyzer, device: Optional[str] = None,
                             prev_widgets: Optional[List[dict]] = None,
                             route: Optional[List[str]] = None,
                             same_page: Optional[bool] = None,
                             max_items: int = 10) -> None:
    """打印操作后当前页面的紧凑状态摘要（自动调用，无需额外参数）

    让 AI 每步操作后掌握页面实况，而不只是"变了什么"：
    - 警告：白屏/加载中/被踢下线
    - 路由栈（页面跳转是否生效）
    - Toast 提示（保存成功/密码错误等瞬时反馈）
    - 弹窗/覆盖层（类型 + 标题/按钮文字）
    - 开关/勾选等状态控件的实际值
    - 输入框的实际内容（可发现"旧值未删干净导致新旧混合"）
    - 页面内错误文案

    分级输出：同页面小操作只打增量（delta），页面跳转打全量（full）。
    标签 CURRENT_PAGE_STATE 供 agent 程序化定位。
    """
    widgets = analyzer.widgets
    if same_page is None:
        if prev_widgets:
            same_page = (_compute_page_signature(prev_widgets)
                         == _compute_page_signature(widgets))
        else:
            same_page = False

    print("\n" + "─" * 60)
    print("📋 页面状态增量  (CURRENT_PAGE_STATE delta)"
          if same_page else "📋 当前页面状态  (CURRENT_PAGE_STATE)")
    print("─" * 60)

    # ── 警告（优先级最高，AI 需先排除异常再继续）──
    if len(widgets) < WHITE_SCREEN_MIN_WIDGETS:
        print("  ⚠️ 控件数异常少，页面疑似白屏或未渲染完成，禁止继续点击")
    if any('loadingprogress' in (w.get('type', '') or '').lower()
           for w in widgets):
        print("  ⏳ 检测到加载指示器，页面内容可能未就绪，重要判定请等加载完成")
    if route:
        top = (route[-1] or '').lower()
        if 'login' in top and len(route) > 1:
            print("  ⚠️ 当前处于登录页，会话可能已失效（被踢下线），后续操作会失败")

    # ── 路由（仅 full 模式，delta 时路由未变）──
    if route and not same_page:
        print(f"  📍 路由: {' → '.join(route[-3:])}")

    # ── Toast（瞬时反馈，delta/full 都打）──
    for t in widgets:
        if 'toast' in (t.get('type', '') or '').lower():
            txt = (t.get('text') or '').strip()
            if txt:
                print(f"  💬 Toast: \"{txt}\"")

    # ── 弹窗/覆盖层 ──
    try:
        screen_bounds = analyzer.get_screen_bounds()
    except Exception:
        screen_bounds = None
    overlays = [ov for ov in WidgetTreeDiff.detect_overlays(widgets, screen_bounds)
                if 'toast' not in (ov.get('type', '') or '').lower()]
    ov_lines = []
    for ov in overlays:
        texts = _overlay_texts(widgets, ov)
        line = f"  🪟 弹窗[{ov.get('type', '')}]"
        if texts:
            line += ": " + " / ".join(texts[:6])
        ov_lines.append(line)
    if ov_lines:
        for l in ov_lines[:3]:
            print(l)
    elif not same_page:
        print("  🪟 弹窗: 无")

    # ── 状态控件 / 输入框（delta 模式只打变化项）──
    after_states = _collect_state_items(widgets, max_items)
    after_inputs = _collect_input_items(widgets, max_items)
    if same_page and prev_widgets:
        changed = (_delta_changed_lines(
            after_states, _collect_state_items(prev_widgets, max_items * 2),
            max_items)
            + _delta_changed_lines(
                after_inputs, _collect_input_items(prev_widgets, max_items * 2),
                max_items))
        if changed:
            for l in changed:
                print(l)
        else:
            print("  🔘 状态控件/输入框: 无变化")
    else:
        if after_states:
            print(f"  ── 状态控件 ({len(after_states)}) ──")
            for _, l, _ in after_states:
                print(l)
        if after_inputs:
            print(f"  ── 输入框 ({len(after_inputs)}) ──")
            for _, l, _ in after_inputs:
                print(l)

    # ── 页面内错误文案 ──
    err_lines = []
    for w in widgets:
        if (w.get('type', '') or '') != 'Text':
            continue
        txt = (w.get('text') or '').strip()
        if txt and any(k in txt for k in ERROR_TEXT_KEYWORDS) and len(txt) <= 50:
            err_lines.append(f"  ⚠️ 错误文案: \"{txt}\"")
            if len(err_lines) >= 3:
                break
    for l in err_lines:
        print(l)

    print("─" * 60)


# 弹窗自动处理：常见关闭按钮（**负向/跳过优先**，正向仅作兜底）
# 单一事实来源：diff_engine.WidgetTreeDiff.DIALOG_BUTTON_TEXTS
AUTO_DIALOG_BUTTONS = list(WidgetTreeDiff.DIALOG_BUTTON_TEXTS)


def auto_handle_dialogs(device: Optional[str] = None, max_rounds: int = 3,
                        engine=None, wait: float = 0.0,
                        grace: float = 0.0, max_backs: int = 1) -> bool:
    """检测并自动关闭系统级弹窗（权限/云存/引导等覆盖层）。

    dump 控件树 → 找 Overlay 类型容器（Dialog/Sheet/Popup 等）→
    在弹窗后代中找常见确认按钮 → 坐标点击 → 循环直到无弹窗。
    支持**延迟弹出**：wait 先等 N 秒再开始检测；已清干净后再等 grace 秒
    补查一次，抓到“动作后才过一两秒才弹出来”的弹窗。
    支持**无法用按钮关闭时**：按系统返回键兜底（限 max_backs 次，避免把
    整个 App 退到桌面）。

    注意：只处理覆盖层弹窗，页面级隐私政策弹窗（非 overlay）不在此范围。

    Args:
        device: 设备 ID
        max_rounds: 最大处理轮数（防止无限弹窗）
        engine: 可选，复用已有引擎（走 daemon 快路径 dump，且复用其连接）；
                不传则自建（用后关闭）
        wait: 开始检测前先等待的秒数（等弹窗先弹出来）
        grace: 判断已清干净后再等待并补查一次，抓延迟弹出的弹窗（秒）
        max_backs: 未识别按钮时可按下系统返回键的最大次数（0=禁用）

    Returns:
        最终是否无覆盖层（True=已清干净，False=仍有弹窗/未识别按钮）
    """
    import time
    from engines.verdict import find_overlays, find_dismiss_button, top_overlay

    own_engine = engine is None
    if engine is None:
        engine = HdcUITestEngine(device=device)
    clicked_any = False
    backs = 0

    def _pass(_wait: float) -> bool:
        """一轮：先等 _wait 秒，再点掉已知弹窗直到无覆盖层或轮数用尽；
        返回是否最终无覆盖层（已校验）。"""
        nonlocal clicked_any, backs
        if _wait > 0:
            time.sleep(_wait)
        for _ in range(max_rounds):
            analyzer = engine._dump_and_load("dialog_poll")
            if analyzer is None:
                print("⚠️  弹窗检测跳过（获取控件树失败）")
                return False
            try:
                widgets = analyzer.widgets
                overlays = find_overlays(widgets, analyzer.get_screen_bounds())
                if not overlays:
                    return True
                btn = find_dismiss_button(widgets, top_overlay(overlays))
            finally:
                analyzer.cleanup()
            if btn is None:
                if backs < max_backs:
                    # 没有已知确认按钮 → 系统返回键兜底（限次，防退到桌面）
                    print("🔙 未识别弹窗按钮，按系统返回键兜底...")
                    engine._hdc_cmd(
                        ["shell", "uitest", "uiInput", "keyEvent", "Back"])
                    backs += 1
                    time.sleep(1.5)
                    continue
                # 有弹窗但没有认识的按钮，不盲目点击
                print(f"ℹ️  检测到弹窗但未识别已知按钮（overlay: "
                      f"{overlays[-1].get('type')}），跳过自动处理")
                return False

            import re as _re
            nums = _re.findall(r'\d+', btn.get('bounds', '') or '')
            if len(nums) < 4:
                return False
            x = (int(nums[0]) + int(nums[2])) // 2
            y = (int(nums[1]) + int(nums[3])) // 2
            label = btn.get('text', '')
            print(f"🔔 检测到弹窗，自动点击 '{label}' ({x}, {y})")
            if not engine.click(x, y, f"自动处理弹窗: 点击 '{label}'",
                                auto_recover=False):
                return False
            clicked_any = True
            time.sleep(1.5)
        return False

    cleared = False
    try:
        # 先等 wait：让即将弹出的弹窗先弹出来
        cleared = _pass(wait)
        # 延迟弹窗：已清干净时再等 grace 秒补查一次，抓到“动作后才弹出”的弹窗
        if cleared and grace > 0:
            time.sleep(grace)
            analyzer = engine._dump_and_load("dialog_grace")
            if analyzer is not None:
                try:
                    late = find_overlays(analyzer.widgets,
                                         analyzer.get_screen_bounds())
                finally:
                    analyzer.cleanup()
                if late:
                    print("⏳ 检测到延迟弹出的弹窗，继续自动处理...")
                    cleared = _pass(0.0)
    finally:
        if own_engine:
            engine.close()
    if not cleared:
        print("⚠️  仍有弹窗未关闭（自动处理未能清干净）")
    elif clicked_any:
        print("✅ 弹窗自动处理完成")
    return cleared


def _is_descendant_index(widgets: List[dict], idx: int, ancestor_idx: int) -> bool:
    """判断 idx 控件是否为 ancestor_idx 的后代（沿 parent_index 链）"""
    cur = widgets[idx].get('parent_index')
    guard = 0
    while cur is not None and guard < 50:
        if cur == ancestor_idx:
            return True
        cur = widgets[cur].get('parent_index') if 0 <= cur < len(widgets) else None
        guard += 1
    return False


class HdcUITestEngine:
    """HDC UI 测试引擎

    封装 HDC 操作 + 控件树差异比较 + 自动闪退检测。
    一条命令完成整套流程：执行 UI 操作 → 比较控件树差异 → 检测闪退。
    """

    def __init__(self, device: Optional[str] = None, history_dir: Optional[str] = None):
        self.device = device
        self.history_dir = history_dir or tempfile.gettempdir()
        self.manager = AutoDiffManager(history_dir)
        self.diff = WidgetTreeDiff()
        self.crash_detector = CrashDetector(device=device)
        self._cached_route = None
        self._driver = None

    @staticmethod
    def _suppress_logging():
        import logging
        sys.log_mode = "no_console"
        logging.disable(logging.INFO)

    def _get_driver(self):
        """懒加载 hypium daemon driver（复用持久连接，避免每次重拉 uitest 进程）

        用于 dump 控件树的快路径：连接一次性（约 2~5s），之后每次 dump 约 0.7s，
        对比 hdc uitest dumpLayout 的每次 ~5s，快约 7 倍。获取失败返回 None（回退慢路径）。
        """
        if self._driver is not None:
            return self._driver
        try:
            self._suppress_logging()
            from hypium import UiDriver
            report_path = tempfile.mkdtemp(prefix="hdc_hypium_report_")
            if self.device:
                self._driver = UiDriver.connect(device_sn=self.device, report_path=report_path)
            else:
                self._driver = UiDriver.connect(report_path=report_path)
            return self._driver
        except Exception:
            self._driver = None
            return None

    def close(self):
        """断开 hypium daemon driver 连接"""
        if self._driver is not None:
            try:
                self._driver.close()
            except Exception:
                pass
            self._driver = None

    def _hdc_cmd(self, cmd_parts: List[str], timeout: int = 30) -> tuple:
        """执行 hdc 命令"""
        cmd = ["hdc"]
        if self.device:
            cmd.extend(["-t", self.device])
        cmd.extend(cmd_parts)
        return run_hdc_command(cmd, timeout)

    def _dump_and_load(self, tag: str = "layout") -> Optional[WidgetTreeAnalyzer]:
        """从设备获取控件树并加载（纯 hdc 快路径，无需 hypium 连接）

        快路径：`uitest dumpLayout`（默认不带 -a）+ `file recv`，实测合计约 0.53s，
        与 hypium daemon dump 输出完全一致（逐控件字段相同），但省去每次进程约
        2.3s 的 driver 连接开销。失败时回退 hypium daemon，再回退带 -a 的慢路径。

        Returns:
            加载成功的 WidgetTreeAnalyzer，失败返回 None
        """
        # 快路径：纯 hdc dumpLayout（不带 -a，输出与 daemon 一致）
        remote = f'/data/local/tmp/_uitest_{tag}.json'
        tmp_path = os.path.join(
            tempfile.gettempdir(),
            f'hdc_layout_{tag}_{int(time.time() * 1000000)}.json')
        for attempt in range(2):
            ok, out = self._hdc_cmd(
                ["shell", "uitest", "dumpLayout", "-e", "uniqueId", "-p", remote], timeout=30)
            if ok:
                break
            if attempt == 0:
                print(f"⚠️  dumpLayout 失败，重试 1/1 ...")
        if ok:
            rok, rout = self._hdc_cmd(
                ["file", "recv", remote, tmp_path], timeout=30)
            if rok and os.path.exists(tmp_path) and os.path.getsize(tmp_path) > 0:
                analyzer = WidgetTreeAnalyzer(
                    json_file=tmp_path, device=self.device)
                analyzer._temp_file = tmp_path
                if analyzer.load_tree():
                    _record_page_path(analyzer, self.device)
                    return analyzer
                analyzer.cleanup()

        # 回退：hypium daemon 快路径（需连接，约 2.3s 连接 + 0.6s/dump）
        driver = self._get_driver()
        if driver is not None:
            tmp_path = os.path.join(
                tempfile.gettempdir(),
                f'hdc_hypium_{tag}_{int(time.time() * 1000000)}.json')
            try:
                driver.UiTree.dump_to_file(tmp_path)
                if os.path.getsize(tmp_path) > 0:
                    analyzer = WidgetTreeAnalyzer(
                        json_file=tmp_path, device=self.device)
                    analyzer._temp_file = tmp_path
                    if analyzer.load_tree():
                        _record_page_path(analyzer, self.device)
                        return analyzer
                    analyzer.cleanup()
            except Exception:
                pass

        # 慢路径回退：hdc uitest dumpLayout -a
        analyzer = WidgetTreeAnalyzer(
            json_file=f"auto_{tag}.json",
            device=self.device
        )
        success, msg = analyzer.dump_layout()
        if not success:
            print(f"❌ 获取控件树失败: {msg}")
            # dumpLayout 失败 → 自动检测是否闪退
            self.crash_detector.detect_and_report()
            return None

        if not analyzer.load_tree():
            analyzer.cleanup()
            return None

        _record_page_path(analyzer, self.device)
        return analyzer

    def has_crashed(self) -> bool:
        """是否有闪退（程序化接口）

        Returns:
            True 表示检测到崩溃信号
        """
        return self.crash_detector.has_crashed()

    def _compare_with_history(self, analyzer: WidgetTreeAnalyzer,
                              operation: str) -> Optional[ChangeReport]:
        """与历史比较并输出报告

        Returns:
            变化报告（无历史时返回 None）
        """
        previous_widgets = self.manager.load_history()

        if previous_widgets:
            before_route = self.manager.load_route_history()
            after_route = self._get_current_route_cached()
            report = self.diff.compare(
                previous_widgets,
                analyzer.widgets,
                operation,
                before_route=before_route,
                after_route=after_route,
                after_analyzer=analyzer
            )
            report.print()
            return report
        else:
            print("ℹ️  首次运行，已保存当前控件树作为基准")
            return None

    def _get_current_route_cached(self) -> Optional[List[str]]:
        """获取当前路由栈并缓存（同一操作流程内复用，避免重复查询）"""
        if self._cached_route is None:
            self._cached_route = WidgetTreeDiff.get_current_route(self.device)
        return self._cached_route

    def _save_and_cleanup(self, analyzer: WidgetTreeAnalyzer,
                          save_route: bool = False):
        """保存历史并清理

        Args:
            analyzer: 控件树分析器
            save_route: 是否同时保存当前路由（用于操作前快照）
        """
        route = None
        if save_route:
            route = self._get_current_route_cached()
            self._cached_route = None
        self.manager.save_history(analyzer.widgets, route=route)
        analyzer.cleanup()

    # ===== 公开的UI操作方法 =====

    def _execute_hdc_and_compare(self, ui_input_args: List[str],
                                 desc: str, action_name: str,
                                 expectations: Optional[dict] = None,
                                 skip_before: bool = False,
                                 fresh_before: bool = False,
                                 auto_recover: bool = True,
                                 is_back: bool = False,
                                 check_exit: bool = False,
                                 daemon_fn=None,
                                 hdc_fn=None) -> bool:
        """通用流程：前置快照 → 执行操作 → 统一裁决管线

        操作原语优先走 `hdc shell uitest uiInput`（约 0.34s，无需 hypium 连接）；
        失败或不可用时回退到 hypium daemon（`daemon_fn(driver)`，约 0.2s，但需
        支付一次性 ~2.3s 连接开销，故仅在 hdc 失败时启用）。

        Args:
            ui_input_args: 回退用的 `uitest uiInput` 子命令和参数
            desc: 操作描述
            action_name: 操作名称（用于错误提示，如 "点击"）
            expectations: 期望断言字典（route/text_exists/text_gone/no_change/dialog）
            skip_before: 跳过 before-dump（批量模式复用上一步 after-state）
            fresh_before: 强制 fresh before-dump（禁用历史基准复用）
            auto_recover: 被弹窗挡住时自动清理并重试一次
            is_back: 返回类动作（无变化标记 BACK_INEFFECTIVE）
            daemon_fn: 可选，`fn(driver)` 形式的 daemon 操作（hdc 失败时的回退）
            hdc_fn: 可选，`fn() -> (success, output)` 多步 hdc 操作（如输入：
                点击聚焦 → Ctrl+A → text）。提供时替代单条 ui_input_args。

        Returns:
            裁决+断言是否全部通过（决定 exit code）
        """
        def action_fn():
            if hdc_fn is not None:
                success, output = hdc_fn()
            else:
                success, output = self._hdc_cmd(
                    ["shell", "uitest", "uiInput"] + ui_input_args
                )
            if success:
                return True
            if daemon_fn is not None:
                driver = self._get_driver()
                if driver is not None:
                    try:
                        daemon_fn(driver)
                        return True
                    except Exception as ex:
                        print(f"ℹ️ daemon {action_name} 失败({ex})，回退失败")
            print(f"❌ HDC{action_name}失败: {output}")
            return False

        pipeline = ActionPipeline(self, auto_recover=auto_recover)
        return pipeline.run(action_fn, desc, expectations=expectations,
                            is_back=is_back, check_exit=check_exit,
                            skip_before=skip_before,
                            fresh_before=fresh_before)

    def click(self, x: int, y: int, operation: str = "",
              expectations: Optional[dict] = None,
              skip_before: bool = False, fresh_before: bool = False,
              auto_recover: bool = True) -> bool:
        """点击坐标并比较变化"""
        desc = operation or f"点击 ({x}, {y})"
        return self._execute_hdc_and_compare(
            ["click", str(x), str(y)], desc, "点击",
            expectations=expectations, skip_before=skip_before,
            fresh_before=fresh_before, auto_recover=auto_recover,
            daemon_fn=lambda d: d.touch((x, y)))

    def double_click(self, x: int, y: int, operation: str = "",
                     expectations: Optional[dict] = None,
                     skip_before: bool = False, fresh_before: bool = False,
                     auto_recover: bool = True) -> bool:
        """双击坐标并比较变化"""
        desc = operation or f"双击 ({x}, {y})"
        return self._execute_hdc_and_compare(
            ["doubleClick", str(x), str(y)], desc, "双击",
            expectations=expectations, skip_before=skip_before,
            fresh_before=fresh_before, auto_recover=auto_recover,
            daemon_fn=lambda d: d.double_click((x, y)))

    def long_click(self, x: int, y: int, operation: str = "",
                   expectations: Optional[dict] = None,
                   skip_before: bool = False, fresh_before: bool = False,
                   auto_recover: bool = True) -> bool:
        """长按坐标并比较变化"""
        desc = operation or f"长按 ({x}, {y})"
        return self._execute_hdc_and_compare(
            ["longClick", str(x), str(y)], desc, "长按",
            expectations=expectations, skip_before=skip_before,
            fresh_before=fresh_before, auto_recover=auto_recover,
            daemon_fn=lambda d: d.long_click((x, y)))

    def swipe(self, x1: int, y1: int, x2: int, y2: int,
              operation: str = "", expectations: Optional[dict] = None,
              skip_before: bool = False, fresh_before: bool = False,
              auto_recover: bool = True) -> bool:
        """滑动并比较变化"""
        desc = operation or f"滑动 ({x1},{y1}) → ({x2},{y2})"
        return self._execute_hdc_and_compare(
            ["swipe", str(x1), str(y1), str(x2), str(y2)], desc, "滑动",
            expectations=expectations, skip_before=skip_before,
            fresh_before=fresh_before, auto_recover=auto_recover,
            daemon_fn=lambda d: d.slide((x1, y1), (x2, y2)))

    def text_input(self, text: str, operation: str = "",
                   expectations: Optional[dict] = None,
                   skip_before: bool = False, fresh_before: bool = False,
                   auto_recover: bool = True) -> bool:
        """输入文本并比较变化"""
        desc = operation or f'输入文本: "{text}"'
        return self._execute_hdc_and_compare(
            ["text", text], desc, "输入",
            expectations=expectations, skip_before=skip_before,
            fresh_before=fresh_before, auto_recover=auto_recover,
            daemon_fn=lambda d: d.input_text_on_current_cursor(text))

    def key_back(self, operation: str = "",
                 expectations: Optional[dict] = None,
                 skip_before: bool = False, fresh_before: bool = False,
                 auto_recover: bool = True) -> bool:
        """返回键并比较变化（无变化标记 BACK_INEFFECTIVE；已在桌面时不执行返回）"""
        desc = operation or "按下返回键"
        return self._execute_hdc_and_compare(
            ["keyEvent", "Back"], desc, "返回键",
            expectations=expectations, skip_before=skip_before,
            fresh_before=fresh_before, auto_recover=auto_recover,
            is_back=True, check_exit=True,
            daemon_fn=lambda d: d.press_back())


class HypiumEngine:
    """基于 hypium 的语义化 UI 操作引擎

    通过 BY 选择器按文本/ID/类型查找控件，无需硬编码坐标。
    自动执行差异比较：通过 driver.UiTree.dump_to_file() 获取控件树
    （daemon 自身连接运行 dumpLayout，无冲突）→ 执行操作 → dump 对比。

    依赖: pip install hypium
    """

    def __init__(self, device: Optional[str] = None, history_dir: Optional[str] = None):
        self.device = device
        self.history_dir = history_dir or tempfile.gettempdir()
        self.manager = AutoDiffManager(history_dir)
        self.diff = WidgetTreeDiff()
        self.crash_detector = CrashDetector(device=device)
        self._driver = None
        self._cached_route = None
        self._screen_cache = None

    @staticmethod
    def _suppress_logging():
        import sys
        import logging
        sys.log_mode = "no_console"
        logging.disable(logging.INFO)

    def _hdc_cmd(self, cmd_parts: List[str], timeout: int = 30) -> tuple:
        cmd = ["hdc"]
        if self.device:
            cmd.extend(["-t", self.device])
        cmd.extend(cmd_parts)
        return run_hdc_command(cmd, timeout)

    def _get_driver(self):
        if self._driver is None:
            self._suppress_logging()
            _BY, UiDriver = _import_hypium()
            report_path = tempfile.mkdtemp(prefix="hypium_report_")
            if self.device:
                self._driver = UiDriver.connect(device_sn=self.device, report_path=report_path)
            else:
                self._driver = UiDriver.connect(report_path=report_path)
        return self._driver

    def close(self):
        """断开 hypium driver"""
        if self._driver is not None:
            try:
                self._driver.close()
            except Exception:
                pass
            self._driver = None

    def _dump_and_load(self, tag: str = "layout") -> Optional[WidgetTreeAnalyzer]:
        """获取控件树并加载（纯 hdc 快路径优先，无需 hypium 连接）

        快路径：`uitest dumpLayout`（不带 -a）+ `file recv`，实测 ~0.53s，输出与
        daemon dump 完全一致。hypium daemon 连接（~2.3s）仅在快路径失败时启用。
        """
        remote = f'/data/local/tmp/_uitest_{tag}.json'
        tmp_path = os.path.join(
            tempfile.gettempdir(),
            f'hdc_layout_{tag}_{int(time.time() * 1000000)}.json')
        for attempt in range(2):
            ok, out = self._hdc_cmd(
                ["shell", "uitest", "dumpLayout", "-e", "uniqueId", "-p", remote], timeout=30)
            if ok:
                break
            if attempt == 0:
                print(f"⚠️  dumpLayout 失败，重试 1/1 ...")
        if ok:
            rok, rout = self._hdc_cmd(
                ["file", "recv", remote, tmp_path], timeout=30)
            if rok and os.path.exists(tmp_path) and os.path.getsize(tmp_path) > 0:
                analyzer = WidgetTreeAnalyzer(json_file=tmp_path, device=self.device)
                analyzer._temp_file = tmp_path
                if analyzer.load_tree():
                    _record_page_path(analyzer, self.device)
                    return analyzer
                analyzer.cleanup()

        # 回退：hypium daemon dump（需连接）
        driver = self._get_driver()
        if driver is not None:
            try:
                driver.UiTree.dump_to_file(tmp_path)
            except Exception as e:
                print(f"❌ 获取控件树失败: {e}")
                self.crash_detector.detect_and_report()
                return None
            if os.path.exists(tmp_path) and os.path.getsize(tmp_path) > 0:
                analyzer = WidgetTreeAnalyzer(json_file=tmp_path, device=self.device)
                analyzer._temp_file = tmp_path
                if analyzer.load_tree():
                    _record_page_path(analyzer, self.device)
                    return analyzer
                analyzer.cleanup()
        self.crash_detector.detect_and_report()
        return None

    def _get_current_route_cached(self) -> Optional[List[str]]:
        """获取当前路由栈并缓存（同一操作流程内复用，避免重复查询）"""
        if self._cached_route is None:
            self._cached_route = WidgetTreeDiff.get_current_route(self.device)
        return self._cached_route

    def _compare_with_history(self, analyzer: WidgetTreeAnalyzer,
                              operation: str) -> Optional[ChangeReport]:
        previous_widgets = self.manager.load_history()
        if previous_widgets:
            before_route = self.manager.load_route_history()
            after_route = self._get_current_route_cached()
            report = self.diff.compare(
                previous_widgets, analyzer.widgets, operation,
                before_route=before_route, after_route=after_route,
                after_analyzer=analyzer)
            report.print()
            return report
        else:
            print("ℹ️  首次运行，已保存当前控件树作为基准")
            return None

    def _save_and_cleanup(self, analyzer: WidgetTreeAnalyzer, save_route: bool = False):
        route = None
        if save_route:
            route = self._get_current_route_cached()
            self._cached_route = None
        self.manager.save_history(analyzer.widgets, route=route)
        analyzer.cleanup()

    @staticmethod
    def _compute_page_signature(widgets) -> tuple:
        """从 widget 列表提取页面签名（委托给模块级同名函数）"""
        return _compute_page_signature(widgets)

    def _page_signature(self, analyzer) -> tuple:
        """提取页面签名（委托给 _compute_page_signature）"""
        return self._compute_page_signature(analyzer.widgets)

    @staticmethod
    def _looks_like_desktop(analyzer) -> bool:
        """粗略判断页面是否已非 App 内容（控件过少且无可读文字，疑似系统桌面）"""
        texts = [w for w in analyzer.widgets if (w.get('text') or '').strip()]
        return len(analyzer.widgets) < 15 and len(texts) < 3

    @_guard_timeout(180)
    def _execute_hypium_and_compare(self, action_fn: Callable, desc: str,
                                    check_exit: bool = False,
                                    expectations: Optional[dict] = None,
                                    skip_before: bool = False,
                                    fresh_before: bool = False,
                                    auto_recover: bool = True,
                                    is_back: bool = False) -> bool:
        """通用流程：前置快照 → 执行操作 → 统一裁决管线

        Args:
            action_fn: 执行操作的可调用对象（可重复调用以自愈重试）
            desc: 操作描述
            check_exit: 为 True 时（返回键操作）检测是否意外退出 App
            expectations: 期望断言字典
            skip_before: 跳过 before-dump（批量模式复用上一步 after-state）
            fresh_before: 强制 fresh before-dump（禁用历史基准复用）
            auto_recover: 被弹窗挡住时自动清理并重试一次
            is_back: 返回类动作（无变化标记 BACK_INEFFECTIVE）
        """
        pipeline = ActionPipeline(self, auto_recover=auto_recover)
        return pipeline.run(action_fn, desc, expectations=expectations,
                            is_back=is_back, check_exit=check_exit,
                            skip_before=skip_before, fresh_before=fresh_before)

    def _touch_guard(self, fn: Callable) -> bool:
        """hypium 匹配不到目标时抛异常而非返回值，这里归一为 False 供 fuzzy 回退判定。"""
        try:
            result = fn()
            return True if result is None else bool(result)
        except Exception:
            return False

    def click_by_text(self, text: str, operation: str = "",
                      expectations: Optional[dict] = None,
                      skip_before: bool = False,
                      index: Optional[int] = None,
                      fresh_before: bool = False,
                      auto_recover: bool = True) -> bool:
        desc = operation or f"点击文本为 '{text}' 的控件"
        if index is not None:
            # 指定索引：直接 dump 控件树按文本匹配列表取第 N 个坐标点击
            return self._click_by_text_fuzzy(text, desc, index=index,
                                             expectations=expectations,
                                             skip_before=skip_before,
                                             fresh_before=fresh_before,
                                             auto_recover=auto_recover)
        # 快路径：dump 精确匹配单个控件 → 坐标点击（纯 hdc，无需 hypium 连接）
        found, ok = self._hdc_click_matches(
            desc,
            lambda ws: [w for w in ws
                        if (w.get('text') or '') == text
                        or (w.get('hint') or '') == text],
            lambda e, x, y: e.click(x, y, desc,
                                    expectations=expectations,
                                    skip_before=True,
                                    fresh_before=fresh_before,
                                    auto_recover=auto_recover))
        if found:
            return ok
        # 回退：hypium BY.text 精确匹配（能解析 dump 未覆盖的复杂控件）
        BY, _UiDriver = _import_hypium()
        result = self._execute_hypium_and_compare(
            lambda: self._touch_guard(
                lambda: self._get_driver().touch(BY.text(text))), desc,
            expectations=expectations, skip_before=skip_before,
            fresh_before=fresh_before, auto_recover=auto_recover)
        if result:
            return True
        if skip_before:
            return False
        print(f"ℹ️  精确匹配失败，尝试文本包含搜索...")
        return self._click_by_text_fuzzy(text, desc,
                                         expectations=expectations,
                                         skip_before=skip_before,
                                         fresh_before=fresh_before,
                                         auto_recover=auto_recover)

    def _click_by_text_fuzzy(self, text: str, desc: str,
                             index: Optional[int] = None,
                             expectations: Optional[dict] = None,
                             skip_before: bool = False,
                             fresh_before: bool = False,
                             auto_recover: bool = True) -> bool:
        """精确匹配失败后，dump 控件树，文本包含搜索，坐标点击

        Args:
            text: 目标文本（包含匹配）
            desc: 操作描述
            index: 指定匹配列表中的第 N 个控件（默认第 0 个）
            expectations: 期望断言字典
            skip_before: 跳过 before-dump
        """
        found, ok = self._hdc_click_matches(
            desc,
            lambda ws: [w for w in ws
                        if text.lower() in (w.get('text', '') or '').lower()
                        or text.lower() in (w.get('hint', '') or '').lower()],
            lambda e, x, y: e.click(x, y, desc,
                                    expectations=expectations,
                                    skip_before=True,
                                    fresh_before=fresh_before,
                                    auto_recover=auto_recover),
            index=index)
        if not found:
            print(f"❌ 未找到文本/占位符包含 '{text}' 的控件")
        return ok

    @staticmethod
    def _parse_bounds_center(bounds_str: str) -> Optional[tuple]:
        """解析 bounds 字符串 '[x1,y1][x2,y2]'，返回中心坐标 (x, y)"""
        import re
        nums = re.findall(r'\d+', bounds_str)
        if len(nums) >= 4:
            x1, y1, x2, y2 = int(nums[0]), int(nums[1]), int(nums[2]), int(nums[3])
            return ((x1 + x2) // 2, (y1 + y2) // 2)
        return None

    def _hdc_click_matches(self, desc: str, matcher_fn, engine_action,
                           expectations: Optional[dict] = None,
                           skip_before: bool = False,
                           fresh_before: bool = False,
                           auto_recover: bool = True,
                           index: Optional[int] = None) -> tuple:
        """hdc 快路径通用动作：dump 控件树（纯 hdc）→ matcher_fn(widgets)
        匹配 → 取 [index] 控件中心坐标 → engine_action(engine, x, y) 走
        HdcUITestEngine 坐标动作管线（纯 hdc，无 hypium 连接）。

        查找 dump 同时保存为历史基线并传 skip_before=True，省去一次 before dump。

        Returns:
            (found, ok): found=True 表示找到控件并执行了动作；ok 为裁决结果。
            found=False 表示未找到/坐标无法解析（未执行动作）。
        """
        analyzer = self._dump_and_load("hdc_action")
        if analyzer is not None:
            matched = matcher_fn(analyzer.widgets)
            idx = index or 0
            if matched and idx < len(matched):
                w = matched[idx]
                coords = self._parse_bounds_center(w.get('bounds', ''))
                if coords:
                    x, y = coords
                    print(f"  匹配控件 [{idx}/{len(matched)}]: type={w.get('type', '')} "
                          f"text={w.get('text', '')}")
                    print(f"  点击坐标: ({x}, {y})")
                    self._save_and_cleanup(analyzer, save_route=True)
                    engine = HdcUITestEngine(device=self.device,
                                             history_dir=self.history_dir)
                    return True, engine_action(engine, x, y)
            analyzer.cleanup()
        return False, False

    def _hdc_input_matches(self, desc: str, matcher_fn, input_text: str,
                           daemon_fn=None,
                           expectations: Optional[dict] = None,
                           skip_before: bool = False,
                           fresh_before: bool = False,
                           auto_recover: bool = True,
                           index: Optional[int] = None) -> tuple:
        """hdc 快路径输入：dump 控件树（纯 hdc）→ matcher_fn(widgets) 匹配输入框
        → 坐标点击聚焦 → Ctrl+A 全选 → `uiInput text` 覆盖输入（无 hypium 连接）。

        查找 dump 保存为历史基线 + skip_before=True，省一次 before dump。
        hdc 动作失败时回退 daemon_fn(driver)（hypium daemon 输入）。

        Returns:
            (found, ok): found=False 表示未匹配到输入框（未执行动作）；
            found=True 时 ok 为裁决结果。
        """
        analyzer = self._dump_and_load("hdc_input")
        if analyzer is not None:
            matched = matcher_fn(analyzer.widgets)
            idx = index or 0
            if matched and idx < len(matched):
                w = matched[idx]
                coords = self._parse_bounds_center(w.get('bounds', ''))
                if coords:
                    x, y = coords
                    print(f"  匹配输入框 [{idx}/{len(matched)}]: "
                          f"type={w.get('type', '')} text={w.get('text', '')} "
                          f"placeholder={w.get('placeholder', '')}")
                    print(f"  点击坐标: ({x}, {y})")
                    self._save_and_cleanup(analyzer, save_route=True)
                    engine = HdcUITestEngine(device=self.device,
                                             history_dir=self.history_dir)

                    def hdc_fn():
                        ok1, o1 = engine._hdc_cmd(
                            ["shell", "uitest", "uiInput", "click", str(x), str(y)])
                        if not ok1:
                            return False, o1
                        time.sleep(0.2)  # 等待输入框聚焦
                        # Ctrl+A 全选（覆盖输入语义，避免追加）；失败则退回追加
                        okk, _ = engine._hdc_cmd(
                            ["shell", "uitest", "uiInput",
                             "keyEvent", "2072", "2017"])
                        if not okk:
                            print("ℹ️  Ctrl+A 全选失败，将在现有内容后追加输入")
                        time.sleep(0.15)
                        return engine._hdc_cmd(
                            ["shell", "uitest", "uiInput", "text", input_text])

                    return True, engine._execute_hdc_and_compare(
                        [], desc, "输入", expectations=expectations,
                        skip_before=True, fresh_before=fresh_before,
                        auto_recover=auto_recover,
                        hdc_fn=hdc_fn, daemon_fn=daemon_fn)
            analyzer.cleanup()
        return False, False

    def click_by_id(self, key: str, operation: str = "",
                    expectations: Optional[dict] = None,
                    skip_before: bool = False,
                    fresh_before: bool = False,
                    auto_recover: bool = True) -> bool:
        desc = operation or f"点击 id/key='{key}' 的控件"
        # 控件树能读到 id 字段：优先按 id 搜索取中心坐标点击（适用于 hypium
        # BY.key 无法遍历的组件，如播放器右侧控制条 Image 按钮）。
        if self._click_by_id_fuzzy(key, desc, expectations=expectations,
                                   skip_before=skip_before,
                                   fresh_before=fresh_before,
                                   auto_recover=auto_recover):
            return True
        # 回退到 BY.key（组件设了 .key() 时可精确命中并触发断言）
        BY, _UiDriver = _import_hypium()
        return self._execute_hypium_and_compare(
            lambda: self._get_driver().touch(BY.key(key)), desc,
            expectations=expectations, skip_before=skip_before,
            fresh_before=fresh_before, auto_recover=auto_recover)

    def _click_by_id_fuzzy(self, key: str, desc: str,
                           expectations: Optional[dict] = None,
                           skip_before: bool = False,
                           fresh_before: bool = False,
                           auto_recover: bool = True) -> bool:
        """dump 控件树，按 id/key 字段搜索，坐标点击（带断言）"""
        found, ok = self._hdc_click_matches(
            desc,
            lambda ws: ([w for w in ws if (w.get('id', '') or '') == key]
                        or [w for w in ws
                            if (w.get('attributes', {}) or {}).get('key', '') == key]
                        or [w for w in ws
                            if key.lower() in (w.get('id', '') or '').lower()]),
            lambda e, x, y: e.click(x, y, desc,
                                    expectations=expectations,
                                    skip_before=True,
                                    fresh_before=fresh_before,
                                    auto_recover=auto_recover))
        if not found:
            print(f"❌ 未找到 id/key 含 '{key}' 的控件")
        return ok

    def click_by_type(self, widget_type: str, operation: str = "",
                      expectations: Optional[dict] = None,
                      skip_before: bool = False,
                      fresh_before: bool = False,
                      auto_recover: bool = True) -> bool:
        desc = operation or f"点击类型为 '{widget_type}' 的控件"
        # 快路径：dump 按 type 精确匹配 → 坐标点击（纯 hdc，无 hypium 连接）
        found, ok = self._hdc_click_matches(
            desc,
            lambda ws: [w for w in ws if (w.get('type') or '') == widget_type],
            lambda e, x, y: e.click(x, y, desc,
                                    expectations=expectations,
                                    skip_before=True,
                                    fresh_before=fresh_before,
                                    auto_recover=auto_recover))
        if found:
            return ok
        # 回退：hypium BY.type
        from hypium import BY
        return self._execute_hypium_and_compare(
            lambda: self._get_driver().touch(
                self._get_driver().find_component(BY.type(widget_type))), desc,
            expectations=expectations, skip_before=skip_before,
            fresh_before=fresh_before, auto_recover=auto_recover)

    def double_click_by_text(self, text: str, operation: str = "",
                             expectations: Optional[dict] = None,
                             skip_before: bool = False,
                             fresh_before: bool = False,
                             auto_recover: bool = True) -> bool:
        desc = operation or f"双击文本为 '{text}' 的控件"
        found, ok = self._hdc_click_matches(
            desc,
            lambda ws: [w for w in ws
                        if (w.get('text') or '') == text
                        or (w.get('hint') or '') == text],
            lambda e, x, y: e.double_click(x, y, desc,
                                           expectations=expectations,
                                           skip_before=True,
                                           fresh_before=fresh_before,
                                           auto_recover=auto_recover))
        if found:
            return ok
        from hypium import BY
        return self._execute_hypium_and_compare(
            lambda: self._get_driver().double_click(BY.text(text)), desc,
            expectations=expectations, skip_before=skip_before,
            fresh_before=fresh_before, auto_recover=auto_recover)

    def long_click_by_text(self, text: str, operation: str = "",
                           expectations: Optional[dict] = None,
                           skip_before: bool = False,
                           fresh_before: bool = False,
                           auto_recover: bool = True) -> bool:
        desc = operation or f"长按文本为 '{text}' 的控件"
        found, ok = self._hdc_click_matches(
            desc,
            lambda ws: [w for w in ws
                        if (w.get('text') or '') == text
                        or (w.get('hint') or '') == text],
            lambda e, x, y: e.long_click(x, y, desc,
                                         expectations=expectations,
                                         skip_before=True,
                                         fresh_before=fresh_before,
                                         auto_recover=auto_recover))
        if found:
            return ok
        from hypium import BY
        return self._execute_hypium_and_compare(
            lambda: self._get_driver().long_click(BY.text(text)), desc,
            expectations=expectations, skip_before=skip_before,
            fresh_before=fresh_before, auto_recover=auto_recover)

    def input_by_text(self, target_text: str, input_text: str,
                      operation: str = "",
                      expectations: Optional[dict] = None,
                      skip_before: bool = False,
                      fresh_before: bool = False,
                      auto_recover: bool = True) -> bool:
        desc = operation or f"在 '{target_text}' 输入框中输入 '{input_text}'"

        def daemon_text(d):
            from hypium import BY
            d.input_text(d.find_component(BY.text(target_text)), input_text)

        def daemon_hint(d):
            from hypium import BY
            d.input_text(d.find_component(BY.hint(target_text)), input_text)

        # 快路径：hdc（无需 hypium 连接）—— text 精确匹配
        found, ok = self._hdc_input_matches(
            desc,
            lambda ws: [w for w in ws if (w.get('text') or '') == target_text],
            input_text, daemon_fn=daemon_text,
            expectations=expectations, skip_before=skip_before,
            fresh_before=fresh_before, auto_recover=auto_recover)
        if found:
            return ok
        # 精确 text 匹配失败：输入框的常见目标其实是占位符 hint（如"请输入手机号码"）
        print("ℹ️  text 精确匹配失败，尝试按 hint 占位符匹配...")
        found, ok = self._hdc_input_matches(
            desc,
            lambda ws: [w for w in ws
                        if (w.get('hint') or w.get('placeholder') or '') == target_text],
            input_text, daemon_fn=daemon_hint,
            expectations=expectations, skip_before=skip_before,
            fresh_before=fresh_before, auto_recover=auto_recover)
        if found:
            return ok
        # 兜底：hypium driver 路径（原实现）
        from hypium import BY
        result = self._execute_hypium_and_compare(
            lambda: (self._get_driver().input_text(
                self._get_driver().find_component(BY.text(target_text)),
                input_text) or True), desc,
            expectations=expectations, skip_before=skip_before,
            fresh_before=fresh_before, auto_recover=auto_recover)
        if result:
            return True
        return self._execute_hypium_and_compare(
            lambda: (self._get_driver().input_text(
                self._get_driver().find_component(BY.hint(target_text)),
                input_text) or True), desc,
            expectations=expectations, skip_before=skip_before,
            fresh_before=fresh_before, auto_recover=auto_recover)

    def input_by_type(self, widget_type: str, input_text: str,
                      operation: str = "",
                      expectations: Optional[dict] = None,
                      skip_before: bool = False,
                      fresh_before: bool = False,
                      auto_recover: bool = True) -> bool:
        desc = operation or f"在 {widget_type} 中输入 '{input_text}'"

        def daemon_fn(d):
            from hypium import BY
            d.input_text(d.find_component(BY.type(widget_type)), input_text)

        # 快路径：hdc（无需 hypium 连接）
        found, ok = self._hdc_input_matches(
            desc,
            lambda ws: [w for w in ws if (w.get('type') or '') == widget_type],
            input_text, daemon_fn=daemon_fn,
            expectations=expectations, skip_before=skip_before,
            fresh_before=fresh_before, auto_recover=auto_recover)
        if found:
            return ok
        # 兜底：hypium driver 路径（原实现）
        from hypium import BY
        return self._execute_hypium_and_compare(
            lambda: (self._get_driver().input_text(
                self._get_driver().find_component(BY.type(widget_type)),
                input_text) or True), desc,
            expectations=expectations, skip_before=skip_before,
            fresh_before=fresh_before, auto_recover=auto_recover)

    def swipe_direction(self, direction: str, distance: int = 60,
                        operation: str = "",
                        expectations: Optional[dict] = None,
                        skip_before: bool = False,
                        fresh_before: bool = False,
                        auto_recover: bool = True) -> bool:
        desc = operation or f"向 {direction} 滑动 {distance}"
        # 快路径：纯 hdc uiInput swipe（无需 hypium 连接），从屏幕中心按方向滑动
        if self._driver is None:
            w, h = self._screen_wh()
            cx, cy = w // 2, h // 2
            d = max(10, int(distance))
            step = {
                'up': (cx, cy - d), 'down': (cx, cy + d),
                'left': (cx - d, cy), 'right': (cx + d, cy),
            }.get(direction)
            if step:
                ex, ey = step
                engine = HdcUITestEngine(device=self.device, history_dir=self.history_dir)
                return engine.swipe(cx, cy, ex, ey, desc,
                                    expectations=expectations,
                                    skip_before=skip_before,
                                    fresh_before=fresh_before,
                                    auto_recover=auto_recover)
        # 回退：hypium swipe()（返回 None，包装为成功）
        return self._execute_hypium_and_compare(
            lambda: (self._get_driver().swipe(direction, distance=distance) or True), desc,
            expectations=expectations, skip_before=skip_before,
            fresh_before=fresh_before, auto_recover=auto_recover)

    def go_back(self, operation: str = "",
                expectations: Optional[dict] = None,
                skip_before: bool = False,
                fresh_before: bool = False,
                auto_recover: bool = True) -> bool:
        desc = operation or "按下返回键"
        # 快路径：纯 hdc uiInput keyEvent Back（无需 hypium 连接）
        if self._driver is None:
            engine = HdcUITestEngine(device=self.device, history_dir=self.history_dir)
            return engine.key_back(desc, expectations=expectations,
                                   skip_before=skip_before,
                                   fresh_before=fresh_before,
                                   auto_recover=auto_recover)
        return self._execute_hypium_and_compare(
            lambda: self._get_driver().press_back(), desc, check_exit=True,
            expectations=expectations, skip_before=skip_before,
            fresh_before=fresh_before, auto_recover=auto_recover,
            is_back=True)

    # === 以下为纯检查/截图操作，不执行 dumpLayout 差异比较 ===

    def check_dialog(self, dialog_type: str = "Dialog") -> bool:
        from hypium import BY
        driver = self._get_driver()
        try:
            driver.check_component_exist(BY.type(dialog_type), wait_time=2)
            print(f"✅ 检测到 {dialog_type} 弹窗")
            return True
        except Exception:
            print(f"❌ 未检测到 {dialog_type} 弹窗")
            return False

    def check_component(self, text: Optional[str] = None, key: Optional[str] = None,
                        widget_type: Optional[str] = None, wait_time: int = 2) -> bool:
        conditions = []
        if text: conditions.append(f"text='{text}'")
        if key: conditions.append(f"key='{key}'")
        if widget_type: conditions.append(f"type='{widget_type}'")
        if not conditions:
            print("❌ 至少指定一个搜索条件 (text/key/type)")
            return False
        cond_str = " + ".join(conditions)

        def _match(w) -> bool:
            if text and text.lower() not in (w.get('text', '') or '').lower() \
                    and text.lower() not in (w.get('hint', '') or '').lower():
                return False
            if key and (w.get('id', '') or '') != key \
                    and (w.get('attributes', {}) or {}).get('key', '') != key:
                return False
            if widget_type and (w.get('type', '') or '') != widget_type:
                return False
            return True

        # 快路径：dump 控件树匹配（纯 hdc ~1.2s，无需 hypium 连接）
        a = self._dump_and_load("checkex")
        if a is not None:
            try:
                if any(_match(w) for w in a.widgets):
                    print(f"✅ 控件存在: {cond_str}")
                    return True
            finally:
                a.cleanup()
        # 回退：hypium 精确检查（异步渲染/复杂控件场景，带等待）
        from hypium import BY
        selector = None
        if text:
            selector = BY.text(text) if selector is None else selector.text(text)
        if key:
            selector = BY.key(key) if selector is None else selector.key(key)
        if widget_type:
            selector = BY.type(widget_type) if selector is None else selector.type(widget_type)
        driver = self._get_driver()
        try:
            driver.check_component_exist(selector, wait_time=wait_time)
            print(f"✅ 控件存在: {cond_str}")
            return True
        except Exception:
            if text and self._screen_has_text(text):
                print(f"✅ 控件存在(文本包含匹配): {cond_str}")
                return True
            print(f"❌ 控件不存在: {cond_str}")
            return False

    def find_and_print(self, text: Optional[str] = None, key: Optional[str] = None,
                       widget_type: Optional[str] = None) -> bool:
        from hypium import BY
        selector = None
        if text:
            selector = BY.text(text) if selector is None else selector.text(text)
        if key:
            selector = BY.key(key) if selector is None else selector.key(key)
        if widget_type:
            selector = BY.type(widget_type) if selector is None else selector.type(widget_type)
        if selector is None:
            print("❌ 至少指定一个搜索条件 (text/key/type)")
            return False
        driver = self._get_driver()
        conditions = []
        if text: conditions.append(f"text='{text}'")
        if key: conditions.append(f"key='{key}'")
        if widget_type: conditions.append(f"type='{widget_type}'")
        cond_str = " + ".join(conditions)
        try:
            driver.check_component_exist(selector, wait_time=2)
        except Exception:
            if not (text and self._screen_has_text(text)):
                print(f"❌ 未找到匹配控件: {cond_str}")
                return False
        analyzer = self._dump_and_load("find")
        if not analyzer:
            print("❌ 控件存在但获取控件树失败")
            return False
        if text:
            analyzer.search_by_text(text)
        elif key:
            analyzer.search_by_id(key)
        elif widget_type:
            analyzer.search_by_type(widget_type)
        analyzer.cleanup()
        return True

    # === 智能滚动查找 ===

    def _fuzzy_match_widgets(self, text: str):
        """dump 当前屏幕控件树，按文本包含匹配（text + hint 占位符）。返回 (matched, sig)"""
        analyzer = self._dump_and_load("fuzzyfind")
        if not analyzer:
            return None, None
        matched = [w for w in analyzer.widgets
                   if text.lower() in (w.get('text', '') or '').lower()
                   or text.lower() in (w.get('hint', '') or '').lower()]
        # 返回内容敏感签名（标题栏签名 _page_signature 只覆盖 y100~320，
        # 对滚动内容不敏感，不能用于判断"是否滚动了"）
        sig = self._scroll_signature(analyzer.widgets)
        analyzer.cleanup()
        return matched, sig

    def _screen_has_text(self, text: str) -> bool:
        """当前屏幕是否存在含 text 的控件（dump 包含匹配优先 + hypium 精确兜底）"""
        matched, _ = self._fuzzy_match_widgets(text)
        if matched:
            return True
        from hypium import BY
        try:
            self._get_driver().check_component_exist(BY.text(text), wait_time=1)
            return True
        except Exception:
            pass
        return False

    @staticmethod
    def _max_y_of_widgets(widgets) -> int:
        import re
        max_y = 0
        for w in widgets:
            nums = re.findall(r'\d+', w.get('bounds', '') or '')
            if len(nums) >= 4:
                max_y = max(max_y, int(nums[3]))
        return max_y

    @staticmethod
    def _scroll_signature(widgets) -> tuple:
        """滚动内容签名：所有可见 Text 的 (文本, y/5) 集合。

        标题栏签名 _compute_page_signature 只覆盖 y100~320 区域，滚动内容
        不改变它，因此不能用来判断"是否滚动了"。本签名对任何竖向位移敏感，
        用于检测滚动是否真的产生位移（判断列表是否到底）。
        """
        import re
        sig = []
        for w in widgets:
            if w.get('type') != 'Text':
                continue
            t = (w.get('text') or '').strip()
            if not t:
                continue
            m = re.findall(r'\d+', w.get('bounds', '') or '')
            y = int(m[1]) if len(m) >= 2 else 0
            sig.append((t, y // 5))
        return tuple(sorted(sig))

    def _find_scroll_candidates(self, analyzer) -> list:
        """识别候选竖向滚动容器，返回 bounds 列表（按高度降序）。

        只认竖向滚动容器，排除横向 Swiper（竖滑无效且可能误触发翻页）：
        - 类型 Scroll/List/ListItemGroup/Grid
        - 宽高均 >= 80px
        高度越大越可能是主滚动区，故按高度降序。
        """
        import re
        cands, seen = [], set()
        for w in analyzer.widgets:
            t = (w.get('type') or '').lower()
            if t not in ('scroll', 'list', 'listitemgroup', 'grid'):
                continue
            nums = re.findall(r'\d+', w.get('bounds', '') or '')
            if len(nums) < 4:
                continue
            x1, y1, x2, y2 = (int(n) for n in nums[:4])
            if y2 - y1 < 80 or x2 - x1 < 80:
                continue
            key = (x1, y1, x2, y2)
            if key in seen:
                continue
            seen.add(key)
            cands.append(key)
        cands.sort(key=lambda b: -(b[3] - b[1]))
        return cands

    # ---------- 底层滑动 ----------

    def _do_swipe(self, x: int, y1: int, y2: int, duration: int) -> bool:
        """执行一次 hdc uiInput swipe，返回是否成功。"""
        import subprocess
        cmd = ['hdc']
        if self.device:
            cmd += ['-t', self.device]
        cmd += ['shell', 'uitest', 'uiInput', 'swipe',
                str(x), str(y1), str(x), str(y2), str(duration)]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
            return r.returncode == 0
        except Exception:
            return False

    def _screen_wh(self) -> tuple:
        """屏幕宽高 (px)，带缓存。仅当 daemon 已连接时用 get_display_size()（~0.006s）；
        否则走快速 dump 的 get_screen_bounds()（纯 hdc ~0.45s），再失败回退 (1260, 2720)。

        注意：不能为取尺寸而新建 hypium 连接（~2.3s），得不偿失。"""
        if self._screen_cache is not None:
            return self._screen_cache
        w, h = None, None
        driver = self._driver  # 仅复用已存在的连接
        if driver is not None:
            try:
                size = driver.get_display_size()
                if size and len(size) >= 2 and size[0] > 0 and size[1] > 0:
                    w, h = int(size[0]), int(size[1])
            except Exception:
                w, h = None, None
        if w is None:
            a = self._dump_and_load("screenwh")
            if a:
                sb = a.get_screen_bounds()
                if sb:
                    w, h = sb[2], sb[3]
                a.cleanup()
        if not w or not h:
            w, h = 1260, 2720
        self._screen_cache = (w, h)
        return w, h

    def _swipe_page_up_in(self, cb) -> bool:
        """在指定容器 bounds 内上滑一屏（内容下滚）。"""
        x = (cb[0] + cb[2]) // 2
        y1 = int(cb[1] + (cb[3] - cb[1]) * 0.80)
        y2 = int(cb[1] + (cb[3] - cb[1]) * 0.25)
        return self._do_swipe(x, y1, y2, 600)

    def _swipe_page_up_fullscreen(self) -> bool:
        """整屏中线上滑一屏（兜底，不依赖容器识别）。"""
        w, h = self._screen_wh()
        return self._do_swipe(w // 2, int(h * 0.80), int(h * 0.25), 600)

    def _swipe_small_up_in(self, cb) -> bool:
        """在指定容器内小幅上滑（约 1/4 容器高），修正目标被底边截断。"""
        x = (cb[0] + cb[2]) // 2
        y1 = int(cb[1] + (cb[3] - cb[1]) * 0.78)
        y2 = int(cb[1] + (cb[3] - cb[1]) * 0.62)
        return self._do_swipe(x, y1, y2, 400)

    def _swipe_small_up_fullscreen(self) -> bool:
        """整屏中线小幅上滑（兜底）。"""
        w, h = self._screen_wh()
        return self._do_swipe(w // 2, int(h * 0.78), int(h * 0.62), 400)

    # ---------- 自适应滚动 ----------

    def _adaptive_scroll(self, working_region, settle_interval):
        """执行一次"向下滚动"，自适应选有效区域并用内容签名验证真实位移。

        依次尝试：已验证区域 → 候选竖向容器(按高度) → 整屏兜底。
        只有当内容签名发生变化才算滚动成功（避免在不可滚区域误判"到底"）。

        返回 (moved: bool, 新内容签名, 生效区域)。
        """
        import time
        a = self._dump_and_load("scrollstep")
        if not a:
            return False, None, working_region
        base_sig = self._scroll_signature(a.widgets)
        candidates = []
        if working_region:
            candidates.append(working_region)
        for cb in self._find_scroll_candidates(a):
            if cb not in candidates:
                candidates.append(cb)
        a.cleanup()

        for cb in candidates:
            if not self._swipe_page_up_in(cb):
                continue
            time.sleep(settle_interval)
            a2 = self._dump_and_load("scrollstep2")
            if not a2:
                continue
            sig = self._scroll_signature(a2.widgets)
            a2.cleanup()
            if sig != base_sig:
                return True, sig, cb

        # 兜底：整屏中线滑动（容器识别可能漏掉/误选时仍能尝试）
        if self._swipe_page_up_fullscreen():
            time.sleep(settle_interval)
            a3 = self._dump_and_load("scrollstep3")
            if a3:
                sig = self._scroll_signature(a3.widgets)
                a3.cleanup()
                if sig != base_sig:
                    return True, sig, None
        return False, base_sig, working_region

    def _scroll_ancestor_of(self, ws, w) -> Optional[tuple]:
        """沿 parent_index 向上，返回目标最近的竖向滚动容器 bounds。

        用于"修正目标被底边截断"时，把小幅滑动准确落在目标所属的滚动容器内
        （应对页面只有部分区域可滚动的场景）。
        """
        import re
        idx = None
        for i, x in enumerate(ws):
            if x is w:
                idx = i
                break
        if idx is None:  # 跨 dump 对象不同 → 按 类型+bounds+文本 匹配
            for i, x in enumerate(ws):
                if (x.get('bounds') == w.get('bounds')
                        and x.get('text') == w.get('text')
                        and x.get('type') == w.get('type')):
                    idx = i
                    break
        if idx is None:
            return None
        p = ws[idx].get('parent_index')
        depth = 0
        while p is not None and depth < 60 and 0 <= p < len(ws):
            pw = ws[p]
            t = (pw.get('type') or '').lower()
            if t in ('scroll', 'list', 'listitemgroup', 'grid'):
                nums = re.findall(r'\d+', pw.get('bounds', '') or '')
                if len(nums) >= 4:
                    return (int(nums[0]), int(nums[1]), int(nums[2]), int(nums[3]))
            p = pw.get('parent_index')
            depth += 1
        return None

    def _is_bottom_clipped(self, w: dict, screen_h: int, margin: int = 80) -> bool:
        """判断控件是否被屏幕底部截断（bottom 贴/超屏底，只露出残影）。"""
        import re
        nums = re.findall(r'\d+', w.get('bounds', '') or '')
        if len(nums) < 4:
            return False
        y2 = int(nums[3])
        return y2 > screen_h - margin

    @staticmethod
    def _match_in_widgets(widgets, text) -> Optional[dict]:
        """在 widget 列表中按文本包含匹配（text + hint），返回首个匹配。"""
        tl = text.lower()
        for w in widgets:
            if (tl in (w.get('text', '') or '').lower()
                    or tl in (w.get('hint', '') or '').lower()):
                return w
        return None

    def _ensure_match_fully_visible(self, text: str, settle_interval: float,
                                    max_adjust: int = 6) -> tuple:
        """确保已找到的目标完整显示在屏幕内（不被底边截断）。

        被截断时在**目标所属的滚动容器**内小幅上滑并重新查找，直到：
        - 目标 bottom 脱离屏底区域 → (True, 最终控件)
        - 内容不再移动（到底）或超过 max_adjust 次 → (False, 最终控件)
        """
        import time
        a = self._dump_and_load("vfix")
        if not a:
            return True, None
        sb = a.get_screen_bounds()
        screen_h = sb[3] if sb else 2720
        ws = a.widgets
        w = self._match_in_widgets(ws, text)
        if w is None:
            a.cleanup()
            return True, None
        region = self._scroll_ancestor_of(ws, w)
        clipped = self._is_bottom_clipped(w, screen_h)
        a.cleanup()
        if not clipped:
            return True, w

        last_sig = None
        for _ in range(max_adjust):
            ok = (self._swipe_small_up_in(region) if region
                  else self._swipe_small_up_fullscreen())
            if not ok:
                break
            time.sleep(settle_interval)
            a2 = self._dump_and_load("vfix2")
            if not a2:
                break
            ws2 = a2.widgets
            sig = self._scroll_signature(ws2)
            sb2 = a2.get_screen_bounds()
            screen_h = sb2[3] if sb2 else screen_h
            w2 = self._match_in_widgets(ws2, text)
            a2.cleanup()
            if w2 is None:
                break
            w = w2
            if not self._is_bottom_clipped(w, screen_h):
                return True, w
            if last_sig is not None and sig == last_sig:
                break  # 内容不再移动 → 已到列表底
            last_sig = sig
        return False, w

    def scroll_find(self, text: str, max_swipes: int = 8,
                    settle_interval: float = 1.0) -> bool:
        """智能滚动查找：
        1. 先不滚动，在当前屏幕轮询（应对异步渲染）——首屏 2 次、滚动后 1 次，
           每次轮询间隔 settle_interval（原实现首屏 4 次/滚动后 2 次，dump 偏多）
        2. 找不到才滚动，自适应选择有效滚动区域（候选竖向容器 → 整屏兜底），
           并用"内容签名是否变化"判断是否真的滚动（避免在不可滚区域误判到底）
        3. 找到目标后，若其被屏幕底边截断（只露出残影），在目标所属滚动
           容器内继续小幅上滑，确保目标完整显示在屏幕内
        """
        import time
        working_region = None
        for i in range(max_swipes + 1):
            polls = 2 if i == 0 else 1
            for p in range(polls):
                matched, _ = self._fuzzy_match_widgets(text)
                if matched:
                    visible, w = self._ensure_match_fully_visible(
                        text, settle_interval)
                    if w is None:
                        w = matched[0]
                    cx = self._parse_bounds_center(w.get('bounds', ''))
                    if visible:
                        print(f"✅ 找到 '{text}' (第{i}屏): "
                              f"type={w.get('type', '')} bounds={w.get('bounds', '')} "
                              f"center={cx} 共{len(matched)}个匹配")
                        return True
                    print(f"❌ 找到 '{text}' 但在屏幕底部被截断，"
                          f"滚动到底仍未完整显示: bounds={w.get('bounds', '')}")
                    return False
                if p < polls - 1:
                    time.sleep(settle_interval)
            if i >= max_swipes:
                break
            moved, _sig, working_region = self._adaptive_scroll(
                working_region, settle_interval)
            if not moved:
                print(f"❌ 列表已到底，未找到 '{text}'")
                return False
            time.sleep(settle_interval)
        print(f"❌ 已滚动 {max_swipes} 屏，未找到 '{text}'")
        return False

    def screenshot(self, save_path: str) -> bool:
        import subprocess
        import os
        # 快路径：纯 hdc screenCap + recv（约 0.6s，无需 hypium 连接）
        device_path = '/data/local/tmp/_screenshot_tmp.png'
        cmd = ['hdc']
        if self.device:
            cmd += ['-t', self.device]
        try:
            r1 = subprocess.run(cmd + ['shell', 'uitest', 'screenCap', '-p', device_path],
                                capture_output=True, text=True, timeout=30)
            if r1.returncode == 0:
                r2 = subprocess.run(cmd + ['file', 'recv', device_path, save_path],
                                    capture_output=True, text=True, timeout=30)
                # 复用固定远端临时文件（下次 screenCap 覆盖），省去一次 rm 调用（~0.3s）
                if r2.returncode == 0 and os.path.exists(save_path) \
                        and os.path.getsize(save_path) > 0:
                    print(f"✅ 截图已保存: {save_path}")
                    return True
                print(f"❌ 拉取截图失败: {r2.stderr or r2.stdout}")
            else:
                print(f"❌ 截图失败: {r1.stderr or r1.stdout}")
        except Exception as e:
            print(f"❌ 截图失败: {e}")
        # 回退：daemon capture_screen（单次调用；仅支持 jpeg 后缀）
        driver = self._get_driver()
        if driver is not None and save_path.lower().endswith((".jpg", ".jpeg")):
            try:
                driver.capture_screen(save_path)
                if os.path.exists(save_path) and os.path.getsize(save_path) > 0:
                    print(f"✅ 截图已保存: {save_path}")
                    return True
            except Exception:
                pass
        return False

    def aa_start(self, uri: str, action: str = "ohos.want.action.viewData",
                 bundle_name: str = "com.cmcc.DigitalHome",
                 ability_name: Optional[str] = None,
                 module_name: Optional[str] = None,
                 params: Optional[dict] = None,
                 operation: str = "",
                 expectations: Optional[dict] = None,
                 skip_before: bool = False,
                 fresh_before: bool = False,
                 auto_recover: bool = True) -> bool:
        """通过 deep link 启动应用（aa start 命令，走统一裁决管线）

        Args:
            uri: deep link URI（如 cmcc://digitalhome/speakingTest）
            action: Want 的 action（默认 ohos.want.action.viewData）
            bundle_name: 目标 bundleName
            ability_name: 目标 abilityName（可选）
            module_name: 目标 moduleName（可选）
            params: 额外参数字典（可选，支持 int/bool/str 类型）
            operation: 操作描述
            expectations: 期望断言字典
            skip_before: 跳过 before-dump
            fresh_before: 强制 fresh before-dump（禁用历史基准复用）
            auto_recover: 被弹窗挡住时自动清理并重试一次

        Returns:
            裁决+断言是否全部成功
        """
        import subprocess

        # 构建 aa start 命令
        cmd = ["shell", "aa", "start"]
        if action:
            cmd.extend(["-A", action])
        if uri:
            cmd.extend(["-U", uri])
        if bundle_name:
            cmd.extend(["-b", bundle_name])
        if ability_name:
            cmd.extend(["-a", ability_name])
        if module_name:
            cmd.extend(["-m", module_name])

        # 处理额外参数
        if params:
            for key, value in params.items():
                if isinstance(value, bool):
                    cmd.extend(["--pb", key, str(value).lower()])
                elif isinstance(value, int):
                    cmd.extend(["--pi", key, str(value)])
                else:
                    cmd.extend(["--ps", key, str(value)])

        # 执行 hdc 命令
        hdc_cmd = ["hdc"]
        if self.device:
            hdc_cmd.extend(["-t", self.device])
        hdc_cmd.extend(cmd)

        def action_fn():
            try:
                r = subprocess.run(hdc_cmd, capture_output=True, text=True,
                                   timeout=30)
            except subprocess.TimeoutExpired:
                print("❌ aa start 超时")
                return False
            if r.returncode != 0 or "error" in (r.stdout or "").lower():
                print(f"❌ aa start 失败: {r.stdout or r.stderr}")
                return False
            # 等待页面加载（比常规操作更长的稳定窗）
            time.sleep(1)
            return True

        desc = operation or f"aa start {uri}"
        pipeline = ActionPipeline(self, auto_recover=auto_recover)
        return pipeline.run(action_fn, desc, expectations=expectations,
                            skip_before=skip_before, fresh_before=fresh_before)


class BatchRunner:
    """批量执行测试用例脚本

    单进程内复用 hypium driver，并复用上一步的 after-state 作为下一步的 before-state，
    将每步的 dump 次数从 2 次降为 1 次（首步除外）。
    支持桥接步骤（navigate/login 等，通过 TcpBridge 直连 App），
    与 UI 操作步骤混合编排成完整用例。

    脚本 JSON 格式::

        {
          "name": "用例名称",
          "steps": [
            {
              "action": "navigate",
              "params": {"page": "MainPage"},
              "desc": "导航到首页",
              "expect": {"timeout": 8}
            },
            {
              "action": "click_by_text",
              "params": {"text": "登录"},
              "desc": "点击登录",
              "expect": {"route": "MainPage", "text_exists": ["首页"], "timeout": 5},
              "stop_on_fail": false
            }
          ]
        }
    """

    # 桥接操作（TcpBridge 直连 App；每步独立建连用完即关，避免 fport 冲突）
    BRIDGE_ACTIONS = {
        'navigate', 'navigate_back', 'login', 'logout',
        'get_user_info', 'get_route', 'query_devices', 'click_device_card',
    }
    # 会改变 UI 的桥接操作：执行后刷新 diff 历史基准
    UI_MUTATING_BRIDGE_ACTIONS = {'navigate', 'navigate_back', 'login',
                                   'logout', 'click_device_card'}

    # 录制产出 ({cmd, args}) 到脚本 step 的映射；坐标族由 Hdc 引擎回放
    RECORD_ACTION_MAP: dict = {
        'ui back': ('go_back', ()),
        'ui click-by-text': ('click_by_text', ('text',)),
        'ui click-by-id': ('click_by_id', ('key',)),
        'ui long-click-by-text': ('long_click_by_text', ('text',)),
        'ui click': ('click', ('x', 'y')),
        'ui double-click': ('double_click', ('x', 'y')),
        'ui long-click': ('long_click', ('x', 'y')),
        'ui swipe': ('swipe', ('x1', 'y1', 'x2', 'y2')),
        'ui input': ('text_input', ('text',)),
        'aa start': ('aa_start', ('uri', 'bundle_name', 'ability_name')),
    }
    COORD_ACTIONS = {'click', 'double_click', 'long_click', 'swipe',
                     'text_input', 'key_back'}

    def __init__(self, device: Optional[str] = None,
                 history_dir: Optional[str] = None,
                 engine_type: str = 'hypium'):
        self.device = device
        self.history_dir = history_dir
        self.engine_type = engine_type
        self.engine = None
        self.results: list = []

    def _ensure_engine(self, engine_type: str):
        """按需创建引擎（lazy；录制含坐标动作时自动切 Hdc 引擎）"""
        if self.engine is not None and self.engine_type == engine_type:
            return self.engine
        if self.engine is not None and hasattr(self.engine, 'close'):
            self.engine.close()
        self.engine_type = engine_type
        if engine_type == 'hypium':
            self.engine = HypiumEngine(device=self.device,
                                       history_dir=self.history_dir)
        else:
            self.engine = HdcUITestEngine(device=self.device,
                                          history_dir=self.history_dir)
        return self.engine

    def _recorded_to_step(self, rec: dict) -> Optional[dict]:
        """把录制的一条步骤 (cmd, args) 转成脚本 step（未知动作跳过）"""
        cmd = (rec.get('cmd') or '').strip()
        if cmd not in self.RECORD_ACTION_MAP:
            print(f"⚠️ 跳过无法回放的动作: {cmd!r}")
            return None
        action, keys = self.RECORD_ACTION_MAP[cmd]
        args = rec.get('args') or []
        params = dict(zip(keys, args))
        return {
            'action': action, 'params': params,
            'desc': (cmd + ' ' + ' '.join(str(a) for a in args)).rstrip(),
            'stop_on_fail': True,
        }

    def _normalize_script(self, script: dict) -> dict:
        """兼容两类输入：{name, steps} 完整脚本 与 [{cmd, args}] 录制清单"""
        if isinstance(script, list):
            steps = [s for s in (self._recorded_to_step(r) for r in script)
                     if s is not None]
            uses_coord = any(s['action'] in self.COORD_ACTIONS for s in steps)
            self._ensure_engine('hdc' if uses_coord else 'hypium')
            return {'name': '录制脚本自动回放', 'steps': steps}
        steps = script.get('steps', [])
        uses_coord = any(s.get('action') in self.COORD_ACTIONS for s in steps)
        self._ensure_engine('hdc' if uses_coord else self.engine_type)
        return script

    def _ensure_start_state(self, start: Optional[dict]) -> Optional[str]:
        """回放前置状态校验 + 恢复（脚本可选 start/setup 字段）

        支持字段：
            start:
              route: str | [str]   期望路由（字符串=栈中任一层包含该页名；列表=栈末尾连续匹配）
              text: [str]          期望当前页存在的文本（全部命中）
              max_backs: int       校验失败时允许的有界返回次数（默认 3）
              recover: str | dict  恢复策略：'back'（默认，有限次返回）或
                                    {'navigate': '<page>'}（bridge 导航直达）

        Returns:
            None 表示前置状态就绪；否则返回失败原因字符串。
        """
        if not start:
            return None
        from engines.diff_engine import WidgetTreeDiff
        route = start.get('route')
        texts = start.get('text') or []
        if isinstance(texts, str):
            texts = [texts]
        max_backs = int(start.get('max_backs', 3))
        recover = start.get('recover', 'back')

        def _check() -> Optional[str]:
            """当前页是否满足前置条件：满足返回 None，否则返回原因"""
            route_reason = self._check_route(route)
            if route_reason:
                return route_reason
            return self._check_texts(texts)

        reason = _check()
        if reason is None:
            return None

# 校验失败 → 按策略恢复
        if isinstance(recover, dict) and recover.get('navigate'):
            page = recover['navigate']
            bundle = recover.get('bundle') or start.get(
                'bundle', 'com.cmcc.DigitalHome')
            ability = recover.get('ability') or start.get('ability', 'EntryAbility')
            print(f"🔧 前置状态不匹配（{reason}），尝试 bridge 导航到 {page} ...")
            try:
                from utils.hdc import detect_device_id
                import sys as _sys
                _sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
                from bridge.tcp_bridge import TcpBridge
                dev = self.device or detect_device_id()
                b = TcpBridge(device=dev)
                try:
                    r = b.call('navigate', {'route': page})
                finally:
                    b.close()
                if (r or {}).get('success'):
                    time.sleep(1)
                    if _check() is None:
                        self._refresh_history()
                        print("✅ 已导航到前置页面")
                        return None
                return f"导航到 {page} 后仍不满足前置条件"
            except Exception as nav_ex:
                # 导航连接失败（App 未运行常触发）→ 先 aa start 启动再重试一次
                print(f"ℹ️  bridge 导航失败: {nav_ex}，尝试先启动应用 {bundle} ...")
                import subprocess
                hdc_cmd = ["hdc"]
                if self.device:
                    hdc_cmd.extend(["-t", self.device])
                hdc_cmd.extend(["shell", "aa", "start", "-b", bundle,
                                "-a", ability])
                try:
                    subprocess.run(hdc_cmd, capture_output=True, text=True,
                                   timeout=30)
                except Exception as start_ex:
                    return f"应用启动失败: {start_ex}"
                time.sleep(3)
                try:
                    b = TcpBridge(device=dev)
                    try:
                        r = b.call('navigate', {'route': page})
                    finally:
                        b.close()
                    if (r or {}).get('success'):
                        time.sleep(1)
                        if _check() is None:
                            self._refresh_history()
                            print("✅ 已启动并导航到前置页面")
                            return None
                        return f"启动后导航到 {page} 仍不满足前置条件"
                    return f"启动后导航到 {page} 失败"
                except Exception as retry_ex:
                    # 启动后仍无 bridge（应用未内置 TCP server）→ 回退到有界返回
                    print(f"ℹ️  启动后 bridge 仍不可用: {retry_ex}，回退到有界返回恢复")
                    return self._recover_by_back(start, texts, route, max_backs,
                                                 reason)
        return self._recover_by_back(start, texts, route, max_backs, reason)

    def _check_route(self, route) -> Optional[str]:
        """校验当前路由是否满足前置 route 条件；满足返回 None，否则返回原因"""
        if not route:
            return None
        from engines.diff_engine import WidgetTreeDiff
        cur = WidgetTreeDiff.get_current_route(self.device)
        if not cur:
            return f"获取路由失败，期望含 '{route}'"
        if isinstance(route, str):
            if route not in cur:
                return f"当前路由 {cur} 不包含 '{route}'"
        else:
            if list(cur)[-len(route):] != list(route):
                return f"当前路由 {cur} 末尾不匹配 {route}"
        return None

    def _check_texts(self, texts) -> Optional[str]:
        """校验当前页是否包含全部前置文本；满足返回 None，否则返回原因"""
        if not texts:
            return None
        a = self.engine._dump_and_load("start_check")
        if a is None:
            return "获取控件树失败，无法校验前置文本"
        try:
            missing = [t for t in texts
                       if not any(t in (w.get('text', '') or '')
                                  for w in a.widgets)]
        finally:
            a.cleanup()
        if missing:
            return f"当前页缺少文本: {', '.join(missing)}"
        return None

    def _recover_by_back(self, start, texts, route, max_backs, reason):
        """有界返回恢复：受 ActionPipeline 桌面护栏保护，到桌面即停"""
        print(f"🔧 前置状态不匹配（{reason}），尝试有界返回恢复（最多 {max_backs} 次）...")
        attempts = 0
        for i in range(max_backs):
            attempts = i + 1
            backed = self._execute_step(
                'go_back', {}, f"前置恢复返回[{i + 1}]", {}, skip_before=False)
            if not backed:
                print(f"↩️ 返回被拦截（桌面护栏/栈底），停止恢复")
                break
            if self._check_texts(texts) is None:
                self._refresh_history()
                print(f"✅ 返回 {i + 1} 次后到达前置页面")
                return None
        return f"前置状态校验失败（已尝试 {attempts} 次返回恢复）: {reason}"

    def run(self, script: dict) -> dict:
        """执行脚本，返回结果摘要

        支持脚本顶层可选字段 start/setup（前置状态校验与恢复），见
        _ensure_start_state 文档。
        """
        script = self._normalize_script(script)
        name = script.get('name', '未命名脚本')
        steps = script.get('steps', [])

        print(f"\n{'#' * 60}")
        print(f"# 批量执行: {name} ({len(steps)} 步)")
        print(f"{'#' * 60}\n")

        all_passed = True

        # 前置状态校验（脚本可选 start/setup 字段）失败 → 整脚本判定失败
        start_state = script.get('start') or script.get('setup')
        if start_state:
            start_reason = self._ensure_start_state(start_state)
            if start_reason:
                print(f"\n❌ 前置状态校验失败: {start_reason}")
                print("ACTION_VERDICT: PRECONDITION_FAILED | "
                      f"reason={start_reason}")
                self.results.append((f"前置状态校验确认失败: {start_reason}", False))
                self._print_summary(name)
                if hasattr(self.engine, 'close'):
                    self.engine.close()
                return {
                    'name': name,
                    'total': len(self.results),
                    'passed': 0,
                    'failed': len(self.results),
                    'all_passed': False,
                }
        for i, step in enumerate(steps, 1):
            step_desc = step.get('desc', f"步骤 {i}")
            action = step.get('action')
            params = step.get('params', {})
            expect = step.get('expect', {})
            stop_on_fail = step.get('stop_on_fail', False)

            print(f"\n{'=' * 60}")
            print(f"📋 步骤 {i}/{len(steps)}: {step_desc}")
            print(f"{'=' * 60}")

            if action in self.BRIDGE_ACTIONS:
                passed = self._execute_bridge_step(action, params, step_desc,
                                                   expect)
            else:
                skip_before = i > 1
                passed = self._execute_step(action, params, step_desc,
                                             expect, skip_before)
            self.results.append((step_desc, passed))

            if not passed:
                all_passed = False
                if stop_on_fail:
                    print(f"\n⚠️ 步骤 {i} 失败，停止执行（stop_on_fail）")
                    break

        self._print_summary(name)

        if hasattr(self.engine, 'close'):
            self.engine.close()

        return {
            'name': name,
            'total': len(self.results),
            'passed': sum(1 for _, p in self.results if p),
            'failed': sum(1 for _, p in self.results if not p),
            'all_passed': all_passed,
        }

    def _execute_step(self, action: str, params: dict, desc: str,
                      expect: dict, skip_before: bool) -> bool:
        """执行单个步骤"""
        e = self.engine
        kw = {'operation': desc,
              'expectations': expect if expect else None,
              'skip_before': skip_before}

        if isinstance(e, HypiumEngine):
            dispatch = {
                'click_by_text': lambda: e.click_by_text(
                    params['text'], index=params.get('index'), **kw),
                'click_by_id': lambda: e.click_by_id(params['key'], **kw),
                'click_by_type': lambda: e.click_by_type(params['widget_type'], **kw),
                'double_click_by_text': lambda: e.double_click_by_text(params['text'], **kw),
                'long_click_by_text': lambda: e.long_click_by_text(params['text'], **kw),
                'input_by_text': lambda: e.input_by_text(
                    params['target'], params['text'], **kw),
                'input_by_type': lambda: e.input_by_type(
                    params['widget_type'], params['text'], **kw),
                'swipe_direction': lambda: e.swipe_direction(
                    params['direction'], params.get('distance', 60), **kw),
                'go_back': lambda: e.go_back(**kw),
                'screenshot': lambda: e.screenshot(params['path']),
                'check_component': lambda: e.check_component(
                    text=params.get('text'), key=params.get('key'),
                    widget_type=params.get('type')),
                'check_dialog': lambda: e.check_dialog(params.get('type', 'Dialog')),
                'aa_start': lambda: e.aa_start(
                    uri=params['uri'],
                    action=params.get('action', 'ohos.want.action.viewData'),
                    bundle_name=params.get('bundle_name', 'com.cmcc.DigitalHome'),
                    ability_name=params.get('ability_name'),
                    module_name=params.get('module_name'),
                    params=params.get('params'),
                    **kw),
            }
        else:
            dispatch = {
                'click': lambda: e.click(params['x'], params['y'], **kw),
                'double_click': lambda: e.double_click(params['x'], params['y'], **kw),
                'long_click': lambda: e.long_click(params['x'], params['y'], **kw),
                'swipe': lambda: e.swipe(
                    params['x1'], params['y1'], params['x2'], params['y2'], **kw),
                'text_input': lambda: e.text_input(params['text'], **kw),
                'key_back': lambda: e.key_back(**kw),
                'go_back': lambda: e.key_back(**kw),
            }

        if action not in dispatch:
            print(f"❌ 未知操作类型: {action}")
            return False

        try:
            return dispatch[action]()
        except KeyError as ex:
            print(f"❌ 缺少参数: {ex}")
            return False
        except Exception as ex:
            print(f"❌ 执行异常: {ex}")
            return False

    def _execute_bridge_step(self, action: str, params: dict, desc: str,
                             expect: Optional[dict]) -> bool:
        """执行桥接步骤（navigate/login 等，通过 TcpBridge 直连 App）

        每步独立创建 TcpBridge、用完即关：避免与 WidgetTreeDiff.get_current_route
        的临时 bridge 产生 fport 冲突。
        """
        import json as _json
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from bridge.tcp_bridge import TcpBridge
        from utils.hdc import detect_device_id

        device_id = self.device or detect_device_id()
        if not device_id:
            print("❌ 未检测到 hdc 设备（hdc list targets 确认连接，或设 HARMONY_DEVICE_ID）")
            return False

        expect_to_check = expect
        bridge = TcpBridge(device=device_id)
        try:
            if action == 'navigate':
                page = params.get('page')
                if not page:
                    print("❌ navigate 步骤缺少 params.page")
                    return False
                result = bridge.navigate(page, params.get('nav_params'))
                if not result.get('success'):
                    print(f"❌ 导航失败: {result.get('message', '未知错误')}")
                    return False
                # 默认断言路由已跳转（轮询 5s），可被 expect 覆盖/扩展
                expect_to_check = {'route': page, 'timeout': 5}
                expect_to_check.update(expect or {})
            elif action == 'navigate_back':
                before_route = bridge.get_current_route()
                bridge.navigate_back()
                # 轮询等待路由栈稳定（最多 5s，连续两次一致视为稳定），避免瞬时状态误判
                import time as _time
                deadline = _time.time() + 5
                stable_route = None
                last_route = object()  # 哨兵：与任何列表都不相等
                while _time.time() < deadline:
                    after_route = bridge.get_current_route()
                    if after_route == last_route:
                        stable_route = after_route
                        break
                    last_route = after_route
                    _time.sleep(1)
                if stable_route is None:
                    stable_route = last_route if isinstance(last_route, list) else None

                if stable_route and before_route is not None \
                        and stable_route != before_route:
                    print(f"✅ 返回上一页: {before_route[-1]} ⟶ {stable_route[-1]}")
                elif not stable_route:
                    print("❌ 返回后路由栈为空，App 可能已退出")
                    print("ACTION_VERDICT: BACK_INEFFECTIVE | reason=App 已退出 | "
                          "suggestion=重启应用后重新导航")
                    return False
                elif before_route is not None and len(before_route) <= 1:
                    print(f"⚠️ 已在路由栈底（{before_route[-1]}），返回无上级页面")
                else:
                    print("❌ 返回未生效：路由栈无变化（返回可能被拦截）")
                    print("ACTION_VERDICT: BACK_INEFFECTIVE | "
                          "reason=路由栈前后一致 | "
                          "suggestion=用 get_route 确认栈状态或 navigate 目标页")
                    return False
            elif action == 'login':
                phone = params.get('phone')
                if not phone:
                    print("❌ login 步骤缺少 params.phone")
                    return False
                result = bridge.login(phone)
                if not result.get('success'):
                    print(f"❌ 登录失败: {result.get('message', '未知错误')}")
                    return False
                print(f"✅ 登录成功: {phone}")
            elif action == 'logout':
                bridge.logout()
                print("✅ 退出登录成功")
            elif action == 'get_user_info':
                result = bridge.get_user_info()
                print("👤 用户信息:")
                print(_json.dumps(result, ensure_ascii=False, indent=2))
            elif action == 'get_route':
                route = bridge.get_current_route()
                print("📍 当前路由栈:")
                for j, r in enumerate(route, 1):
                    print(f"   {j}. {r}")
            elif action == 'query_devices':
                filter_keys = ('device_category', 'device_name', 'device_id',
                               'device_class', 'device_status',
                               'platform_type', 'device_type_id')
                filters = {k: params[k] for k in filter_keys if k in params}
                result = bridge.query_devices(**filters)
                if not result.get('success'):
                    print(f"❌ 查询设备失败: {result.get('message', '未知错误')}")
                    return False
                devices = result.get('devices', [])
                print(f"✅ 查询成功，共 {result.get('totalCount', len(devices))} 个设备:")
                for j, d in enumerate(devices, 1):
                    print(f"   {j}. [{d.get('deviceId', 'N/A')}] "
                          f"{d.get('deviceName', '未知')} - {d.get('statusDesc', '')}")
            elif action == 'click_device_card':
                name = params.get('name')
                if not name:
                    print("❌ click_device_card 步骤缺少 params.name")
                    return False
                result = bridge.click_device_card(name)
                if not result.get('success'):
                    print(f"❌ 设备卡片点击失败: {result.get('message', '未知错误')}")
                    return False
                print(f"✅ 设备卡片点击成功: {name}")
        finally:
            bridge.close()

        ok = True
        if expect_to_check:
            ok = self._run_bridge_expectations(expect_to_check)
        if action in self.UI_MUTATING_BRIDGE_ACTIONS:
            self._refresh_history()
        return ok

    def _run_bridge_expectations(self, expect: dict) -> bool:
        """桥接步骤断言（复用轮询机制；不支持 no_change）"""
        if expect.get('no_change'):
            print("⚠️  桥接步骤不支持 no_change 断言，已忽略")
            expect = {k: v for k, v in expect.items() if k != 'no_change'}
            if not expect:
                return True
        return check_expectations_with_polling(
            expect,
            initial_widgets=None,
            dump_fn=lambda: self.engine._dump_and_load("bridge_poll"),
            cleanup_fn=lambda a: a.cleanup(),
            device=self.device)

    def _refresh_history(self):
        """刷新 diff 历史基准

        桥接步骤改变了 UI 后，把当前状态存为新的 before 基准，
        保证后续 skip_before 步骤的差异比较正确。
        """
        analyzer = self.engine._dump_and_load("bridge")
        if analyzer is not None:
            self.engine._save_and_cleanup(analyzer, save_route=True)

    def _print_summary(self, name: str):
        print(f"\n{'#' * 60}")
        print(f"# 执行结果: {name}")
        print(f"{'#' * 60}")
        for i, (desc, passed) in enumerate(self.results, 1):
            icon = "✅" if passed else "❌"
            print(f"  {icon} 步骤 {i}: {desc}")
        total = len(self.results)
        passed = sum(1 for _, p in self.results if p)
        failed = total - passed
        print(f"\n  总计: {total} 步 | 通过: {passed} | 失败: {failed}")
        print(f"  {'✅ 全部通过' if failed == 0 else '❌ 存在失败'}")
        print(f"{'#' * 60}")
