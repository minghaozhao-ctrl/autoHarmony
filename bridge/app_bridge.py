#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""App 桥接契约（CLI / 批量脚本依赖的语义方法）。

``bridge/tcp_bridge.py`` 只提供通用传输（fport + JSON-RPC 2.0）与 ``call()``；
业务语义方法由使用方在 ``TcpBridge`` 子类中实现，并通过环境变量
``HMUITEST_BRIDGE_CLASS`` 注入（模板见 ``bridge/bridge_template.py``）。

本模块集中声明「哪些方法必须存在」，供启动期校验：缺失时立即给出
``ACTION_VERDICT: ERROR | reason=bridge_methods_missing``，而不是运行到深层
才抛 ``AttributeError`` traceback（违反工具自身的确定性裁决承诺）。
"""
from typing import Any, Dict, List, Optional, Protocol, runtime_checkable

# CLI 的 app 子命令 / 批量桥接步骤实际会调用的语义方法
REQUIRED_BRIDGE_METHODS = (
    "navigate",
    "navigate_back",
    "get_current_route",
    "login",
    "logout",
    "get_user_info",
    "query_devices",
    "click_device_card",
)


@runtime_checkable
class AppBridge(Protocol):
    """业务桥接契约：传输层用 ``TcpBridge``，语义层实现本协议。"""

    def navigate(self, page: str,
                 nav_params: Optional[Dict[str, Any]] = None
                 ) -> Dict[str, Any]: ...

    def navigate_back(self) -> Dict[str, Any]: ...

    def get_current_route(self) -> List[str]: ...

    def login(self, phone: str,
              password: Optional[str] = None) -> Dict[str, Any]: ...

    def logout(self) -> Dict[str, Any]: ...

    def get_user_info(self) -> Dict[str, Any]: ...

    def query_devices(self, **filters: Any) -> Dict[str, Any]: ...

    def click_device_card(self, name: str) -> Dict[str, Any]: ...


def missing_bridge_methods(target: Any) -> List[str]:
    """返回 target（类或实例）缺失的必需桥接方法名列表（空表示完整）。"""
    return [m for m in REQUIRED_BRIDGE_METHODS
            if not callable(getattr(target, m, None))]
