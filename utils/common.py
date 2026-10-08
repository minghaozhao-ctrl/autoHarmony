#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""HDC 命令执行工具"""
from typing import List, Optional
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


def is_dark_image(path: str, sample_rows: int = 120) -> bool:
    """PNG 全黑检测（无 PIL 依赖）：手动解码 IDAT 采样统计像素平均亮度。

    禁止截屏(FLAG_SECURE)/息屏时系统返回黑帧。实测坑：黑帧带噪点，
    PNG 压缩后仍 ~3.9MB，与正常页面大小区间重叠——文件大小阈值不可靠，
    必须解码像素。采样顶部+中部各 sample_rows//2 行（深色主题头部可能
    近全黑，只采顶部会误判；黑图整图黑，两段局部即足够），控制纯 Python
    unfilter 的耗时（~0.5s）。
    """
    import struct
    import zlib
    try:
        with open(path, 'rb') as f:
            data = f.read()
    except OSError:
        return False
    if len(data) < 8 or data[:8] != b'\x89PNG\r\n\x1a\n':
        return False
    width = height = bit_depth = color_type = 0
    idat = bytearray()
    pos = 8
    while pos + 8 <= len(data):
        # 畸形/截断 PNG：chunk 头或数据不足、CRC 越界等，解码失败按非黑图
        # 处理（返回 False 不触发唤醒），不能让 struct.error 冒泡导致
        # 调用方把已成功保存的截图误报为「截图失败」
        try:
            length = struct.unpack('>I', data[pos:pos + 4])[0]
            ctype = data[pos + 4:pos + 8]
            chunk = data[pos + 8:pos + 8 + length]
            if ctype == b'IHDR':
                width = struct.unpack('>I', chunk[0:4])[0]
                height = struct.unpack('>I', chunk[4:8])[0]
                bit_depth = chunk[8]
                color_type = chunk[9]
            elif ctype == b'IDAT':
                idat += chunk
            elif ctype == b'IEND':
                break
            pos += 12 + length
        except (struct.error, IndexError, ValueError):
            return False
    if not width or not height or bit_depth != 8:
        return False
    bpp = {0: 1, 2: 3, 4: 2, 6: 4}.get(color_type)
    if bpp is None:
        return False
    try:
        raw = zlib.decompress(bytes(idat))
    except zlib.error:
        return False
    stride = width * bpp + 1  # 每行首字节为 filter 类型
    total = 0
    count = 0
    prev = bytearray(width * bpp)

    def _decode_row(row: int) -> Optional[bytearray]:
        """unfilter 单行；越界返回 None"""
        base = row * stride
        if base + stride > len(raw):
            return None
        ft = raw[base]
        line = bytearray(raw[base + 1:base + stride])
        if ft == 1:  # Sub
            for i in range(bpp, len(line)):
                line[i] = (line[i] + line[i - bpp]) & 0xFF
        elif ft == 2:  # Up
            for i in range(len(line)):
                line[i] = (line[i] + prev[i]) & 0xFF
        elif ft == 3:  # Average
            for i in range(len(line)):
                left = line[i - bpp] if i >= bpp else 0
                line[i] = (line[i] + ((left + prev[i]) >> 1)) & 0xFF
        elif ft == 4:  # Paeth
            for i in range(len(line)):
                a = line[i - bpp] if i >= bpp else 0
                b = prev[i]
                c = prev[i - bpp] if i >= bpp else 0
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                pr = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                line[i] = (line[i] + pr) & 0xFF
        return line

    def _accumulate(line: bytearray) -> None:
        nonlocal total, count
        for i in range(len(line)):
            # 跳过 alpha 通道（RGBA=6 与 gray+alpha=4，alpha 不代表亮度）
            if (color_type == 6 and i % 4 == 3) or (color_type == 4 and i % 2 == 1):
                continue
            total += line[i]
            count += 1

    half = max(sample_rows // 2, 1)
    # 顶部一段 + 中部一段：深色主题头部/深色状态栏的合法页面可能顶部近全黑，
    # 只采顶部会误判；黑图整图黑，两段采样同样足够
    for row in list(range(min(half, height))) + \
            list(range(height // 2, min(height // 2 + half, height))):
        line = _decode_row(row)
        if line is None:
            break
        _accumulate(line)
        prev = line
    if count == 0:
        return False
    return total / count < 10  # 平均亮度 < 10/255 视为全黑
