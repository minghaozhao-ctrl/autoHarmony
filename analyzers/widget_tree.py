#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
控件树分析器（类库，CLI 入口见 autoharmony.py）

用于解析HarmonyOS UITest框架生成的控件树JSON文件，提供搜索功能。
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
from utils.bundle import default_bundle


def filter_to_top_page(data: Dict, bundle: Optional[str] = None) -> Dict:
    """过滤 dumpLayout JSON：剔除 Navigation 栈内被覆盖的下层页面。

    场景：事件页（AFMsgPushSetPage）用 NavDestinationMode.DIALOG 覆盖式 push，
    下层设置页仍留在控件树中（visible=true），导致 dump 混层、判断页面归属出错。

    规则：
    - 目标页面 = App 窗口内 DFS 遍历的最后一个 NavDestination（Navigation 栈顶）
    - 保留：目标 NavDestination 及其祖先 NavDestination 的子树 + 不在任何 NavDestination
      内的节点（弹窗/覆盖层）
    - 剔除：被覆盖的下层 NavDestination（target 的兄弟分支）、其他窗口（桌面/状态栏等）
    - 安全降级：找不到 App 窗口或 NavDestination 时原样返回（不过滤）

    Args:
        data: dumpLayout 解析出的 JSON 树（dict）
        bundle: 目标应用包名（默认取 HMUITEST_BUNDLE，否则内置回退）

    Returns:
        过滤后的 JSON 树（dict）
    """
    if not isinstance(data, dict):
        return data
    bundle = bundle or default_bundle()
    app_win = None
    for child in data.get('children') or []:
        if child.get('attributes', {}).get('bundleName') == bundle:
            app_win = child
            break
    if app_win is None:
        return data

    navs: List[Tuple[Dict, List[Dict]]] = []

    def collect_nav(n, anc: List[Dict]):
        if n.get('attributes', {}).get('type') == 'NavDestination':
            navs.append((n, anc))
            anc = anc + [n]
        for c in n.get('children') or []:
            collect_nav(c, anc)

    collect_nav(app_win, [])
    if not navs:
        return data
    target, target_anc = navs[-1]

    def _nav_has_content(n) -> bool:
        """NavDestination 子树内是否有任何非空文本"""
        stack = [n]
        while stack:
            m = stack.pop()
            if ((m.get('attributes') or {}).get('text') or '').strip():
                return True
            stack.extend(m.get('children') or [])
        return False

    # 实测坑：空占位 NavDestination（无任何文本）可能出现在 DFS 最后
    # （restart 过渡页/DIALOG 包裹层，实测消息推送页树内 navs[-1] 为
    # 0 文本空占位，navs[0]/[1] 才是页面内容）——盲选 navs[-1] 会把
    # 有文本的页面全部剔除，find/collect 误报"控件不存在"。
    # 从后往前回退到最后一个有内容的 NavDestination 作为栈顶。
    if not _nav_has_content(target):
        for n, anc in reversed(navs):
            if _nav_has_content(n):
                target, target_anc = n, anc
                break
    keep_ids = {id(target)} | {id(a) for a in target_anc}

    owner: Dict[int, Optional[Dict]] = {}

    def mark(n, cur):
        if n.get('attributes', {}).get('type') == 'NavDestination':
            cur = n
        owner[id(n)] = cur
        for c in n.get('children') or []:
            mark(c, cur)

    mark(app_win, None)

    def rebuild(n):
        attrs = n.get('attributes', {})
        if attrs.get('type') == 'NavDestination' and id(n) not in keep_ids:
            return None
        cur_owner = owner.get(id(n))
        keep = cur_owner is target or cur_owner is None
        children = []
        for c in n.get('children') or []:
            r = rebuild(c)
            if r is not None:
                children.append(r)
        if not keep and not children:
            return None
        return {'attributes': attrs, 'children': children}

    new_app = rebuild(app_win)
    new_root = {'attributes': data.get('attributes', {}), 'children': []}
    if new_app is not None:
        new_root['children'].append(new_app)
    return new_root


class WidgetTreeAnalyzer:
    """控件树分析器"""
    
    # HDC 远程操作的默认配置
    REMOTE_PATH = "/data/local/tmp/layout.json"
    TEMP_FILE_PREFIX = "widget_tree_"

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
    
    def _dispatch_analysis(self, mode: Optional[str] = None,
                           search_param: Optional[str] = None,
                           as_json: bool = False):
        """根据模式执行分析（共享调度逻辑）

        Args:
            mode: 分析模式 (type/text/id/clickable/input/list-types/detail)，None 时提示
            search_param: 搜索参数（type/text/id/detail 模式时需要）
            as_json: 搜索类模式以 JSON 数组输出
        """
        if as_json:
            filter_fn = self._get_filter(mode, search_param)
            if filter_fn is not None:
                self.search_json(filter_fn)
                return
        if mode == "type" and search_param:
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
            print("未指定分析模式（--type/--text/--id/--clickable/--input/--list-types/--detail）")

    def _get_filter(self, mode: Optional[str], search_param: Optional[str]):
        """返回搜索模式对应的过滤函数（非搜索模式返回 None）"""
        if mode == "type" and search_param:
            return lambda w: (w.get('type') or '').lower() == search_param.lower()
        if mode == "text" and search_param:
            return lambda w: (search_param.lower() in (w.get('text') or '').lower()
                              or search_param.lower() in (w.get('hint') or '').lower())
        if mode == "id" and search_param:
            return lambda w: search_param.lower() in (w.get('id') or '').lower()
        if mode == "clickable":
            return lambda w: w.get('clickable') == 'true'
        if mode == "input":
            input_types = ['TextInput', 'TextArea', 'TextField', 'RichEditor', 'Search']
            return lambda w: (w.get('type') or '') in input_types
        return None

    def search_json(self, filter_fn):
        """以 JSON 数组输出搜索结果（供程序化消费，如 pytest conftest）"""
        found = [w for w in self.widgets if filter_fn(w)]
        out = []
        for w in found:
            # 负坐标支持（与 _parse_center 同口径，勿再分叉）
            m = re.match(r'\((-?\d+),\s*(-?\d+)\)', w.get('center') or '')
            out.append({
                'type': w.get('type') or '',
                'id': w.get('id') or '',
                'text': w.get('text') or '',
                'hint': w.get('hint') or '',
                'bounds': w.get('bounds') or '',
                'center': {'x': int(m.group(1)), 'y': int(m.group(2))} if m else None,
                'clickable': w.get('clickable') or '',
                'enabled': w.get('enabled') or '',
            })
        print(json.dumps(out, ensure_ascii=False, indent=2))

    def load_tree(self, filter_top: bool = True) -> bool:
        """加载控件树JSON文件

        Args:
            filter_top: 是否只保留当前可见页面（剔除被 DIALOG 覆盖的下层页面与系统窗口）。
                默认 True；传 False 可拿到完整原始树（含栈内下层页面）。

        Returns:
            bool: 加载成功返回True，否则返回False
        """
        try:
            with open(self.json_file, 'r', encoding='utf-8') as f:
                self.tree_data = json.load(f)
            if filter_top:
                self.tree_data = filter_to_top_page(self.tree_data)
            # 重置解析状态（支持重复 load_tree，如 --no-filter 重载原树）
            self.widgets = []
            self.widget_count = 0
            self.max_depth = 0
            self.type_stats = defaultdict(int)
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
        # 负坐标支持（与 WidgetTreeDiff._parse_bounds 同口径，勿再分叉）
        match = re.match(r'\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]', bounds_str)
        if match:
            left, top, right, bottom = (int(x) for x in match.groups())
            cx = (left + right) // 2
            cy = (top + bottom) // 2
            return f"({cx}, {cy})"
        return ""
    
    def _parse_tree(self, node: Dict, depth: int, parent_index: Optional[int] = None,
                    parent_bundle: str = ''):
        """递归解析控件树（保留父子关系和渲染顺序用于遮挡分析）

        Args:
            node: 当前节点
            depth: 当前深度
            parent_index: 父节点在 self.widgets 中的索引（根节点为 None）
            parent_bundle: 最近祖先的 bundleName（用于向子节点继承归属窗口；
                状态栏/桌面等系统窗口的子节点自身多不携带 bundleName）
        """
        if not isinstance(node, dict):
            return

        self.widget_count += 1
        self.max_depth = max(self.max_depth, depth)

        attrs = node.get('attributes', {})
        # 归属 bundle：自身缺失时继承最近祖先，用于识别系统 UI（状态栏/桌面）
        bundle = (attrs.get('bundleName') or parent_bundle or '')
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
            'bundleName': bundle,
            'attributes': attrs,
            # 树结构信息：用于遮挡关系和弹窗内容归属分析
            'parent_index': parent_index,
            # DFS 渲染顺序：值越大表示越后渲染，视觉上越靠上层
            'render_order': current_index
        }
        self.widgets.append(widget_info)

        children = node.get('children') or []
        for child in children:
            self._parse_tree(child, depth + 1, parent_index=current_index,
                             parent_bundle=bundle)

    def get_screen_bounds(self) -> Optional[Tuple[int, int, int, int]]:
        """获取屏幕边界（取根节点 bounds；失败时回退到常见 1080x2400）"""
        if self.widgets:
            root_bounds = WidgetTreeDiff._parse_bounds(self.widgets[0].get('bounds', ''))
            if root_bounds:
                return root_bounds
        # 回退默认值（WidgetTreeDiff.DEFAULT_SCREEN_W/H，单一事实来源）
        return (0, 0, WidgetTreeDiff.DEFAULT_SCREEN_W, WidgetTreeDiff.DEFAULT_SCREEN_H)
    
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
            lambda w: (w.get('type') or '').lower() == widget_type.lower(),
            f"搜索类型为 '{widget_type}' 的控件",
            f"未找到类型为 '{widget_type}' 的控件")

    def search_by_text(self, text: str):
        """根据文本搜索控件"""
        self._search_and_print(
            lambda w: text.lower() in (w.get('text') or '').lower()
            or text.lower() in (w.get('hint') or '').lower(),
            f"搜索文本包含 '{text}' 的控件",
            f"未找到文本包含 '{text}' 的控件")

    def search_by_id(self, widget_id: str):
        """根据ID搜索控件"""
        self._search_and_print(
            lambda w: widget_id.lower() in (w.get('id') or '').lower(),
            f"搜索ID包含 '{widget_id}' 的控件",
            f"未找到ID包含 '{widget_id}' 的控件")

    def search_clickable(self):
        """搜索可点击的控件"""
        self._search_and_print(
            lambda w: w.get('clickable') == 'true',
            "搜索可点击的控件",
            "未找到可点击的控件")

    def search_input(self):
        """搜索输入框控件"""
        input_types = ['TextInput', 'TextArea', 'TextField', 'RichEditor', 'Search']
        self._search_and_print(
            lambda w: (w.get('type') or '') in input_types,
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


