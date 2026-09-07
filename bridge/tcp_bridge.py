"""
TCP Bridge — Connect to device app via hdc fport + socket (JSON-RPC 2.0)

Generic bridge framework for HarmonyOS app communication.
Business methods are registered by the user, not hardcoded.

Usage:
    bridge = TcpBridge(device="xxx")
    result = bridge.call("navigate", {"route": "MainPage"})
    bridge.close()
"""

import json
import logging
import socket as sock
import subprocess
import time
import uuid
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)

DEVICE_BRIDGE_PORT = 9999
FPORT_BASE = 19999


def _device_fport(device: Optional[str]) -> int:
    if device:
        return FPORT_BASE + (hash(device) & 0x7FFFFFFF) % 1000
    return FPORT_BASE


def _ensure_fport(device: Optional[str]) -> int:
    from utils.hdc import detect_device_id
    pc_port = _device_fport(device)
    hdc = ["hdc"]
    if device:
        hdc.extend(["-t", device])
    try:
        subprocess.run(
            hdc + ["fport", "rm", f"tcp:{pc_port}", f"tcp:{DEVICE_BRIDGE_PORT}"],
            capture_output=True, timeout=5
        )
    except Exception:
        pass
    try:
        result = subprocess.run(
            hdc + ["fport", f"tcp:{pc_port}", f"tcp:{DEVICE_BRIDGE_PORT}"],
            capture_output=True, timeout=5
        )
        if result.returncode != 0:
            logger.warning(f"fport setup failed: {result.stderr.decode(errors='ignore')}")
    except Exception as e:
        logger.warning(f"fport setup error: {e}")
    return pc_port


def _cleanup_fport(device: Optional[str], pc_port: int) -> None:
    hdc = ["hdc"]
    if device:
        hdc.extend(["-t", device])
    try:
        subprocess.run(
            hdc + ["fport", "rm", f"tcp:{pc_port}", f"tcp:{DEVICE_BRIDGE_PORT}"],
            capture_output=True, timeout=5
        )
    except Exception:
        pass


class TcpBridge:
    """Generic TCP bridge for HarmonyOS app communication via JSON-RPC 2.0.

    Register your own business methods:
        bridge = TcpBridge(device="xxx")
        bridge.register("login", login_handler)
        bridge.register("queryDevices", query_devices_handler)

    Or use the generic call():
        result = bridge.call("navigate", {"route": "MainPage"})
    """

    def __init__(self, device: Optional[str] = None, timeout: int = 10):
        from utils.hdc import detect_device_id
        self.device = device or detect_device_id()
        self.timeout = timeout
        self.pc_port = _ensure_fport(self.device)
        self._handlers: Dict[str, Callable] = {}
        logger.info(f"TCP Bridge: device={self.device}, pc_port={self.pc_port}")

    def close(self) -> None:
        _cleanup_fport(self.device, self.pc_port)

    def register(self, method: str, handler: Callable) -> None:
        """Register a business method handler."""
        self._handlers[method] = handler

    def call(self, method: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Call a remote method via JSON-RPC 2.0."""
        return self._send_request(method, params)

    def _send_request(self, method: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        request: Dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": str(uuid.uuid4()),
            "method": method,
        }
        if params:
            request["params"] = params

        raw_request = json.dumps(request) + "\n"
        last_err: Optional[Exception] = None
        for attempt in range(3):
            if attempt > 0:
                time.sleep(0.5)
            try:
                return self._send_once(raw_request, method)
            except (ConnectionError, BrokenPipeError) as e:
                last_err = e
                logger.warning(f"{method} connection error (attempt {attempt + 1}): {e}")
        raise last_err

    def _send_once(self, raw_request: str, method: str) -> Dict[str, Any]:
        s = sock.socket(sock.AF_INET, sock.SOCK_STREAM)
        s.settimeout(self.timeout)
        try:
            s.connect(("127.0.0.1", self.pc_port))
            s.sendall(raw_request.encode("utf-8"))
            data = b""
            while True:
                chunk = s.recv(65536)
                if not chunk:
                    break
                data += chunk
            s.close()
            if not data:
                raise ConnectionError("empty response from device")
            text = data.decode("utf-8", errors="replace")
            response: dict = json.loads(text, strict=False)
            if "result" in response:
                return response["result"]
            error = response.get("error", {})
            raise RuntimeError(error.get("message", "unknown error"))
        except sock.timeout:
            raise TimeoutError(f"request timeout ({self.timeout}s): {method}")
        finally:
            try:
                s.close()
            except Exception:
                pass
