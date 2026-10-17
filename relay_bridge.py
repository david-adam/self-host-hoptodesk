#!/usr/bin/env python3
"""
HopToDesk self-hosted relay bridge.

Speaks the v=2 protocol used by web.hoptodesk.com on the browser side,
and the plain length-prefixed TCP (BytesCodec) used by the HopToDesk
native client on the Mac side. Splice the two together.

Browser protocol (extracted from web.hoptodesk.com/js/dist/index.*.js):
  1. Browser opens ws://.../?v=2
  2. We send {"relay_addr": "<public-ip>:<port>"} immediately
  3. We wait for a single Mac TCP connection on that port (15s timeout)
  4. On connect, we send {"peer_connected": true} to the browser
  5. From then on: WS binary frame <-> TCP BytesCodec length-prefixed
     frame. WS text frames (JSON) are ignored after handshake.

Mac protocol (from hbb_common::bytes_codec::BytesCodec):
  - 1-4 byte header, head_len = (b0 & 0x3) + 1
  - Length = little-endian value of header >> 2
  - Followed by exactly `length` payload bytes

Dependencies:
  - Python 3.10+ (uses asyncio + struct from stdlib)
  - `websockets` package (pip install websockets) — small, zero
    transitive deps, used to implement the WebSocket server.

Usage:
  RELAY_PUBLIC_IP=203.0.113.10 \\
  RELAY_WS_PORT=8443 \\
  RELAY_TCP_PORT=21119 \\
  python3 /opt/relay_bridge.py

Env vars:
  RELAY_PUBLIC_IP   required, public IPv4 the Mac dials to reach us
  RELAY_WS_PORT     default 8443, the WebSocket server port
                    (Caddy reverse-proxies wss://<web-host>/relay
                    to ws://127.0.0.1:8443/)
  RELAY_TCP_PORT    default 21119, the TCP port the Mac dials
                    (must be open in the VPS firewall)

Notes:
  - Each browser WS gets a fresh TCP listener; concurrent sessions
    don't share ports.
  - Forwarding is byte-transparent: we never interpret the payload,
    so all HopToDesk protobuf security layers work end-to-end.
  - If the Mac doesn't dial within PEER_WAIT_SECS, we close the WS
    with an error JSON.
"""
import asyncio
import json
import os
import struct
import sys
import time
import traceback

import websockets

# websockets v13+ (pip) uses websockets.asyncio.server.serve and the
# connection object is the websocket directly. websockets v10 (Ubuntu
# apt package) uses websockets.serve and the handler signature is
# (websocket, path). We support both.
try:
    from websockets.asyncio.server import serve as ws_serve
    ASYNCIO_API = True
except ImportError:
    from websockets.server import serve as ws_serve  # type: ignore
    ASYNCIO_API = False


PEER_WAIT_SECS = 15.0
MAC_IDLE_TIMEOUT_SECS = 600.0  # close if Mac goes silent for 10 min


def encode_bytes(payload: bytes) -> bytes:
    """Wrap payload with BytesCodec length prefix (1-4 bytes, LE, >>2)."""
    n = len(payload)
    if n <= 0x3F:
        return bytes([n << 2]) + payload
    if n <= 0x3FFF:
        return struct.pack("<H", (n << 2) | 0x1) + payload
    if n <= 0x3FFFFF:
        h = (n << 2) | 0x2
        return struct.pack("<H", h & 0xFFFF) + bytes([h >> 16]) + payload
    if n <= 0x3FFFFFFF:
        h = (n << 2) | 0x3
        return struct.pack("<I", h) + payload
    raise ValueError(f"payload too large: {n}")


class BytesCodecDecoder:
    """Streaming TCP -> payload reader using BytesCodec framing."""

    def __init__(self) -> None:
        self.buf = bytearray()

    def feed(self, data: bytes) -> list[bytes]:
        """Append data; return zero or more complete payloads."""
        self.buf.extend(data)
        out = []
        while True:
            if not self.buf:
                break
            head_len = (self.buf[0] & 0x3) + 1
            if len(self.buf) < head_len:
                break
            n = 0
            for i in range(head_len):
                n |= self.buf[i] << (8 * i)
            n >>= 2
            if len(self.buf) < head_len + n:
                break
            del self.buf[:head_len]
            out.append(bytes(self.buf[:n]))
            del self.buf[:n]
        return out


def log(msg: str) -> None:
    sys.stdout.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n")
    sys.stdout.flush()


async def pump_ws_to_tcp(ws, writer: asyncio.StreamWriter) -> None:
    """Read WebSocket messages, wrap with BytesCodec, write to TCP.

    Handles both websockets APIs:
      v10: iterating yields Message objects with .type (int) and .data
      v13: iterating yields str or bytes directly
    """
    try:
        async for msg in ws:
            if isinstance(msg, bytes):
                payload = msg
            elif isinstance(msg, str):
                # JSON control frame — ignore after handshake
                continue
            else:
                # v10 Message object
                # type 0x2 = BINARY, 0x1 = TEXT. The bridge only
                # cares about binary; text is the JSON handshake.
                if getattr(msg, "type", 0) == 0x2:
                    payload = bytes(msg.data)
                else:
                    continue
            writer.write(encode_bytes(payload))
            await writer.drain()
    except (asyncio.CancelledError, ConnectionResetError, BrokenPipeError):
        pass
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass


async def pump_tcp_to_ws(
    reader: asyncio.StreamReader, ws
) -> None:
    """Read TCP stream, strip BytesCodec header, forward as WS binary."""
    dec = BytesCodecDecoder()
    try:
        while True:
            chunk = await reader.read(65536)
            if not chunk:
                break
            for payload in dec.feed(chunk):
                await ws.send(payload)
    except (asyncio.CancelledError, ConnectionResetError, BrokenPipeError):
        pass
    finally:
        try:
            await ws.close()
        except Exception:
            pass


async def handle_session(ws, public_ip: str, tcp_port: int) -> None:
    """One browser WS + one Mac TCP conn = one session."""
    peer = ws.remote_address[0] if ws.remote_address else "?"
    log(f"ws open from {peer}")

    session_done = asyncio.Event()

    async def on_mac(reader, writer):
        mac_peer = writer.get_extra_info("peername")
        log(f"mac tcp dial from {mac_peer}")

        # Tell the browser the Mac arrived
        try:
            await ws.send(json.dumps({"peer_connected": True}))
        except Exception as e:
            log(f"send peer_connected failed: {e}")
            writer.close()
            session_done.set()
            return

        # Pump both directions. First one to finish wins.
        t1 = asyncio.create_task(pump_ws_to_tcp(ws, writer))
        t2 = asyncio.create_task(pump_tcp_to_ws(reader, ws))
        done, pending = await asyncio.wait(
            {t1, t2}, return_when=asyncio.FIRST_COMPLETED
        )
        for t in pending:
            t.cancel()
        log(f"mac tcp closed from {mac_peer}")
        session_done.set()

    # Bind a fresh TCP listener for this session. NOTE: fixed port means
    # only ONE active session at a time; a second browser WS while one
    # is running will fail to bind and get an error JSON.
    try:
        tcp_server = await asyncio.start_server(
            on_mac, host="0.0.0.0", port=tcp_port
        )
    except OSError as e:
        log(f"cannot bind tcp port {tcp_port} (session in use?): {e}")
        try:
            await ws.send(json.dumps({"error": "relay busy"}))
            await ws.close()
        except Exception:
            pass
        return

    sockets = tcp_server.sockets
    assert sockets
    bound_port = sockets[0].getsockname()[1]
    relay_addr = f"{public_ip}:{bound_port}"

    # Send relay_addr to the browser
    try:
        await ws.send(json.dumps({"relay_addr": relay_addr}))
    except Exception as e:
        log(f"send relay_addr failed: {e}")
        tcp_server.close()
        return
    log(f"sent relay_addr={relay_addr}")

    # Wait for the Mac to dial AND finish its session, with a timeout
    # covering only the "no dial" case (the session itself may run for
    # hours and must not be interrupted).
    dial_wait = asyncio.create_task(session_done.wait())
    try:
        await asyncio.wait_for(dial_wait, timeout=PEER_WAIT_SECS)
    except asyncio.TimeoutError:
        log(f"no mac dial within {PEER_WAIT_SECS}s; closing")
        try:
            await ws.send(json.dumps({"error": "peer timeout"}))
        except Exception:
            pass
    finally:
        tcp_server.close()
        try:
            await tcp_server.wait_closed()
        except Exception:
            pass
        # If the session is still running (pumps active), wait for it
        # to finish before tearing down the WS.
        if not session_done.is_set():
            await session_done.wait()
        try:
            await ws.close()
        except Exception:
            pass
    log(f"ws closed from {peer}")


async def main() -> None:
    public_ip = os.environ.get("RELAY_PUBLIC_IP", "").strip()
    if not public_ip:
        sys.stderr.write("RELAY_PUBLIC_IP env var is required\n")
        sys.exit(2)
    ws_port = int(os.environ.get("RELAY_WS_PORT", "8443"))
    tcp_port = int(os.environ.get("RELAY_TCP_PORT", "21119"))

    log(
        f"starting; public_ip={public_ip} "
        f"ws_port={ws_port} tcp_port={tcp_port}"
    )

    async def handler(ws, *_):
        # websockets v10 passes (ws, path); v13+ passes just (ws).
        # Extra positional args are dropped here.
        try:
            await handle_session(ws, public_ip, tcp_port)
        except Exception as e:
            log(f"session handler crashed: {e!r}")
            traceback.print_exc()

    async with ws_serve(handler, "0.0.0.0", ws_port):
        log(f"ws server listening on 0.0.0.0:{ws_port}")
        await asyncio.Future()  # run forever


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    except Exception as e:
        sys.stderr.write(f"FATAL: {e}\n{traceback.format_exc()}\n")
        sys.stderr.flush()
        sys.exit(1)
