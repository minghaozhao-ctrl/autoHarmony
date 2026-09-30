#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""目标应用包名（bundleName）集中配置。

避免 ``com.cmcc.DigitalHome`` 等包名散落硬编码在各模块。解析优先级：

  1. 显式参数（如 ``--bundle``）——由各调用点自行优先
  2. 环境变量 ``HMUITEST_BUNDLE``
  3. 内置回退 ``FALLBACK_BUNDLE``

用法：

    from utils.bundle import default_bundle
    bundle = explicit or default_bundle()
"""
import os

ENV_VAR = "HMUITEST_BUNDLE"
FALLBACK_BUNDLE = "com.cmcc.DigitalHome"


def default_bundle() -> str:
    """返回默认 bundleName（env ``HMUITEST_BUNDLE`` 优先，否则回退常量）。"""
    return os.environ.get(ENV_VAR, "").strip() or FALLBACK_BUNDLE
