# Bridge Extension Guide

`autoharmony` ships with a generic TCP bridge for communicating with your HarmonyOS app. This doc explains how to extend it with your own business methods.

## How It Works

```
┌─────────────┐    hdc fport     ┌─────────────┐    TCP/JSON-RPC    ┌──────────────┐
│  autoharmony   │ ──────────────── │   hdc bridge │ ────────────────── │  Your App    │
│  (Python)   │                  │  (pc port)   │                    │  (ArkTS)     │
└─────────────┘                  └─────────────┘                    └──────────────┘
```

1. `autoharmony` sets up an `hdc fport` forwarding from `PC:19999+hash` → `device:9999`
2. Your HarmonyOS app listens on port 9999 and speaks JSON-RPC 2.0
3. Bridge calls are sent over this tunnel

## Quick Start

```python
from bridge.tcp_bridge import TcpBridge

# Connect to device
bridge = TcpBridge(device="your-device-id")

# Call a method on the app
result = bridge.call("navigate", {"route": "MainPage"})
print(result)  # {"success": True, ...}

# Clean up
bridge.close()
```

## Registering Business Methods

For server-side dispatch (if you're building a bridge server), register handlers:

```python
from bridge.tcp_bridge import TcpBridge

bridge = TcpBridge(device="your-device-id")

def handle_login(params):
    phone = params.get("phone")
    # ... perform login logic ...
    return {"success": True, "token": "xxx"}

def handle_query_devices(params):
    # ... query device list ...
    return {"devices": [{"name": "Light", "id": "xxx"}]}

bridge.register("login", handle_login)
bridge.register("queryDevices", handle_query_devices)

# Now these methods can be called via JSON-RPC
result = bridge.call("login", {"phone": "13800138000"})
```

## JSON-RPC 2.0 Protocol

Requests:
```json
{
  "jsonrpc": "2.0",
  "id": "unique-request-id",
  "method": "navigate",
  "params": {"route": "SettingsPage"}
}
```

Success response:
```json
{
  "jsonrpc": "2.0",
  "id": "unique-request-id",
  "result": {"success": true}
}
```

Error response:
```json
{
  "jsonrpc": "2.0",
  "id": "unique-request-id",
  "error": {"code": -1, "message": "Unknown route"}
}
```

## ArkTS Side Example

Your HarmonyOS app needs a TCP server listening on port 9999. Here's a minimal example:

```typescript
// In your ArkTS entry ability
import { socket } from '@kit.NetworkKit';

const serverPort = 9999;

async function startBridgeServer() {
  const tcpServer = new socket.TCPSocketServer();
  await tcpServer.listen({ address: { address: '0.0.0.0', family: 1 }, port: serverPort });

  tcpServer.on('connect', (clientSocket: socket.TCPSocketConnection) => {
    clientSocket.on('message', (value: ArrayBuffer) => {
      const text = new TextDecoder().decode(value);
      const request = JSON.parse(text);

      // Dispatch by method
      let result;
      switch (request.method) {
        case 'navigate':
          result = handleNavigate(request.params);
          break;
        case 'queryDevices':
          result = handleQueryDevices(request.params);
          break;
        default:
          clientSocket.send(JSON.stringify({
            jsonrpc: '2.0', id: request.id,
            error: { code: -1, message: `Unknown method: ${request.method}` }
          }));
          return;
      }

      clientSocket.send(JSON.stringify({
        jsonrpc: '2.0', id: request.id, result
      }));
    });
  });
}
```

## Port Mapping

- Device port: `9999` (fixed)
- PC port: `19999 + (hash(device) % 1000)` (auto-calculated)
- `fport` is set up automatically by `TcpBridge.__init__()` and cleaned up by `close()`

## Error Handling

The bridge handles connection retries (3 attempts with 0.5s backoff). If your app isn't running or the bridge isn't initialized, you'll get a `ConnectionError`.

Timeout is configurable via `TcpBridge(device, timeout=10)`.
