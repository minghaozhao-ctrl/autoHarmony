#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
控件树分析器（类库，CLI 入口见 hmuitest.py）

用于解析HarmonyOS UITest框架生成的控件树JSON文件，提供概览和搜索功能。
支持本地文件模式和远程原子操作模式（直接从设备获取并分析）。
"""

import json
import os
import tempfile
import re
from typing import Dict, List, Optional, Tuple
from collections import defaultdict
from engines.diff_engine import WidgetTreeDiff
from utils.common import run_hdc_command


class WidgetTreeAnalyzer:
    """控件树分析器"""
    
    # HDC 远程操作的默认配置
    REMOTE_PATH = "/data/local/tmp/layout.json"
    TEMP_FILE_PREFIX = "widget_tree_"

    # 弹窗/覆盖层控件类型
    OVERLAY_TYPES = {
        'dialog', 'alertdialog', 'customdialog', 'sheet', 'bindsheet',
        'popup', 'menu', 'actionsheet', 'toast', 'panel',
    }

    # 纯布局容器类型（不画框线）
    CONTAINER_TYPES = {
        'row', 'column', 'stack', 'flex', 'relativecontainer',
        'navigation', 'navigationcontent', 'navdestination', 'navdestinationcontent',
        'navbar', 'navbarcontent', 'tabs', 'tabcontent', 'tabbar',
        'swiper', 'scroll', 'refresh',
        'blank', 'line', '__common__', 'windowscene', 'root', 'metaballnode',
    }
    
    def __init__(self, json_file: str, device: Optional[str] = None):
        """初始化分析器
        
        Args:
            json_file: 控件树JSON文件路径（本地文件或 --remote 模式下的临时文件）
            device: 目标设备序列号（可选，默认使用第一个连接的设备）
        """
        self.json_file = json_file
        self.device = device
        self.tree_data = None
        self.widgets = []
        self.widget_count = 0
        self.max_depth = 0
        self.type_stats = defaultdict(int)
        self._temp_file = None
        
    def _hdc_cmd(self, cmd_parts: List[str]) -> tuple:
        """构建并执行 hdc 命令
        
        Args:
            cmd_parts: hdc 后面的子命令和参数
            
        Returns:
            (成功状态, 输出内容)
        """
        cmd = ["hdc"]
        if self.device:
            cmd.extend(["-t", self.device])
        cmd.extend(cmd_parts)
        return run_hdc_command(cmd)
    
    def dump_layout(self, remote_path: Optional[str] = None) -> tuple:
        """从设备获取控件树
        
        Args:
            remote_path: 设备端临时存储路径
            
        Returns:
            (成功状态, 结果信息)
        """
        target_path = remote_path or self.REMOTE_PATH
        
        # 步骤1：执行 dumpLayout（大屏设备偶发超时，失败重试2次）
        success, output = self._hdc_cmd(["shell", "uitest", "dumpLayout", "-a", "-p", target_path])
        for attempt in range(2):
            if success:
                break
            print(f"⚠️  dumpLayout 失败，重试 {attempt + 1}/2 ...")
            success, output = self._hdc_cmd(["shell", "uitest", "dumpLayout", "-a", "-p", target_path])
        if not success:
            return False, f"dumpLayout 失败: {output}"
        
        # 步骤2：创建本地临时文件
        temp_fd, temp_path = tempfile.mkstemp(
            suffix=".json",
            prefix=self.TEMP_FILE_PREFIX
        )
        os.close(temp_fd)
        self._temp_file = temp_path
        
        # 步骤3：拉取到本地
        success, output = self._hdc_cmd(["file", "recv", target_path, temp_path])
        if not success:
            os.unlink(temp_path)
            self._temp_file = None
            return False, f"拉取文件失败: {output}"

        # 校验文件非空（多设备/无设备/App 未运行时 hdc 可能返回空文件）
        if os.path.getsize(temp_path) == 0:
            os.unlink(temp_path)
            self._temp_file = None
            return False, "控件树为空（设备未连接/App 未运行/多设备未指定 --device）"

        # 更新 json_file 为临时文件路径
        self.json_file = temp_path
        return True, temp_path
    
    def cleanup(self):
        """清理临时文件"""
        if self._temp_file and os.path.exists(self._temp_file):
            try:
                os.unlink(self._temp_file)
            except OSError:
                pass
            self._temp_file = None
    
    def _dispatch_analysis(self, mode: str = "overview",
                           search_param: Optional[str] = None,
                           rw: int = 120, rh: int = 110,
                           as_json: bool = False):
        """根据模式执行分析（共享调度逻辑）

        Args:
            mode: 分析模式 (overview/type/text/id/clickable/input/list-types/detail)
            search_param: 搜索参数（type/text/id/detail 模式时需要）
            rw: overview 网格宽度
            rh: overview 网格高度
            as_json: 搜索类模式以 JSON 数组输出
        """
        if as_json:
            filter_fn = self._get_filter(mode, search_param)
            if filter_fn is not None:
                self.search_json(filter_fn)
                return
        if mode == "overview":
            self.overview(rw, rh)
        elif mode == "type" and search_param:
            self.search_by_type(search_param)
        elif mode == "text" and search_param:
            self.search_by_text(search_param)
        elif mode == "id" and search_param:
            self.search_by_id(search_param)
        elif mode == "clickable":
            self.search_clickable()
        elif mode == "input":
            self.search_input()
        elif mode == "list-types":
            self.list_all_types()
        elif mode == "detail" and search_param:
            try:
                self.show_widget_detail(int(search_param))
            except ValueError:
                print(f"❌ detail 参数需要整数索引: {search_param}")
        else:
            self.overview(rw, rh)

    def _get_filter(self, mode: str, search_param: Optional[str]):
        """返回搜索模式对应的过滤函数（非搜索模式返回 None）"""
        if mode == "type" and search_param:
            return lambda w: w['type'].lower() == search_param.lower()
        if mode == "text" and search_param:
            return lambda w: (search_param.lower() in w['text'].lower()
                              or search_param.lower() in w['hint'].lower())
        if mode == "id" and search_param:
            return lambda w: search_param.lower() in w['id'].lower()
        if mode == "clickable":
            return lambda w: w['clickable'] == 'true'
        if mode == "input":
            input_types = ['TextInput', 'TextArea', 'TextField', 'RichEditor', 'Search']
            return lambda w: w['type'] in input_types
        return None

    def search_json(self, filter_fn):
        """以 JSON 数组输出搜索结果（供程序化消费，如 pytest conftest）"""
        found = [w for w in self.widgets if filter_fn(w)]
        out = []
        for w in found:
            m = re.match(r'\((\d+),\s*(\d+)\)', w.get('center') or '')
            out.append({
                'type': w['type'],
                'id': w['id'],
                'text': w['text'],
                'hint': w['hint'],
                'bounds': w['bounds'],
                'center': {'x': int(m.group(1)), 'y': int(m.group(2))} if m else None,
                'clickable': w['clickable'],
                'enabled': w['enabled'],
            })
        print(json.dumps(out, ensure_ascii=False, indent=2))

    def dump_and_analyze(self, mode: str = "overview", search_param: Optional[str] = None,
                         rw: int = 120, rh: int = 110,
                         as_json: bool = False) -> bool:
        """原子操作：从设备获取控件树并立即分析

        Args:
            mode: 分析模式 (overview/type/text/id/clickable/input/list-types)
            search_param: 搜索参数（type/text/id 模式时需要）
            as_json: 搜索类模式以 JSON 数组输出

        Returns:
            执行成功返回 True
        """
        success, msg = self.dump_layout()
        if not success:
            print(f"❌ 获取控件树失败: {msg}")
            return False

        if not self.load_tree():
            self.cleanup()
            return False

        self._dispatch_analysis(mode, search_param, rw, rh, as_json=as_json)

        self.cleanup()
        return True
        
    def load_tree(self) -> bool:
        """加载控件树JSON文件
        
        Returns:
            bool: 加载成功返回True，否则返回False
        """
        try:
            with open(self.json_file, 'r', encoding='utf-8') as f:
                self.tree_data = json.load(f)
            self._parse_tree(self.tree_data, 0)
            return True
        except Exception as e:
            print(f"加载文件失败: {e}")
            return False
    
    @staticmethod
    def _parse_center(bounds_str: str) -> str:
        """解析边界字符串 [left,top][right,bottom] 返回中心坐标 (cx,cy)
        
        Args:
            bounds_str: 边界字符串
            
        Returns:
            中心坐标字符串 "(cx, cy)"
        """
        if not bounds_str:
            return ""
        match = re.match(r'\[(\d+),(\d+)\]\[(\d+),(\d+)\]', bounds_str)
        if match:
            left, top, right, bottom = (int(x) for x in match.groups())
            cx = (left + right) // 2
            cy = (top + bottom) // 2
            return f"({cx}, {cy})"
        return ""
    
    def _parse_tree(self, node: Dict, depth: int, parent_index: Optional[int] = None):
        """递归解析控件树（保留父子关系和渲染顺序用于遮挡分析）

        Args:
            node: 当前节点
            depth: 当前深度
            parent_index: 父节点在 self.widgets 中的索引（根节点为 None）
        """
        if not isinstance(node, dict):
            return

        self.widget_count += 1
        self.max_depth = max(self.max_depth, depth)

        attrs = node.get('attributes', {})
        widget_type = attrs.get('type', 'unknown')
        self.type_stats[widget_type] += 1

        bounds_str = attrs.get('bounds', '')
        center = self._parse_center(bounds_str)

        current_index = len(self.widgets)
        widget_info = {
            'depth': depth,
            'type': widget_type,
            'id': attrs.get('id', ''),
            'text': attrs.get('text', ''),
            'hint': attrs.get('hint', ''),
            'bounds': bounds_str,
            'center': center,
            'clickable': attrs.get('clickable', ''),
            'enabled': attrs.get('enabled', ''),
            'visible': attrs.get('visible', ''),
            'checked': attrs.get('checked', ''),
            'accessibilityId': attrs.get('accessibilityId', ''),
            'description': attrs.get('description', ''),
            'attributes': attrs,
            # 树结构信息：用于遮挡关系和弹窗内容归属分析
            'parent_index': parent_index,
            # DFS 渲染顺序：值越大表示越后渲染，视觉上越靠上层
            'render_order': current_index
        }
        self.widgets.append(widget_info)

        children = node.get('children', [])
        for child in children:
            self._parse_tree(child, depth + 1, parent_index=current_index)

    def _is_descendant(self, widget_idx: int, ancestor_idx: int) -> bool:
        """判断 widget_idx 是否为 ancestor_idx 的后代节点"""
        if widget_idx < 0 or widget_idx >= len(self.widgets):
            return False
        if ancestor_idx < 0 or ancestor_idx >= len(self.widgets):
            return False
        cur = self.widgets[widget_idx].get('parent_index')
        while cur is not None:
            if cur == ancestor_idx:
                return True
            cur = self.widgets[cur].get('parent_index') if 0 <= cur < len(self.widgets) else None
        return False

    def get_screen_bounds(self) -> Optional[Tuple[int, int, int, int]]:
        """获取屏幕边界（取根节点 bounds；失败时回退到常见 1080x2400）"""
        if self.widgets:
            root_bounds = WidgetTreeDiff._parse_bounds(self.widgets[0].get('bounds', ''))
            if root_bounds:
                return root_bounds
        # 回退默认值（多数 HarmonyOS 设备）
        return (0, 0, 1080, 2400)
    
    def overview(self, rw: Optional[int] = None, rh: int = 110):
        """显示控件树概览 - 屏幕布局模拟图

        用字符网格还原屏幕布局，框线 ┌─┐│└┘ 表示控件边界，
        符号标注控件类型：◉按钮 ◆SymbolGlyph ▦图片 ░图片填充区。
        检测到弹窗/覆盖层时，分别渲染"弹窗内容"与"底层内容"。

        Args:
            rw: 网格宽度（字符数，None 时自动铺满终端宽度，下限 120）
            rh: 网格高度（字符行数，默认110）
        """
        if rw is None:
            try:
                import shutil
                rw = max(120, shutil.get_terminal_size((120, 24)).columns - 2)
            except Exception:
                rw = 120
        parsed = []
        for w in self.widgets:
            b = WidgetTreeDiff._parse_bounds(w.get('bounds', ''))
            if b:
                parsed.append((w, b))

        if not parsed:
            print("无控件可显示")
            return

        screen_w = max(b[2] for _, b in parsed)
        screen_h = max(b[3] for _, b in parsed)

        # 按实际屏幕宽高比调整网格尺寸：用统一每格像素数保持高度适配
        pixels_per_w = screen_w / max(rw, 1)
        pixels_per_h = screen_h / max(rh, 1)
        pixels_per_unit = max(pixels_per_w, pixels_per_h)  # 取更受限的维度
        effective_rh = max(int(screen_h / pixels_per_unit), 10)
        # 宽度直接铺满 rw（竖屏等比宽度会被压到 51 列，太窄），高度保持适配不超出 rh
        effective_rw = rw

        overlays, base = self._split_overlays(parsed)

        if overlays:
            screen_area = max(screen_w * screen_h, 1)
            fullscreen_overlay = any(
                w['type'].lower() in self.OVERLAY_TYPES
                and ((b[2] - b[0]) * (b[3] - b[1])) / screen_area > 0.8
                for w, b in overlays)
            print("═" * effective_rw)
            print("【弹窗内容】")
            print("═" * effective_rw)
            self._render_grid(overlays, effective_rw, effective_rh, screen_w, screen_h)
            print()
            print("═" * effective_rw)
            print("【底层内容】")
            print("═" * effective_rw)
            base_renderable = [item for item in base
                               if self._is_renderable(item, screen_w, screen_h,
                                                      effective_rw, effective_rh)]
            if base_renderable:
                if fullscreen_overlay:
                    print("（弹窗为全屏/近全屏，底层内容可能大部分被遮挡）")
                self._render_grid(base, effective_rw, effective_rh, screen_w, screen_h)
            else:
                print("（未检测到底层内容，可能被全屏弹窗完全遮挡）")
        else:
            self._render_grid(parsed, effective_rw, effective_rh, screen_w, screen_h)

    def _is_renderable(self, item, screen_w: int, screen_h: int,
                       rw: int = 120, rh: int = 110) -> bool:
        """判断控件是否会在网格图中实际渲染（与 _render_grid 筛选一致）"""
        w, b = item
        x1 = int(b[0] / screen_w * rw)
        y1 = int(b[1] / screen_h * rh)
        x2 = int(b[2] / screen_w * rw)
        y2 = int(b[3] / screen_h * rh)
        if x2 - x1 < 2 or y2 - y1 < 1:
            return False
        if (b[2] - b[0]) >= screen_w * 0.98 and (b[3] - b[1]) >= screen_h * 0.98:
            return False
        wtype = w['type'].lower()
        if wtype in self.CONTAINER_TYPES and not (w.get('text') or '').strip():
            return False
        return True

    def _split_overlays(self, widgets_with_bounds):
        """分离弹窗/覆盖层与底层内容（按弹窗自身子树划分，避免全屏 Dialog 误判）"""
        overlay_ids = set()
        for w in self.widgets:
            if w['type'].lower() in self.OVERLAY_TYPES:
                overlay_ids.add(id(w))
                continue
            cur = w.get('parent_index')
            while cur is not None:
                if cur < 0 or cur >= len(self.widgets):
                    break
                if self.widgets[cur]['type'].lower() in self.OVERLAY_TYPES:
                    overlay_ids.add(id(w))
                    break
                cur = self.widgets[cur].get('parent_index')

        overlays = [item for item in widgets_with_bounds if id(item[0]) in overlay_ids]
        base = [item for item in widgets_with_bounds if id(item[0]) not in overlay_ids]
        return overlays, base

    def _render_grid(self, widgets_with_bounds, rw, rh, screen_w, screen_h):
        """渲染字符网格布局图"""
        grid = [[' '] * rw for _ in range(rh)]

        candidates = []
        seen_bounds = set()
        for w, b in widgets_with_bounds:
            x1 = int(b[0] / screen_w * rw)
            y1 = int(b[1] / screen_h * rh)
            x2 = int(b[2] / screen_w * rw)
            y2 = int(b[3] / screen_h * rh)

            if x2 - x1 < 2 or y2 - y1 < 1:
                continue
            if (b[2] - b[0]) >= screen_w * 0.98 and (b[3] - b[1]) >= screen_h * 0.98:
                continue
            # 跳过纯布局容器（无内容时不画框线）
            wtype = w['type'].lower()
            if wtype in self.CONTAINER_TYPES and not (w.get('text') or '').strip():
                continue
            # 去重需放在各跳过条件之后：否则无文本容器会占用槽位，
            # 导致与其同界的子 Text 节点被误丢弃（设置项文字丢失的根因）
            if b in seen_bounds:
                continue
            seen_bounds.add(b)

            candidates.append((w, b, x1, y1, x2, y2))

        candidates.sort(key=lambda c: c[0]['depth'])

        for w, b, x1, y1, x2, y2 in candidates:
            self._draw_widget(grid, w, x1, y1, x2, y2, rw, rh)

        # 屏幕外边框（最后画，强制覆盖保证边框完整连续）
        for x in range(rw):
            grid[0][x] = '─'
            grid[rh - 1][x] = '─'
        for y in range(rh):
            grid[y][0] = '│'
            grid[y][rw - 1] = '│'
        grid[0][0] = '┌'
        grid[0][rw - 1] = '┐'
        grid[rh - 1][0] = '└'
        grid[rh - 1][rw - 1] = '┘'

        for row in grid:
            print(''.join(row))

    def _draw_widget(self, grid, w, x1, y1, x2, y2, rw, rh):
        """在网格上绘制单个控件"""
        x1 = max(0, x1)
        y1 = max(0, y1)
        x2 = min(rw - 1, x2)
        y2 = min(rh - 1, y2)

        if x1 >= x2 or y1 >= y2:
            return

        wtype = w['type'].lower()
        text = (w.get('text') or '').strip()

        # 小图标（图片/SymbolGlyph 面积 <12）：只放符号不画框，避免 tab 图标框线交叉
        if not text and wtype in ('image', 'imagecomponent', 'pic', 'symbolglyph') \
                and (x2 - x1) * (y2 - y1) < 12:
            symbol = '◆' if wtype == 'symbolglyph' else '▦'
            self._place_symbol(grid, symbol, x1, y1, x2, y2)
            return

        if y2 - y1 >= 2 and x2 - x1 >= 2:
            grid[y1][x1] = '┌'
            grid[y1][x2] = '┐'
            grid[y2][x1] = '└'
            grid[y2][x2] = '┘'
            for x in range(x1 + 1, x2):
                grid[y1][x] = '─'
                grid[y2][x] = '─'
            for y in range(y1 + 1, y2):
                grid[y][x1] = '│'
                grid[y][x2] = '│'
        elif text and y2 - y1 == 1 and x2 - x1 >= 2:
            # 单行文字控件：画左右端点（扁平胶囊形态，如底部tab）
            grid[y1][x1] = '┌'
            grid[y1][x2] = '┐'
        elif y2 - y1 >= 2 and x2 - x1 == 1:
            # 单列控件：画上下端点，竖线形态
            grid[y1][x1] = '┌'
            grid[y2][x1] = '└'

        if text:
            avail = x2 - x1 - 1
            if avail > 0:
                display = self._fit_text(text, avail)
                mid_y = (y1 + y2) // 2
                for i, ch in enumerate(display):
                    px = x1 + 1 + i
                    if px < x2:
                        grid[mid_y][px] = ch
        elif wtype == 'button':
            self._place_symbol(grid, '◉', x1, y1, x2, y2)
        elif wtype == 'symbolglyph':
            self._place_symbol(grid, '◆', x1, y1, x2, y2)
        elif wtype in ('image', 'imagecomponent', 'pic'):
            if (x2 - x1) * (y2 - y1) >= 12:
                for y in range(y1 + 1, y2):
                    for x in range(x1 + 1, x2):
                        grid[y][x] = '░'
            else:
                self._place_symbol(grid, '▦', x1, y1, x2, y2)
        elif wtype == 'toggle':
            self._place_symbol(grid, '◐', x1, y1, x2, y2)
        elif wtype in ('textinput', 'textarea'):
            self._place_symbol(grid, '▏', x1, y1, x2, y2)

    def _place_symbol(self, grid, symbol, x1, y1, x2, y2):
        """在控件框中心放置符号"""
        mid_x = (x1 + x2) // 2
        mid_y = (y1 + y2) // 2
        if y1 < mid_y < y2 and x1 < mid_x < x2:
            grid[mid_y][mid_x] = symbol
        elif y1 + 1 <= y2 and x1 + 1 <= x2:
            if y1 + 1 < y2:
                grid[y1 + 1][x1 + 1] = symbol

    @staticmethod
    def _fit_text(text: str, avail_cols: int) -> str:
        """截断文字到可用列数（中文占2列），截断时尾部加省略号；换行符先替换为空格防止撑破网格"""
        text = re.sub(r'[\r\n\t\u3000]+', ' ', text).strip()
        if avail_cols < 2 or not text:
            return ''
        result = []
        cols = 0
        truncated = False
        for ch in text:
            w = 2 if ord(ch) > 127 else 1
            if cols + w > avail_cols:
                truncated = True
                break
            result.append(ch)
            cols += w
        if truncated and cols > 0:
            while result and cols > avail_cols - 2:
                ch = result.pop()
                cols -= 2 if ord(ch) > 127 else 1
            result.append('…')
        return ''.join(result)
    
    def _search_and_print(self, filter_fn, title: str, not_found_msg: str):
        """通用搜索并打印结果

        Args:
            filter_fn: 接受 widget dict、返回 bool 的过滤函数
            title: 搜索标题（如 "搜索类型为 'Button' 的控件"）
            not_found_msg: 未找到时的提示
        """
        print("=" * 60)
        print(title)
        print("=" * 60)

        found = [w for w in self.widgets if filter_fn(w)]

        if not found:
            print(not_found_msg)
            return

        print(f"找到 {len(found)} 个控件:")
        print("-" * 40)
        for i, widget in enumerate(found, 1):
            print(f"[{i}] 类型: {widget['type']}")
            print(f"    深度: {widget['depth']}")
            print(f"    ID: {widget['id']}")
            print(f"    文本: {widget['text']}")
            print(f"    提示: {widget['hint']}")
            print(f"    边界: {widget['bounds']}")
            center = widget.get('center', '')
            if center:
                print(f"    中心: {center}")
            print(f"    可点击: {widget['clickable']}")
            print(f"    可用: {widget['enabled']}")
            print(f"    可见: {widget['visible']}")
            print()

    def search_by_type(self, widget_type: str):
        """根据类型搜索控件"""
        self._search_and_print(
            lambda w: w['type'].lower() == widget_type.lower(),
            f"搜索类型为 '{widget_type}' 的控件",
            f"未找到类型为 '{widget_type}' 的控件")

    def search_by_text(self, text: str):
        """根据文本搜索控件"""
        self._search_and_print(
            lambda w: text.lower() in w['text'].lower()
            or text.lower() in w['hint'].lower(),
            f"搜索文本包含 '{text}' 的控件",
            f"未找到文本包含 '{text}' 的控件")

    def search_by_id(self, widget_id: str):
        """根据ID搜索控件"""
        self._search_and_print(
            lambda w: widget_id.lower() in w['id'].lower(),
            f"搜索ID包含 '{widget_id}' 的控件",
            f"未找到ID包含 '{widget_id}' 的控件")

    def search_clickable(self):
        """搜索可点击的控件"""
        self._search_and_print(
            lambda w: w['clickable'] == 'true',
            "搜索可点击的控件",
            "未找到可点击的控件")

    def search_input(self):
        """搜索输入框控件"""
        input_types = ['TextInput', 'TextArea', 'TextField', 'RichEditor', 'Search']
        self._search_and_print(
            lambda w: w['type'] in input_types,
            "搜索输入框控件",
            "未找到输入框控件")
    
    def show_widget_detail(self, index: int):
        """显示控件详细信息
        
        Args:
            index: 控件索引（从1开始）
        """
        if index < 1 or index > len(self.widgets):
            print(f"无效的索引: {index}，有效范围: 1-{len(self.widgets)}")
            return
        
        widget = self.widgets[index - 1]
        print("=" * 60)
        print(f"控件详细信息 [{index}]")
        print("=" * 60)
        
        for key, value in widget['attributes'].items():
            if value and value != 'false' and value != '':
                print(f"{key}: {value}")
        print()
    
    def list_all_types(self):
        """列出所有控件类型"""
        print("=" * 60)
        print("所有控件类型")
        print("=" * 60)
        
        for i, widget_type in enumerate(sorted(self.type_stats.keys()), 1):
            count = self.type_stats[widget_type]
            print(f"{i}. {widget_type} ({count})")
        print()


