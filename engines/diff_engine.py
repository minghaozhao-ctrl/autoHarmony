#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""控件树差异比较引擎

包含：变化类型定义、变化报告、控件树差异比较器、自动差异管理器。
"""
import json
import os
import re
from typing import Dict, List, Optional, Tuple
from collections import defaultdict
from enum import Enum
from datetime import datetime


# ==================== 变化类型定义 ====================

class ChangeType(Enum):
    ADDED = "新增"
    REMOVED = "消失"
    TEXT_CHANGED = "文本变化"
    POSITION_CHANGED = "位置变化"
    STATE_CHANGED = "状态变化"
    PROPERTY_CHANGED = "属性变化"
    OVERLAY_APPEARED = "覆盖层出现"
    OVERLAY_DISAPPEARED = "覆盖层消失"
    OCCLUDED = "被遮挡"
    REVEALED = "解除遮挡"


class ChangePriority(Enum):
    HIGH = "🔴"
    MEDIUM = "🟡"
    LOW = "🟢"


# ==================== 变化对象 ====================

class Change:
    """单个变化"""

    def __init__(self, change_type: ChangeType, widget: Dict,
                 before: Optional[Dict] = None, after: Optional[Dict] = None,
                 description: str = ""):
        self.type = change_type
        self.widget = widget
        self.before = before
        self.after = after
        self.description = description
        self.priority = self._calculate_priority()

    def _calculate_priority(self) -> ChangePriority:
        if self.type == ChangeType.ADDED:
            widget_type = self.widget.get('type', '').lower()
            text = self.widget.get('text', '')
            if widget_type in ['dialog', 'alertdialog', 'sheet', 'popup']:
                return ChangePriority.HIGH
            if any(kw in text for kw in ['弹窗', '确认', '提示', '警告']):
                return ChangePriority.HIGH
        if self.type == ChangeType.REMOVED:
            text = self.widget.get('text', '')
            if any(kw in text for kw in ['提交', '确定', '确认', '登录']):
                return ChangePriority.HIGH
        if self.type == ChangeType.TEXT_CHANGED:
            return ChangePriority.MEDIUM
        if self.type == ChangeType.POSITION_CHANGED:
            if self.before and self.after:
                distance = self._calculate_distance()
                if distance > 20:
                    return ChangePriority.MEDIUM
        return ChangePriority.LOW

    def _calculate_distance(self) -> float:
        if not self.before or not self.after:
            return 0
        before_bounds = WidgetTreeDiff._parse_bounds(self.before.get('bounds', ''))
        after_bounds = WidgetTreeDiff._parse_bounds(self.after.get('bounds', ''))
        if not before_bounds or not after_bounds:
            return 0
        before_center = ((before_bounds[0] + before_bounds[2]) / 2,
                        (before_bounds[1] + before_bounds[3]) / 2)
        after_center = ((after_bounds[0] + after_bounds[2]) / 2,
                       (after_bounds[1] + after_bounds[3]) / 2)
        return ((before_center[0] - after_center[0]) ** 2 +
                (before_center[1] - after_center[1]) ** 2) ** 0.5


# ==================== 变化报告 ====================

class ChangeReport:
    """变化报告"""

    def __init__(self, operation: str = ""):
        self.operation = operation
        self.timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.changes: List[Change] = []
        self.route_changed = False
        self.before_route: Optional[List[str]] = None
        self.after_route: Optional[List[str]] = None
        self.after_analyzer = None
        self.before_overlays: List[Dict] = []
        self.after_overlays: List[Dict] = []

    def add_change(self, change: Change):
        self.changes.append(change)

    def add_changes(self, changes: List[Change]):
        self.changes.extend(changes)

    def set_route_change(self, before_route: List[str], after_route: List[str]):
        self.route_changed = True
        self.before_route = before_route
        self.after_route = after_route

    def has_dialog(self) -> bool:
        for change in self.changes:
            if change.type == ChangeType.ADDED:
                widget_type = change.widget.get('type', '').lower()
                if widget_type in ['dialog', 'alertdialog', 'sheet', 'popup']:
                    return True
        return False

    def print(self):
        print()
        if self.route_changed:
            self._print_route_report()
        else:
            self._print_change_report()
        print()

    def _print_route_report(self):
        print("=" * 100)
        print("🚀 页面路由发生变化")
        print("=" * 100)
        print(f"  操作: {self.operation}")
        print(f"  时间: {self.timestamp}")
        print()
        print("  路由栈变化:")
        print("  " + "-" * 80)
        if self.before_route:
            print("  之前路由:")
            for i, route in enumerate(self.before_route, 1):
                print(f"    {i}. {route}")
        else:
            print("  之前路由: (未知)")
        print()
        if self.after_route:
            print("  当前路由:")
            for i, route in enumerate(self.after_route, 1):
                marker = "👉 " if i == len(self.after_route) else "   "
                print(f"  {marker}{i}. {route}")
        else:
            print("  当前路由: (未知)")
        print()
        if self.after_analyzer:
            print("  " + "=" * 80)
            print("  📋 新页面内容概览")
            print("  " + "=" * 80)
            self.after_analyzer.overview()
        print("PAGE_RESULT: ROUTE_CHANGED")

    def _print_change_report(self):
        print("=" * 100)
        print("📋 组件树变化报告")
        print("=" * 100)
        print(f"  操作: {self.operation}")
        print(f"  时间: {self.timestamp}")
        print()

        stats = defaultdict(int)
        for change in self.changes:
            stats[change.type.value] += 1
        print("📊 统计数据:")
        for change_type, count in stats.items():
            print(f"  {change_type}: {count}")
        print()

        if not self.changes:
            print("  ✅ 未检测到显著变化")
            print("PAGE_RESULT: NO_CHANGES")
            return

        overlay_types = {ChangeType.OVERLAY_APPEARED, ChangeType.OVERLAY_DISAPPEARED}
        occluded_types = {ChangeType.OCCLUDED}
        revealed_types = {ChangeType.REVEALED}

        overlay_changes = [c for c in self.changes if c.type in overlay_types]
        occluded_changes = [c for c in self.changes if c.type in occluded_types]
        revealed_changes = [c for c in self.changes if c.type in revealed_types]
        other_changes = [c for c in self.changes
                         if c.type not in overlay_types
                         and c.type not in occluded_types
                         and c.type not in revealed_types]

        has_popup_section = bool(overlay_changes or occluded_changes or revealed_changes)

        if has_popup_section:
            self._print_section_with_type("🪟 覆盖层变化", overlay_changes)
            self._print_section_with_type("🌫️ 被遮挡的底层控件", occluded_changes)
            self._print_section_with_type("✨ 解除遮挡的控件", revealed_changes)
            self._print_grouped_changes(other_changes)
        else:
            self._print_grouped_changes(self.changes)
        print("PAGE_RESULT: CHANGES_DETECTED")

    def _print_section_with_type(self, title: str, changes: List[Change]):
        if not changes:
            return
        print("  " + "-" * 80)
        print(f"  {title} ({len(changes)})")
        print("  " + "-" * 80)
        detail_types = {ChangeType.TEXT_CHANGED, ChangeType.POSITION_CHANGED,
                        ChangeType.STATE_CHANGED, ChangeType.PROPERTY_CHANGED}
        self._print_change_tree(changes, detail_types)
        print()

    def _print_grouped_changes(self, changes: List[Change]):
        if not changes:
            return
        type_groups = defaultdict(list)
        for c in changes:
            type_groups[c.type.value].append(c)

        detail_types = {ChangeType.TEXT_CHANGED, ChangeType.POSITION_CHANGED,
                       ChangeType.STATE_CHANGED, ChangeType.PROPERTY_CHANGED}

        for type_name, group in type_groups.items():
            print("  " + "-" * 80)
            print(f"  {type_name} ({len(group)})")
            print("  " + "-" * 80)
            self._print_change_tree(group, detail_types)
            print()

    def _print_change_tree(self, changes: List[Change], detail_types: set):
        parsed_bounds = []
        for change in changes:
            b = WidgetTreeDiff._parse_bounds(change.widget.get('bounds', ''))
            parsed_bounds.append(b)

        parent_idx = [-1] * len(changes)
        for i in range(len(changes)):
            if not parsed_bounds[i]:
                continue
            best_parent = -1
            best_area = float('inf')
            bi = parsed_bounds[i]
            for j in range(len(changes)):
                if i == j or not parsed_bounds[j]:
                    continue
                bj = parsed_bounds[j]
                if (bj[0] <= bi[0] and bj[1] <= bi[1] and
                        bj[2] >= bi[2] and bj[3] >= bi[3] and
                        (bj[0] != bi[0] or bj[1] != bi[1] or
                         bj[2] != bi[2] or bj[3] != bi[3])):
                    area = (bj[2] - bj[0]) * (bj[3] - bj[1])
                    if area < best_area:
                        best_area = area
                        best_parent = j
            parent_idx[i] = best_parent

        children_map: Dict[int, List[int]] = defaultdict(list)
        roots: List[int] = []
        for i in range(len(changes)):
            if parent_idx[i] >= 0:
                children_map[parent_idx[i]].append(i)
            else:
                roots.append(i)

        def print_node(idx: int, prefix: str, is_last: bool):
            change = changes[idx]
            branch = "└── " if is_last else "├── "
            child_prefix = prefix + ("    " if is_last else "│   ")

            if change.type in detail_types:
                self._print_change_detail(change, prefix + branch)
            else:
                self._print_widget_info(change.widget, prefix + branch)

            child_indices = children_map.get(idx, [])
            for ci, child_idx in enumerate(child_indices):
                print_node(child_idx, child_prefix, ci == len(child_indices) - 1)

        for i, root_idx in enumerate(roots):
            print_node(root_idx, "  ", i == len(roots) - 1)

    def _print_change_detail(self, change: Change, indent: str = ""):
        widget_type = change.widget.get('type', 'Unknown')
        text = change.widget.get('text', '')
        bounds = change.widget.get('bounds', '')

        header = f"{indent}{widget_type}"
        if text:
            header += f" \"{text[:35]}\""
        if bounds:
            header += f"  {bounds}"
        print(header)

        if change.type == ChangeType.TEXT_CHANGED and change.before and change.after:
            before_text = (change.before.get('text', '') or change.before.get('hint', ''))[:35]
            after_text = (change.after.get('text', '') or change.after.get('hint', ''))[:35]
            print(f"{indent}│   └── \"{before_text}\" → \"{after_text}\"")

        if change.type == ChangeType.POSITION_CHANGED and change.before and change.after:
            before_bounds = change.before.get('bounds', '')
            after_bounds = change.after.get('bounds', '')
            distance = WidgetTreeDiff._calculate_distance(before_bounds, after_bounds)
            print(f"{indent}│   └── {before_bounds} → {after_bounds} (距离: {distance:.1f}px)")

        if change.type == ChangeType.STATE_CHANGED and change.before and change.after:
            self._print_state_diff(change.before, change.after)

        if change.type == ChangeType.PROPERTY_CHANGED and change.before and change.after:
            self._print_property_diff(change.before, change.after)

    @staticmethod
    def _print_state_diff(before: Dict, after: Dict):
        state_attrs = [
            'visible', 'enabled', 'clickable', 'checked',
            'selected', 'focused', 'longClickable',
            'scrollable', 'checkable', 'zoomable',
        ]
        for attr in state_attrs:
            if before.get(attr) != after.get(attr):
                print(f"  {attr}: {before.get(attr)} → {after.get(attr)}")

    @staticmethod
    def _print_property_diff(before: Dict, after: Dict):
        if (before.get('id') or '') != (after.get('id') or ''):
            print(f"  id: \"{before.get('id', '')}\" → \"{after.get('id', '')}\"")
        if (before.get('accessibilityId') or '') != (after.get('accessibilityId') or ''):
            print(f"  accessibilityId: \"{before.get('accessibilityId', '')}\" → \"{after.get('accessibilityId', '')}\"")
        if (before.get('description') or '') != (after.get('description') or ''):
            print(f"  description: \"{before.get('description', '')}\" → \"{after.get('description', '')}\"")

    def _print_widget_info(self, widget: Dict, indent: str = ""):
        widget_type = widget.get('type', 'Unknown')
        widget_id = widget.get('id', '')
        text = widget.get('text', '')
        hint = widget.get('hint', '')
        bounds = widget.get('bounds', '')

        line = f"{indent}{widget_type}"
        if widget_id:
            line += f" id=\"{widget_id[:30]}\""
        display_text = text or hint
        if display_text:
            line += f" \"{display_text[:35]}\""
        if bounds:
            line += f"  {bounds}"
        print(line)


# ==================== 控件树差异比较器 ====================

class WidgetTreeDiff:
    """组件树差异比较器"""

    LAYOUT_TYPES = {
        'column', 'row', 'stack', 'flex', 'scroll', 'list', 'grid',
        'griditem', 'listitem', 'badge', 'panel', 'swiper', 'navigator',
        'divider', 'relativecontainer', 'counter', 'waterflow', 'tabs',
        'tabcontent', 'refresh'
    }

    PERSONALIZED_TYPES = {
        'button', 'text', 'image', 'toggle', 'checkbox', 'radio',
        'textinput', 'textarea', 'slider', 'progress', 'loadingprogress',
        'select', 'dialog', 'alertdialog', 'sheet', 'popup',
        'imagecomponent', 'search', 'richtext', 'hyperlink',
        'datepicker', 'timepicker', 'textpicker', 'switch',
        'menu', 'menuitem', 'navigation', 'toolbar', 'tabcontent',
        'badge', 'piece', 'datapanel'
    }

    OVERLAY_TYPES = {
        'dialog', 'alertdialog', 'sheet', 'popup', 'menu', 'menuitem',
        'actionmenu', 'contextmenu', 'toast', 'customdialog',
        'bottomsheet', 'sidebarm', 'toastdialog'
    }

    OCCLUSION_RATIO_THRESHOLD = 0.7
    OVERLAY_COVERAGE_THRESHOLD = 0.8

    @staticmethod
    def is_overlay(widget: Dict) -> bool:
        widget_type = widget.get('type', '').lower()
        return widget_type in WidgetTreeDiff.OVERLAY_TYPES

    @staticmethod
    def _parse_bounds(bounds_str: str) -> Optional[Tuple[int, int, int, int]]:
        if not bounds_str:
            return None
        match = re.match(r'\[(\d+),(\d+)\]\[(\d+),(\d+)\]', bounds_str)
        if match:
            return (int(match.group(1)), int(match.group(2)),
                    int(match.group(3)), int(match.group(4)))
        return None

    @staticmethod
    def _calculate_distance(before_bounds: str, after_bounds: str) -> float:
        before = WidgetTreeDiff._parse_bounds(before_bounds)
        after = WidgetTreeDiff._parse_bounds(after_bounds)
        if not before or not after:
            return 0.0
        before_center = ((before[0] + before[2]) / 2, (before[1] + before[3]) / 2)
        after_center = ((after[0] + after[2]) / 2, (after[1] + after[3]) / 2)
        return ((before_center[0] - after_center[0]) ** 2 +
                (before_center[1] - after_center[1]) ** 2) ** 0.5

    @staticmethod
    def detect_overlays(widgets: List[Dict],
                        screen_bounds: Optional[Tuple[int, int, int, int]] = None
                        ) -> List[Dict]:
        """识别当前控件树中的覆盖层（弹窗/遮罩）"""
        if not screen_bounds:
            # 未显式传入时，取控件树中面积最大的控件（根节点）作为屏幕边界，
            # 避免硬编码默认分辨率在真实设备（如 1260x2720）上导致覆盖率误算
            best, best_area = None, 0
            for w in widgets:
                b = WidgetTreeDiff._parse_bounds(w.get('bounds', ''))
                if b:
                    area = (b[2] - b[0]) * (b[3] - b[1])
                    if area > best_area:
                        best, best_area = b, area
            screen_bounds = best if best else (0, 0, 1080, 2400)
        screen_w = screen_bounds[2] - screen_bounds[0]
        screen_h = screen_bounds[3] - screen_bounds[1]
        screen_area = max(screen_w * screen_h, 1)

        def _coverage(w: Dict) -> float:
            b = WidgetTreeDiff._parse_bounds(w.get('bounds', ''))
            if not b:
                return 0.0
            return ((b[2] - b[0]) * (b[3] - b[1])) / screen_area

        def _is_visible(w: Dict) -> bool:
            """过滤控件树中保留的不可见图层（visible=false 的历史/隐藏层）"""
            return str(w.get('visible', 'true') or 'true').lower() != 'false'

        content_widget_indices = [
            i for i, w in enumerate(widgets)
            if _coverage(w) < 0.5
            and _is_visible(w)
            and (w.get('text', '').strip() or w.get('clickable') == 'true')
        ]
        content_orders = [widgets[i].get('render_order', 0) for i in content_widget_indices]
        min_content_order = min(content_orders) if content_orders else float('inf')
        total_content = len(content_widget_indices)

        def _is_descendant_of(widget_idx: int, ancestor_idx: int) -> bool:
            """判断 widget_idx 是否为 ancestor_idx 的后代（带环检测）"""
            if widget_idx < 0 or widget_idx >= len(widgets):
                return False
            if ancestor_idx < 0 or ancestor_idx >= len(widgets):
                return False
            cur = widgets[widget_idx].get('parent_index')
            visited = set()
            while cur is not None:
                if cur == ancestor_idx:
                    return True
                if cur in visited or cur < 0 or cur >= len(widgets):
                    return False
                visited.add(cur)
                cur = widgets[cur].get('parent_index') if 0 <= cur < len(widgets) else None
            return False

        overlays: List[Dict] = []
        picked_ids = set()

        for idx, w in enumerate(widgets):
            cov = _coverage(w)
            if cov < WidgetTreeDiff.OVERLAY_COVERAGE_THRESHOLD:
                continue
            if not _is_visible(w):
                continue
            if w.get('render_order', 0) > min_content_order:
                if total_content > 0:
                    content_descendants = sum(
                        1 for j in content_widget_indices
                        if _is_descendant_of(j, idx)
                    )
                    if content_descendants / total_content > 0.5:
                        continue
                w['_overlay_coverage'] = round(cov, 2)
                overlays.append(w)
                picked_ids.add(id(w))

        for w in widgets:
            if id(w) in picked_ids:
                continue
            wt = w.get('type', '').lower()
            attrs = w.get('attributes', {})
            is_type_overlay = wt in WidgetTreeDiff.OVERLAY_TYPES
            is_modal = attrs.get('modal') == 'true' or attrs.get('isModal') == 'true'
            if (is_type_overlay or is_modal) and _is_visible(w):
                w['_overlay_coverage'] = round(_coverage(w), 2)
                overlays.append(w)
                picked_ids.add(id(w))

        return overlays

    @staticmethod
    def is_occluded(widget: Dict, overlays: List[Dict]) -> bool:
        w_bounds = WidgetTreeDiff._parse_bounds(widget.get('bounds', ''))
        if not w_bounds:
            return False
        w_area = (w_bounds[2] - w_bounds[0]) * (w_bounds[3] - w_bounds[1])
        if w_area <= 0:
            return False
        w_order = widget.get('render_order', 0)
        for ov in overlays:
            if ov.get('render_order', 0) <= w_order:
                continue
            ov_bounds = WidgetTreeDiff._parse_bounds(ov.get('bounds', ''))
            if not ov_bounds:
                continue
            ix = max(0, min(w_bounds[2], ov_bounds[2]) - max(w_bounds[0], ov_bounds[0]))
            iy = max(0, min(w_bounds[3], ov_bounds[3]) - max(w_bounds[1], ov_bounds[1]))
            if ix <= 0 or iy <= 0:
                continue
            overlap = ix * iy
            if overlap / w_area >= WidgetTreeDiff.OCCLUSION_RATIO_THRESHOLD:
                return True
        return False

    @staticmethod
    def _detect_overlay_changes(before_widgets: List[Dict],
                                after_widgets: List[Dict]) -> List[Change]:
        def _key(w: Dict):
            wid = w.get('id') or w.get('accessibilityId')
            return (wid or id(w), w.get('type'), w.get('text'))

        before_map = {_key(w): w for w in before_widgets if WidgetTreeDiff.is_overlay(w)}
        after_map = {_key(w): w for w in after_widgets if WidgetTreeDiff.is_overlay(w)}

        changes: List[Change] = []
        for k, widget in after_map.items():
            if k not in before_map:
                changes.append(Change(ChangeType.OVERLAY_APPEARED, widget))
        for k, widget in before_map.items():
            if k not in after_map:
                changes.append(Change(ChangeType.OVERLAY_DISAPPEARED, widget))
        return changes

    @staticmethod
    def is_significant_widget(widget: Dict) -> bool:
        widget_type = widget.get('type', '').lower()
        if widget_type in WidgetTreeDiff.PERSONALIZED_TYPES:
            return True
        if widget_type in WidgetTreeDiff.OVERLAY_TYPES:
            return True
        if widget.get('text', '').strip():
            return True
        if widget.get('clickable') == 'true':
            return True
        if widget.get('description', '').strip():
            return True
        bounds = WidgetTreeDiff._parse_bounds(widget.get('bounds', ''))
        if bounds:
            area = (bounds[2] - bounds[0]) * (bounds[3] - bounds[1])
            if area >= 1080 * 2400 * 0.5:
                return True
        return False

    @staticmethod
    def get_current_route(device: Optional[str] = None) -> Optional[List[str]]:
        """获取当前路由栈（通过 TcpBridge 直连 App）"""
        try:
            import sys
            sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
            from bridge.tcp_bridge import TcpBridge
            from utils.hdc import detect_device_id
            dev = device or detect_device_id()
            if not dev:
                return None
            bridge = TcpBridge(device=dev)
            try:
                return bridge.get_current_route()
            finally:
                bridge.close()
        except Exception:
            return None

    def compare(self, before_widgets: List[Dict], after_widgets: List[Dict],
                operation: str = "", before_route: Optional[List[str]] = None,
                after_route: Optional[List[str]] = None,
                after_analyzer=None) -> ChangeReport:
        report = ChangeReport(operation)

        # 覆盖层检测必须使用原始（未过滤）小部件列表，因为 parent_index 指向原始索引
        before_overlays = self.detect_overlays(before_widgets, after_analyzer.get_screen_bounds() if after_analyzer else None)
        after_overlays = self.detect_overlays(after_widgets, after_analyzer.get_screen_bounds() if after_analyzer else None)
        before_widgets = [w for w in before_widgets if self.is_significant_widget(w)]
        after_widgets = [w for w in after_widgets if self.is_significant_widget(w)]

        if before_route and after_route:
            if before_route != after_route:
                report.set_route_change(before_route, after_route)
                report.after_analyzer = after_analyzer
                return report

        overlay_changes = self._detect_overlay_changes(before_widgets, after_widgets)
        for change in overlay_changes:
            report.add_change(change)
        if overlay_changes:
            report.changes = overlay_changes + [c for c in report.changes if c not in overlay_changes]

        before_by_id = self._build_id_index(before_widgets)
        after_by_id = self._build_id_index(after_widgets)

        matched_pairs = []
        unmatched_before = set(range(len(before_widgets)))
        unmatched_after = set(range(len(after_widgets)))

        for i, widget in enumerate(before_widgets):
            widget_id = widget.get('id') or widget.get('accessibilityId')
            if widget_id:
                widget_id_lower = widget_id.lower()
                if widget_id_lower in after_by_id:
                    candidates = after_by_id[widget_id_lower]
                    if len(candidates) == 1:
                        matched_pairs.append((widget, candidates[0], 1.0))
                        unmatched_before.discard(i)
                        unmatched_after.discard(candidates[0]['_index'])

        for i in list(unmatched_before):
            before_widget = before_widgets[i]
            best_match, best_score = self._find_best_match(
                before_widget,
                [after_widgets[j] for j in unmatched_after]
            )
            if best_match and best_score >= 0.5:
                matched_pairs.append((before_widget, best_match, best_score))
                unmatched_before.discard(i)
                unmatched_after.discard(best_match['_index'])

        seen_removed = set()
        for i in unmatched_before:
            widget = before_widgets[i]
            if self.is_overlay(widget):
                continue
            text = widget.get('text', '')
            key = (widget.get('type', ''), widget.get('bounds', ''), text)
            if key in seen_removed:
                continue
            seen_removed.add(key)
            report.add_change(Change(ChangeType.REMOVED, widget))

        seen_added = set()
        for i in unmatched_after:
            widget = after_widgets[i]
            if self.is_overlay(widget):
                continue
            text = widget.get('text', '')
            key = (widget.get('type', ''), widget.get('bounds', ''), text)
            if key in seen_added:
                continue
            seen_added.add(key)
            report.add_change(Change(ChangeType.ADDED, widget))

        for before_w, after_w, score in matched_pairs:
            changes = self._compare_properties(before_w, after_w)
            report.add_changes(changes)
            before_occluded = self.is_occluded(before_w, before_overlays)
            after_occluded = self.is_occluded(after_w, after_overlays)
            if after_occluded and not before_occluded:
                report.add_change(Change(ChangeType.OCCLUDED, after_w, before_w, after_w))
            elif before_occluded and not after_occluded:
                report.add_change(Change(ChangeType.REVEALED, after_w, before_w, after_w))

        return report

    def _build_id_index(self, widgets: List[Dict]) -> Dict[str, List[Dict]]:
        index = defaultdict(list)
        for i, widget in enumerate(widgets):
            widget['_index'] = i
            widget_id = widget.get('id') or widget.get('accessibilityId')
            if widget_id:
                index[widget_id.lower()].append(widget)
        return index

    def _find_best_match(self, before: Dict, candidates: List[Dict]) -> Tuple[Optional[Dict], float]:
        if not candidates:
            return None, 0.0
        best_match = None
        best_score = 0.0
        for candidate in candidates:
            score = self._calculate_similarity(before, candidate)
            if score > best_score:
                best_score = score
                best_match = candidate
        return best_match, best_score

    def _calculate_similarity(self, before: Dict, after: Dict) -> float:
        if before.get('type', '').lower() != after.get('type', '').lower():
            return 0.0
        before_text = before.get('text', '')
        after_text = after.get('text', '')
        text_mismatch_penalty = 0.0
        if before_text and after_text and before_text != after_text:
            text_mismatch_penalty = 0.6
        bounds_score = self._calculate_bounds_similarity(
            before.get('bounds', ''), after.get('bounds', ''))
        attr_score = 0.0
        if before_text == after_text and before_text:
            attr_score = 1.0
        elif before.get('hint') == after.get('hint') and before.get('hint'):
            attr_score = 0.8
        total_score = bounds_score * 0.7 + attr_score * 0.3 - text_mismatch_penalty
        return max(total_score, 0.0)

    def _calculate_bounds_similarity(self, before_bounds: str, after_bounds: str) -> float:
        before = self._parse_bounds(before_bounds)
        after = self._parse_bounds(after_bounds)
        if not before or not after:
            return 0.0
        overlap_left = max(before[0], after[0])
        overlap_top = max(before[1], after[1])
        overlap_right = min(before[2], after[2])
        overlap_bottom = min(before[3], after[3])
        if overlap_right <= overlap_left or overlap_bottom <= overlap_top:
            return 0.0
        overlap_area = (overlap_right - overlap_left) * (overlap_bottom - overlap_top)
        before_area = (before[2] - before[0]) * (before[3] - before[1])
        after_area = (after[2] - after[0]) * (after[3] - after[1])
        return overlap_area / min(before_area, after_area)

    def _compare_properties(self, before: Dict, after: Dict) -> List[Change]:
        changes = []
        if before.get('text') != after.get('text') or before.get('hint') != after.get('hint'):
            changes.append(Change(ChangeType.TEXT_CHANGED, after, before, after))
        if before.get('bounds') != after.get('bounds'):
            distance = self._calculate_distance(before.get('bounds', ''), after.get('bounds', ''))
            if distance > 5:
                changes.append(Change(ChangeType.POSITION_CHANGED, after, before, after))
        state_attrs = [
            'visible', 'enabled', 'clickable', 'checked',
            'selected', 'focused', 'longClickable',
            'scrollable', 'checkable', 'zoomable'
        ]
        if any(before.get(attr) != after.get(attr) for attr in state_attrs):
            changes.append(Change(ChangeType.STATE_CHANGED, after, before, after))
        id_attrs = ['id', 'accessibilityId', 'description']
        if any((before.get(a) or '') != (after.get(a) or '') for a in id_attrs):
            # accessibilityId 纯数字序号变化（如 6514→6504）视为无意义，不报
            before_acc = str(before.get('accessibilityId') or '')
            after_acc = str(after.get('accessibilityId') or '')
            acc_noise = (before.get('id') == after.get('id')
                         and before.get('description') == after.get('description')
                         and before_acc.isdigit() and after_acc.isdigit())
            if not acc_noise:
                changes.append(Change(ChangeType.PROPERTY_CHANGED, after, before, after))
        return changes


# ==================== 自动差异管理器 ====================

class AutoDiffManager:
    """自动差异管理器"""

    def __init__(self, history_dir: Optional[str] = None):
        import tempfile
        self.history_dir = history_dir or tempfile.gettempdir()
        os.makedirs(self.history_dir, exist_ok=True)
        self.history_file = os.path.join(self.history_dir, "widget_tree_history.json")
        self.route_history_file = os.path.join(self.history_dir, "widget_tree_route_history.json")

    def load_history(self) -> Optional[List[Dict]]:
        if not os.path.exists(self.history_file):
            return None
        try:
            with open(self.history_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
                return data.get('widgets', [])
        except Exception:
            return None

    def save_history(self, widgets: List[Dict], route: Optional[List[str]] = None):
        try:
            data = {'timestamp': datetime.now().isoformat(), 'widgets': widgets}
            with open(self.history_file, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            if route is not None:
                route_data = {'timestamp': datetime.now().isoformat(), 'route': route}
                with open(self.route_history_file, 'w', encoding='utf-8') as f:
                    json.dump(route_data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"⚠️  保存历史失败: {e}")

    def load_route_history(self) -> Optional[List[str]]:
        if not os.path.exists(self.route_history_file):
            return None
        try:
            with open(self.route_history_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
                return data.get('route')
        except Exception:
            return None

    def clear_history(self):
        if os.path.exists(self.history_file):
            os.unlink(self.history_file)
        if os.path.exists(self.route_history_file):
            os.unlink(self.route_history_file)
