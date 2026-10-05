#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""UI 引擎公共层：模块级辅助函数与常量（由原 engines.py 拆分）

包含：控件树 dump 快路径、断言轮询、页面状态摘要、弹窗自动处理等。
"""
import re
import time
from typing import Callable, List, Optional

from engines.diff_engine import WidgetTreeDiff
from engines.logger import archive_dump, log_line

__all__ = [
    '_UID_FLAG_SUPPORT',
    'POLLABLE_KEYS', 'WIDGET_KEYS', 'STATE_TYPE_LABELS', 'INPUT_TYPE_SET',
    'ERROR_TEXT_KEYWORDS', 'ERROR_TEXT_EXCLUDE', 'WHITE_SCREEN_MIN_WIDGETS',
    '_record_page_path', '_archive_dump_file', '_dump_layout_remote_cat',
    'check_expectations_with_polling', '_state_value_text',
    '_sort_text_candidates', '_point_in_bounds', '_print_toggle_list',
    '_nearest_label', '_in_subtree', '_overlay_texts',
    '_compute_page_signature', '_collect_state_items',
    '_collect_input_items', '_delta_changed_lines',
    'print_page_state_summary', 'auto_handle_dialogs',
    '_is_descendant_index',
]


# 设备是否支持 `dumpLayout -e uniqueId`（按设备缓存；模拟器不支持时会回退）。
_UID_FLAG_SUPPORT = {}


def _archive_dump_file(analyzer, device) -> None:
    """归档本次 dump 的本地 JSON 进日志目录，路径挂到 analyzer._archived_path。

    复用差异报告用的同一次 dump 产物（零额外 dump），供 tree dump/show 与
    ui 操作后输出（AI/人定位产物）。
    """
    try:
        dst = archive_dump(getattr(analyzer, '_temp_file', None) or None,
                           "layout", device=device)
        if dst:
            analyzer._archived_path = dst
            log_line("[page] 本次 dump 已归档 device=%s dst=%s" % (device, dst))
    except Exception as e:
        log_line("[err] archive_dump: %s" % e)


def _record_page_path(analyzer, device) -> None:
    """从 dump 的窗口节点属性提取 pagePath，登记到路由缓存（纯 hdc 零开销）。

    app 前台时 dumpLayout 的窗口根节点带 pagePath（如 pages/LaunchPage）；
    桌面/非 app 窗口为空。供 WidgetTreeDiff.get_current_route 回退使用。
    """
    try:
        for w in analyzer.widgets:
            pp = (w.get('attributes') or {}).get('pagePath')
            if pp:
                WidgetTreeDiff.record_page_path(device, pp)
                # 真实 pagePath（区别于文本面包屑），落盘便于事后定位页面
                log_line("[page] pagePath=%s device=%s" % (pp, device))
                return
    except Exception as e:
        log_line("[err] record_page_path: %s" % e)


def _dump_layout_remote_cat(engine, remote) -> tuple:
    """一条 shell 完成 dumpLayout + cat（省一次 hdc 进程 + file recv 往返）

    实测 2.13s → 1.51s（约 -0.6s/次）。兼容不支持 `-e uniqueId` 的设备
    （如 DevEco 模拟器报 "unrecognized option: e"）：先试带 -e，输出非 JSON
    （文件未生成）时回退不带 -e，是否支持按设备缓存避免反复试错。

    Returns:
        (ok: bool, out: str) out 为 JSON 文本（失败时可能为空）
    """
    dev = getattr(engine, 'device', None) or ''

    def _run(extra: str) -> tuple:
        cmd = (f"rm -f {remote}; uitest dumpLayout {extra} -p {remote} "
               f">/dev/null 2>&1; cat {remote} 2>/dev/null")
        return engine._hdc_cmd(["shell", cmd], timeout=30)

    def _is_json(res: tuple) -> bool:
        rok, rout = res
        return bool(rok and rout and rout.lstrip().startswith('{'))

    if _UID_FLAG_SUPPORT.get(dev) is False:
        return _run("")
    res = _run("-e uniqueId")
    if _is_json(res):
        _UID_FLAG_SUPPORT[dev] = True
        return res
    res2 = _run("")
    if _is_json(res2):
        _UID_FLAG_SUPPORT[dev] = False
        return res2
    return res2


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
    from engines.assertions import AssertionChecker

    # timeout 容错（脚本字段笔误传字符串时结构化失败而非 TypeError）
    try:
        timeout = float(expectations.get('timeout', 0) or 0)
    except (TypeError, ValueError):
        print(f"❌ expect.timeout 须为数字，收到: {expectations.get('timeout')!r}")
        return False
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
            if dump_fn is None:
                print("❌ 文本/弹窗断言轮询需要 dump_fn，停止轮询")
                return AssertionChecker.print_results(results)
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
# 说明性文案排除：如"声音异常时视频记录"是功能说明而非报错
ERROR_TEXT_EXCLUDE = ('异常时', '错误时', '失败时', '超时时', '无效时')
# 白屏判定：控件总数低于该值疑似白屏/未渲染完成
WHITE_SCREEN_MIN_WIDGETS = 6


def _state_value_text(w: dict) -> str:
    """把控件状态属性转成可读文本（checked/selected/indeterminate 等）"""
    # 滑块：真实刻度值在 text 里（如 "60.000000"），比 checked 更有信息量
    if (w.get('type', '') or '').lower() == 'slider':
        txt = (w.get('text', '') or '').strip()
        if txt:
            try:
                num = float(txt)
                return str(int(num)) if num == int(num) else f"{num:g}"
            except ValueError:
                return txt
    checked = w.get('checked', '')
    if checked in ('true', 'false'):
        return '开' if checked == 'true' else '关'
    selected = w.get('selected', '')
    if selected in ('true', 'false'):
        return '选中' if selected == 'true' else '未选中'
    return ''


def _sort_text_candidates(matches: List[dict]) -> List[dict]:
    """文本候选排序：最短文本优先 → 面积小优先（叶子节点更可能是目标）

    修复实测误触：'关闭' 曾优先匹配到 '关闭推送时间段' 行容器而非按钮；
    '云端分析' 曾命中列表行容器导致点到开关。
    """
    def key(w: dict):
        txt = (w.get('text', '') or '')
        b = WidgetTreeDiff._parse_bounds(w.get('bounds', ''))
        area = (b[2] - b[0]) * (b[3] - b[1]) if b else 10 ** 9
        return (len(txt), area)
    return sorted(matches, key=key)


def _point_in_bounds(w: dict, x: int, y: int) -> bool:
    """坐标 (x, y) 是否命中控件 bounds 区域"""
    b = WidgetTreeDiff._parse_bounds(w.get('bounds', ''))
    if not b:
        return False
    return b[0] <= x <= b[2] and b[1] <= y <= b[3]


def _print_toggle_list(toggles: List[dict], widgets: List[dict]):
    """打印页面 Toggle 清单（checked/bounds/邻近标签），供坐标定位参考"""
    for i, w in enumerate(toggles):
        checked = (w.get('checked', '') or '').lower() == 'true'
        label = _nearest_label(widgets, w)
        print(f"  #{i} checked={'true' if checked else 'false'} "
              f"bounds={w.get('bounds', '')}"
              + (f" label={label}" if label else ""))


def _nearest_label(widgets: List[dict], w: dict, max_dist: int = 200) -> str:
    """为控件找就近文本标签

    优先同一行左侧的 Text（如开关行的标题，不受 max_dist 限制，取 x 最右者）；
    没有时回退到左侧/上方 max_dist 内的最近 Text。
    用于把纯状态控件（如 Toggle 无文本）与页面上的文字标签关联起来。
    """
    b = WidgetTreeDiff._parse_bounds(w.get('bounds', ''))
    if not b:
        return ''
    cy = (b[1] + b[3]) / 2
    same_row, same_row_key = '', None  # 同行标签（优先 y 最近，平手取 x 最右）
    nearby, nearby_d = '', None        # 其他方位回退
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
            key = (abs(t_cy - cy), -tb[2])
            if same_row_key is None or key < same_row_key:
                same_row_key, same_row = key, txt
        else:
            d = abs(b[1] - tb[3])
        if d <= max_dist and (nearby_d is None or d < nearby_d):
            nearby_d, nearby = d, txt
    return same_row or nearby


def _in_subtree(widgets: List[dict], idx: int, root_idx: int) -> bool:
    """判断 idx 节点是否在 root_idx 子树内（委托 _is_descendant_index，
    单一实现，勿再各写一份追溯逻辑）"""
    return _is_descendant_index(widgets, idx, root_idx)


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
    """收集输入框实际内容项：[(key, line, value)]，空框显示占位提示

    隐藏/屏外输入框（如密码点位页用 margin-top:-500 移出屏幕的透明
    TextInput）**同样报告**并标注 [隐藏/屏外]——实测事故：误点输入法键盘
    在隐藏框留下残留字符，导致后续 ui input 拼接成错误密码而误判密码错误，
    而状态摘要从未暴露该框的实际内容。
    """
    items = []
    # 屏幕边界：从根节点 bounds 解析（拿不到时不做屏外标注）
    screen = None
    if widgets:
        m = re.findall(r'-?\d+', str(widgets[0].get('bounds', '') or ''))
        if len(m) >= 4:
            screen = (int(m[0]), int(m[1]), int(m[2]), int(m[3]))
    for w in widgets:
        wt = (w.get('type', '') or '').lower()
        if wt not in INPUT_TYPE_SET:
            continue
        text = (w.get('text', '') or '').strip()
        hint = (w.get('hint', '') or '').strip()
        # 屏外/隐藏判定：bounds 完全或大部分在屏幕外（负 margin 移出）
        offscreen_tag = ""
        if screen is not None:
            m = re.findall(r'-?\d+', str(w.get('bounds', '') or ''))
            if len(m) >= 4:
                left, top, right, bottom = (int(n) for n in m[:4])
                if top >= screen[3] or bottom <= screen[1] \
                        or left >= screen[2] or right <= screen[0] \
                        or top < screen[1] or left < screen[0]:
                    offscreen_tag = " [隐藏/屏外]"
        if not text and not hint and not offscreen_tag:
            continue
        label = _nearest_label(widgets, w)
        label_str = f"「{label}」" if label else ""
        if text:
            val = text
        elif hint:
            val = f"(空 — 占位提示: {hint})"
        else:
            val = "(空)"
        key = (wt, label or hint or w.get('bounds', ''))
        items.append((key, f"  ⌨️ {w.get('type')}{label_str}{offscreen_tag}: "
                           f"\"{val}\"", val))
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
        print(f"  📍 路由: route={' → '.join(route[-3:])}")

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
            if any(x in txt for x in ERROR_TEXT_EXCLUDE):
                continue
            err_lines.append(f"  ⚠️ 错误文案: \"{txt}\"")
            if len(err_lines) >= 3:
                break
    for l in err_lines:
        print(l)

    print("─" * 60)


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
        from .hdc_engine import HdcUITestEngine
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
                top = top_overlay(overlays)
                assert top is not None
                btn = find_dismiss_button(widgets, top)
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
