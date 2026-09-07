"""
hdc 工具集：设备 ID 自动检测 + session 锁定

设备选择优先级：
  1. HARMONY_DEVICE_ID 环境变量
  2. ~/.hm_bridge_device session 锁文件
  3. hdc list targets（仅单设备时自动锁定）
"""

import os
import subprocess
import logging
from typing import Optional

logger = logging.getLogger(__name__)

_SESSION_FILE = os.path.expanduser("~/.hm_bridge_device")


def _session_device() -> Optional[str]:
    try:
        with open(_SESSION_FILE) as f:
            val = f.read().strip()
            return val if val else None
    except (FileNotFoundError, OSError):
        return None


def _save_session_device(device: str) -> None:
    try:
        with open(_SESSION_FILE, 'w') as f:
            f.write(device.strip())
    except OSError:
        pass


def _list_online_devices() -> list:
    """获取当前在线的 HDC 设备 ID 列表"""
    try:
        result = subprocess.run(
            ["hdc", "list", "targets"],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode != 0:
            return []
        return [l.strip().split()[0] for l in result.stdout.strip().split('\n') if l.strip()]
    except FileNotFoundError:
        logger.warning("未找到 hdc 命令")
        return []
    except Exception as e:
        logger.warning(f"设备检测异常: {e}")
        return []


def detect_device_id(allow_env: bool = True, allow_session: bool = True) -> Optional[str]:
    """
    自动检测可用的 HDC 设备 ID

    优先级：
     1. HARMONY_DEVICE_ID 环境变量（设置即锁定到 session 文件）
     2. ~/.hm_bridge_device session 锁文件（需验证设备仍在线）
     3. hdc list targets 自动检测（仅单设备时）

    规则：
      - 0 台设备 → 返回 None
      - 1 台设备 → 自动使用并锁定到 session
      - 2+ 台设备 → 打印列表，返回 None（让用户通过 HARMONY_DEVICE_ID 指定）

    Args:
        allow_env: 是否检查环境变量
        allow_session: 是否检查 session 文件

    Returns:
        设备序列号，或 None
    """
    # 1. 环境变量（最高优先级，设置即锁定）
    if allow_env:
        env_id = os.environ.get('HARMONY_DEVICE_ID')
        if env_id:
            _save_session_device(env_id)
            logger.info(f"使用环境变量指定的设备: {env_id}")
            return env_id

    online_devices = _list_online_devices()

    # 2. Session 锁文件（需验证设备仍在线，离线则清除并重新检测）
    if allow_session:
        cached = _session_device()
        if cached:
            if cached in online_devices:
                logger.info(f"使用 session 锁定设备: {cached}")
                return cached
            logger.warning(f"session 锁定设备 {cached} 已离线，清除 session 并重新检测")
            try:
                os.remove(_SESSION_FILE)
            except OSError:
                pass

    # 3. hdc list targets 自动检测（仅单设备时自动锁定）
    if not online_devices:
        return None
    if len(online_devices) == 1:
        serial = online_devices[0]
        _save_session_device(serial)
        logger.info(f"自动检测并锁定设备: {serial}")
        return serial

    logger.warning(f"检测到 {len(online_devices)} 台设备，请设置 HARMONY_DEVICE_ID 指定目标设备:")
    for serial in online_devices:
        logger.warning(f"  {serial}")
    logger.warning("提示: 可执行 export HARMONY_DEVICE_ID=<serial> 锁定设备")
    return None
