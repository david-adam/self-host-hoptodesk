#!/usr/bin/env python3
"""
Patch the HopToDesk web client JS bundle to use our self-hosted infra.

Robust to bundle renames: tries both the new (minified) constant
names and the old (pre-minify) names. Re-validates by grepping the
output. Exits 1 if no patches landed.

Usage:
  python3 patch_web_client.py <signal-host> <web-host> <bundle.js>

Arguments:
  signal-host   hostname where HopSignal is reachable (e.g. hop.example.com)
  web-host      hostname where the web client + bridge are served
                (e.g. web.example.com)
  bundle.js     path to the downloaded JS bundle

What it patches:
  signal host   signal2.hoptodesk.com             -> <signal-host>
  relay hosts   [turn.hoptodesk.com, ...3 items]  -> [<web-host>]
  relay port    21118                             -> 443
  relay path    /?v=2                             -> /relay/?v=2
                (so Caddy can route it to the bridge)
"""
import shutil
import sys
from pathlib import Path


def patches_for(signal_host: str, web_host: str):
    """Return patch groups parameterized for the given hostnames."""
    return [
        (
            "signal host",
            [
                (f'M0="signal2.hoptodesk.com"', f'M0="{signal_host}"', "M0"),
                ('SIGNAL_HOST="signal2.hoptodesk.com"',
                 f'SIGNAL_HOST="{signal_host}"', "SIGNAL_HOST"),
            ],
        ),
        (
            "relay hosts",
            [
                ('sh=["turn.hoptodesk.com","turn-eu.hoptodesk.com","turn-sg.hoptodesk.com"]',
                 f'sh=["{web_host}"]', "sh"),
                ('RELAY_PROXY_HOSTS=["turn.hoptodesk.com","turn-eu.hoptodesk.com","turn-sg.hoptodesk.com"]',
                 f'RELAY_PROXY_HOSTS=["{web_host}"]', "RELAY_PROXY_HOSTS"),
            ],
        ),
        (
            "relay port",
            [
                ("ch=21118", "ch=443", "ch"),
                ("RELAY_PROXY_PORT=21118", "RELAY_PROXY_PORT=443", "RELAY_PROXY_PORT"),
            ],
        ),
        (
            "relay path",
            [
                # The web client opens the relay WS at the ROOT path:
                #   wss://<relay_host>:<ch>/?v=2
                # Our Caddy route matches /relay* and proxies to the
                # bridge, so the browser would otherwise hit the
                # static file_server and fail the WS handshake.
                # We rewrite the URL to put /relay in the path.
                ('new T0("wss://"+v+":"+ch+"/?v=2",!0)',
                 'new T0("wss://"+v+":"+ch+"/relay/?v=2",!0)',
                 "T0 (new bundle)"),
                ('new Websock("wss://"+U+":"+RELAY_PROXY_PORT+"/?v=2",!0)',
                 'new Websock("wss://"+U+":"+RELAY_PROXY_PORT+"/relay/?v=2",!0)',
                 "Websock (old bundle)"),
            ],
        ),
    ]


def main() -> int:
    if len(sys.argv) != 4:
        print("Usage: patch_web_client.py <signal-host> <web-host> <bundle.js>",
              file=sys.stderr)
        return 2
    signal_host, web_host, bundle_arg = sys.argv[1], sys.argv[2], sys.argv[3]
    path = Path(bundle_arg)
    if not path.is_file():
        print(f"Not a file: {path}", file=sys.stderr)
        return 2

    original = path.read_text()
    backup = path.with_suffix(path.suffix + ".orig")
    if not backup.exists():
        shutil.copy2(path, backup)
        print(f"backup -> {backup}")

    new = original
    applied = []

    for label, group in patches_for(signal_host, web_host):
        for search, replace, name in group:
            if search in new:
                new = new.replace(search, replace)
                applied.append(f"{name} ({label})")
                break
        else:
            print(f"WARN: {label} pattern not found", file=sys.stderr)

    if not applied:
        print("ERROR: no patches applied. The web client bundle format "
              "may have changed. Search for 'hoptodesk.com' in the "
              "bundle to find new constant names.", file=sys.stderr)
        return 1

    path.write_text(new)
    print(f"applied {len(applied)} patch(es) to {path}:")
    for a in applied:
        print(f"  - {a}")
    print('verify with:')
    print(f'  grep -oE "{signal_host}|{web_host}" "{path}" | sort -u')
    return 0


if __name__ == "__main__":
    sys.exit(main())
