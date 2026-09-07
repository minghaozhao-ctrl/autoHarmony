#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""断言检查器

对操作后的控件树状态进行精确验证，将描述性差异报告转化为可判定的 pass/fail。
支持：路由断言、文本存在/消失断言、无变化断言、弹窗断言、控件状态断言。
"""
from typing import List, Optional, Dict


class AssertionResult:
    """单条断言结果"""

    def __init__(self, passed: bool, description: str, detail: str = ""):
        self.passed = passed
        self.description = description
        self.detail = detail

    def __str__(self):
        icon = "✅" if self.passed else "❌"
        s = f"{icon} {self.description}"
        if self.detail:
            s += f" — {self.detail}"
        return s


class AssertionChecker:
    """断言检查器：对操作后的控件树状态进行验证"""

    def __init__(self, widgets: List[Dict], route: Optional[List[str]] = None,
                 change_report=None):
        """
        Args:
            widgets: 操作后的控件树 widget 列表
            route: 操作后的当前路由栈
            change_report: 差异报告（用于 no_change 断言）
        """
        self.widgets = widgets
        self.route = route
        self.change_report = change_report

    def check_route(self, expected_route: str) -> AssertionResult:
        """检查当前路由栈是否包含期望路由（部分匹配，不区分大小写）"""
        if not self.route:
            return AssertionResult(
                False, f"期望路由包含 '{expected_route}'",
                "无法获取当前路由（hm-app-bridge-mcp 未连接？）")
        matched = [r for r in self.route if expected_route.lower() in r.lower()]
        if matched:
            return AssertionResult(
                True, f"期望路由包含 '{expected_route}'",
                f"匹配: {matched[0]}")
        top = self.route[-1] if self.route else "(空)"
        return AssertionResult(
            False, f"期望路由包含 '{expected_route}'",
            f"当前路由栈顶: {top}")

    def _sample_screen_texts(self, exclude: str = "", limit: int = 8) -> str:
        """页面现有文本样本（断言失败时帮助 AI 对照实际值与期望值）"""
        texts = []
        for w in self.widgets:
            t = (w.get('text', '') or '').strip()
            if t and t not in texts:
                texts.append(t)
            if len(texts) >= limit * 2:
                break
        shown = [t[:20] for t in texts if t != exclude][:limit]
        return " | ".join(f"\"{t}\"" for t in shown) if shown else "(页面无可读文本)"

    def check_text_exists(self, text: str) -> AssertionResult:
        """检查页面是否存在包含指定文本的控件"""
        found = [w for w in self.widgets
                 if text.lower() in (w.get('text', '') or '').lower()
                 or text.lower() in (w.get('hint', '') or '').lower()]
        if found:
            return AssertionResult(
                True, f"期望文本存在 '{text}'",
                f"匹配 {len(found)} 个控件")
        return AssertionResult(
            False, f"期望文本存在 '{text}'",
            f"未找到匹配控件；页面现有文本: {self._sample_screen_texts(text)}")

    def check_text_gone(self, text: str) -> AssertionResult:
        """检查页面是否已不存在包含指定文本的控件"""
        found = [w for w in self.widgets
                 if text.lower() in (w.get('text', '') or '').lower()
                 or text.lower() in (w.get('hint', '') or '').lower()]
        if not found:
            return AssertionResult(
                True, f"期望文本消失 '{text}'", "控件已不存在")
        actual = (found[0].get('text', '') or found[0].get('hint', ''))[:40]
        return AssertionResult(
            False, f"期望文本消失 '{text}'",
            f"仍存在 {len(found)} 个匹配控件，实际值: \"{actual}\"")

    def check_no_change(self) -> AssertionResult:
        """检查操作是否未产生变化"""
        if self.change_report is None:
            return AssertionResult(False, "期望无变化", "无差异报告可用")
        if self.change_report.route_changed:
            return AssertionResult(False, "期望无变化", "路由发生了变化")
        if self.change_report.changes:
            return AssertionResult(
                False, "期望无变化",
                f"检测到 {len(self.change_report.changes)} 处变化")
        return AssertionResult(True, "期望无变化", "未检测到变化")

    def check_state(self, spec: str) -> AssertionResult:
        """检查控件状态属性（如 "状态灯:checked=true"）

        格式: <文本>:<属性>=<期望值>
        定位包含该文本的控件后，沿自身及父容器链查找带该属性的控件
        （覆盖 "Text 标签 + 同行 Switch 容器" 的常见布局）。
        """
        try:
            text, attr_val = spec.split(':', 1)
            attr, expected = attr_val.split('=', 1)
            text, attr, expected = text.strip(), attr.strip(), expected.strip().lower()
        except ValueError:
            return AssertionResult(False, f"状态断言 '{spec}'",
                                   "格式错误，应为 '文本:属性=值'（如 状态灯:checked=true）")

        matched = [w for w in self.widgets
                   if text.lower() in (w.get('text', '') or '').lower()
                   or text.lower() in (w.get('hint', '') or '').lower()]
        if not matched:
            return AssertionResult(False, f"状态断言 '{spec}'",
                                   f"未找到文本包含 '{text}' 的控件")

        for w in matched:
            if self._state_attr_match(w, attr, expected):
                return AssertionResult(
                    True, f"状态断言 '{spec}'",
                    f"控件 type={w.get('type', '')} {attr}={expected}")
        return AssertionResult(
            False, f"状态断言 '{spec}'",
            f"找到 {len(matched)} 个文本匹配控件，但无 {attr}={expected} 状态")

    def _state_attr_match(self, widget: Dict, attr: str, expected: str) -> bool:
        """沿 widget 自身及父容器链查找属性值是否匹配（宽松字符串比较）"""
        cur = widget
        guard = 0
        while cur is not None and guard < 20:
            value = cur.get(attr, '')
            if str(value).strip().lower() == expected:
                return True
            parent_idx = cur.get('parent_index')
            if parent_idx is None or not (0 <= parent_idx < len(self.widgets)):
                break
            cur = self.widgets[parent_idx]
            guard += 1
        return False

    def check_dialog(self, dialog_type: str = "Dialog") -> AssertionResult:
        """检查是否出现了指定类型的弹窗"""
        target = dialog_type.lower()
        found = [w for w in self.widgets
                 if w.get('type', '').lower() == target
                 or w.get('type', '').lower() in target
                 or target in w.get('type', '').lower()]
        if found:
            return AssertionResult(
                True, f"期望弹窗出现 '{dialog_type}'",
                f"检测到 {len(found)} 个弹窗控件")
        return AssertionResult(
            False, f"期望弹窗出现 '{dialog_type}'", "未检测到弹窗")

    def check_all(self, expectations: Dict) -> List[AssertionResult]:
        """批量检查所有期望

        Args:
            expectations: 期望字典，支持以下键：
                - route: str — 期望路由包含
                - text_exists: List[str] — 期望文本存在
                - text_gone: List[str] — 期望文本消失
                - no_change: bool — 期望无变化
                - dialog: str — 期望弹窗类型
                - state: List[str] — 期望控件状态（"文本:属性=值"）
        """
        results = []
        if 'route' in expectations and expectations['route']:
            results.append(self.check_route(expectations['route']))
        if 'text_exists' in expectations:
            texts = expectations['text_exists']
            if isinstance(texts, str):
                texts = [texts]
            for t in texts:
                results.append(self.check_text_exists(t))
        if 'text_gone' in expectations:
            texts = expectations['text_gone']
            if isinstance(texts, str):
                texts = [texts]
            for t in texts:
                results.append(self.check_text_gone(t))
        if expectations.get('no_change'):
            results.append(self.check_no_change())
        if 'dialog' in expectations and expectations['dialog']:
            results.append(self.check_dialog(expectations['dialog']))
        if 'state' in expectations:
            specs = expectations['state']
            if isinstance(specs, str):
                specs = [specs]
            for s in specs:
                results.append(self.check_state(s))
        return results

    @staticmethod
    def print_results(results: List[AssertionResult]) -> bool:
        """打印断言结果，返回是否全部通过"""
        if not results:
            return True
        print("\n" + "=" * 60)
        print("🔍 断言验证")
        print("=" * 60)
        all_passed = True
        for r in results:
            print(f"  {r}")
            if not r.passed:
                all_passed = False
        passed = sum(1 for r in results if r.passed)
        total = len(results)
        icon = "✅" if all_passed else "❌"
        print(f"\n  {icon} 断言结果: {passed}/{total} 通过")
        print("=" * 60)
        return all_passed
