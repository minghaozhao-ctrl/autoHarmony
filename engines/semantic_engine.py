#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SemanticEngine：基于控件树语义匹配的 UI 操作引擎"""
import os
import re
import shlex
import time
from typing import List, Optional

from analyzers.widget_tree import WidgetTreeAnalyzer
from engines.diff_engine import WidgetTreeDiff, AutoDiffManager, ChangeReport
from analyzers.crash_detector import CrashDetector
from utils.common import run_hdc_command
from utils.bundle import default_bundle
from engines.verdict import ActionPipeline

from .helpers import (
    _compute_page_signature,
    _nearest_label,
    _point_in_bounds,
    _print_toggle_list,
    _sort_text_candidates,
)
from .hdc_engine import HdcUITestEngine


def _semantic_fail_verdict(reason: str) -> None:
    """未进裁决管线的失败出口（not found/越界等）统一输出一行机器可读
    裁决——四层反馈契约：语义族失败路径不进 ActionPipeline，AI 主路径
    （非 --json）也需要 verdict 行做闭环输入
    """
    print(f"ACTION_VERDICT: ERROR | reason={reason}")


class SemanticEngine(HdcUITestEngine):
    """基于控件树语义匹配的 UI 操作引擎

    先 dump 控件树（纯 hdc），再按 text/id/type 匹配目标控件，取中心坐标复用
    HdcUITestEngine 的坐标动作管线执行 + 差异比较；不依赖任何第三方 daemon。
    """

    def __init__(self, device: Optional[str] = None, history_dir: Optional[str] = None):
        super().__init__(device=device, history_dir=history_dir)

    def click_by_text(self, text: str, operation: str = "",
                      expectations: Optional[dict] = None,
                      skip_before: bool = False,
                      index: Optional[int] = None,
                      fresh_before: bool = False,
                      auto_recover: bool = True,
                      exact: bool = False) -> bool:
        desc = operation or f"点击文本为 '{text}' 的控件"
        if index is not None:
            # 指定索引：直接 dump 控件树按文本匹配列表取第 N 个坐标点击
            return self._click_by_text_fuzzy(text, desc, index=index,
                                             expectations=expectations,
                                             skip_before=skip_before,
                                             fresh_before=fresh_before,
                                             auto_recover=auto_recover)
        # 快路径：dump 精确匹配单个控件 → 坐标点击（纯 hdc）
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
        if exact:
            print(f"❌ --exact：未找到文本完全等于 '{text}' 的控件（不尝试包含匹配）")
            _semantic_fail_verdict(f"widget_not_found_exact: {text}")
            return False
        if skip_before:
            # 批量模式不降级 fuzzy（设计如此）：exact 失败也要有输出——
            # 完全静默会让"UI 改名"这类回归最常见失败无任何提示
            print(f"❌ --exact：未找到文本完全等于 '{text}' 的控件（批量模式不降级）")
            _semantic_fail_verdict(f"widget_not_found_exact: {text}")
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
            lambda ws: _sort_text_candidates(
                [w for w in ws
                 if text.lower() in (w.get('text', '') or '').lower()
                 or text.lower() in (w.get('hint', '') or '').lower()]),
            lambda e, x, y: e.click(x, y, desc,
                                    expectations=expectations,
                                    skip_before=True,
                                    fresh_before=fresh_before,
                                    auto_recover=auto_recover),
            index=index)
        if not found:
            print(f"❌ 未找到文本/占位符包含 '{text}' 的控件")
            _semantic_fail_verdict(f"widget_not_found: {text}")
        return ok

    @staticmethod
    def _parse_bounds_center(bounds_str: str) -> Optional[tuple]:
        """解析 bounds 字符串 '[x1,y1][x2,y2]'，返回中心坐标 (x, y)。

        复用 WidgetTreeDiff._parse_bounds（单一解析，支持负坐标：
        margin-top 为负的隐藏输入框 bounds 含负数，re.findall(r'\\d+')
        会丢负号导致中心算到错误位置）
        """
        b = WidgetTreeDiff._parse_bounds(bounds_str)
        if b:
            return ((b[0] + b[2]) // 2, (b[1] + b[3]) // 2)
        return None

    def _hdc_click_matches(self, desc: str, matcher_fn, engine_action,
                           expectations: Optional[dict] = None,
                           skip_before: bool = False,
                           fresh_before: bool = False,
                           auto_recover: bool = True,
                           index: Optional[int] = None) -> tuple:
        """hdc 快路径通用动作：dump 控件树（纯 hdc）→ matcher_fn(widgets)
        匹配 → 取 [index] 控件中心坐标 → engine_action(engine, x, y) 走
        HdcUITestEngine 坐标动作管线（纯 hdc）。

        查找 dump 同时保存为历史基线并传 skip_before=True，省去一次 before dump。

        Returns:
            (found, ok): found=True 表示找到控件并执行了动作；ok 为裁决结果。
            found=False 表示未找到/坐标无法解析（未执行动作）。
        """
        analyzer = self._dump_and_load("hdc_action")
        if analyzer is not None:
            matched = matcher_fn(analyzer.widgets)
            idx = index or 0
            # index 越界（负索引静默取最后/超长不执行）：明确报错而非静默歧义
            if matched and (idx < 0 or idx >= len(matched)):
                print(f"❌ index={idx} 越界（共 {len(matched)} 个候选，"
                      f"有效范围 0-{len(matched) - 1}）")
                _semantic_fail_verdict(f"index_out_of_range: {idx}")
                analyzer.cleanup()
                # 返回 (True, False)：路径已走到（非"未找到"），调用方
                # if found: return ok 直接透传——避免再打 not-found 双打
                return True, False
            if matched:
                w = matched[idx]
                coords = self._parse_bounds_center(w.get('bounds', ''))
                if coords:
                    x, y = coords
                    print(f"  匹配控件 [{idx}/{len(matched)}]: type={w.get('type', '')} "
                          f"text={w.get('text', '')}")
                    if len(matched) > 1:
                        print(f"  ℹ️ 共 {len(matched)} 个候选"
                              f"（已按最短文本+最小面积排序）：")
                        for i, m in enumerate(matched[:5]):
                            mt = (m.get('text') or m.get('hint') or '')[:30]
                            print(f"     [{i}] '{mt}' {m.get('bounds', '')}")
                    print(f"  点击坐标: ({x}, {y})")
                    self._save_and_cleanup(analyzer, save_route=True)
                    engine = HdcUITestEngine(device=self.device,
                                             history_dir=self.history_dir)
                    return True, engine_action(engine, x, y)
            analyzer.cleanup()
        return False, False

    def _hdc_input_matches(self, desc: str, matcher_fn, input_text: str,
                           expectations: Optional[dict] = None,
                           skip_before: bool = False,
                           fresh_before: bool = False,
                           auto_recover: bool = True,
                           index: Optional[int] = None) -> tuple:
        """hdc 快路径输入：dump 控件树（纯 hdc）→ matcher_fn(widgets) 匹配输入框
        → 坐标点击聚焦 → Ctrl+A 全选 → `uiInput text` 覆盖输入。

        查找 dump 保存为历史基线 + skip_before=True，省一次 before dump。

        Returns:
            (found, ok): found=False 表示未匹配到输入框（未执行动作）；
            found=True 时 ok 为裁决结果。
        """
        analyzer = self._dump_and_load("hdc_input")
        if analyzer is not None:
            matched = matcher_fn(analyzer.widgets)
            idx = index or 0
            # index 越界（负索引静默取最后/超长不执行）：明确报错而非静默歧义
            if matched and (idx < 0 or idx >= len(matched)):
                print(f"❌ index={idx} 越界（共 {len(matched)} 个候选，"
                      f"有效范围 0-{len(matched) - 1}）")
                _semantic_fail_verdict(f"index_out_of_range: {idx}")
                analyzer.cleanup()
                # 返回 (True, False)：非"未找到"，调用方直接透传避免双打
                return True, False
            if matched:
                w = matched[idx]
                coords = self._parse_bounds_center(w.get('bounds', ''))
                if coords:
                    x, y = coords
                    print(f"  匹配输入框 [{idx}/{len(matched)}]: "
                          f"type={w.get('type', '')} text={w.get('text', '')}")
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
                        # shlex.quote：hdc 会把参数 join 后交设备 shell 解释，
                        # 自由文本含空格/;/& 等会被拆分或注入（与 hdc_engine 同）
                        return engine._hdc_cmd(
                            ["shell", "uitest", "uiInput", "text",
                             shlex.quote(input_text)])

                    return True, engine._execute_hdc_and_compare(
                        [], desc, "输入", expectations=expectations,
                        skip_before=True, fresh_before=fresh_before,
                        auto_recover=auto_recover,
                        hdc_fn=hdc_fn)
            analyzer.cleanup()
        return False, False

    def click_by_id(self, key: str, operation: str = "",
                    expectations: Optional[dict] = None,
                    skip_before: bool = False,
                    fresh_before: bool = False,
                    auto_recover: bool = True) -> bool:
        desc = operation or f"点击 id/key='{key}' 的控件"
        # 控件树能读到 id/key 字段：按 id/key 搜索取中心坐标点击（覆盖播放器
        # 右侧控制条 Image 按钮等 .key() 组件）。
        return self._click_by_id_fuzzy(key, desc, expectations=expectations,
                                       skip_before=skip_before,
                                       fresh_before=fresh_before,
                                       auto_recover=auto_recover)

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
            _semantic_fail_verdict(f"widget_not_found: {key}")
        return ok

    def click_by_type(self, widget_type: str, operation: str = "",
                      expectations: Optional[dict] = None,
                      skip_before: bool = False,
                      fresh_before: bool = False,
                      auto_recover: bool = True) -> bool:
        desc = operation or f"点击类型为 '{widget_type}' 的控件"
        # 快路径：dump 按 type 匹配（lower 统一口径：CLI 示例传小写也能命中）
        found, ok = self._hdc_click_matches(
            desc,
            lambda ws: [w for w in ws
                        if (w.get('type') or '').lower() == widget_type.lower()],
            lambda e, x, y: e.click(x, y, desc,
                                    expectations=expectations,
                                    skip_before=True,
                                    fresh_before=fresh_before,
                                    auto_recover=auto_recover))
        if not found:
            print(f"❌ 未找到类型为 '{widget_type}' 的控件")
            _semantic_fail_verdict(f"widget_not_found: {widget_type}")
        return ok

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
        if not found:
            print(f"❌ 未找到文本为 '{text}' 的控件")
            _semantic_fail_verdict(f"widget_not_found: {text}")
        return ok

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
        if not found:
            print(f"❌ 未找到文本为 '{text}' 的控件")
            _semantic_fail_verdict(f"widget_not_found: {text}")
        return ok

    def input_by_text(self, target_text: str, input_text: str,
                      operation: str = "",
                      expectations: Optional[dict] = None,
                      skip_before: bool = False,
                      fresh_before: bool = False,
                      auto_recover: bool = True) -> bool:
        desc = operation or f"在 '{target_text}' 输入框中输入 '{input_text}'"

        # text 精确匹配
        found, ok = self._hdc_input_matches(
            desc,
            lambda ws: [w for w in ws if (w.get('text') or '') == target_text],
            input_text,
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
            input_text,
            expectations=expectations, skip_before=skip_before,
            fresh_before=fresh_before, auto_recover=auto_recover)
        if not found:
            print(f"❌ 未找到文本/占位符为 '{target_text}' 的输入框")
            _semantic_fail_verdict(f"input_not_found: {target_text}")
        return ok

    def input_by_type(self, widget_type: str, input_text: str,
                      operation: str = "",
                      expectations: Optional[dict] = None,
                      skip_before: bool = False,
                      fresh_before: bool = False,
                      auto_recover: bool = True) -> bool:
        desc = operation or f"在 {widget_type} 中输入 '{input_text}'"

        found, ok = self._hdc_input_matches(
            desc,
            lambda ws: [w for w in ws
                        if (w.get('type') or '').lower() == widget_type.lower()],
            input_text,
            expectations=expectations, skip_before=skip_before,
            fresh_before=fresh_before, auto_recover=auto_recover)
        if not found:
            print(f"❌ 未找到类型为 '{widget_type}' 的输入框")
            _semantic_fail_verdict(f"input_not_found: {widget_type}")
        return ok

    def swipe_direction(self, direction: str, distance: int = 60,
                        operation: str = "",
                        expectations: Optional[dict] = None,
                        skip_before: bool = False,
                        fresh_before: bool = False,
                        auto_recover: bool = True) -> bool:
        desc = operation or f"向 {direction} 滑动 {distance}"
        # 纯 hdc uiInput swipe，从屏幕中心按方向滑动
        w, h = self._screen_wh()
        cx, cy = w // 2, h // 2
        d = max(10, int(distance))
        step = {
            'up': (cx, cy - d), 'down': (cx, cy + d),
            'left': (cx - d, cy), 'right': (cx + d, cy),
        }.get((direction or '').strip().lower())
        if not step:
            print(f"❌ 不支持的滑动方向: {direction}")
            _semantic_fail_verdict(f"invalid_direction: {direction}")
            return False
        ex, ey = step
        return self.swipe(cx, cy, ex, ey, desc,
                          expectations=expectations,
                          skip_before=skip_before,
                          fresh_before=fresh_before,
                          auto_recover=auto_recover)

    def go_back(self, operation: str = "",
                expectations: Optional[dict] = None,
                skip_before: bool = False,
                fresh_before: bool = False,
                auto_recover: bool = True) -> bool:
        desc = operation or "按下返回键"
        # 纯 hdc uiInput keyEvent Back
        return self.key_back(desc, expectations=expectations,
                             skip_before=skip_before,
                             fresh_before=fresh_before,
                             auto_recover=auto_recover)

    # === 以下为纯检查/截图操作，不执行 dumpLayout 差异比较 ===

    def check_dialog(self, dialog_type: str = "Dialog") -> bool:
        """dump 控件树，检查是否存在指定类型的弹窗控件（纯 hdc）"""
        target = (dialog_type or 'Dialog').lower()
        a = self._dump_and_load("check_dialog")
        if a is not None:
            try:
                for w in a.widgets:
                    t = (w.get('type', '') or '').lower()
                    # 无 type 节点不算弹窗：旧逻辑 `'' in target` 恒真，
                    # 导致任何缺 type 字段的节点都误报“检测到 Dialog 弹窗”
                    if not t:
                        continue
                    if str(w.get('visible', 'true') or 'true').lower() == 'false':
                        continue
                    if t == target or target in t:
                        print(f"✅ 检测到 {dialog_type} 弹窗")
                        return True
            finally:
                a.cleanup()
        print(f"❌ 未检测到 {dialog_type} 弹窗")
        return False

    def toggle_state(self, x: Optional[int] = None, y: Optional[int] = None,
                     index: Optional[int] = None,
                     expect_checked: Optional[bool] = None) -> bool:
        """读取（或断言）页面 Toggle/开关状态

        定位方式：
          - --x/--y：坐标命中 bounds 的 Toggle（推荐，和 click 坐标一致）
          - --index：页面第 N 个 Toggle（0-based，按控件树顺序）
          - 都不给：列出页面全部 Toggle
        expect_checked 非 None 时做断言（状态不符返回 False）。
        """
        analyzer = self._dump_and_load("toggle")
        if analyzer is None:
            return False
        try:
            toggles = [w for w in analyzer.widgets
                       if (w.get('type', '') or '').lower() in ('toggle', 'switch')]
            if not toggles:
                print("❌ 页面上未找到 Toggle 控件")
                return False
            if x is not None and y is not None:
                targets = [w for w in toggles if _point_in_bounds(w, x, y)]
                if not targets:
                    print(f"❌ 坐标 ({x},{y}) 未命中任何 Toggle，页面 Toggle 列表：")
                    _print_toggle_list(toggles, analyzer.widgets)
                    return False
            elif index is not None:
                if index < 0 or index >= len(toggles):
                    print(f"❌ --index {index} 越界（页面共 {len(toggles)} 个 Toggle）")
                    _print_toggle_list(toggles, analyzer.widgets)
                    return False
                targets = [toggles[index]]
            else:
                targets = toggles

            ok_all = True
            for w in targets:
                checked = (w.get('checked', '') or '').lower() == 'true'
                idx = toggles.index(w)
                label = _nearest_label(analyzer.widgets, w)
                print(f"Toggle #{idx} checked={'true' if checked else 'false'} "
                      f"bounds={w.get('bounds', '')}"
                      + (f" label={label}" if label else ""))
                if expect_checked is not None and checked != expect_checked:
                    ok_all = False
            if expect_checked is not None:
                exp = 'true' if expect_checked else 'false'
                print(f"✅ Toggle 状态符合期望 checked={exp}" if ok_all
                      else f"❌ Toggle 状态不符合期望 checked={exp}")
            return ok_all
        finally:
            analyzer.cleanup()

    def picker_set(self, column_x: int, value: str,
                   max_iter: int = 10) -> bool:
        """把滚轮（Picker）列滚动到目标值

        定位：column-x 水平 ±60px 内的纯数值 Text 视为一列。
        策略：每轮 dump 读取列内可见值与当前选中值（列中心最接近者）：
          - 已达目标 → 成功
          - 目标可见 → 直接点击该项
          - 目标不可见 → 按目标方向滑动一屏（约 3 项高度，用中位项距推算）
        实测替代手工试错 swipe（恢复自定义时间场景曾手滑 8+ 次）。
        """
        # value 须为纯数值（定位策略依赖数值比较）；非数值提前结构化报错
        try:
            float(value)
        except (TypeError, ValueError):
            print(f"❌ value 须为纯数值，收到: {value!r}")
            _semantic_fail_verdict(f"picker_invalid_value: {value}")
            return False
        for _ in range(max(1, max_iter)):
            analyzer = self._dump_and_load("picker")
            if analyzer is None:
                print("❌ 获取控件树失败（picker）")
                _semantic_fail_verdict("picker_dump_failed")
                return False
            try:
                items = []  # (top, bottom, num, bounds, raw)
                for w in analyzer.widgets:
                    if (w.get('type', '') or '').lower() != 'text':
                        continue
                    txt = (w.get('text', '') or '').strip()
                    if not txt:
                        continue
                    try:
                        num = float(txt)
                    except ValueError:
                        continue
                    b = WidgetTreeDiff._parse_bounds(w.get('bounds', ''))
                    if not b:
                        continue
                    cx = (b[0] + b[2]) / 2
                    if abs(cx - column_x) > 60:
                        continue
                    items.append((b[1], b[3], num, b, txt))
                if not items:
                    print(f"❌ 未找到 x≈{column_x} 的滚轮数值列")
                    _semantic_fail_verdict(f"picker_not_found: {column_x}")
                    return False
                items.sort(key=lambda t: t[0])
                col_top = min(t[0] for t in items)
                col_bottom = max(t[1] for t in items)
                col_mid = (col_top + col_bottom) / 2
                best = min(items, key=lambda t: abs((t[0] + t[1]) / 2 - col_mid))
                cur_num, cur_txt = best[2], best[4]
                target_num = float(value)
                if cur_num == target_num:
                    print(f"✅ 滚轮已到达目标值 {value}（列 x≈{column_x}，"
                          f"当前 {cur_txt}）")
                    return True
                visible = [t for t in items if t[2] == target_num]
                if visible:
                    tb = visible[0][3]
                    cx_click = (tb[0] + tb[2]) // 2
                    cy_click = (tb[1] + tb[3]) // 2
                    print(f"ℹ️ 目标 {value} 可见，点击 ({cx_click},{cy_click})")
                    self._hdc_cmd(["shell", "uitest", "uiInput",
                                   "click", str(cx_click), str(cy_click)])
                    continue
                # 目标不可见 → 滑动（步长 ≈ 3 项，用中位项距）
                if len(items) >= 2:
                    gaps = sorted(items[i + 1][0] - items[i][0]
                                  for i in range(len(items) - 1))
                    step = max(gaps[len(gaps) // 2], 40)
                else:
                    step = 117
                dist = int(step * 3)
                y_lo, y_hi = col_top + 15, col_bottom - 15
                if target_num > cur_num:
                    y1, y2 = y_hi, max(y_lo, y_hi - dist)   # 向上滑
                else:
                    y1, y2 = y_lo, min(y_hi, y_lo + dist)   # 向下滑
                print(f"ℹ️ 目标 {value} 不可见（当前 {cur_txt}），滑动列 "
                      f"({column_x},{y1})→({column_x},{y2})")
                self._hdc_cmd(["shell", "uitest", "uiInput",
                               "swipe", str(column_x), str(y1),
                               str(column_x), str(y2)])
            finally:
                analyzer.cleanup()
        print(f"❌ 超过 {max_iter} 次迭代仍未到达目标值 {value}")
        _semantic_fail_verdict(f"picker_not_reached: {value}")
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
            # type 精确串匹配但大小写不敏感（与 analyzer search_by_type 同口径）
            if widget_type and (w.get('type', '') or '').lower() != widget_type.lower():
                return False
            return True

        # 纯 hdc：dump 控件树匹配；未命中时在 wait_time 内轮询（等异步渲染）
        deadline = time.time() + max(0, wait_time)
        while True:
            a = self._dump_and_load("checkex")
            if a is not None:
                try:
                    if any(_match(w) for w in a.widgets):
                        print(f"✅ 控件存在: {cond_str}")
                        return True
                finally:
                    a.cleanup()
            if time.time() >= deadline:
                break
            time.sleep(0.5)
        print(f"❌ 控件不存在: {cond_str}")
        return False

    def find_and_print(self, text: Optional[str] = None, key: Optional[str] = None,
                       widget_type: Optional[str] = None) -> bool:
        conditions = []
        if text: conditions.append(f"text='{text}'")
        if key: conditions.append(f"key='{key}'")
        if widget_type: conditions.append(f"type='{widget_type}'")
        if not conditions:
            print("❌ 至少指定一个搜索条件 (text/key/type)")
            return False
        cond_str = " + ".join(conditions)
        if not self.check_component(text=text, key=key, widget_type=widget_type):
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
        # 返回内容敏感签名（标题栏签名 _compute_page_signature 只覆盖 y100~320，
        # 对滚动内容不敏感，不能用于判断"是否滚动了"）
        sig = self._scroll_signature(analyzer.widgets)
        analyzer.cleanup()
        return matched, sig

    @staticmethod
    def _max_y_of_widgets(widgets) -> int:
        import re
        max_y = 0
        for w in widgets:
            nums = re.findall(r'-?\d+', w.get('bounds', '') or '')
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
            m = re.findall(r'-?\d+', w.get('bounds', '') or '')
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
            nums = re.findall(r'-?\d+', w.get('bounds', '') or '')
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
                nums = re.findall(r'-?\d+', pw.get('bounds', '') or '')
                if len(nums) >= 4:
                    return (int(nums[0]), int(nums[1]), int(nums[2]), int(nums[3]))
            p = pw.get('parent_index')
            depth += 1
        return None

    def _is_bottom_clipped(self, w: dict, screen_h: int, margin: int = 80) -> bool:
        """判断控件是否被屏幕底部截断（bottom 贴/超屏底，只露出残影）。"""
        import re
        nums = re.findall(r'-?\d+', w.get('bounds', '') or '')
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
                    _semantic_fail_verdict(f"widget_clipped: {text}")
                    return False
                if p < polls - 1:
                    time.sleep(settle_interval)
            if i >= max_swipes:
                break
            moved, _sig, working_region = self._adaptive_scroll(
                working_region, settle_interval)
            if not moved:
                print(f"❌ 列表已到底，未找到 '{text}'")
                _semantic_fail_verdict(f"widget_not_found: {text}")
                return False
            time.sleep(settle_interval)
        print(f"❌ 已滚动 {max_swipes} 屏，未找到 '{text}'")
        _semantic_fail_verdict(f"widget_not_found: {text}")
        return False

    def collect_texts(self, regex: str, max_swipes: int = 6,
                      settle_interval: float = 1.0) -> List[str]:
        """滚动收集去重计数：数列表项数类验证的固化（如 AI 事件 18 开关 vs 副标题 /16）。

        1. 当前屏 dump，提取所有匹配 regex 的 text/hint 文本（去重保序；
           hint-only 控件如无 text 的输入框同样收集）
        2. 复用 _adaptive_scroll 滚动（内容签名验证真实位移，不可滚区域不误判）
        3. 列表到底（moved=False）或连续 2 屏无新增即停，返回去重列表（调用方输出计数）
        """
        import re
        import time
        pattern = re.compile(regex)
        collected: List[str] = []
        seen = set()
        working_region = None
        stale_rounds = 0
        for i in range(max_swipes + 1):
            a = self._dump_and_load("collect")
            if a:
                for w in a.widgets:
                    t = (w.get('text') or '').strip()
                    if not t:
                        # text 为空时兜底匹配 hint（hint-only 控件漏采会导致计数偏少）
                        t = (w.get('hint') or '').strip()
                    if not t or pattern.search(t) is None:
                        continue
                    if t not in seen:
                        seen.add(t)
                        collected.append(t)
                a.cleanup()
            if i >= max_swipes:
                break
            before = len(collected)
            moved, _sig, working_region = self._adaptive_scroll(
                working_region, settle_interval)
            if len(collected) == before:
                stale_rounds += 1
                if stale_rounds >= 2:
                    break
            else:
                stale_rounds = 0
            if not moved:
                break
            time.sleep(settle_interval)
        return collected

    def screenshot(self, save_path: str) -> bool:
        import subprocess
        import os
        from utils.common import is_dark_image
        # 纯 hdc screenCap + recv（约 0.6s）
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
                    # 全黑检测：熄屏（黑帧）则静默唤醒+解锁后自动重截一次；
                    # 登录页等 FLAG_SECURE 页面的黑图是预期，唤醒无效，仍黑
                    # 时由调用方走 _diagnose_dark_screenshot 提示
                    if is_dark_image(save_path) and self._ensure_awake():
                        r3 = subprocess.run(cmd + ['shell', 'uitest', 'screenCap', '-p', device_path],
                                            capture_output=True, text=True, timeout=30)
                        if r3.returncode == 0:
                            r4 = subprocess.run(cmd + ['file', 'recv', device_path, save_path],
                                                capture_output=True, text=True, timeout=30)
                            # recv 失败时旧黑图仍在：不能当成功返回过期黑图，
                            # 降级走下方失败分支
                            if r4.returncode != 0:
                                print(f"❌ 重截后拉取失败: {r4.stderr or r4.stdout}")
                                return False
                        # 重截后再验一次：仍黑说明是 FLAG_SECURE 页面或唤醒无效，
                        # 保留黑图走调用方诊断；不再黑说明重截成功
                        if is_dark_image(save_path):
                            print("⚠️ 重截后仍为全黑（可能禁止截屏页面），"
                                  "请结合 DIAGNOSE 判断")
                    if os.path.exists(save_path) and os.path.getsize(save_path) > 0:
                        print(f"✅ 截图已保存: {save_path}")
                        return True
                print(f"❌ 拉取截图失败: {r2.stderr or r2.stdout}")
            else:
                print(f"❌ 截图失败: {r1.stderr or r1.stdout}")
        except Exception as e:
            print(f"❌ 截图失败: {e}")
        return False

    def aa_start(self, uri: str, action: str = "ohos.want.action.viewData",
                 bundle_name: Optional[str] = None,
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
        import shlex

        bundle_name = bundle_name or default_bundle()
        # 构建 aa start 命令。shlex.quote：hdc 会把参数 join 后交设备
        # shell 解释，URI 含 &/| 等特殊字符时会被截断，必须 quote
        cmd = ["shell", "aa", "start"]
        if action:
            cmd.extend(["-A", shlex.quote(action)])
        if uri:
            cmd.extend(["-U", shlex.quote(uri)])
        if bundle_name:
            cmd.extend(["-b", shlex.quote(bundle_name)])
        if ability_name:
            cmd.extend(["-a", shlex.quote(ability_name)])
        if module_name:
            cmd.extend(["-m", shlex.quote(module_name)])

        # 处理额外参数
        if params:
            for key, value in params.items():
                # key/value 一并 quote（与 action/uri 同纪律：hdc join 后
                # 交设备 shell 解释，含空格/;/& 会被拆分或注入）
                if isinstance(value, bool):
                    cmd.extend(["--pb", shlex.quote(key), shlex.quote(str(value).lower())])
                elif isinstance(value, int):
                    cmd.extend(["--pi", shlex.quote(key), shlex.quote(str(value))])
                else:
                    cmd.extend(["--ps", shlex.quote(key), shlex.quote(str(value))])

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
