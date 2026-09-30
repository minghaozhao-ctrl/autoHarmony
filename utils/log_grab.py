"""hilog / 故障日志抓取（防卡死设计）

防卡死关键（实测教训：直接 `hdc shell "hilog -x" | grep` 会挂起）：
1. 设备侧重定向到文件（hdc shell 的 stdout 不进管道，避免缓冲区卡死）
2. hdc shell 调用带 timeout；超时后仍尝试拉取已落盘的部分内容
3. 一律 `-x` 非阻塞导出（读完缓冲区即退出），不用阻塞式读取
4. 大输出走 file recv 回退（cat 失败时）

故障日志：`hidumper -e`（shell 用户无权限读 /data/log/faultlog，必须走系统服务）。
"""
import os
import re
import shlex
import subprocess
import tempfile
import time
from typing import List, Optional, Tuple

from utils.bundle import default_bundle


def _hdc(device: Optional[str], args: List[str], timeout: int = 20) -> Tuple[bool, str]:
    """执行 hdc 命令（带超时；超时返回已捕获的部分输出）"""
    cmd = ["hdc"]
    if device:
        cmd.extend(["-t", device])
    cmd.extend(args)
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode == 0, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired as e:
        out = ""
        if e.stdout:
            out = e.stdout if isinstance(e.stdout, str) \
                else e.stdout.decode("utf-8", "replace")
        return False, out
    except Exception as e:  # hdc 不存在/进程异常
        return False, str(e)


def _fetch_lines(device: Optional[str], remote: str) -> List[str]:
    """拉取设备文件内容（cat 优先；失败回退 file recv）"""
    ok, out = _hdc(device, ["shell", f"cat {remote} 2>/dev/null"], timeout=30)
    if ok and out and out.strip():
        return out.splitlines()
    tmp = os.path.join(tempfile.gettempdir(),
                       f"grab_{int(time.time() * 1000)}.txt")
    ok2, _ = _hdc(device, ["file", "recv", remote, tmp], timeout=30)
    if ok2 and os.path.exists(tmp):
        try:
            with open(tmp, "r", encoding="utf-8", errors="replace") as f:
                return f.read().splitlines()
        except OSError:
            return []
    return []


def resolve_pids(device: Optional[str], bundle: str) -> List[str]:
    """按包名解析 PID 列表（最多 5 个，hilog -P 上限）"""
    ok, out = _hdc(device, ["shell", f"pidof {bundle}"], timeout=10)
    if not ok or not out:
        return []
    return (out or "").strip().split()[:5]


def grab_hilog(device: Optional[str] = None, pid: Optional[int] = None,
               bundle: Optional[str] = None, tags: Optional[List[str]] = None,
               level: Optional[str] = None, log_type: Optional[str] = None,
               regex: Optional[str] = None, tail: Optional[int] = None,
               head: Optional[int] = None, out_file: Optional[str] = None,
               local_filter: Optional[str] = None,
               timeout: int = 25) -> Tuple[bool, str, List[str]]:
    """抓取 hilog 缓冲区日志（设备侧落盘 → 拉取 → 可选本地过滤）

    Returns:
        (ok, message, lines)
    """
    bundle = bundle or default_bundle()
    remote = "/data/local/tmp/_hmuitest_grab.txt"
    # 注意：-x 与 -z/-a 互斥（报 "Mutlti commands can't be used in combination"）；
    # -z/-a 本身即非阻塞读取，故仅在不限行数时加 -x（实测验证）。
    parts = ["hilog"]
    if tail:
        parts += ["-z", str(int(tail))]
    elif head:
        parts += ["-a", str(int(head))]
    else:
        parts += ["-x"]
    if pid is not None:
        parts += ["-P", str(pid)]
    elif bundle:
        pids = resolve_pids(device, bundle)
        if not pids:
            return False, f"未找到进程: {bundle}", []
        parts += ["-P", ",".join(pids)]
    if tags:
        parts += ["-T", ",".join(tags[:10])]
    if level:
        parts += ["-L", level]
    if log_type:
        parts += ["-t", log_type]
    if regex:
        # shlex.quote：regex 含 |/& 等特殊字符时防设备 shell 误解释
        parts += ["-e", shlex.quote(regex)]

    shell_cmd = " ".join(parts) + f" > {remote} 2>&1"
    ok, out = _hdc(device, ["shell", shell_cmd], timeout=timeout)
    # 无论成功/超时，都尝试拉取已落盘内容（超时场景文件里已有部分内容）
    lines = _fetch_lines(device, remote)
    if not lines:
        return False, f"抓取失败: {(out or '').strip()[:200]}", []
    if local_filter:
        try:
            pat = re.compile(local_filter)
        except re.error as e:
            return False, f"本地过滤正则错误: {e}", []
        lines = [ln for ln in lines if pat.search(ln)]
    # 本地兜底截断（设备侧 -z/-a 与 -t 等组合时可能返回多于 N 行，实测 -z 500 曾得 2824 行）
    if tail and len(lines) > int(tail):
        lines = lines[-int(tail):]
    elif head and len(lines) > int(head):
        lines = lines[:int(head)]
    if out_file:
        try:
            with open(out_file, "w", encoding="utf-8") as f:
                f.write("\n".join(lines))
        except OSError as e:
            return False, f"写入失败: {e}", lines
        return True, f"已保存 {len(lines)} 行 → {out_file}", lines
    return True, f"抓取 {len(lines)} 行", lines


def list_faults(device: Optional[str] = None,
                bundle: Optional[str] = None) -> Tuple[bool, str]:
    """列出应用异常退出记录（时间/前后台/原因/record_id）"""
    return _hdc(device, ["shell", f"hidumper -e --list {bundle or default_bundle()}"],
                timeout=20)


def print_fault(device: Optional[str] = None, bundle: Optional[str] = None,
                n: int = 1, out_file: Optional[str] = None,
                timeout: int = 30) -> Tuple[bool, str, List[str]]:
    """抓取最近 N 条故障日志（含完整堆栈）"""
    remote = "/data/local/tmp/_hmuitest_fault.txt"
    cmd = f"hidumper -e --print {bundle or default_bundle()} -n {int(n)} > {remote} 2>&1"
    ok, out = _hdc(device, ["shell", cmd], timeout=timeout)
    lines = _fetch_lines(device, remote)
    if not lines:
        return False, f"故障日志抓取失败: {(out or '').strip()[:200]}", []
    if out_file:
        try:
            with open(out_file, "w", encoding="utf-8") as f:
                f.write("\n".join(lines))
        except OSError as e:
            return False, f"写入失败: {e}", lines
        return True, f"已保存 {len(lines)} 行 → {out_file}", lines
    return True, f"抓取 {len(lines)} 行", lines
