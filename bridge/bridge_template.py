#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""桥接扩展模板（复制到你的工程，按 App 真实协议实现）。

`bridge/tcp_bridge.py` 只提供通用传输（fport + JSON-RPC 2.0）与 `call()`；
业务方法由使用方自行扩展。`script run` 的桥接步骤会按下面的**固定签名/返回
形状**调用你的桥接类，因此请继承 `TcpBridge` 并实现同名方法：

    navigate(page, nav_params=None) -> {"success": bool, "message"?: str}
    navigate_back()                 -> {"success": bool}
    login(phone)                    -> {"success": bool, "message"?: str}
    logout()                        -> {"success": bool}
    get_user_info()                 -> dict
    get_current_route()             -> list[str]
    query_devices(**filters)        -> {"success": bool, "devices": [...], "totalCount": int}
    click_device_card(name)         -> {"success": bool, "message"?: str}

接入方式（把本文件复制为你的模块，实现后指向它即可）：

    HMUITEST_BRIDGE_CLASS=myproject.bridge:DigitalHomeBridge \
        autoharmony script run plan.json

说明：
  - 方法名与 App 端 JSON-RPC 方法名不必一致，在方法体内用 `self.call(...)` 映射；
  - 只需实现你会用到的动作：缺失的方法会让对应步骤以
    `ACTION_VERDICT: ERROR | reason=bridge_action_unsupported` 干净失败，不会崩溃；
  - `self.call()` 在连接失败/JSON-RPC 报错/超时时抛异常，框架会把它降级为
    步骤失败（`reason=bridge_call_failed`）。
"""

from typing import Any, Dict, List, Optional

from bridge.tcp_bridge import TcpBridge


class AppBridgeTemplate(TcpBridge):
    """按 App 协议实现以下方法；方法体内换成真实的 JSON-RPC 方法名与参数。"""

    def navigate(self, page: str,
                 nav_params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """跳转到指定页面。返回 {"success": bool, "message"?: str}。"""
        params: Dict[str, Any] = {"page": page}
        if nav_params is not None:
            params["params"] = nav_params
        # TODO: 换成 App 的真实方法名，如 self.call("router.push", params)
        return self.call("navigate", params)

    def navigate_back(self) -> Dict[str, Any]:
        """返回上一页。"""
        return self.call("navigate_back", {})

    def login(self, phone: str) -> Dict[str, Any]:
        """按手机号登录。"""
        return self.call("login", {"phone": phone})

    def logout(self) -> Dict[str, Any]:
        """退出登录。"""
        return self.call("logout", {})

    def get_user_info(self) -> Dict[str, Any]:
        """获取当前用户信息。"""
        result = self.call("get_user_info", {})
        return result if isinstance(result, dict) else {"result": result}

    def get_current_route(self) -> List[str]:
        """获取当前路由栈（归一化为字符串列表）。"""
        result = self.call("get_current_route", {})
        if isinstance(result, list):
            return result
        if isinstance(result, dict):
            for key in ("route", "routes", "stack", "result"):
                value = result.get(key)
                if isinstance(value, list):
                    return value
        return []

    def query_devices(self, **filters: Any) -> Dict[str, Any]:
        """按条件查询设备列表。"""
        return self.call("query_devices", filters)

    def click_device_card(self, name: str) -> Dict[str, Any]:
        """点击指定名称的设备卡片。"""
        return self.call("click_device_card", {"name": name})
