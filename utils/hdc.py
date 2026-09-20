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

_DEFAULT_SESSION_FILE = os.path.expanduser("~/.hm_bridge_device")


def session_file() -> str:
    """会话设备锁文件路径。

    可用环境变量 HMUITEST_SESSION_FILE 覆盖，便于多会话并行时各自独立，
    避免共用 ~/.hm_bridge_device 导致会话间设备选择互相污染。
    """
    return os.path.expanduser(
        os.environ.get("HMUITEST_SESSION_FILE") or _DEFAULT_SESSION_FILE)


def _session_device() -> Optional[str]:
    try:
        with open(session_file()) as f:
            val = f.read().strip()
            return val if val else None
    except (FileNotFoundError, OSError):
        return None


def _save_session_device(device: str) -> None:
    try:
        with open(session_file(), 'w') as f:
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


def claim_device(device: str, quiet: bool = False):
    """为目标设备登记/续约占用声明。

    Returns:
        None 表示放行；被其他活跃会话占用时返回 ClaimOutcome（含 owner 信息）。
    """
    try:
        try:
            from utils.device_lock import ensure_claim
        except ImportError:
            from device_lock import ensure_claim  # skill 侧扁平导入
        outcome = ensure_claim(device)
    except Exception as e:  # 锁失败不应阻断主流程
        logger.warning(f"设备占用锁异常（忽略，继续执行）: {e}")
        return None
    if outcome.ok:
        if outcome.code == "STALE_STOLEN":
            logger.warning(
                f"设备 {device} 原会话 {outcome.owner_session} 声明已过期，已接替")
        return None
    if not quiet:
        print(f"❌ 设备 {device} 已被会话 {outcome.owner_session} 占用"
              f"（pid={outcome.owner_pid}）")
        print("   等待/抢占：--device-wait <秒> / --device-takeover；"
              "查看：autoharmony device status")
        print(f"ACTION_VERDICT: DEVICE_IN_USE | "
              f"reason=held by session {outcome.owner_session} | "
              f"suggestion=retry with --device-wait or --device-takeover")
    return outcome


def detect_device_id(allow_env: bool = True, allow_session: bool = True) -> Optional[str]:
    """自动检测设备并在会话内登记占用；冲突时返回 None。

    设备解析见 :func:`detect_device_raw`；解析成功后调用 device_lock 登记 claim，
    使同机并行的其他会话不会抢用同一台设备（可用 HMUITEST_DEVICE_LOCK=off 关闭）。
    """
    device = detect_device_raw(allow_env=allow_env, allow_session=allow_session)
    if device and claim_device(device) is not None:
        return None
    return device


def detect_device_raw(allow_env: bool = True, allow_session: bool = True) -> Optional[str]:
    """
    自动检测可用的 HDC 设备 ID（不含占用锁）

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
                os.remove(session_file())
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
