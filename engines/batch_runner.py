#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""BatchRunner：批量执行测试用例脚本（复用引擎与上一步 after-state）"""
import os
import sys
import time
from typing import Optional

from utils.bundle import default_bundle

from .helpers import check_expectations_with_polling
from .semantic_engine import SemanticEngine


class BatchRunner:
    """批量执行测试用例脚本

    单进程内复用同一引擎，并复用上一步的 after-state 作为下一步的 before-state，
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

    # 录制产出 ({cmd, args}) 到脚本 step 的映射；坐标族由 SemanticEngine
    # 回放（继承 HdcUITestEngine 坐标方法）。键须与录制端 _record_cmd 一致、
    # 值的参数序须与录制端 _record_args 顺序一致（见 autoharmony.py 各 cmd_*）
    RECORD_ACTION_MAP: dict = {
        'ui back': ('go_back', ()),
        'ui click-by-text': ('click_by_text', ('text',)),
        'ui click-by-id': ('click_by_id', ('key',)),
        'ui long-click-by-text': ('long_click_by_text', ('text',)),
        'ui click': ('click', ('x', 'y')),
        'ui double-click': ('double_click', ('x', 'y')),
        'ui long-click': ('long_click', ('x', 'y')),
        'ui click-sequence': ('click_sequence', ('points', 'interval')),
        'ui swipe': ('swipe', ('x1', 'y1', 'x2', 'y2')),
        'ui input': ('text_input', ('text', 'clear')),
        'aa start': ('aa_start', ('uri', 'bundle_name', 'ability_name')),
    }

    # 坐标参数键：录制文件中是字符串（JSON 数组），回放必须转数值——
    # point 消费处（CLICK_ON_DISABLED 预检 _bounds_contain 等做数值区间
    # 比较）str 直接透传会 TypeError（int 与 str 不可比）
    _COORD_KEYS = frozenset(('x', 'y', 'x1', 'y1', 'x2', 'y2'))

    def __init__(self, device: Optional[str] = None,
                 history_dir: Optional[str] = None):
        self.device = device
        self.history_dir = history_dir
        self.engine = None
        self.results: list = []

    def _ensure_engine(self):
        """按需创建引擎（lazy；恒用 SemanticEngine——继承 HdcUITestEngine
        全部坐标能力，语义/坐标/桥接通吃）"""
        if self.engine is not None:
            return self.engine
        self.engine = SemanticEngine(device=self.device,
                                     history_dir=self.history_dir)
        return self.engine

    def _recorded_to_step(self, rec: dict) -> Optional[dict]:
        """把录制的一条步骤 (cmd, args) 转成脚本 step（未知动作跳过）"""
        cmd = (rec.get('cmd') or '').strip()
        if cmd not in self.RECORD_ACTION_MAP:
            print(f"⚠️ 跳过无法回放的动作: {cmd!r}")
            return None
        action, keys = self.RECORD_ACTION_MAP[cmd]
        args = rec.get('args') or []
        params = dict(zip(keys, args))
        # 坐标字符串 → 数值（录制文件中坐标是 JSON 字符串数组）
        for k, v in list(params.items()):
            if k in self._COORD_KEYS:
                try:
                    params[k] = int(float(v))
                except (TypeError, ValueError):
                    pass
        # --clear 标记转 bool（录制端记录为 "--clear" 字符串）
        if 'clear' in params:
            params['clear'] = True
        # click-sequence：points "x1,y1;x2,y2" → [(x, y), ...]
        if action == 'click_sequence':
            try:
                params['points'] = [
                    (int(float(p.split(',')[0])), int(float(p.split(',')[1])))
                    for p in str(params.get('points', '')).split(';')
                    if p.strip()]
            except (TypeError, ValueError):
                params['points'] = []
            try:
                params['interval'] = float(params.get('interval') or 0.2)
            except (TypeError, ValueError):
                params['interval'] = 0.2
        # aa start 缺省 bundle 对齐 CLI 默认（录制端 --bundle 可能空字符串）
        if action == 'aa_start' and not params.get('bundle_name'):
            params['bundle_name'] = default_bundle()
        return {
            'action': action, 'params': params,
            'desc': (cmd + ' ' + ' '.join(str(a) for a in args)).rstrip(),
            'stop_on_fail': True,
        }

    def _normalize_script(self, script: dict) -> dict:
        """兼容两类输入：{name, steps} 完整脚本 与 [{cmd, args}] 录制清单"""
        # 恒用 SemanticEngine：它是 HdcUITestEngine 的子类（坐标+语义通吃）。
        # 旧逻辑按 uses_coord 选 'hdc'，混合脚本（click + click_by_text）
        # 的语义步骤会全部报"未知操作类型"
        if isinstance(script, list):
            steps = [s for s in (self._recorded_to_step(r) for r in script)
                     if s is not None]
            self._ensure_engine()
            return {'name': '录制脚本自动回放', 'steps': steps}
        self._ensure_engine()
        return script

    def _ensure_start_state(self, start: Optional[dict]) -> Optional[str]:
        """回放前置状态校验 + 恢复（脚本可选 start/setup 字段）

        支持字段：
            start:
              route: str | [str]   期望路由（字符串=栈中任一层包含该页名；列表=栈末尾连续匹配）
              text: [str]          期望当前页存在的文本（全部命中）
              max_backs: int       校验失败时允许的有界返回次数（默认 3）
              recover: str | dict  恢复策略：'back'（默认，有限次返回）或
                                    {'navigate': '<page>'}（bridge 导航直达）

        Returns:
            None 表示前置状态就绪；否则返回失败原因字符串。
        """
        if not start:
            return None
        from engines.diff_engine import WidgetTreeDiff
        route = start.get('route')
        texts = start.get('text') or []
        if isinstance(texts, str):
            texts = [texts]
        # 数值字段容错（脚本作者笔误传字符串时结构化失败而非裸 Traceback）
        try:
            max_backs = int(start.get('max_backs', 3))
        except (TypeError, ValueError):
            reason = (f"脚本字段类型错误: start.max_backs="
                      f"'{start.get('max_backs')}' 须为整数")
            print(f"❌ {reason}")
            return reason
        recover = start.get('recover', 'back')

        def _check() -> Optional[str]:
            """当前页是否满足前置条件：满足返回 None，否则返回原因"""
            route_reason = self._check_route(route)
            if route_reason:
                return route_reason
            return self._check_texts(texts)

        reason = _check()
        if reason is None:
            return None

# 校验失败 → 按策略恢复
        if isinstance(recover, dict) and recover.get('navigate'):
            page = recover['navigate']
            bundle = recover.get('bundle') or start.get('bundle') or default_bundle()
            ability = recover.get('ability') or start.get('ability', 'EntryAbility')
            print(f"🔧 前置状态不匹配（{reason}），尝试 bridge 导航到 {page} ...")
            try:
                import sys as _sys
                _root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
                if _root not in _sys.path:
                    _sys.path.insert(0, _root)
                from utils.hdc import detect_device_id
                from bridge.tcp_bridge import TcpBridge
                dev = self.device
                try:
                    dev = dev or detect_device_id()
                except Exception:
                    pass
                b = TcpBridge(device=dev)
                try:
                    r = b.navigate(page)
                finally:
                    b.close()
                if (r or {}).get('success'):
                    time.sleep(1)
                    if _check() is None:
                        self._refresh_history()
                        print("✅ 已导航到前置页面")
                        return None
                return f"导航到 {page} 后仍不满足前置条件"
            except Exception as nav_ex:
                # 导航连接失败（App 未运行常触发）→ 先 aa start 启动再重试一次
                print(f"ℹ️  bridge 导航失败: {nav_ex}，尝试先启动应用 {bundle} ...")
                import subprocess
                hdc_cmd = ["hdc"]
                if self.device:
                    hdc_cmd.extend(["-t", self.device])
                hdc_cmd.extend(["shell", "aa", "start", "-b", bundle,
                                "-a", ability])
                try:
                    subprocess.run(hdc_cmd, capture_output=True, text=True,
                                   timeout=30)
                except Exception as start_ex:
                    return f"应用启动失败: {start_ex}"
                time.sleep(3)
                try:
                    b = TcpBridge(device=dev)
                    try:
                        r = b.navigate(page)
                    finally:
                        b.close()
                    if (r or {}).get('success'):
                        time.sleep(1)
                        if _check() is None:
                            self._refresh_history()
                            print("✅ 已启动并导航到前置页面")
                            return None
                        return f"启动后导航到 {page} 仍不满足前置条件"
                    return f"启动后导航到 {page} 失败"
                except Exception as retry_ex:
                    # 启动后仍无 bridge（应用未内置 TCP server）→ 回退到有界返回
                    print(f"ℹ️  启动后 bridge 仍不可用: {retry_ex}，回退到有界返回恢复")
                    return self._recover_by_back(start, texts, route, max_backs,
                                                 reason)
        return self._recover_by_back(start, texts, route, max_backs, reason)

    def _check_route(self, route) -> Optional[str]:
        """校验当前路由是否满足前置 route 条件；满足返回 None，否则返回原因"""
        if not route:
            return None
        from engines.diff_engine import WidgetTreeDiff
        cur = WidgetTreeDiff.get_current_route(self.device)
        if not cur:
            return f"获取路由失败，期望含 '{route}'"
        if isinstance(route, str):
            # 子串匹配（与 assertions.expect.route 同口径：路由元素可能带
            # 前缀如 entry/MainPage，精确匹配会假失败触发无谓恢复）
            if not any(route.lower() in r.lower() for r in cur):
                return f"当前路由 {cur} 不包含 '{route}'"
        else:
            if list(cur)[-len(route):] != list(route):
                return f"当前路由 {cur} 末尾不匹配 {route}"
        return None

    def _check_texts(self, texts) -> Optional[str]:
        """校验当前页是否包含全部前置文本；满足返回 None，否则返回原因"""
        if not texts:
            return None
        a = self.engine._dump_and_load("start_check")
        if a is None:
            return "获取控件树失败，无法校验前置文本"
        try:
            # 匹配 text+hint（与 autoharmony._check_page_text_exists 一致：
            # 覆盖层/提示文本常只出现在 hint）
            missing = [t for t in texts
                       if not any(t in (w.get('text', '') or '')
                                  or t in (w.get('hint', '') or '')
                                  for w in a.widgets)]
        finally:
            a.cleanup()
        if missing:
            return f"当前页缺少文本: {', '.join(missing)}"
        return None

    def _recover_by_back(self, start, texts, route, max_backs, reason):
        """有界返回恢复：受 ActionPipeline 桌面护栏保护，到桌面即停。

        恢复判定用完整校验（route+texts）——只查 texts 会假通过：
        route 未恢复时返回后所有步骤都在错误页面执行
        """
        print(f"🔧 前置状态不匹配（{reason}），尝试有界返回恢复（最多 {max_backs} 次）...")
        attempts = 0
        for i in range(max_backs):
            attempts = i + 1
            backed = self._execute_step(
                'go_back', {}, f"前置恢复返回[{i + 1}]", {}, skip_before=False)
            if not backed:
                print(f"↩️ 返回被拦截（桌面护栏/栈底），停止恢复")
                break
            if self._check_route(route) is None and self._check_texts(texts) is None:
                self._refresh_history()
                print(f"✅ 返回 {i + 1} 次后到达前置页面")
                return None
        return f"前置状态校验失败（已尝试 {attempts} 次返回恢复）: {reason}"

    def run(self, script: dict) -> dict:
        """执行脚本，返回结果摘要

        支持脚本顶层可选字段 start/setup（前置状态校验与恢复），见
        _ensure_start_state 文档。
        """
        script = self._normalize_script(script)
        name = script.get('name', '未命名脚本')
        steps = script.get('steps', [])

        print(f"\n{'#' * 60}")
        print(f"# 批量执行: {name} ({len(steps)} 步)")
        print(f"{'#' * 60}\n")

        all_passed = True

        # 前置状态校验（脚本可选 start/setup 字段）失败 → 整脚本判定失败
        start_state = script.get('start') or script.get('setup')
        if start_state:
            start_reason = self._ensure_start_state(start_state)
            if start_reason:
                print(f"\n❌ 前置状态校验失败: {start_reason}")
                print("ACTION_VERDICT: PRECONDITION_FAILED | "
                      f"reason={start_reason}")
                self.results.append((f"前置状态校验确认失败: {start_reason}", False))
                self._print_summary(name)
                if hasattr(self.engine, 'close'):
                    self.engine.close()
                return {
                    'name': name,
                    'total': len(self.results),
                    'passed': 0,
                    'failed': len(self.results),
                    'all_passed': False,
                }
        for i, step in enumerate(steps, 1):
            step_desc = step.get('desc', f"步骤 {i}")
            action = step.get('action')
            params = step.get('params', {})
            expect = step.get('expect', {})
            stop_on_fail = step.get('stop_on_fail', False)

            print(f"\n{'=' * 60}")
            print(f"📋 步骤 {i}/{len(steps)}: {step_desc}")
            print(f"{'=' * 60}")

            # 每步独立留证（否则管线标记会只留第一步的现场）
            if self.engine is not None:
                self.engine._artifacts_captured = False

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
                self._capture_step_failure(i, step_desc)
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

    def _capture_step_failure(self, index: int, desc: str):
        """脚本步骤失败留证（管线未留证时兜底，如桥接步骤）。"""
        e = self.engine
        if e is not None and getattr(e, '_artifacts_captured', False):
            return
        try:
            from engines.artifacts import capture_failure
            capture_failure(getattr(e, 'device', None) or self.device,
                            "script step %d failed: %s" % (index, desc))
        except Exception:
            pass
        if e is not None:
            try:
                e._artifacts_captured = True
            except Exception:
                pass

    def _execute_step(self, action: str, params: dict, desc: str,
                      expect: dict, skip_before: bool) -> bool:
        """执行单个步骤"""
        e = self.engine
        kw = {'operation': desc,
              'expectations': expect if expect else None,
              'skip_before': skip_before}

        # 单一派发表：SemanticEngine 继承 HdcUITestEngine 全部坐标方法，
        # 语义/坐标/桥接步骤同一引擎通吃（勿再按脚本类型拆分支——曾因
        # 坐标族派发在不可达 else 分支导致 script run 点击回放全军覆没）
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
                bundle_name=params.get('bundle_name') or default_bundle(),
                ability_name=params.get('ability_name'),
                module_name=params.get('module_name'),
                params=params.get('params'),
                **kw),
            'click': lambda: e.click(params['x'], params['y'],
                                     uinput=bool(params.get('uinput')), **kw),
            'double_click': lambda: e.double_click(params['x'], params['y'], **kw),
            'long_click': lambda: e.long_click(params['x'], params['y'], **kw),
            'click_sequence': lambda: e.click_sequence(
                [tuple(pt) for pt in params['points']],
                interval=params.get('interval', 0.2), **kw),
            'swipe': lambda: e.swipe(
                params['x1'], params['y1'], params['x2'], params['y2'], **kw),
            'text_input': lambda: e.text_input(
                params['text'], clear_first=bool(params.get('clear')), **kw),
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
        _root = os.path.dirname(os.path.abspath(__file__))
        if _root not in sys.path:
            sys.path.insert(0, _root)
        from bridge.tcp_bridge import resolve_bridge_class
        from utils.hdc import detect_device_id

        device_id = self.device or detect_device_id()
        if not device_id:
            print("❌ 未检测到 hdc 设备（hdc list targets 确认连接，或设 HARMONY_DEVICE_ID）")
            return False

        expect_to_check = expect
        bridge = resolve_bridge_class()(device=device_id)
        # 防御：桥接动作必须映射到桥接类上真实存在的方法，否则立即失败，
        # 避免 AttributeError 向上冒泡打断整个脚本（bridge 属可选集成）。
        method_name = {
            'navigate': 'navigate',
            'navigate_back': 'navigate_back',
            'login': 'login',
            'logout': 'logout',
            'get_user_info': 'get_user_info',
            'get_route': 'get_current_route',
            'query_devices': 'query_devices',
            'click_device_card': 'click_device_card',
        }.get(action)
        if method_name is None or not callable(getattr(bridge, method_name, None)):
            print(f"❌ 桥接动作 '{action}' 未实现：桥接类缺少方法 {method_name or action}()")
            print("   桥接属可选扩展，请继承 TcpBridge 实现该方法，并用 "
                  "HMUITEST_BRIDGE_CLASS 指向你的子类（模板见 skill 根目录 bridge_template.py）")
            print(f"ACTION_VERDICT: ERROR | reason=bridge_action_unsupported: {action}")
            bridge.close()
            return False
        try:
            if action == 'navigate':
                page = params.get('page')
                if not page:
                    print("❌ navigate 步骤缺少 params.page")
                    print("ACTION_VERDICT: ERROR | reason=missing_params.page")
                    return False
                result = bridge.navigate(page, params.get('nav_params'))
                if not result.get('success'):
                    print(f"❌ 导航失败: {result.get('message', '未知错误')}")
                    print("ACTION_VERDICT: ERROR | reason=bridge_navigate_failed")
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
                    print("ACTION_VERDICT: ERROR | reason=missing_params.phone")
                    return False
                result = bridge.login(phone)
                if not result.get('success'):
                    print(f"❌ 登录失败: {result.get('message', '未知错误')}")
                    print("ACTION_VERDICT: ERROR | reason=bridge_login_failed")
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
                               'platform_type', 'device_type_id', 'is_shared')
                filters = {k: params[k] for k in filter_keys if k in params}
                result = bridge.query_devices(**filters)
                if not result.get('success'):
                    print(f"❌ 查询设备失败: {result.get('message', '未知错误')}")
                    print("ACTION_VERDICT: ERROR | reason=bridge_query_devices_failed")
                    return False
                devices = result.get('devices', [])
                print(f"✅ 查询成功，共 {result.get('totalCount', len(devices))} 个设备:")
                for j, d in enumerate(devices, 1):
                    share_tag = "[分享设备]" if d.get('isShared') else "[自有设备]"
                    print(f"   {j}. [{d.get('deviceId', 'N/A')}] "
                          f"{d.get('deviceName', '未知')} {share_tag} - {d.get('statusDesc', '')}")
            elif action == 'click_device_card':
                name = params.get('name')
                if not name:
                    print("❌ click_device_card 步骤缺少 params.name")
                    print("ACTION_VERDICT: ERROR | reason=missing_params.name")
                    return False
                result = bridge.click_device_card(name)
                if not result.get('success'):
                    print(f"❌ 设备卡片点击失败: {result.get('message', '未知错误')}")
                    print("ACTION_VERDICT: ERROR | reason=bridge_click_device_failed")
                    return False
                print(f"✅ 设备卡片点击成功: {name}")
            else:
                print(f"❌ 未知桥接动作: {action}")
                return False
        except Exception as ex:
            # 桥接是可选集成：任何 RPC/连接异常都降级为步骤失败，不打断整脚本
            print(f"❌ 桥接动作 '{action}' 执行异常: {ex}")
            print(f"ACTION_VERDICT: ERROR | reason=bridge_call_failed: {ex}")
            return False
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
