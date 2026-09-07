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


class _OperationTimeout(Exception):
    """操作超时异常"""


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


# 弹窗自动处理：常见确认按钮（按优先级排序，正向按钮优先于中性按钮）
# 与 verdict.DISMISS_BUTTONS 保持一致（verdict 为单一事实来源）
AUTO_DIALOG_BUTTONS = [
    "同意", "允许", "仅在使用中允许", "知道了", "我知道了", "好的",
    "确定", "继续", "立即开启", "去开启", "授权",
    "跳过", "暂不", "稍后", "以后再说", "以后再說", "取消", "关闭", "暂不开启",
]


def auto_handle_dialogs(device: Optional[str] = None, max_rounds: int = 3) -> bool:
    """检测并自动关闭系统级弹窗（权限/云存/引导等覆盖层）

    dump 控件树 → 找 Overlay 类型容器（Dialog/Sheet/Popup 等）→
    在弹窗后代中找常见确认按钮 → 坐标点击 → 循环直到无弹窗。

    注意：只处理覆盖层弹窗，页面级隐私政策弹窗（非 overlay）不在此范围。

    Args:
        device: 设备 ID
        max_rounds: 最大处理轮数（防止无限弹窗）

    Returns:
        是否处理过至少一个弹窗
    """
    import time
    from verdict import find_overlays, find_dismiss_button

    engine = HdcUITestEngine(device=device)
    analyzer = WidgetTreeAnalyzer(json_file="auto_dialog.json", device=device)
    handled = False
    try:
        for _ in range(max_rounds):
            success, msg = analyzer.dump_layout()
            if not success:
                print(f"⚠️  弹窗检测跳过（获取控件树失败: {msg}）")
                break
            if not analyzer.load_tree():
                break
            widgets = analyzer.widgets

            overlays = find_overlays(widgets, analyzer.get_screen_bounds())
            if not overlays:
                break

            btn = find_dismiss_button(widgets, overlays[-1])
            if btn is None:
                # 有弹窗但没有认识的按钮，不盲目点击
                print(f"ℹ️  检测到弹窗但未识别已知按钮（overlay: "
                      f"{overlays[-1].get('type')}），跳过自动处理")
                break

            import re as _re
            nums = _re.findall(r'\d+', btn.get('bounds', '') or '')
            if len(nums) < 4:
                break
            x = (int(nums[0]) + int(nums[2])) // 2
            y = (int(nums[1]) + int(nums[3])) // 2
            label = btn.get('text', '')
            print(f"🔔 检测到弹窗，自动点击 '{label}' ({x}, {y})")
            if not engine.click(x, y, f"自动处理弹窗: 点击 '{label}'",
                                auto_recover=False):
                break
            handled = True
            time.sleep(1.5)
    finally:
        analyzer.cleanup()
    if handled:
        print("✅ 弹窗自动处理完成")
    return handled


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

    def _hdc_cmd(self, cmd_parts: List[str], timeout: int = 30) -> tuple:
        """执行 hdc 命令"""
        cmd = ["hdc"]
        if self.device:
            cmd.extend(["-t", self.device])
        cmd.extend(cmd_parts)
        return run_hdc_command(cmd, timeout)

    def _dump_and_load(self, tag: str = "layout") -> Optional[WidgetTreeAnalyzer]:
        """从设备获取控件树并加载

        dumpLayout 失败时自动检测闪退。

        Returns:
            加载成功的 WidgetTreeAnalyzer，失败返回 None
        """
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
                                 is_back: bool = False) -> bool:
        """通用流程：前置快照 → 执行 HDC uiInput 操作 → 统一裁决管线

        Args:
            ui_input_args: `uitest uiInput` 之后的子命令和参数
            desc: 操作描述
            action_name: 操作名称（用于错误提示，如 "点击"）
            expectations: 期望断言字典（route/text_exists/text_gone/no_change/dialog）
            skip_before: 跳过 before-dump（批量模式复用上一步 after-state）
            fresh_before: 强制 fresh before-dump（禁用历史基准复用）
            auto_recover: 被弹窗挡住时自动清理并重试一次
            is_back: 返回类动作（无变化标记 BACK_INEFFECTIVE）

        Returns:
            裁决+断言是否全部通过（决定 exit code）
        """
        def action_fn():
            success, output = self._hdc_cmd(
                ["shell", "uitest", "uiInput"] + ui_input_args
            )
            if not success:
                print(f"❌ HDC{action_name}失败: {output}")
                return False
            return True

        pipeline = ActionPipeline(self, auto_recover=auto_recover)
        return pipeline.run(action_fn, desc, expectations=expectations,
                            is_back=is_back, skip_before=skip_before,
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
            fresh_before=fresh_before, auto_recover=auto_recover)

    def double_click(self, x: int, y: int, operation: str = "",
                     expectations: Optional[dict] = None,
                     skip_before: bool = False, fresh_before: bool = False,
                     auto_recover: bool = True) -> bool:
        """双击坐标并比较变化"""
        desc = operation or f"双击 ({x}, {y})"
        return self._execute_hdc_and_compare(
            ["doubleClick", str(x), str(y)], desc, "双击",
            expectations=expectations, skip_before=skip_before,
            fresh_before=fresh_before, auto_recover=auto_recover)

    def long_click(self, x: int, y: int, operation: str = "",
                   expectations: Optional[dict] = None,
                   skip_before: bool = False, fresh_before: bool = False,
                   auto_recover: bool = True) -> bool:
        """长按坐标并比较变化"""
        desc = operation or f"长按 ({x}, {y})"
        return self._execute_hdc_and_compare(
            ["longClick", str(x), str(y)], desc, "长按",
            expectations=expectations, skip_before=skip_before,
            fresh_before=fresh_before, auto_recover=auto_recover)

    def swipe(self, x1: int, y1: int, x2: int, y2: int,
              operation: str = "", expectations: Optional[dict] = None,
              skip_before: bool = False, fresh_before: bool = False,
              auto_recover: bool = True) -> bool:
        """滑动并比较变化"""
        desc = operation or f"滑动 ({x1},{y1}) → ({x2},{y2})"
        return self._execute_hdc_and_compare(
            ["swipe", str(x1), str(y1), str(x2), str(y2)], desc, "滑动",
            expectations=expectations, skip_before=skip_before,
            fresh_before=fresh_before, auto_recover=auto_recover)

    def text_input(self, text: str, operation: str = "",
                   expectations: Optional[dict] = None,
                   skip_before: bool = False, fresh_before: bool = False,
                   auto_recover: bool = True) -> bool:
        """输入文本并比较变化"""
        desc = operation or f'输入文本: "{text}"'
        return self._execute_hdc_and_compare(
            ["text", text], desc, "输入",
            expectations=expectations, skip_before=skip_before,
            fresh_before=fresh_before, auto_recover=auto_recover)

    def key_back(self, operation: str = "",
                 expectations: Optional[dict] = None,
                 skip_before: bool = False, fresh_before: bool = False,
                 auto_recover: bool = True) -> bool:
        """返回键并比较变化（无变化标记 BACK_INEFFECTIVE）"""
        desc = operation or "按下返回键"
        return self._execute_hdc_and_compare(
            ["keyEvent", "Back"], desc, "返回键",
            expectations=expectations, skip_before=skip_before,
            fresh_before=fresh_before, auto_recover=auto_recover,
            is_back=True)


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

    @staticmethod
    def _suppress_logging():
        import sys
        import logging
        sys.log_mode = "no_console"
        logging.disable(logging.INFO)

    def _get_driver(self):
        if self._driver is None:
            self._suppress_logging()
            from hypium import UiDriver
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
        """通过 hypium UiTree.dump_to_file() 获取控件树（daemon 不冲突）"""
        import time
        driver = self._get_driver()
        tmp_path = os.path.join(
            tempfile.gettempdir(),
            f'hypium_{tag}_{int(time.time() * 1000000)}.json')
        try:
            driver.UiTree.dump_to_file(tmp_path)
        except Exception as e:
            print(f"❌ 获取控件树失败: {e}")
            self.crash_detector.detect_and_report()
            return None
        analyzer = WidgetTreeAnalyzer(json_file=tmp_path, device=self.device)
        analyzer._temp_file = tmp_path  # 统一由 cleanup() 清理临时文件
        if not analyzer.load_tree():
            analyzer.cleanup()
            return None
        return analyzer

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

    def click_by_text(self, text: str, operation: str = "",
                      expectations: Optional[dict] = None,
                      skip_before: bool = False,
                      index: Optional[int] = None,
                      fresh_before: bool = False,
                      auto_recover: bool = True) -> bool:
        from hypium import BY
        desc = operation or f"点击文本为 '{text}' 的控件"
        if index is not None:
            # 指定索引：直接 dump 控件树按文本匹配列表取第 N 个坐标点击
            return self._click_by_text_fuzzy(text, desc, index=index,
                                             expectations=expectations,
                                             skip_before=skip_before,
                                             fresh_before=fresh_before,
                                             auto_recover=auto_recover)
        result = self._execute_hypium_and_compare(
            lambda: self._get_driver().touch(BY.text(text)), desc,
            expectations=expectations, skip_before=skip_before,
            fresh_before=fresh_before, auto_recover=auto_recover)
        if result:
            return True
        if expectations or skip_before:
            return False
        print(f"ℹ️  精确匹配失败，尝试文本包含搜索...")
        return self._click_by_text_fuzzy(text, desc,
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
        analyzer = WidgetTreeAnalyzer(json_file="fuzzy_dump.json", device=self.device)
        success, msg = analyzer.dump_layout()
        if not success:
            print(f"❌ 获取控件树失败: {msg}")
            return False
        if not analyzer.load_tree():
            analyzer.cleanup()
            return False
        matched = [w for w in analyzer.widgets
                   if text.lower() in (w.get('text', '') or '').lower()
                   or text.lower() in (w.get('hint', '') or '').lower()]
        if not matched:
            print(f"❌ 未找到文本/占位符包含 '{text}' 的控件")
            analyzer.cleanup()
            return False
        idx = index or 0
        if idx >= len(matched):
            print(f"❌ 文本 '{text}' 匹配 {len(matched)} 个控件，索引 {idx} 越界（0-{len(matched)-1}）")
            analyzer.cleanup()
            return False
        w = matched[idx]
        coords = self._parse_bounds_center(w.get('bounds', ''))
        if not coords:
            print(f"❌ 无法解析控件坐标: {w.get('bounds', '')}")
            analyzer.cleanup()
            return False
        x, y = coords
        print(f"  匹配控件 [{idx}/{len(matched)}]: type={w.get('type', '')} text={w.get('text', '')}")
        print(f"  点击坐标: ({x}, {y})")
        analyzer.cleanup()
        engine = HdcUITestEngine(device=self.device, history_dir=self.history_dir)
        return engine.click(x, y, desc, expectations=expectations,
                            skip_before=skip_before, fresh_before=fresh_before,
                            auto_recover=auto_recover)

    @staticmethod
    def _parse_bounds_center(bounds_str: str) -> Optional[tuple]:
        """解析 bounds 字符串 '[x1,y1][x2,y2]'，返回中心坐标 (x, y)"""
        import re
        nums = re.findall(r'\d+', bounds_str)
        if len(nums) >= 4:
            x1, y1, x2, y2 = int(nums[0]), int(nums[1]), int(nums[2]), int(nums[3])
            return ((x1 + x2) // 2, (y1 + y2) // 2)
        return None

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
        from hypium import BY
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
        analyzer = WidgetTreeAnalyzer(json_file="id_dump.json", device=self.device)
        success, msg = analyzer.dump_layout()
        if not success:
            print(f"❌ 获取控件树失败: {msg}")
            return False
        if not analyzer.load_tree():
            analyzer.cleanup()
            return False
        # 优先精确匹配 id，其次按 key 字段，最后 id 包含匹配
        widgets = analyzer.widgets
        matched = ([w for w in widgets if (w.get('id', '') or '') == key]
                   or [w for w in widgets if (w.get('attributes', {}).get('key', '') or '') == key]
                   or [w for w in widgets if key.lower() in (w.get('id', '') or '').lower()])
        if not matched:
            analyzer.cleanup()
            return False
        w = matched[0]
        coords = self._parse_bounds_center(w.get('bounds', ''))
        if not coords:
            print(f"❌ 无法解析控件坐标: {w.get('bounds', '')}")
            analyzer.cleanup()
            return False
        x, y = coords
        print(f"  按 id 匹配控件: type={w.get('type', '')} id={w.get('id', '')} bounds={w.get('bounds', '')}")
        print(f"  点击坐标: ({x}, {y})")
        analyzer.cleanup()
        engine = HdcUITestEngine(device=self.device, history_dir=self.history_dir)
        return engine.click(x, y, desc, expectations=expectations,
                            skip_before=skip_before, fresh_before=fresh_before,
                            auto_recover=auto_recover)

    def click_by_type(self, widget_type: str, operation: str = "",
                      expectations: Optional[dict] = None,
                      skip_before: bool = False,
                      fresh_before: bool = False,
                      auto_recover: bool = True) -> bool:
        from hypium import BY
        desc = operation or f"点击类型为 '{widget_type}' 的控件"
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
        from hypium import BY
        desc = operation or f"双击文本为 '{text}' 的控件"
        return self._execute_hypium_and_compare(
            lambda: self._get_driver().double_click(BY.text(text)), desc,
            expectations=expectations, skip_before=skip_before,
            fresh_before=fresh_before, auto_recover=auto_recover)

    def long_click_by_text(self, text: str, operation: str = "",
                           expectations: Optional[dict] = None,
                           skip_before: bool = False,
                           fresh_before: bool = False,
                           auto_recover: bool = True) -> bool:
        from hypium import BY
        desc = operation or f"长按文本为 '{text}' 的控件"
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
        from hypium import BY
        desc = operation or f"在 '{target_text}' 输入框中输入 '{input_text}'"
        result = self._execute_hypium_and_compare(
            lambda: self._get_driver().input_text(
                self._get_driver().find_component(BY.text(target_text)), input_text), desc,
            expectations=expectations, skip_before=skip_before,
            fresh_before=fresh_before, auto_recover=auto_recover)
        if result:
            return True
        # 精确 text 匹配失败：输入框的常见目标其实是占位符 hint（如"请输入手机号码"）。
        # 第二次尝试复用同一 before 基准（首次未生效，页面未变）。
        print("ℹ️  text 精确匹配失败，尝试按 hint 占位符匹配...")
        return self._execute_hypium_and_compare(
            lambda: self._get_driver().input_text(
                self._get_driver().find_component(BY.hint(target_text)), input_text), desc,
            expectations=expectations, skip_before=skip_before,
            fresh_before=fresh_before, auto_recover=auto_recover)

    def input_by_type(self, widget_type: str, input_text: str,
                      operation: str = "",
                      expectations: Optional[dict] = None,
                      skip_before: bool = False,
                      fresh_before: bool = False,
                      auto_recover: bool = True) -> bool:
        from hypium import BY
        desc = operation or f"在 {widget_type} 中输入 '{input_text}'"
        return self._execute_hypium_and_compare(
            lambda: self._get_driver().input_text(
                self._get_driver().find_component(BY.type(widget_type)), input_text), desc,
            expectations=expectations, skip_before=skip_before,
            fresh_before=fresh_before, auto_recover=auto_recover)

    def swipe_direction(self, direction: str, distance: int = 60,
                        operation: str = "",
                        expectations: Optional[dict] = None,
                        skip_before: bool = False,
                        fresh_before: bool = False,
                        auto_recover: bool = True) -> bool:
        desc = operation or f"向 {direction} 滑动 {distance}"
        return self._execute_hypium_and_compare(
            lambda: self._get_driver().swipe(direction, distance=distance), desc,
            expectations=expectations, skip_before=skip_before,
            fresh_before=fresh_before, auto_recover=auto_recover)

    def go_back(self, operation: str = "",
                expectations: Optional[dict] = None,
                skip_before: bool = False,
                fresh_before: bool = False,
                auto_recover: bool = True) -> bool:
        desc = operation or "按下返回键"
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
        sig = self._page_signature(analyzer)
        analyzer.cleanup()
        return matched, sig

    def _screen_has_text(self, text: str) -> bool:
        """当前屏幕是否存在含 text 的控件（精确查询 + dump 包含匹配双通道）"""
        from hypium import BY
        try:
            self._get_driver().check_component_exist(BY.text(text), wait_time=1)
            return True
        except Exception:
            pass
        matched, _ = self._fuzzy_match_widgets(text)
        return bool(matched)

    @staticmethod
    def _max_y_of_widgets(widgets) -> int:
        import re
        max_y = 0
        for w in widgets:
            nums = re.findall(r'\d+', w.get('bounds', '') or '')
            if len(nums) >= 4:
                max_y = max(max_y, int(nums[3]))
        return max_y

    def _raw_swipe_page_up(self):
        """上滑一屏（内容向下滚动），失败返回 False"""
        import subprocess
        analyzer = self._dump_and_load("swipegeo")
        width, max_y = 1256, 2720
        if analyzer:
            widgets = analyzer.widgets
            for w in widgets:
                import re
                nums = re.findall(r'\d+', w.get('bounds', '') or '')
                if len(nums) >= 4:
                    width = max(width, int(nums[2]))
            max_y = max(max_y, self._max_y_of_widgets(widgets))
            analyzer.cleanup()
        x = width // 2
        # 避开底部系统返回手势区（起点<=65%屏高）和顶部状态栏（终点>=30%屏高）
        y1, y2 = int(max_y * 0.65), int(max_y * 0.30)
        cmd = ['hdc']
        if self.device:
            cmd += ['-t', self.device]
        cmd += ['shell', 'uitest', 'uiInput', 'swipe',
                str(x), str(int(max_y * 0.8)), str(x), str(int(max_y * 0.25)), '600']
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
            return r.returncode == 0
        except Exception:
            return False

    def scroll_find(self, text: str, max_swipes: int = 8,
                    settle_interval: float = 1.0) -> bool:
        """智能滚动查找：
        1. 先不滚动，在当前屏幕轮询（应对异步渲染，最多 settle_interval*4 秒）
        2. 找不到才每次滚动一屏，滚动后重新轮询
        3. 连续两屏页面签名不变视为到底，停止并报告未找到
        """
        import time
        last_sig = None
        for i in range(max_swipes + 1):
            polls = 4 if i == 0 else 2
            cur_sig = None
            for p in range(polls):
                matched, sig = self._fuzzy_match_widgets(text)
                cur_sig = sig
                if matched:
                    w = matched[0]
                    cx = self._parse_bounds_center(w.get('bounds', ''))
                    print(f"✅ 找到 '{text}' (第{i}屏): "
                          f"type={w.get('type', '')} bounds={w.get('bounds', '')} "
                          f"center={cx} 共{len(matched)}个匹配")
                    return True
                if p < polls - 1:
                    time.sleep(settle_interval)
            if i > 0 and last_sig is not None and cur_sig is not None and cur_sig == last_sig:
                print(f"❌ 列表已到底，未找到 '{text}'")
                return False
            last_sig = cur_sig
            if i < max_swipes:
                if not self._raw_swipe_page_up():
                    print("❌ 滚动执行失败")
                    return False
                time.sleep(settle_interval)
        print(f"❌ 已滚动 {max_swipes} 屏，未找到 '{text}'")
        return False

    def screenshot(self, save_path: str) -> bool:
        import subprocess
        import os
        device_path = '/data/local/tmp/_screenshot_tmp.png'
        cmd = ['hdc']
        if self.device:
            cmd += ['-t', self.device]
        try:
            r1 = subprocess.run(cmd + ['shell', 'uitest', 'screenCap', '-p', device_path],
                                capture_output=True, text=True, timeout=30)
            if r1.returncode != 0:
                print(f"❌ 截图失败: {r1.stderr or r1.stdout}")
                return False
            r2 = subprocess.run(cmd + ['file', 'recv', device_path, save_path],
                                capture_output=True, text=True, timeout=30)
            subprocess.run(cmd + ['shell', 'rm', '-f', device_path],
                           capture_output=True, text=True, timeout=10)
            if r2.returncode == 0:
                print(f"✅ 截图已保存: {save_path}")
                return True
            print(f"❌ 拉取截图失败: {r2.stderr or r2.stdout}")
            return False
        except Exception as e:
            print(f"❌ 截图失败: {e}")
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

    def __init__(self, device: Optional[str] = None,
                 history_dir: Optional[str] = None,
                 engine_type: str = 'hypium'):
        self.device = device
        if engine_type == 'hypium':
            self.engine = HypiumEngine(device=device, history_dir=history_dir)
        else:
            self.engine = HdcUITestEngine(device=device, history_dir=history_dir)
        self.results: list = []

    def run(self, script: dict) -> dict:
        """执行脚本，返回结果摘要"""
        name = script.get('name', '未命名脚本')
        steps = script.get('steps', [])

        print(f"\n{'#' * 60}")
        print(f"# 批量执行: {name} ({len(steps)} 步)")
        print(f"{'#' * 60}\n")

        all_passed = True
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
