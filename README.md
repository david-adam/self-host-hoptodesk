# self-host-hoptodesk

Self-host the [HopToDesk](https://hoptodesk.com) web client so it works on
networks (corporate firewalls, restrictive Wi-Fi) that block HopToDesk's
own relay servers on port 21118.

This pairs with [HopSignal](https://hoptodesk.com/self-host.html) and
[coturn](https://github.com/coturn/coturn) for the rest of the HopToDesk
self-host stack.

## What this adds

HopToDesk ships a self-hostable signal server (`hopsignal`) but does
**not** ship a self-hostable web client. The web client
(`web.hoptodesk.com`) hardcodes:

- `signal2.hoptodesk.com:443` — the signal host
- `turn.hoptodesk.com:21118` — the relay proxy (TCP, not TURN/ICE)

Port 21118 is blocked by most corporate firewalls, so the web client
fails with "Could not establish a relay connection" on those networks.

This repo gives you a custom TCP↔WebSocket relay bridge plus a way to
patch the official web client bundle to use your own hostnames, so all
traffic stays on port 443 (which most corporate firewalls allow).

## What you get

After running `setup_web_client.sh` on your VPS:

| Component        | Where it lives             | What it does                              |
|------------------|----------------------------|-------------------------------------------|
| HopSignal        | `:9000`                    | Signal/registration (unchanged)            |
| coturn           | `:3478`/`:5349`            | TURN/ICE for native apps (unchanged)       |
| **relay bridge** | `:8443`                    | TCP↔WS splice for the web client          |
| **Caddy**        | `:80`/`:443`               | TLS + reverse-proxy + serves web bundle   |
| patched web      | `/var/www/web/`            | Self-hosted web client (HopToDesk-patched)|

End result: a browser on any network that allows outbound :443 can
remote-control your Mac by visiting `https://<web-host>/#id=<DEVICE_ID>`.

## Components

- `relay_bridge.py` — Python 3.10+ bridge. Speaks the v=2 protocol
  the web client uses, and the plain BytesCodec length-prefixed TCP
  the native client uses. Splice the two together. Pinned port 21119
  for the Mac-side TCP listener (one concurrent web session at a time).
- `patch_web_client.py` — Rewrites the constants in the official web
  client bundle to point at your hostnames. Handles both the
  pre-2025 readable form and the post-2025 minified form. Also adds
  the `/relay` URL path so Caddy can route it to the bridge.
- `setup_web_client.sh` — One-shot VPS deployment. Installs the
  bridge, downloads the web client, patches the bundle, writes the
  Caddyfile, gets a Let's Encrypt cert, restarts everything.
- `Caddyfile` — Caddy v2 config. Two vhosts (signal + web), TLS to
  both, reverse-proxy for the relay path with `transport http
  { versions 1.1 }` because the bridge is HTTP/1.1-only.
- `test_relay_bridge.py` — Local smoke test. Spawns the bridge and
  exercises the full handshake + framing both ways + the no-Mac-dial
  timeout path.

## Architecture (the protocol, decoded from the JS)

The web client **does not use WebRTC/TURN at all** (zero
`RTCPeerConnection` in the bundle). The flow is all WebSocket:

```
[Browser] ---wss---> [<web-host>/relay/?v=2]
                          |
                          v
                      [bridge :8443]
                          |  (bridge waits for a Mac TCP dial)
                          v
[Mac native client] --tcp---> [<vps-ip>:21119]
```

1. Browser opens `wss://<web-host>/relay/?v=2`
2. Bridge replies with JSON: `{"relay_addr": "<vps-ip>:21119"}`
3. Browser sends `RelayConnection` to the signal server:
   `{protocol:"one-to-one", endpoint:<id>, addr:<relay_addr>}`
4. Mac native client receives `RelayConnection` over its signal WS
   connection and dials plain TCP to `<vps-ip>:21119`
5. Bridge detects the Mac TCP conn, replies to browser
   `{"peer_connected": true}`
6. Browser sends `RelayReady` via signal; signaling WS is closed
7. All subsequent session bytes flow:
   Browser WS binary frame ↔ [bridge adds/strips BytesCodec length
   prefix] ↔ Mac TCP stream

End-to-end encryption is above the bridge layer (protobuf secure
handshake), so the bridge is byte-transparent.

## Quick start

### 1. Pre-reqs (one-time, on the VPS)

- A Linux VPS (Ubuntu 22.04+ recommended)
- **HopSignal** running on port 80 (the script will move it to 9000)
- **coturn** running (TURN/STUN for native clients)
- A domain you control, with DNS-only (grey cloud) A records for
  `<signal-host>`, `<web-host>`, `<turn-host>` all pointing at the VPS
- A Let's Encrypt cert for `<signal-host>` already in
  `/etc/letsencrypt/live/<signal-host>/`
- VPS firewall open: TCP 80, 443, 21119

### 2. Run the setup

Copy `relay_bridge.py`, `patch_web_client.py`, and `setup_web_client.sh`
to `/tmp/` on the VPS. Then:

```bash
# Adjust the env vars to match your setup
export SIGNAL_HOST=hop.example.com
export WEB_HOST=web.example.com
export VPS_PUBLIC_IP=203.0.113.10
export TURN_HOST=turn.example.com
export LE_EMAIL=you@example.com

sudo bash /tmp/setup_web_client.sh
```

The script is idempotent; re-running it is safe.

### 3. Use it

From any browser on a network that allows outbound :443:

```
https://<web-host>/#id=<DEVICE_ID>
```

`<DEVICE_ID>` is the Mac's HopToDesk ID, shown in the main window.

## Tested against

- HopToDesk Mac client 1.46.60
- HopToDesk web client bundle `index.80662980.js` and
  `index.DKYCtYvy.js` (HopToDesk rolls the bundle hash; the patcher
  handles both)
- Python 3.10 + 3.12
- Caddy 2.11
- Ubuntu 22.04 Lightsail

## License

MIT. See [LICENSE](LICENSE).

## Credits

- [HopToDesk](https://hoptodesk.com) — the open-source remote desktop
  this all plugs into
- [RustDesk](https://rustdesk.com) — the codebase HopToDesk forked
  from; the `BytesCodec` framing we replicate is from
  `libs/hbb_common/src/bytes_codec.rs` in their repo
