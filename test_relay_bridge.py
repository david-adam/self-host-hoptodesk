#!/usr/bin/env python3
"""Smoke test for relay_bridge.py.

Simulates: browser opens WS, gets relay_addr; Mac dials that addr;
then we send a WS binary frame, expect it on the Mac's TCP stream
(BytesCodec-wrapped), and vice versa.
"""
import asyncio
import os
import struct
import sys

# Add notes dir to path so we can use the same module
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

# Run the bridge as a subprocess so we test the real code
import subprocess
import time
import socket

import websockets
import websockets.frames


def encode_bytes(payload: bytes) -> bytes:
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
    raise ValueError


async def main():
    # Start bridge on ephemeral ports
    env = os.environ.copy()
    env["RELAY_PUBLIC_IP"] = "127.0.0.1"
    env["RELAY_WS_PORT"] = "18443"
    env["RELAY_TCP_PORT"] = "0"  # we want ephemeral, but bridge uses fixed
    # Actually our bridge uses a fixed tcp_port, so use 0 and a fixed WS
    # Re-set tcp_port to a specific unused one
    env["RELAY_TCP_PORT"] = "21119"

    proc = subprocess.Popen(
        [".venv/bin/python3", "notes/relay_bridge.py"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    await asyncio.sleep(0.5)

    try:
        # 1. Browser opens WS
        async with websockets.connect("ws://127.0.0.1:18443/?v=2") as ws:
            # 2. Should receive relay_addr
            msg = await asyncio.wait_for(ws.recv(), timeout=2.0)
            print("got from bridge:", msg)
            assert msg.startswith("{"), f"expected JSON, got: {msg!r}"
            import json
            data = json.loads(msg)
            assert "relay_addr" in data, f"missing relay_addr: {data}"
            relay_addr = data["relay_addr"]
            ip, port = relay_addr.split(":")
            print(f"  relay_addr={relay_addr}")
            assert ip == "127.0.0.1"
            assert int(port) == 21119

            # 3. Mac opens a TCP connection to the relay_addr
            reader, writer = await asyncio.open_connection(ip, int(port))
            print("  mac connected to TCP")

            # 4. Bridge should send {"peer_connected": true}
            msg = await asyncio.wait_for(ws.recv(), timeout=2.0)
            print("got:", msg)
            data2 = json.loads(msg)
            assert data2.get("peer_connected") is True

            # 5. Send a WS binary frame; expect it BytesCodec-wrapped on TCP
            test_payload = b"hello-from-browser"
            await ws.send(test_payload)
            data = await asyncio.wait_for(reader.read(1024), timeout=2.0)
            print(f"  got on TCP: {data!r}")
            assert data == encode_bytes(test_payload), \
                f"framing mismatch: {data!r} vs {encode_bytes(test_payload)!r}"

            # 6. Send a TCP frame; expect it on WS as raw binary
            tcp_payload = b"hello-from-mac"
            writer.write(encode_bytes(tcp_payload))
            await writer.drain()
            ws_msg = await asyncio.wait_for(ws.recv(), timeout=2.0)
            print(f"  got on WS: {ws_msg!r}")
            assert bytes(ws_msg) == tcp_payload

            print("\nALL ASSERTIONS PASSED")

            writer.close()
            await writer.wait_closed()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()
        out = proc.stdout.read().decode()
        print("--- bridge log ---")
        print(out)


if __name__ == "__main__":
    asyncio.run(main())
