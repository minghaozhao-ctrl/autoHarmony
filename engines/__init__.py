#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""engines 包：UI 操作引擎与公共层（由原 engines.py 拆分）

- engines.helpers        : 模块级辅助函数与常量
- engines.hdc_engine     : HdcUITestEngine
- engines.semantic_engine: SemanticEngine
- engines.batch_runner   : BatchRunner

本包不在 ``__init__`` 中聚合导出子模块：聚合会与 ``analyzers.widget_tree``
形成导入环。请按需直接导入子模块，例如 ``from engines.helpers import ...``。
"""
