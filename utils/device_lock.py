#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""会话 + 设备占用锁（session-scoped device claims）

背景
----
autoharmony 每条命令都是独立短进程（无常驻 daemon）。同机多个 agent / worktree
并行时，如果都连同一台 HarmonyOS 设备，点击/输入会互相踩踏。本模块提供
host-local 的「设备占用声明」：

  - 会话（session）由 ``HMUITEST_SESSION_ID`` 或 agent 对话目录 / git worktree
    根目录推断；
  - 命令启动时对目标设备登记或续约一条 claim（带心跳时间戳）；
  - 若设备被另一个「仍活跃」的会话占用，则拒绝执行（除非等待或显式抢占）。

稳定会话标识
------------
opencode 的 ``shell.env`` 插件（``~/.config/opencode/plugins/device-session-env.ts``）
会把**根 sessionID** 注入 ``HMUITEST_SESSION_ID``，因此同一对话（含其 task 子
agent）解析出同一个会话 id；不同对话才是不同会话，从而做到「一个会话独占一台设备」。
没有该变量时退回下面的 cwd / worktree 推断。

活跃性判定
----------
只用 **claim 心跳 TTL**（默认 180s，可用 ``HMUITEST_CLAIM_TTL`` 调整）：心跳新于 TTL
视为活跃（会话持有中）；过期即成 stale，可被其他会话接替并给出提示。
**不看 pid**：每条命令都是短进程、命令结束 pid 即死，若以 pid 存活判定活跃，则同一
会话的下一条命令会把自己的 claim 判为 stale 而被抢走，跨命令就锁不住了。

因为没有常驻进程，活跃性用 **claim 心跳 TTL** 判定（默认 180s）：心跳新于 TTL
视为活跃；过期即成 stale，可被其他会话接替并给出提示。

存储
----
``~/.hmuitest/device-claims/<safe-device>.json``::

    {"device": "...", "session": "...", "pid": 1234, "cwd": "...",
     "created": 1710000000.0, "heartbeat": 1710000000.0}

策略（环境变量）
----------------
  HMUITEST_SESSION_ID   显式会话名；0/off/false/no/none/- 或空 → 关闭设备锁
  HMUITEST_DEVICE_LOCK  enforce(默认) | off   （off=完全关闭：不登记也不拦截）
  HMUITEST_CLAIM_TTL    心跳过期秒数（默认 180，3 分钟）
  HMUITEST_CLAIM_DIR    声明目录（默认 ~/.hmuitest/device-claims）

CLI
---
  autoharmony device status [--json]
  （无 release 接口：锁的目的是防互拆，被占用时停止等待；过期声明自动被接替）
"""

import hashlib
import json
import os
import re
import subprocess
import time
from typing import Dict, List, Optional

_DEFAULT_SUBDIR = os.path.join(".hmuitest", "device-claims")
_OFF = {"0", "off", "false", "no", "none", "-", "disable", "disabled"}
_SESSION_RE = re.compile(r"[/\\]session-([0-9a-fA-F][0-9a-fA-F-]{6,})")
_DEFAULT_TTL = 180.0

# 进程内已登记的设备（同一命令内 detect_device_id 可能被调用多次）
_CLAIMED = set()
# main() 注入的命令级策略
_POLICY = {"takeover": False, "wait_s": 0.0, "enabled": None}
# 会话/标签缓存（避免每个进程重复解析）
_SESSION_CACHE = {"resolved": False, "value": None}


class ClaimOutcome:
    """一次 claim 的结果（供调用方决定放行 / 拒绝 / 提示）。"""

    def __init__(self, ok: bool, code: str,
                 owner_session: Optional[str] = None,
                 owner_pid: Optional[int] = None,
                 stale: bool = False, waited_ms: int = 0):
        self.ok = ok
        self.code = code
        self.owner_session = owner_session
        self.owner_pid = owner_pid
        self.stale = stale
        self.waited_ms = waited_ms

    def as_dict(self) -> Dict:
        return {
            "ok": self.ok,
            "code": self.code,
            "owner_session": self.owner_session,
            "owner_pid": self.owner_pid,
            "stale": self.stale,
            "waited_ms": self.waited_ms,
        }


class _FileLock:
    """跨进程互斥（best-effort）：fcntl 可用时用 flock，否则退化为无锁。

    锁文件与 claim 文件同目录（``<claim>.lock``），只用于串行化读改写，
    避免两个进程同时抢同一台设备时都判空成功。
    """

    def __init__(self, path: str):
        self.path = path
        self._fh = None

    def __enter__(self):
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            self._fh = open(self.path, "a+")
            try:
                import fcntl
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX)
            except Exception:
                pass
        except Exception:
            self._fh = None
        return self

    def __exit__(self, *exc):
        try:
            if self._fh is not None:
                try:
                    import fcntl
                    fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
                except Exception:
                    pass
                self._fh.close()
        except Exception:
            pass
        return False


# ==================== 策略 / 会话 ====================

def configure(takeover: Optional[bool] = None,
              wait_s: Optional[float] = None,
              enabled: Optional[bool] = None) -> None:
    """main() 注入命令级策略（None 表示不改动）。"""
    if takeover is not None:
        _POLICY["takeover"] = bool(takeover)
    if wait_s is not None:
        _POLICY["wait_s"] = max(float(wait_s), 0.0)
    if enabled is not None:
        _POLICY["enabled"] = bool(enabled)


def _is_off(raw: str) -> bool:
    return (raw or "").strip().lower() in _OFF


def lock_enabled() -> bool:
    if _POLICY["enabled"] is False:
        return False
    if _is_off(os.environ.get("HMUITEST_DEVICE_LOCK", "enforce")):
        return False
    return resolve_session_id() is not None


def _ttl() -> float:
    try:
        return max(float(os.environ.get("HMUITEST_CLAIM_TTL", _DEFAULT_TTL)), 1.0)
    except (TypeError, ValueError):
        return _DEFAULT_TTL


def _worktree_root() -> Optional[str]:
    """git worktree 根目录（同 agent-device 的 worktree 会话语义）；非仓库返回 None。"""
    try:
        r = subprocess.run(["git", "rev-parse", "--show-toplevel"],
                           capture_output=True, text=True, timeout=2)
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    except Exception:
        pass
    return None


def resolve_session_id() -> Optional[str]:
    """解析当前会话标识；返回 None 表示关闭设备锁。

    优先级：
      1. HMUITEST_SESSION_ID（显式；off 值 → None）
      2. opencode / agent 对话目录（.../session-<id>）
      3. git worktree 根目录，否则 cwd —— 用稳定短哈希标识
    """
    if _SESSION_CACHE["resolved"]:
        return _SESSION_CACHE["value"]
    value = _resolve_session_uncached()
    _SESSION_CACHE["resolved"] = True
    _SESSION_CACHE["value"] = value
    return value


def _resolve_session_uncached() -> Optional[str]:
    raw = os.environ.get("HMUITEST_SESSION_ID")
    if raw is not None:
        raw = raw.strip()
        if not raw or _is_off(raw):
            return None
        return raw
    if os.environ.get("OPENCODE") or os.environ.get("AGENT"):
        cands = [os.environ.get("PWD"), os.environ.get("OLDPWD"), os.getcwd()]
        cands.extend(os.environ.values())
        for cand in cands:
            m = _SESSION_RE.search(cand or "")
            if m:
                return "agent-" + m.group(1)[:12]
    base = _worktree_root() or os.getcwd()
    digest = hashlib.sha1(base.encode("utf-8", "replace")).hexdigest()[:6]
    return "cwd-" + digest


# ==================== 存储 ====================

def claim_dir() -> str:
    d = os.environ.get("HMUITEST_CLAIM_DIR")
    if d:
        return os.path.expanduser(d)
    return os.path.join(os.path.expanduser("~"), _DEFAULT_SUBDIR)


def _safe(name: str) -> str:
    return re.sub(r"[^0-9A-Za-z._-]", "_", name or "") or "default"


def _claim_path(device: str) -> str:
    return os.path.join(claim_dir(), _safe(device) + ".json")


def _read(path: str) -> Optional[Dict]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _write(path: str, data: Dict) -> None:
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = "%s.tmp.%d" % (path, os.getpid())
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError:
        pass


def _remove(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def _age(cur: Dict, now: Optional[float] = None) -> float:
    now = time.time() if now is None else now
    try:
        return max(now - float(cur.get("heartbeat", 0)), 0.0)
    except (TypeError, ValueError):
        return float("inf")


# ==================== claim 生命周期 ====================

def ensure_claim(device: str,
                 wait_s: Optional[float] = None,
                 takeover: Optional[bool] = None) -> ClaimOutcome:
    """登记 / 续约目标设备的占用声明。

    Returns:
        ClaimOutcome: ok=True 放行；ok=False 且 code='DEVICE_IN_USE' 表示被占用。
    """
    if not lock_enabled():
        return ClaimOutcome(True, "DISABLED")
    session = resolve_session_id()
    if not session:
        return ClaimOutcome(True, "DISABLED")
    if device in _CLAIMED:
        return ClaimOutcome(True, "OWNED")

    takeover = _POLICY["takeover"] if takeover is None else bool(takeover)
    budget = _POLICY["wait_s"] if wait_s is None else max(float(wait_s), 0.0)
    deadline = time.monotonic() + budget
    waited = 0.0
    path = _claim_path(device)
    lock = path + ".lock"

    while True:
        with _FileLock(lock):
            now = time.time()
            cur = _read(path)
            if cur and cur.get("session") == session:
                cur.update({"pid": os.getpid(), "cwd": os.getcwd(),
                            "heartbeat": now})
                _write(path, cur)
                _CLAIMED.add(device)
                return ClaimOutcome(True, "RENEWED")
            if cur:
                age = _age(cur, now)
                # 活跃性只按心跳租约判定：每条命令都是短进程，pid 命令一结束就死，
                # 若把「pid 已死」当作失效，则同一会话的下一条命令就会把自己的
                # claim 抢占掉 → 跨命令根本锁不住。会话标识稳定后（见 shell.env
                # 插件注入 HMUITEST_SESSION_ID），靠 session 相等即可续约，
                # 不相等且心跳未过期则视为被占用。
                live = age < _ttl()
                if live and not takeover:
                    if time.monotonic() < deadline:
                        pass  # 等一会儿再抢
                    else:
                        return ClaimOutcome(False, "DEVICE_IN_USE",
                                            cur.get("session"),
                                            cur.get("pid"), False,
                                            int(waited * 1000))
                else:
                    stolen = not live
                    _write(path, {"device": device, "session": session,
                                  "pid": os.getpid(), "cwd": os.getcwd(),
                                  "created": now, "heartbeat": now})
                    _CLAIMED.add(device)
                    return ClaimOutcome(True,
                                        "STALE_STOLEN" if stolen else "TAKEOVER",
                                        cur.get("session"), cur.get("pid"),
                                        stolen, int(waited * 1000))
            else:
                _write(path, {"device": device, "session": session,
                              "pid": os.getpid(), "cwd": os.getcwd(),
                              "created": now, "heartbeat": now})
                _CLAIMED.add(device)
                return ClaimOutcome(True, "ACQUIRED")
        # 仅在等待预算内空转
        sleep_for = min(0.2, max(deadline - time.monotonic(), 0.0))
        if sleep_for <= 0:
            # 预算耗尽，回到循环顶部给出 DEVICE_IN_USE
            continue
        time.sleep(sleep_for)
        waited += sleep_for


def list_claims() -> List[Dict]:
    """列出全部 claim（含活跃/过期判定）。"""
    out: List[Dict] = []
    d = claim_dir()
    if not os.path.isdir(d):
        return out
    now = time.time()
    ttl = _ttl()
    for fn in sorted(os.listdir(d)):
        if not fn.endswith(".json"):
            continue
        cur = _read(os.path.join(d, fn))
        if not cur:
            continue
        age = _age(cur, now)
        out.append({
            "device": cur.get("device") or fn[:-5],
            "session": cur.get("session"),
            "pid": cur.get("pid"),
            "cwd": cur.get("cwd"),
            "age_s": int(age),
            "live": age < ttl,
        })
    return out
