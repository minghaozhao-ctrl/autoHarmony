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
from engines.logger import log_line


class VerdictStatus(Enum):
    SUCCESS = "SUCCESS"
    ERROR = "ERROR"
    NO_CHANGE = "NO_CHANGE"
    BLOCKED_BY_DIALOG = "BLOCKED_BY_DIALOG"
    PENDING_DIALOG = "PENDING_DIALOG"
    BACK_INEFFECTIVE = "BACK_INEFFECTIVE"
    CRASHED = "CRASHED"
    RESTARTED = "RESTARTED"
    # 点击命中禁用控件（clickable=false/enabled=false）→ 硬失败，
    # 避免连续无效点击被误判为"已是目标状态"（实测事故：锁定期连点 6 次
    # 数字键全无效，仅报 NO_CHANGE 软失败，无法区分"禁用"与"无需操作"）
    CLICK_ON_DISABLED = "CLICK_ON_DISABLED"
    # App 桥接（TcpBridge）不可达：进程消失/端口无响应，命令级兜底裁决
    BRIDGE_UNREACHABLE = "BRIDGE_UNREACHABLE"


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
            VerdictStatus.ERROR: "❌",
            VerdictStatus.NO_CHANGE: "⚠️",
            VerdictStatus.BLOCKED_BY_DIALOG: "🪟",
            VerdictStatus.PENDING_DIALOG: "⚠️",
            VerdictStatus.BACK_INEFFECTIVE: "↩️",
            VerdictStatus.CRASHED: "💥",
            VerdictStatus.RESTARTED: "🔄",
            VerdictStatus.CLICK_ON_DISABLED: "🚫",
            VerdictStatus.BRIDGE_UNREACHABLE: "🔌",
        }[self.status]
        print(f"{icon} {self.line()}")


# ==================== 桌面/应用内识别（供返回护栏共用） ====================

# 系统 UI bundle（桌面/状态栏/锁屏等），widget 树中仅含这些 bundle 时视为已退到桌面。
# 单一事实来源：diff_engine.WidgetTreeDiff.SYSTEM_UI_BUNDLES（此处引用，勿再各自拷贝）
SYSTEM_UI_BUNDLES = set(WidgetTreeDiff.SYSTEM_UI_BUNDLES)


def widgets_have_app_content(widgets: List[dict]) -> bool:
    """判断控件树是否包含 App 自身内容（非系统 UI bundle）。

    返回 True 表示仍在 App 内（存在非系统 bundle 的控件）；
    返回 False 表示已退到桌面/系统界面（仅剩系统 UI bundle 或 bubdle 信息缺失）。
    """
    if not widgets:
        return False
    for w in widgets:
        attrs = w.get('attributes') or {}
        bundle = (attrs.get('bundleName') or '').strip()
        if bundle and bundle not in SYSTEM_UI_BUNDLES:
            return True
    return False


# ==================== 弹窗识别（供裁决与自愈共用） ====================

# 弹窗关闭按钮（**负向/跳过优先**，尽量无副作用关闭）；单一事实来源见
# diff_engine.DIALOG_BUTTON_TEXTS。verdict 与 engines 均引用此表。
DISMISS_BUTTONS = list(WidgetTreeDiff.DIALOG_BUTTON_TEXTS)


def find_overlays(widgets: List[dict], screen_bounds=None) -> List[dict]:
    """当前控件树中的覆盖层（排除 Toast）"""
    return [ov for ov in WidgetTreeDiff.detect_overlays(widgets, screen_bounds)
            if 'toast' not in (ov.get('type', '') or '').lower()]


def top_overlay(overlays: List[dict]) -> Optional[dict]:
    """取视觉最上层的覆盖层（render_order 最大），而非数组末位"""
    if not overlays:
        return None
    return max(overlays, key=lambda w: (w.get('render_order', 0) or 0))


def overlay_summary(widgets: List[dict], overlay: dict, max_items: int = 6) -> str:
    """覆盖层子树内的可读文本（标题/按钮），用于裁决 reason"""
    from engines.helpers import _overlay_texts
    texts = _overlay_texts(widgets, overlay)
    return " / ".join(texts[:max_items])


def _clickable_target(widgets: List[dict], idx: int,
                      overlay_idx: int) -> Optional[int]:
    """把命中文本的控件解析为**可点击目标**：自身可点则用自身，否则沿
    parent_index 上溯到最近的可点击祖先（须仍在 overlay 子树内，到 overlay
    为止）。找不到返回 None。避免点到不可点的标题文本。"""
    cur = idx
    guard = 0
    while cur is not None and guard < 60:
        if not (0 <= cur < len(widgets)):
            return None
        w = widgets[cur]
        if str(w.get('clickable', 'false')).lower() == 'true':
            return cur
        if cur == overlay_idx:
            return None
        cur = w.get('parent_index')
        guard += 1
    return None


def _find_close_x_button(widgets: List[dict], overlay: dict) -> Optional[dict]:
    """找不到文本关闭按钮时的兜底：识别「右上角 X 关闭钮」。

    很多自绘/活动弹窗的关闭钮是无文本的小按钮/图片（如“7天内不再展示”弹窗，
    关闭 X 仅以 position/size 呈现）。启发式（限 overlay 子树内）：
    可点击、无文本、尺寸 36~150px、中心位于容器右上角区域（右 22% 内 + 上 22% 内）
    → 视为关闭钮，命中多个时取最贴右上角者。
    """
    from engines.helpers import _is_descendant_index
    ov_idx = next((i for i, w in enumerate(widgets) if w is overlay), None)
    if ov_idx is None:
        return None
    ob = WidgetTreeDiff._parse_bounds(overlay.get('bounds', ''))
    if not ob:
        return None
    ow, oh = ob[2] - ob[0], ob[3] - ob[1]
    if ow <= 0 or oh <= 0:
        return None
    x_thr = ob[2] - ow * 0.22   # 右上角横向起点
    y_thr = ob[1] + oh * 0.22   # 右上角纵向终点
    best = None                 # (score, index)
    for i, w in enumerate(widgets):
        if i == ov_idx or not _is_descendant_index(widgets, i, ov_idx):
            continue
        if (w.get('text', '') or '').strip():
            continue
        if str(w.get('clickable', 'false')).lower() != 'true':
            continue
        b = WidgetTreeDiff._parse_bounds(w.get('bounds', ''))
        if not b:
            continue
        bw, bh = b[2] - b[0], b[3] - b[1]
        if not (36 <= bw <= 150 and 36 <= bh <= 150):
            continue
        cx, cy = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2
        if cx < x_thr or cy > y_thr:
            continue
        score = (cx - x_thr) + (y_thr - cy)   # 越大越贴右上角
        if best is None or score > best[0]:
            best = (score, i)
    if best is None:
        return None
    w = dict(widgets[best[1]])  # 浅拷贝：带友好的关闭标签，供日志/建议显示
    w['text'] = '关闭(X)'
    return w


def find_dismiss_button(widgets: List[dict], overlay: dict) -> Optional[dict]:
    """在覆盖层后代中按优先级（DISMISS_BUTTONS：负向/跳过优先）找**可点击**的关闭按钮。

    规则：
    1. 只认可点击目标（自身或最近可点击祖先），避免命中不可点的标题文本；
    2. 按 DISMISS_BUTTONS 顺序匹配（负向在前）；
    3. 同词多处命中时，优先 Button 类型、其次文本更短（更接近整串）；
    4. 找不到文本按钮时，兜底识别右上角“X”关闭钮（见 _find_close_x_button）。
    """
    from engines.helpers import _is_descendant_index
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
        hits: List[int] = []
        for i in descendants:
            if label.lower() in (widgets[i].get('text', '') or '').lower():
                tgt = _clickable_target(widgets, i, ov_idx)
                if tgt is None or tgt in hits:
                    continue
                # 只认可 Button 类型的关闭按钮；toggle/复选框（如“不再提醒”）
                # 点了只是勾选偏好、不会关弹窗，不当作关闭按钮 → 走返回键兜底
                if (widgets[tgt].get('type', '') or '').lower() != 'button':
                    continue
                hits.append(tgt)
        if hits:
            def _rank(t: int):
                w = widgets[t]
                return (len((w.get('text', '') or '')), t)
            best = sorted(hits, key=_rank)[0]
            return widgets[best]
    # 固定 ID 关闭按钮：ArkUI 源码写死 .id() 的关闭钮（如通用运营弹窗
    # AFCommonAlertView 的 dialog_cancel_btn）。比文本稳定、比 X 启发式可靠。
    for fid in WidgetTreeDiff.CLOSE_BUTTON_IDS:
        for i in descendants:
            w = widgets[i]
            attrs = w.get('attributes') or {}
            ids = {str(w.get('id', '') or '').strip(),
                   str(attrs.get('id', '') or '').strip(),
                   str(attrs.get('key', '') or '').strip()}
            if fid not in ids:
                continue
            tgt = _clickable_target(widgets, i, ov_idx)
            if tgt is None:
                continue
            cp = dict(widgets[tgt])          # 浅拷贝：带友好标签供日志/建议显示
            if not (cp.get('text', '') or '').strip():
                cp['text'] = '关闭(×)'
            return cp
    # 兜底：无文本关闭按钮时识别右上角 X 关闭钮
    return _find_close_x_button(widgets, overlay)


def overlay_dismiss_hint(widgets: List[dict], overlay: dict) -> str:
    """在覆盖层子树内查找"可关闭语义"的文本（**不限控件类型/可点性**）。

    与 find_dismiss_button 互补：后者只认 Button 类型（用于安全点击），
    这里用于**判定"操作后是否留下未处理的确认弹窗"**——即使关闭按钮是
    自绘 Text（非 Button），也能识别出来。返回命中文案（如 '关闭'），无则 ''。
    """
    from engines.helpers import _is_descendant_index
    ov_idx = None
    for i, w in enumerate(widgets):
        if w is overlay:
            ov_idx = i
            break
    if ov_idx is None:
        return ''
    for label in DISMISS_BUTTONS:
        for i in range(len(widgets)):
            if i == ov_idx or not _is_descendant_index(widgets, i, ov_idx):
                continue
            txt = (widgets[i].get('text', '') or '').strip()
            # 精确匹配才算按钮：子串匹配会把标题正文误判为按钮，
            # 如“关闭推送时间段”“全天关闭”含“关闭”
            if txt and txt.lower() == label.lower():
                return txt
    return ''


# ==================== 点击目标禁用判定（CLICK_ON_DISABLED 预检） ====================

import re as _re


class ClickTargetStatus:
    """点击坐标命中控件的状态判定结果"""

    def __init__(self, hit: bool, disabled: bool, not_clickable: bool,
                 target: Optional[dict] = None,
                 disabled_by: Optional[dict] = None,
                 chain_desc: str = "",
                 disabled_text: str = ""):
        self.hit = hit                  # 是否命中任何控件
        self.disabled = disabled        # 命中集合存在 enabled=false（级联禁用）
        self.not_clickable = not_clickable  # 命中集合无任何 clickable=true
        self.target = target            # DFS 序号最大的命中控件（最具体）
        self.disabled_by = disabled_by  # 触发禁用判定的控件（enabled=false）
        self.chain_desc = chain_desc    # 命中目标摘要（NO_CHANGE 提示用）
        self.disabled_text = disabled_text  # 禁用控件自身或子树的文本（如数字键 '2'）


def _bounds_contain(bounds_str: str, x: int, y: int) -> bool:
    """解析 '[x1,y1][x2,y2]'，判断点是否在区域内"""
    nums = _re.findall(r'-?\d+', str(bounds_str or ''))
    if len(nums) < 4:
        return False
    left, top, right, bottom = (int(n) for n in nums[:4])
    return left <= x < right and top <= y < bottom


def _bounds_area(bounds_str) -> int:
    """解析 '[x1,y1][x2,y2]' 面积；解析失败返回 -1（排序时沉底）"""
    nums = _re.findall(r'-?\d+', str(bounds_str or ''))
    if len(nums) < 4:
        return -1
    left, top, right, bottom = (int(n) for n in nums[:4])
    return max(0, (right - left) * (bottom - top))


def find_click_target_status(widgets: List[dict], x: int, y: int) -> ClickTargetStatus:
    """判定坐标 (x, y) 的点击目标是否被禁用。

    判定规则（保守，避免误报）：
    - 命中 = bounds 包含该点的所有控件；**目标 = 面积最小的命中控件**
      （最具体的实际目标。不能取 DFS 序号最大者：实测锁屏的全屏遮罩
      RelativeContainer 序号大于键盘按钮会选错；也不能因存在 clickable=true
      的全屏父容器就直接放行——锁屏 Stack clickable=true 仍拦不住子键禁用）
    - 目标链（自身 → 祖先）上任何控件 enabled='false' → disabled=True
      （ArkUI enabled(false) 级联禁用整个子树；典型：锁定期数字键
      Button clickable=false enabled=false，点击必然无效）
    - 无 disabled 且链上存在 clickable='true' 且 enabled≠'false' → 正常可点
    - 无 disabled 且链上无可点控件 → not_clickable=True（保持 NO_CHANGE
      原语义，不升级——透明容器/事件穿透等场景无法穷举）
    - 未命中任何控件 → 全 False（不判禁用）
    """
    hits: List[dict] = [w for w in widgets
                        if _bounds_contain(w.get('bounds', ''), x, y)]
    # 过滤零面积装饰元素后取面积最小者；并列时取 DFS 序号大者（更靠内）
    concrete = [w for w in hits if _bounds_area(w.get('bounds', '')) >= 4]
    pool = concrete or hits
    if not pool:
        return ClickTargetStatus(hit=False, disabled=False, not_clickable=False)
    target = min(pool, key=lambda w: (_bounds_area(w.get('bounds', '')),
                                      -(w.get('render_order', 0) or 0)))

    # 沿 parent_index 构建目标链
    chain: List[dict] = [target]
    cur = target.get('parent_index')
    guard = 0
    while cur is not None and 0 <= cur < len(widgets) and guard < 64:
        chain.append(widgets[cur])
        cur = widgets[cur].get('parent_index')
        guard += 1

    # 1) enabled=false 级联禁用：链上出现即判禁用
    disabled_by = next((w for w in chain
                        if str(w.get('enabled', '')).lower() == 'false'), None)
    if disabled_by is not None:
        return ClickTargetStatus(hit=True, disabled=True, not_clickable=False,
                                 target=target, disabled_by=disabled_by,
                                 chain_desc=_describe_click_chain([target]),
                                 disabled_text=_first_descendant_text(widgets, target))

    # 2) 链上存在可点且启用的控件 → 正常
    if any(str(w.get('clickable', '')).lower() == 'true'
           and str(w.get('enabled', '')).lower() != 'false'
           for w in chain):
        return ClickTargetStatus(hit=True, disabled=False, not_clickable=False,
                                 target=target,
                                 chain_desc=_describe_click_chain([target]))

    # 3) 链上全不可点且无禁用 → not_clickable
    return ClickTargetStatus(hit=True, disabled=False, not_clickable=True,
                             target=target,
                             chain_desc=_describe_click_chain([target]))


def _first_descendant_text(widgets: List[dict], node: dict,
                           max_depth: int = 3) -> str:
    """取控件自身或后代（限深）的首个非空 Text（如数字键 Button 的 '2'）"""
    txt = (node.get('text', '') or '').strip()
    if txt:
        return txt[:20]
    try:
        idx = widgets.index(node)
    except ValueError:
        return ""
    out = ""
    for i, w in enumerate(widgets):
        if i == idx or not (w.get('text') or '').strip():
            continue
        cur = w.get('parent_index')
        depth = 0
        while cur is not None and 0 <= cur < len(widgets) and depth <= max_depth:
            if cur == idx:
                out = (w.get('text') or '').strip()[:20]
                break
            cur = widgets[cur].get('parent_index')
            depth += 1
        if out:
            break
    return out


def _describe_click_chain(chain: List[dict]) -> str:
    """命中链摘要：最上层控件的类型/文本/可点/可用（供 NO_CHANGE 提示）"""
    if not chain:
        return ""
    w = chain[0]
    parts = [f"type={w.get('type', '')}"]
    txt = (w.get('text', '') or '').strip()
    if txt:
        parts.append(f"text='{txt[:20]}'")
    parts.append(f"clickable={w.get('clickable', '')}")
    parts.append(f"enabled={w.get('enabled', '')}")
    return ", ".join(parts)


# ==================== 裁决决策树 ====================

def judge(crash_detector, after_analyzer, report,
          is_back: bool = False, looks_like_desktop: bool = False,
          internal_restarted: bool = False,
          expectations: Optional[dict] = None) -> Verdict:
    """统一裁决决策树

    Args:
        crash_detector: CrashDetector
        after_analyzer: 操作后的控件树分析器
        report: 差异报告（route_changed / changes）
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

    # 覆盖层预检：用于识别"操作有变化但留下未处理弹窗"的假成功
    screen0 = after_analyzer.get_screen_bounds()
    overlays0 = [ov for ov in find_overlays(widgets, screen0)
                 if 'toast' not in (ov.get('type', '') or '').lower()]

    # 2. 路由变化 → 成功
    if report is not None and report.route_changed:
        return Verdict(VerdictStatus.SUCCESS, reason="路由发生变化")

    # 3. 控件树变化 → 成功
    if report is not None and report.changes:
        # 期望弹窗出现（--expect-dialog）→ 弹窗出现即预期结果
        if overlays0 and expectations and expectations.get('dialog'):
            return Verdict(VerdictStatus.SUCCESS, reason="弹窗已按预期出现")
        # 有变化但同时留下「可关闭的模态弹窗」→ 操作尚未最终确认，不算干净成功
        # （--auto-handle-dialog 时弹窗会被自动清理，不在此列）
        if overlays0 and not (expectations and expectations.get('auto_dialog')):
            top0 = top_overlay(overlays0)
            assert top0 is not None
            btn0 = find_dismiss_button(widgets, top0)
            hint0 = (btn0.get('text', '') if btn0
                     else overlay_dismiss_hint(widgets, top0))
            if btn0 or hint0:
                summary0 = overlay_summary(widgets, top0)
                reason0 = (f"操作已产生变化，但留有未处理弹窗"
                           f"[{top0.get('type', '')}]")
                if summary0:
                    reason0 += f": {summary0}"
                return Verdict(
                    VerdictStatus.PENDING_DIALOG, reason=reason0,
                    suggestion=(f"确认/处理弹窗按钮 '{hint0}'"
                                "（ui dismiss-dialogs）后复核操作结果"))
        return Verdict(VerdictStatus.SUCCESS,
                       reason=f"检测到 {len(report.changes)} 处控件变化")

    # 4/5. 无变化：先看是否被弹窗挡住
    screen = after_analyzer.get_screen_bounds() if after_analyzer else None
    overlays = find_overlays(widgets, screen)
    if overlays:
        # 期望弹窗出现（--expect-dialog）时，弹窗出现即预期结果，不判阻挡
        if expectations and expectations.get('dialog'):
            return Verdict(VerdictStatus.SUCCESS, reason="弹窗已按预期出现")
        top = top_overlay(overlays)
        assert top is not None
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

# 空 diff 确认复查的等待时长（秒）：动作后 UI 可能尚未稳定，单次 dump 会
# 误判 NO_CHANGE/假阴性。状态控件异步生效更慢，给更长等待。
RECHECK_EMPTY_WAIT = 0.8
RECHECK_STATE_WAIT = 1.5


class ActionPipeline:
    """统一动作管线：前置快照 → 动作 → 后置快照 → 裁决 → 自愈 → 断言

    两个引擎（HdcUITestEngine / SemanticEngine）通过 duck typing 复用本管线，
    只需提供：_dump_and_load / _save_and_cleanup / manager / diff /
    crash_detector / device / _get_current_route_cached。

    生命周期只写一份，消除双管线散落补丁。
    """

    def __init__(self, engine, auto_recover: bool = True):
        self.engine = engine
        self.auto_recover = auto_recover

    def _settle(self, timeout: float = 0.4):
        """UI 稳定窗：固定等待 timeout 秒，让 Toast/动画/惯性滚动先停下来。
        --fast 模式下缩短为 0.1s。
        （纯 hdc 无 idle 检测 API；如后续需要，可改为对连续两次 dump 做指纹比对。）"""
        if getattr(self.engine, 'fast_mode', False):
            timeout = min(timeout, 0.1)
        time.sleep(timeout)

    # ---- 前置快照（支持复用历史基准，省一次 dump）----

    def _before_snapshot(self, skip_before: bool, fresh_before: bool):
        """返回 (before_widgets, before_route, before_sig, reused)

        复用条件：未强制 fresh，且历史存在、新鲜、路由与当前一致。
        """
        from engines.helpers import _compute_page_signature
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
        """历史路由是否与当前路由一致（一致才敢复用基准）

        用引擎的路由缓存（同一操作流程内复用最近一次查询结果，
        _save_and_cleanup 保存后会重置），避免每步重复走 bridge 查询。
        """
        saved = self.engine.manager.load_route_history()
        if saved is None:
            return False
        current = self.engine._get_current_route_cached()
        if current is None:
            # 拿不到当前路由时保守处理：允许复用（避免额外 dump 失去意义）
            return True
        return saved == current

    def _capture_failure(self, reason: str, analyzer=None, crash: bool = False):
        """失败留证（截屏/hilog/崩溃/dump）。

        每次 run 至多一次（engine 上打标记），且任何异常都吞掉——留证绝不能
        影响主流程或改变退出码。
        """
        e = self.engine
        if getattr(e, '_artifacts_captured', False):
            return
        try:
            from engines.artifacts import capture_failure
            capture_failure(getattr(e, 'device', None), reason,
                            analyzer=analyzer, crash=crash)
        except Exception:
            pass
        try:
            e._artifacts_captured = True
        except Exception:
            pass

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
        from engines.helpers import _compute_page_signature, print_page_state_summary, check_expectations_with_polling, auto_handle_dialogs
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
                # 与 after 路径对称：dump 失败也要输出结构化裁决——
                # AI 闭环依赖 ACTION_VERDICT 行作输入（四层反馈契约）
                Verdict(VerdictStatus.CRASHED,
                        reason="before dump 失败（uitest 并发锁/设备繁忙/闪退）",
                        suggestion="检查应用是否存活（app restart）后重试").print()
                self._capture_failure("before dump failed", crash=True)
                return False

        # 1.5 点击目标禁用预检：命中 clickable=false/enabled=false 控件时
        # 直接硬失败（不执行无效点击）。此前锁定期连点数字键全无效仅报
        # NO_CHANGE 软失败，Agent 无法区分"禁用"与"无需操作"而继续盲试。
        pending_point = getattr(e, '_pending_point', None)
        if pending_point is not None:
            e._pending_point = None
            if before_widgets:
                cts = find_click_target_status(before_widgets,
                                               pending_point[0], pending_point[1])
                if cts.disabled:
                    dis = cts.disabled_by or {}
                    tgt = cts.target or {}
                    dis_txt = cts.disabled_text
                    reason = (f"点击目标被禁用: ({pending_point[0]},{pending_point[1]}) "
                              f"命中 {tgt.get('type', '')}"
                              + (f" '{(tgt.get('text') or '')[:20]}'"
                                 if (tgt.get('text') or '').strip() else "")
                              + f"，目标区域存在 enabled=false 的 "
                                f"{dis.get('type', '')}"
                              + (f" '{dis_txt}'" if dis_txt else "")
                              + "，点击不会生效")
                    verdict = Verdict(
                        VerdictStatus.CLICK_ON_DISABLED,
                        reason=reason,
                        suggestion="目标区域控件当前不可交互（如锁定/冷却/置灰），"
                                   "检查页面状态（tree dump --text）确认是否需要先解锁/等待")
                    verdict.print()
                    self._capture_failure("click on disabled widget")
                    return False

        # 2. 崩溃基线 + 记录操作前 acc
        e.crash_detector.prime_baseline()
        if before_widgets:
            e.crash_detector.record_acc(before_widgets)

        # 2.5 back 护栏：返回动作执行前已在桌面 → 不执行返回，防止一路退到桌面
        if is_back and before_widgets is not None \
                and not widgets_have_app_content(before_widgets):
            verdict = Verdict(
                VerdictStatus.BACK_INEFFECTIVE,
                reason="已在桌面/系统界面，停止返回（bundle: "
                       + ", ".join(sorted({(w.get('attributes') or {}).get('bundleName', '')
                                           for w in before_widgets
                                           if (w.get('attributes') or {}).get('bundleName')}))
                       + ")",
                suggestion="当前已无 App 内容，如需回到 App 请用 app navigate 或 aa start 启动")
            verdict.print()
            self._capture_failure("back ineffective: already on desktop")
            return False

        # 3. 执行动作
        t0 = time.monotonic()
        action_exc = None
        try:
            raw_ok = action_fn()
        except Exception as ex:
            action_exc = ex
            print(f"❌ {desc} 执行异常: {ex}")
            Verdict(VerdictStatus.ERROR,
                    reason="action_exception: %s" % ex).print()
            raw_ok = False
        if not raw_ok:
            print(f"❌ {desc} 执行失败")
            log_line("[time] op=%s elapsed_ms=%d status=ERROR"
                     % (desc, int((time.monotonic() - t0) * 1000)))
            # 失败也必须给出结构化裁决；异常分支已打印 ERROR
            # （action_exception）时不重复打印——ACTION_VERDICT 契约
            # 是"一次操作一行机器可读结论"
            if action_exc is None:
                Verdict(VerdictStatus.ERROR, reason="action_failed").print()
            e.crash_detector.detect_and_report()
            self._capture_failure("action_failed", crash=True)
            return False
        print(f"✅ {desc} 执行完成")
        log_line("[time] op=%s elapsed_ms=%d status=OK"
                 % (desc, int((time.monotonic() - t0) * 1000)))

        # 4. Toast/动画稳定窗（自适应，尽早静止）
        self._settle(0.4)

        # 5. 后置快照 + 裁决（含一次自愈重试）
        verdict, after, report = self._verdict_once(
            before_widgets, before_route, before_sig, desc,
            is_back, check_exit, expectations)
        if after is None:
            # dump 失败（uitest 并发锁/闪退）也要输出结构化裁决：
            # AI 闭环（CRASHED→app restart）依赖 ACTION_VERDICT 行作输入，
            # 缺行会导致闭环无输入可消费（违反四层反馈契约）
            verdict.print()
            self._capture_failure("after dump failed", crash=True)
            return False

        # 自愈：被弹窗挡住时清弹窗并重试一次
        # （期望弹窗出现时不判阻挡，不会进入自愈）
        if (verdict.status == VerdictStatus.BLOCKED_BY_DIALOG
                and self.auto_recover):
            print("🔧 检测到弹窗阻挡，尝试自动清理并重试一次...")
            auto_handle_dialogs(e.device, engine=e)
            try:
                retry_ok = action_fn()
            except Exception as ex:
                print(f"❌ 自愈重试异常: {ex}")
                retry_ok = False
            if retry_ok:
                self._settle(0.4)
                verdict, after, report = self._verdict_once(
                    before_widgets, before_route, before_sig,
                    f"{desc} [自愈重试]", is_back, check_exit, expectations)
                if after is None:
                    verdict.print()
                    self._capture_failure("after dump failed (retry)", crash=True)
                    return False

        # 6. 输出裁决结论
        print()
        verdict.print()

        # 6.2 本次 dump 的归档 JSON 路径：操作后引擎已 dump 一次（差异报告用），
        # 把归档路径直接给出，复核/精析解析该文件即可，省一次重复 tree dump
        # （--fast 批量回归时静默，保持 ACTION_VERDICT 极简输出）
        if not getattr(e, 'fast_mode', False):
            json_path = getattr(after, '_archived_path', None)
            if json_path:
                print(f"📄 控件树 JSON: {json_path}")

        # 6.1 NO_CHANGE 时附目标区域控件实况（含 enabled 状态），帮助区分
        # "点到禁用/不可点控件"与"已是目标状态"——仅提示，不改变裁决
        if (verdict.status == VerdictStatus.NO_CHANGE
                and pending_point is not None and after is not None):
            cts = find_click_target_status(after.widgets,
                                           pending_point[0], pending_point[1])
            if cts.hit and cts.chain_desc:
                flag = "🚫 enabled=false" if cts.disabled else \
                       ("⚠️ 命中链无 clickable=true" if cts.not_clickable else "")
                print(f"🎯 目标 ({pending_point[0]},{pending_point[1]}) 命中控件: "
                      f"{cts.chain_desc}" + (f"  ← {flag}" if flag else ""))

        # 6.5 失败留证（在 cleanup 之前，保证 dump 可归档）
        if not verdict.ok(expectations):
            self._capture_failure("verdict=%s" % verdict.status.value,
                                  analyzer=after)

        # 7. 页面状态摘要
        same_page = None
        if before_sig is not None and after is not None:
            same_page = (before_sig == _compute_page_signature(after.widgets))
        if not getattr(e, 'fast_mode', False):
            print_page_state_summary(after, device=e.device,
                                     prev_widgets=before_widgets,
                                     route=e._get_current_route_cached(),
                                     same_page=same_page)

        # 8. 断言（在保存历史/cleanup 之前：轮询与失败留证都复用本次 dump，
        # cleanup 后 _temp_file 为 None，留证会丢失 dump 证据）
        ok = verdict.ok(expectations)
        if expectations and after is not None:
            if not check_expectations_with_polling(
                    expectations, report=report,
                    initial_widgets=after.widgets,
                    dump_fn=lambda: e._dump_and_load("poll"),
                    cleanup_fn=lambda a: a.cleanup(),
                    device=e.device):
                ok = False
        if not ok and verdict.ok(expectations):
            # 断言失败（裁决本身成功）才在此留证；裁决已失败时 6.5 已留证
            self._capture_failure("assertions failed", analyzer=after)

        # 9. 保存历史 + cleanup（断言全部结束后才销毁本次 dump）
        e._save_and_cleanup(after, save_route=True)
        return ok

    def _verdict_once(self, before_widgets, before_route, before_sig,
                      desc, is_back, check_exit, expectations=None):
        """dump 后置状态 → 差异比较 → 崩溃/重启检查 → 裁决

        Returns:
            (verdict, after_analyzer, report)；dump 失败时 (None, None, None)
        """
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

        # 差异比较：route_changed 由真实路由栈对比产生（compare 内部
        # after_route vs before_route），内容变化由全树 diff 产生。
        # 不用页面签名捷径判路由——签名（顶部文本集合）在顶部区域出现
        # banner/标题/覆盖层文本时也会变，直接判 route_changed 会把
        # BLOCKED_BY_DIALOG/NO_CHANGE 误报成 SUCCESS（exit 0）
        report = e._compare_with_history(after, desc)

        looks_desktop = check_exit and self._looks_like_desktop(after)
        verdict = judge(e.crash_detector, after, report,
                        is_back=is_back, looks_like_desktop=looks_desktop,
                        internal_restarted=internal_restarted,
                        expectations=expectations)

        # 无变化时补一次“确认 dump”：动作后 UI 可能尚未稳定（页面切换/异步
        # 渲染），单次 dump 会把“其实已生效”误判为 NO_CHANGE（假阴性）。状态
        # 控件异步生效更慢，给更长等待；其余空 diff 给短等待。
        if verdict.status in (VerdictStatus.NO_CHANGE,
                              VerdictStatus.BACK_INEFFECTIVE):
            has_state = self._has_state_controls(after.widgets)
            wait_s = RECHECK_STATE_WAIT if has_state else RECHECK_EMPTY_WAIT
            if has_state:
                print("⏳ 检测到状态控件，%ss 后复查是否异步生效..." % wait_s)
            else:
                print("⏳ 空 diff，%ss 后补一次确认 dump（排除异步未稳定）..." % wait_s)
            time.sleep(wait_s)
            recheck = e._dump_and_load("recheck")
            if recheck is not None:
                re_report = e._compare_with_history(recheck, f"{desc} [确认复查]")
                if re_report is not None and (re_report.changes
                                              or re_report.route_changed):
                    print("✅ 复查检测到变化（此前为异步未稳定），以复查结果为准")
                    # 复查通过也更新 acc 基线（主路径已更新，复查替换结果时同步）
                    e.crash_detector.record_acc(recheck.widgets)
                    verdict = judge(e.crash_detector, recheck, re_report,
                                    is_back=is_back,
                                    looks_like_desktop=looks_desktop,
                                    internal_restarted=internal_restarted,
                                    expectations=expectations)
                    after.cleanup()
                    return verdict, recheck, re_report
                recheck.cleanup()

        return verdict, after, report

    @staticmethod
    def _has_state_controls(widgets) -> bool:
        from engines.helpers import STATE_TYPE_LABELS
        return any((w.get('type', '') or '').lower() in STATE_TYPE_LABELS
                   for w in widgets)

    @staticmethod
    def _looks_like_desktop(analyzer) -> bool:
        texts = [w for w in analyzer.widgets if (w.get('text') or '').strip()]
        return len(analyzer.widgets) < 15 and len(texts) < 3
