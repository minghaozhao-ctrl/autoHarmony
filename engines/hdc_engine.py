#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""HdcUITestEngine：基于 hdc shell uitest 的坐标操作引擎（自动差异比较）"""
import os
import shlex
import time
import tempfile
from typing import List, Optional

from analyzers.widget_tree import WidgetTreeAnalyzer
from engines.diff_engine import WidgetTreeDiff, AutoDiffManager, ChangeReport
from analyzers.crash_detector import CrashDetector
from utils.common import run_hdc_command
from engines.verdict import ActionPipeline

from .helpers import (_archive_dump_file, _dump_layout_remote_cat,
                      _record_page_path)


class HdcUITestEngine:
    """HDC UI 测试引擎

    封装 HDC 操作 + 控件树差异比较 + 自动闪退检测。
    一条命令完成整套流程：执行 UI 操作 → 比较控件树差异 → 检测闪退。
    """

    def __init__(self, device: Optional[str] = None, history_dir: Optional[str] = None):
        self.device = device
        self.manager = AutoDiffManager(history_dir, device=device)
        self.history_dir = self.manager.history_dir
        self.diff = WidgetTreeDiff()
        self.crash_detector = CrashDetector(device=device)
        self._cached_route = None
        # --fast 全局模式：settle 缩短 + 静默 diff/页面摘要（批量回归用）
        self.fast_mode = os.environ.get('HMUITEST_FAST') == '1'
        # 最近一次成功 dump 的节点数（完整性校验：骤降时重试）
        self._last_dump_count = 0
        # 屏幕宽高缓存（--pct 比例坐标换算用）
        self._screen_cache = None
        # 待裁决的点击坐标（_execute_hdc_and_compare 设置；pipeline 读取后清除，
        # 用于 CLICK_ON_DISABLED 禁用预检与 NO_CHANGE 目标提示）
        self._pending_point: Optional[tuple] = None

    def close(self):
        """兼容接口：纯 hdc 引擎无常驻连接需要释放（保留以兼容既有调用点）。"""
        return None

    def _hdc_cmd(self, cmd_parts: List[str], timeout: int = 30) -> tuple:
        """执行 hdc 命令"""
        cmd = ["hdc"]
        if self.device:
            cmd.extend(["-t", self.device])
        cmd.extend(cmd_parts)
        return run_hdc_command(cmd, timeout)

    def _dump_once(self, tag: str = "layout") -> Optional[WidgetTreeAnalyzer]:
        """执行一次完整 dump（快路径单命令 → 慢路径回退），返回 analyzer 或 None

        快路径：一条 shell 内 `dumpLayout -p 远程文件; cat 远程文件`，比
        `dumpLayout + file recv` 两条 hdc 调用省约 0.6s（实测 2.13s→1.51s）。
        失败时回退带 -a 的慢路径；再失败则触发崩溃检测并返回 None。
        """
        remote = f'/data/local/tmp/_uitest_{tag}.json'
        tmp_path = os.path.join(
            tempfile.gettempdir(),
            f'hdc_layout_{tag}_{int(time.time() * 1000000)}.json')
        ok, out = _dump_layout_remote_cat(self, remote)
        if ok and out and out.lstrip().startswith('{'):
            try:
                with open(tmp_path, 'w', encoding='utf-8') as f:
                    f.write(out)
            except OSError as e:
                print(f"⚠️ 写入本地 dump 文件失败: {e}")
            else:
                analyzer = WidgetTreeAnalyzer(
                    json_file=tmp_path, device=self.device)
                analyzer._temp_file = tmp_path
                if analyzer.load_tree():
                    _archive_dump_file(analyzer, self.device)
                    _record_page_path(analyzer, self.device)
                    return analyzer
                analyzer.cleanup()

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

        _archive_dump_file(analyzer, self.device)
        _record_page_path(analyzer, self.device)
        return analyzer

    def _dump_and_load(self, tag: str = "layout") -> Optional[WidgetTreeAnalyzer]:
        """从设备获取控件树并加载（含完整性校验）

        控件树节点数相对上次骤降（<50%）时视为疑似不完整 dump（实测同页面
        曾偶发 141→63 节点），自动重试一次并取更完整的结果。
        """
        analyzer = self._dump_once(tag)
        if analyzer is None:
            return None
        cnt = len(analyzer.widgets)
        prev = getattr(self, '_last_dump_count', 0)
        if prev >= 30 and cnt < prev * 0.5:
            print(f"⚠️ 控件树疑似不完整（{cnt} 个节点，上次 {prev}），重试一次...")
            retry = self._dump_once(tag + "_retry")
            if retry is not None:
                if len(retry.widgets) > cnt:
                    analyzer.cleanup()
                    analyzer = retry
                else:
                    retry.cleanup()
        self._last_dump_count = len(analyzer.widgets)
        return analyzer

    def _screen_wh(self) -> tuple:
        """屏幕宽高 (px)，带缓存。走快速 dump 的 get_screen_bounds()（纯 hdc ~0.45s），
        失败回退默认值（WidgetTreeDiff.DEFAULT_SCREEN_W/H，1260x2720）。供 --pct 比例坐标换算等使用。"""
        if self._screen_cache is not None:
            return self._screen_cache
        w, h = None, None
        a = self._dump_and_load("screenwh")
        if a:
            sb = a.get_screen_bounds()
            if sb:
                w, h = sb[2], sb[3]
            a.cleanup()
        if not w or not h:
            w, h = WidgetTreeDiff.DEFAULT_SCREEN_W, WidgetTreeDiff.DEFAULT_SCREEN_H
        self._screen_cache = (w, h)
        return w, h

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
            if not getattr(self, 'fast_mode', False):
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
                                 hdc_fn=None,
                                 point: Optional[tuple] = None) -> bool:
        """通用流程：前置快照 → 执行操作 → 统一裁决管线

        操作原语走 `hdc shell uitest uiInput`（约 0.34s），无需任何 daemon 依赖。

        Args:
            ui_input_args: `uitest uiInput` 子命令和参数
            desc: 操作描述
            action_name: 操作名称（用于错误提示，如 "点击"）
            expectations: 期望断言字典（route/text_exists/text_gone/no_change/dialog）
            skip_before: 跳过 before-dump（批量模式复用上一步 after-state）
            fresh_before: 强制 fresh before-dump（禁用历史基准复用）
            auto_recover: 被弹窗挡住时自动清理并重试一次
            is_back: 返回类动作（无变化标记 BACK_INEFFECTIVE）
            hdc_fn: 可选，`fn() -> (success, output)` 多步 hdc 操作（如输入：
                点击聚焦 → Ctrl+A → text）。提供时替代单条 ui_input_args。
            point: 点击类动作的坐标 (x, y)。供裁决管线做禁用预检
                （CLICK_ON_DISABLED）与 NO_CHANGE 目标提示。

        Returns:
            裁决+断言是否全部通过（决定 exit code）
        """
        self._pending_point = point

        def action_fn():
            if hdc_fn is not None:
                success, output = hdc_fn()
            else:
                success, output = self._hdc_cmd(
                    ["shell", "uitest", "uiInput"] + ui_input_args
                )
            if success:
                return True
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
              auto_recover: bool = True, uinput: bool = False) -> bool:
        """点击坐标并比较变化

        Args:
            uinput: True 时用 `uinput -T -c` 直接注入触摸（更快，跳过 uitest
                服务开销），适合连续批量点击；默认走 uitest uiInput click。
                对应 CLI `ui click --uinput`（勿与全局 --fast 静默模式混淆）
        """
        desc = operation or f"点击 ({x}, {y})"
        if uinput:
            return self._execute_hdc_and_compare(
                [], desc, "点击",
                expectations=expectations, skip_before=skip_before,
                fresh_before=fresh_before, auto_recover=auto_recover,
                point=(x, y),
                hdc_fn=lambda: self._hdc_cmd(
                    ["shell", f"uinput -T -c {x} {y}"]))
        return self._execute_hdc_and_compare(
            ["click", str(x), str(y)], desc, "点击",
            expectations=expectations, skip_before=skip_before,
            fresh_before=fresh_before, auto_recover=auto_recover,
            point=(x, y))

    def click_sequence(self, points: List[tuple], interval: float = 0.2,
                       operation: str = "",
                       expectations: Optional[dict] = None,
                       skip_before: bool = False,
                       fresh_before: bool = False) -> bool:
        """序列点击：一条 hdc shell 批量 uinput 注入，跳过逐步 diff，仅末尾校验

        高频点击场景（如密码键盘连点 N 轮触发锁定）专用：每个点若单独走
        click 管线（2 次 dump + diff + 复查 ≈ 2s/点），12 点×5 轮必然超时；
        本方法全部点击在一条 shell 内完成（uinput ~50ms/点 + interval 间隔），
        之后仅做一次统一裁决（SUCCESS/NO_CHANGE + 页面状态摘要 + 断言）。

        Args:
            points: [(x1, y1), (x2, y2), ...] 坐标序列
            interval: 相邻点击间隔秒数（默认 0.2s）
        """
        desc = operation or f"序列点击 {len(points)} 个点"
        if not points:
            print("❌ click_sequence: 坐标序列为空")
            return False
        seq = ("; sleep %.2f; " % max(interval, 0.0)).join(
            f"uinput -T -c {int(px)} {int(py)}" for px, py in points)
        print(f"⚡ 快速序列点击: {len(points)} 点（uinput 批量注入，间隔 {interval}s）")
        return self._execute_hdc_and_compare(
            [], desc, "序列点击",
            expectations=expectations, skip_before=skip_before,
            fresh_before=fresh_before,
            hdc_fn=lambda: self._hdc_cmd(["shell", seq]))

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
            point=(x, y))

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
            point=(x, y))

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
                   auto_recover: bool = True,
                   clear_first: bool = False) -> bool:
        """输入文本并比较变化

        Args:
            clear_first: True 时先清空当前焦点输入框再注入（DEL×6 → Ctrl+A
                全选 → DEL，一条 shell 完成）。用于隐藏 TextInput
                （如密码点位框）残留字符导致拼接的实测事故：
                `ui input "111111"` 实际输入 "<残留>111111" 造成密码误报。
        """
        desc = operation or (f'清空并输入: "{text}"' if clear_first
                             else f'输入文本: "{text}"')

        def hdc_fn():
            if clear_first:
                # 清空链：DEL×6（覆盖常见 6 位密码残留）→ Ctrl+A 全选 → DEL。
                # 必须逐条独立 hdc 注入——一条 shell 内多次 uitest 调用会触发
                # "uitest-api does not allow calling concurrently" 全部失效
                # （实测 for 循环批量执行静默失败）。
                # 顺序与 docstring 一致：DEL×6 → Ctrl+A 全选 → DEL
                clear_cmds = [["shell", "uitest", "uiInput",
                               "keyEvent", "2055"]] * 6
                clear_cmds += [["shell", "uitest", "uiInput",
                                "keyEvent", "2072", "2017"],   # Ctrl+A 全选
                               ["shell", "uitest", "uiInput",
                                "keyEvent", "2055"]]           # DEL 删除选区
                for cmd in clear_cmds:
                    ok, out = self._hdc_cmd(cmd, timeout=10)
                    if not ok:
                        return False, out
            # shlex.quote：hdc 会把参数 join 后交设备 shell 解释，
            # 文本含空格/特殊字符时必须 quote 为单 token
            return self._hdc_cmd(["shell", "uitest", "uiInput", "text",
                                  shlex.quote(text)])

        return self._execute_hdc_and_compare(
            [], desc, "输入",
            expectations=expectations, skip_before=skip_before,
            fresh_before=fresh_before, auto_recover=auto_recover,
            hdc_fn=hdc_fn)

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
            is_back=True, check_exit=True)
