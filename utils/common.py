#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""HDC 命令执行工具"""
from typing import List
import subprocess


def run_hdc_command(cmd: List[str], timeout: int = 30) -> tuple:
    """执行 HDC 命令

    Args:
        cmd: 命令及参数列表
        timeout: 超时时间（秒）

    Returns:
        (成功状态, 输出内容或错误信息)
    """
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, encoding='utf-8'
        )
        if result.returncode == 0:
            return True, result.stdout
        else:
            return False, result.stderr or result.stdout
    except subprocess.TimeoutExpired:
        return False, f"命令执行超时 ({timeout}s): {' '.join(cmd)}"
    except FileNotFoundError:
        return False, "未找到 hdc 命令，请确保已安装 DevEco Studio 并配置环境变量"
    except Exception as e:
        return False, f"执行命令失败: {e}"
