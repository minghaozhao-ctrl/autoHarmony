#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""统一裁决层（verdict）

所有 UI 动作（坐标/语义化/桥接）执行后统一走同一条裁决管线，
把"意外情况发现 → 上报 → 纠错"收敛到一个地方，替代散落各处的补丁式判定。

核心产物：一行机器可读结论
    ACTION_VERDICT: <STATUS> | reason=<原因> | suggestion=<下一步>

决策树（按优先级）：
    1. 崩溃检查   进程死亡 → CRASHED；accessibilityId 骤降 → RESTARTED
    2. 路由变化   → SUCCESS
    3. 控件树变化 → SUCCESS
    4. 无变化 + 有覆盖层弹窗 → BLOCKED_BY_DIALOG（附弹窗文本/可点按钮建议）
    5. 无变化 + 无弹窗 → NO_CHANGE（back 类动作标记 BACK_INEFFECTIVE）

exit code 语义（硬失败）：除 SUCCESS 外均返回 1；
仅当 --expect-no-change 命中时 NO_CHANGE 视为通过。
"""
import time
from enum import Enum
from typing import Callable, List, Optional

from engines.diff_engine import WidgetTreeDiff


class VerdictStatus(Enum):
    SUCCESS = "SUCCESS"
    NO_CHANGE = "NO_CHANGE"
    BLOCKED_BY_DIALOG = "BLOCKED_BY_DIALOG"
    BACK_INEFFECTIVE = "BACK_INEFFECTIVE"
    CRASHED = "CRASHED"
    RESTARTED = "RESTARTED"


# 裁决 → 是否允许继续（硬失败语义：仅 SUCCESS 为真）
def verdict_ok(status: VerdictStatus, expectations: Optional[dict] = None) -> bool:
    if status == VerdictStatus.SUCCESS:
        return True
    # 期望无变化且确实无变化 → 视为通过
    if status == VerdictStatus.NO_CHANGE and expectations and expectations.get('no_change'):
        return True
    return False


class Verdict:
    """一次动作的裁决结果"""

    def __init__(self, status: VerdictStatus, reason: str = "",
                 suggestion: str = ""):
        self.status = status
        self.reason = reason
        self.suggestion = suggestion

    def ok(self, expectations: Optional[dict] = None) -> bool:
        return verdict_ok(self.status, expectations)

    def line(self) -> str:
        parts = [f"ACTION_VERDICT: {self.status.value}"]
        if self.reason:
            parts.append(f"reason={self.reason}")
        if self.suggestion:
            parts.append(f"suggestion={self.suggestion}")
        return " | ".join(parts)

    def print(self):
        icon = {
            VerdictStatus.SUCCESS: "✅",
            VerdictStatus.NO_CHANGE: "⚠️",
            VerdictStatus.BLOCKED_BY_DIALOG: "🪟",
            VerdictStatus.BACK_INEFFECTIVE: "↩️",
            VerdictStatus.CRASHED: "💥",
            VerdictStatus.RESTARTED: "🔄",
        }[self.status]
        print(f"{icon} {self.line()}")


# ==================== 弹窗识别（供裁决与自愈共用） ====================

# 常见确认按钮（自愈点击用，正向优先）
DISMISS_BUTTONS = [
    "同意", "允许", "仅在使用中允许", "知道了", "我知道了", "好的",
    "确定", "继续", "立即开启", "去开启", "授权",
    "跳过", "暂不", "稍后", "以后再说", "以后再說", "取消", "关闭", "暂不开启",
]


def find_overlays(widgets: List[dict], screen_bounds=None) -> List[dict]:
    """当前控件树中的覆盖层（排除 Toast）"""
    return [ov for ov in WidgetTreeDiff.detect_overlays(widgets, screen_bounds)
            if 'toast' not in (ov.get('type', '') or '').lower()]


def overlay_summary(widgets: List[dict], overlay: dict, max_items: int = 6) -> str:
    """覆盖层子树内的可读文本（标题/按钮），用于裁决 reason"""
    from engines import _overlay_texts
    texts = _overlay_texts(widgets, overlay)
    return " / ".join(texts[:max_items])


def find_dismiss_button(widgets: List[dict], overlay: dict) -> Optional[dict]:
    """在覆盖层后代中按优先级找可点击的确认按钮"""
    from engines import _is_descendant_index
    ov_idx = None
    for i, w in enumerate(widgets):
        if w is overlay:
            ov_idx = i
            break
    if ov_idx is None:
        return None
    descendants = [i for i in range(len(widgets))
                   if i != ov_idx and _is_descendant_index(widgets, i, ov_idx)]
    for label in DISMISS_BUTTONS:
        for i in descendants:
            w = widgets[i]
            if label.lower() in (w.get('text', '') or '').lower():
                return w
    return None


# ==================== 裁决决策树 ====================

def judge(crash_detector, after_analyzer, report,
          before_route: Optional[List[str]], after_route: Optional[List[str]],
          is_back: bool = False, looks_like_desktop: bool = False,
          internal_restarted: bool = False,
          expectations: Optional[dict] = None) -> Verdict:
    """统一裁决决策树

    Args:
        crash_detector: CrashDetector
        after_analyzer: 操作后的控件树分析器
        report: 差异报告（route_changed / changes）
        before_route / after_route: 路由前后快照
        is_back: 是否为返回类动作（无变化时标记 BACK_INEFFECTIVE）
        looks_like_desktop: 操作后是否疑似退到系统桌面
        internal_restarted: 是否检测到内部重启（须在 record_acc 更新基线前判定）
        expectations: 断言字典（dialog 断言存在时弹窗出现视为预期结果）

    Returns:
        Verdict
    """
    # 1. 崩溃 / 内部重启（优先级最高）
    if crash_detector is not None:
        if crash_detector.has_crashed():
            return Verdict(
                VerdictStatus.CRASHED,
                reason="App 进程已退出或出现 faultlog",
                suggestion="重启应用后重新导航（app restart）")
    if internal_restarted:
        return Verdict(
            VerdictStatus.RESTARTED,
            reason="accessibilityId 骤降，App 内部重启（UI 已重建）",
            suggestion="页面状态已重置，重新导航到目标页（app navigate）")

    widgets = after_analyzer.widgets

    # 2. 路由变化 → 成功
    if report is not None and report.route_changed:
        return Verdict(VerdictStatus.SUCCESS, reason="路由发生变化")

    # 3. 控件树变化 → 成功
    if report is not None and report.changes:
        return Verdict(VerdictStatus.SUCCESS,
                       reason=f"检测到 {len(report.changes)} 处控件变化")

    # 4/5. 无变化：先看是否被弹窗挡住
    screen = after_analyzer.get_screen_bounds() if after_analyzer else None
    overlays = find_overlays(widgets, screen)
    if overlays:
        # 期望弹窗出现（--expect-dialog）时，弹窗出现即预期结果，不判阻挡
        if expectations and expectations.get('dialog'):
            return Verdict(VerdictStatus.SUCCESS, reason="弹窗已按预期出现")
        top = overlays[-1]
        summary = overlay_summary(widgets, top)
        btn = find_dismiss_button(widgets, top)
        suggestion = (f"点击弹窗按钮 '{btn.get('text', '')}' 关闭后重试"
                      if btn else "手动关闭弹窗（ui dismiss-dialogs）后重试")
        reason = f"操作无变化且存在覆盖层弹窗[{top.get('type', '')}]"
        if summary:
            reason += f": {summary}"
        return Verdict(VerdictStatus.BLOCKED_BY_DIALOG,
                       reason=reason, suggestion=suggestion)

    # 无变化且无弹窗
    if is_back:
        extra = "，且疑似已退到系统桌面" if looks_like_desktop else ""
        return Verdict(
            VerdictStatus.BACK_INEFFECTIVE,
            reason=f"返回后页面无变化{extra}（可能已在栈底或返回被拦截）",
            suggestion="用 app route 确认路由栈；如需强制回退可 app navigate 目标页")

    return Verdict(
        VerdictStatus.NO_CHANGE,
        reason="操作已执行但未检测到页面变化（可能被遮挡/控件不可点/已是目标状态）",
        suggestion="先复核页面实际状态（tree dump）再决定是否重试")


# ==================== 统一动作管线 ====================

# 复用历史基准的新鲜度阈值（秒）：超过则不信任，强制 fresh dump
HISTORY_FRESH_SECONDS = 120


class ActionPipeline:
    """统一动作管线：前置快照 → 动作 → 后置快照 → 裁决 → 自愈 → 断言

    两个引擎（HdcUITestEngine / HypiumEngine）通过 duck typing 复用本管线，
    只需提供：_dump_and_load / _save_and_cleanup / manager / diff /
    crash_detector / device / _get_current_route_cached。

    生命周期只写一份，消除双管线散落补丁。
    """

    def __init__(self, engine, auto_recover: bool = True):
        self.engine = engine
        self.auto_recover = auto_recover

    # ---- 前置快照（支持复用历史基准，省一次 dump）----

    def _before_snapshot(self, skip_before: bool, fresh_before: bool):
        """返回 (before_widgets, before_route, before_sig, reused)

        复用条件：未强制 fresh，且历史存在、新鲜、路由与当前一致。
        """
        from engines import _compute_page_signature
        e = self.engine

        if not fresh_before:
            hist_widgets = e.manager.load_history()
            if hist_widgets and self._history_fresh() and self._route_matches():
                hist_route = e.manager.load_route_history()
                sig = _compute_page_signature(hist_widgets)
                return hist_widgets, hist_route, sig, True

        # fresh dump
        before = e._dump_and_load("before")
        if before is None:
            return None, None, None, False
        sig = _compute_page_signature(before.widgets)
        e._save_and_cleanup(before, save_route=True)
        return before.widgets, e.manager.load_route_history(), sig, False

    def _history_fresh(self) -> bool:
        """历史基准文件是否在新鲜度阈值内"""
        import os
        hf = self.engine.manager.history_file
        if not os.path.exists(hf):
            return False
        try:
            age = time.time() - os.path.getmtime(hf)
            return age <= HISTORY_FRESH_SECONDS
        except OSError:
            return False

    def _route_matches(self) -> bool:
        """历史路由是否与当前路由一致（一致才敢复用基准）"""
        saved = self.engine.manager.load_route_history()
        if saved is None:
            return False
        current = WidgetTreeDiff.get_current_route(self.engine.device)
        if current is None:
            # 拿不到当前路由时保守处理：允许复用（避免额外 dump 失去意义）
            return True
        return saved == current

    # ---- 主流程 ----

    def run(self, action_fn: Callable[[], bool], desc: str,
            expectations: Optional[dict] = None,
            is_back: bool = False, check_exit: bool = False,
            skip_before: bool = False, fresh_before: bool = False) -> bool:
        """执行动作并裁决

        Args:
            action_fn: 执行动作原语的可调用对象，返回 raw 成功与否（可重复调用以自愈重试）
            desc: 操作描述
            expectations: 断言字典
            is_back: 返回类动作（无变化标记 BACK_INEFFECTIVE）
            check_exit: 返回类动作检测是否退到桌面
            skip_before: 强制跳过 before-dump（批量模式复用上一步 after）
            fresh_before: 强制 fresh before-dump（禁用历史复用）

        Returns:
            裁决 + 断言是否全部通过（决定 exit code）
        """
        from engines import (_compute_page_signature, print_page_state_summary,
                             check_expectations_with_polling,
                             auto_handle_dialogs, STATE_TYPE_LABELS)
        e = self.engine
        print(f"执行: {desc}")

        # 1. 前置快照
        if skip_before:
            before_widgets = e.manager.load_history()
            before_route = e.manager.load_route_history()
            before_sig = (_compute_page_signature(before_widgets)
                          if before_widgets else None)
        else:
            before_widgets, before_route, before_sig, _reused = \
                self._before_snapshot(skip_before=False, fresh_before=fresh_before)
            if before_widgets is None:
                return False

        # 2. 崩溃基线 + 记录操作前 acc
        e.crash_detector.prime_baseline()
        if before_widgets:
            e.crash_detector.record_acc(before_widgets)

        # 3. 执行动作
        try:
            raw_ok = action_fn()
        except Exception as ex:
            print(f"❌ {desc} 执行异常: {ex}")
            raw_ok = False
        if not raw_ok:
            print(f"❌ {desc} 执行失败")
            e.crash_detector.detect_and_report()
            return False
        print(f"✅ {desc} 执行完成")

        # 4. Toast/动画稳定窗
        time.sleep(0.4)

        # 5. 后置快照 + 裁决（含一次自愈重试）
        verdict, after, report = self._verdict_once(
            before_widgets, before_route, before_sig, desc,
            is_back, check_exit, expectations)
        if after is None:
            return False

        # 自愈：被弹窗挡住时清弹窗并重试一次
        # （期望弹窗出现时不判阻挡，不会进入自愈）
        if (verdict.status == VerdictStatus.BLOCKED_BY_DIALOG
                and self.auto_recover):
            print("🔧 检测到弹窗阻挡，尝试自动清理并重试一次...")
            auto_handle_dialogs(e.device)
            try:
                retry_ok = action_fn()
            except Exception as ex:
                print(f"❌ 自愈重试异常: {ex}")
                retry_ok = False
            if retry_ok:
                time.sleep(0.4)
                verdict, after, report = self._verdict_once(
                    before_widgets, before_route, before_sig,
                    f"{desc} [自愈重试]", is_back, check_exit, expectations)
                if after is None:
                    return False

        # 6. 输出裁决结论
        print()
        verdict.print()

        # 7. 页面状态摘要 + 保存历史
        same_page = None
        if before_sig is not None:
            same_page = (before_sig == _compute_page_signature(after.widgets))
        print_page_state_summary(after, device=e.device,
                                 prev_widgets=before_widgets,
                                 route=e._get_current_route_cached(),
                                 same_page=same_page)
        e._save_and_cleanup(after, save_route=True)

        # 8. 断言
        ok = verdict.ok(expectations)
        if expectations:
            if not check_expectations_with_polling(
                    expectations, report=report,
                    initial_widgets=after.widgets,
                    dump_fn=lambda: e._dump_and_load("poll"),
                    cleanup_fn=lambda a: a.cleanup(),
                    device=e.device):
                ok = False
        return ok

    def _verdict_once(self, before_widgets, before_route, before_sig,
                      desc, is_back, check_exit, expectations=None):
        """dump 后置状态 → 差异比较 → 崩溃/重启检查 → 裁决

        Returns:
            (verdict, after_analyzer, report)；dump 失败时 (None, None, None)
        """
        from engines import _compute_page_signature
        e = self.engine

        after = e._dump_and_load("after")
        if after is None:
            return Verdict(VerdictStatus.CRASHED,
                           reason="获取控件树失败", suggestion="检查应用是否存活"), None, None

        # 内部重启检测必须在 record_acc 更新基线之前（否则基线被覆盖无法对比）
        internal_restarted = e.crash_detector.detect_internal_restart(after.widgets)
        # 记录操作后 acc（供后续检测）
        e.crash_detector.record_acc(after.widgets)
        # 主动崩溃检查（进程存活 + faultlog，~0.1s）
        e.crash_detector.detect()

        after_route = e._get_current_route_cached()
        after_sig = _compute_page_signature(after.widgets)

        # 差异比较（路由变化或内容变化）
        report = None
        if before_sig is not None and before_sig != after_sig:
            from diff_engine import ChangeReport
            print(f"🧭 页面变化: {before_sig}  ⟶  {after_sig}")
            after.overview()
            report = ChangeReport(desc)
            report.route_changed = True
        else:
            report = e._compare_with_history(after, desc)

        looks_desktop = check_exit and self._looks_like_desktop(after)
        verdict = judge(e.crash_detector, after, report,
                        before_route, after_route,
                        is_back=is_back, looks_like_desktop=looks_desktop,
                        internal_restarted=internal_restarted,
                        expectations=expectations)

        # Toggle 异步生效复查：无变化且页面有状态控件时延迟复查一次
        if verdict.status in (VerdictStatus.NO_CHANGE,
                              VerdictStatus.BACK_INEFFECTIVE):
            if self._has_state_controls(after.widgets):
                print("⏳ 检测到状态控件，1.5s 后复查是否异步生效...")
                time.sleep(1.5)
                recheck = e._dump_and_load("recheck")
                if recheck is not None:
                    re_report = e._compare_with_history(recheck, f"{desc} [异步复查]")
                    if re_report is not None and (re_report.changes
                                                  or re_report.route_changed):
                        print("✅ 操作延迟生效（状态异步更新），以复查结果为准")
                        recheck_route = e._get_current_route_cached()
                        verdict = judge(e.crash_detector, recheck, re_report,
                                        before_route, recheck_route,
                                        is_back=is_back,
                                        looks_like_desktop=looks_desktop,
                                        expectations=expectations)
                        after.cleanup()
                        return verdict, recheck, re_report
                    recheck.cleanup()

        return verdict, after, report

    @staticmethod
    def _has_state_controls(widgets) -> bool:
        from engines import STATE_TYPE_LABELS
        return any((w.get('type', '') or '').lower() in STATE_TYPE_LABELS
                   for w in widgets)

    @staticmethod
    def _looks_like_desktop(analyzer) -> bool:
        texts = [w for w in analyzer.widgets if (w.get('text') or '').strip()]
        return len(analyzer.widgets) < 15 and len(texts) < 3
