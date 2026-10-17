#!/bin/bash
# Deploy self-hosted HopToDesk web client on a Linux VPS.
# Idempotent. Run as a user with sudo.
#
# What it does:
#   1. Move HopSignal from :80 to :9000 (so Caddy can own :80 for ACME)
#   2. Install the relay bridge + systemd service
#   3. Download web client assets, patch the JS bundle, serve via Caddy
#   4. Update Caddyfile with <signal-host> and <web-host> vhosts
#   5. Get a Let's Encrypt cert for <web-host>
#   6. Restart everything
#
# Configuration (env vars; defaults shown for example.com):
#   SIGNAL_HOST       hop.example.com        (where HopSignal is reached)
#   WEB_HOST          web.example.com        (where the web client is served)
#   VPS_PUBLIC_IP     203.0.113.10           (the VPS's public IPv4)
#   TURN_HOST         turn.example.com       (your existing coturn hostname)
#   RELAY_TCP_PORT    21119                  (Mac dials this)
#   RELAY_WS_PORT     8443                   (Caddy reverse-proxies to this)
#   LE_EMAIL          you@example.com        (for certbot registration)
#
# Pre-reqs (not installed by this script):
#   - Caddy installed and running
#   - HopSignal running on port 80 (this script will move it to 9000)
#   - coturn running
#   - Existing /etc/letsencrypt/live/<signal-host> certs
#   - DNS A records for <signal-host>, <web-host>, <turn-host>
#     all pointing to VPS_PUBLIC_IP, all DNS-only (grey cloud)
#     for certbot HTTP-01 to work
#   - VPS firewall: TCP 80, 443, <RELAY_TCP_PORT> inbound open
#   - This script is meant to be run after copying all three files
#     (relay_bridge.py, patch_web_client.py, setup_web_client.sh) into
#     the same /tmp/ directory on the VPS.
#
# After running, browse to https://<web-host>/#id=<DEVICE_ID>

set -euo pipefail

SIGNAL_HOST="${SIGNAL_HOST:-hop.example.com}"
WEB_HOST="${WEB_HOST:-web.example.com}"
VPS_PUBLIC_IP="${VPS_PUBLIC_IP:-203.0.113.10}"
TURN_HOST="${TURN_HOST:-turn.example.com}"
RELAY_PUBLIC_IP="${RELAY_PUBLIC_IP:-$VPS_PUBLIC_IP}"
RELAY_WS_PORT="${RELAY_WS_PORT:-8443}"
RELAY_TCP_PORT="${RELAY_TCP_PORT:-21119}"
WEB_ROOT="${WEB_ROOT:-/var/www/web}"
LE_EMAIL="${LE_EMAIL:-you@example.com}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

log() { printf "[setup] %s\n" "$*"; }

# ---------- 0. Sanity checks ----------
log "preflight checks"
if ! command -v caddy >/dev/null 2>&1; then
  echo "caddy not found in PATH. Install Caddy first." >&2
  exit 1
fi
if ! command -v systemctl >/dev/null 2>&1; then
  echo "systemctl not found. This script is for systemd-based hosts." >&2
  exit 1
fi
for f in relay_bridge.py patch_web_client.py; do
  if [[ ! -f "$SCRIPT_DIR/$f" ]]; then
    echo "$f not found in $SCRIPT_DIR" >&2
    exit 1
  fi
done
if ! id "caddy" >/dev/null 2>&1; then
  echo "user 'caddy' not found. Is Caddy installed?" >&2
  exit 1
fi

# ---------- 1. Move HopSignal from :80 to :9000 ----------
HOPSIGNAL_SH="/home/hopsignal/hopsignal.sh"
if [[ -f "$HOPSIGNAL_SH" ]]; then
  if grep -q -- "--ADDR 0.0.0.0:80" "$HOPSIGNAL_SH"; then
    log "moving HopSignal :80 -> :9000"
    sudo sed -i 's|--ADDR 0.0.0.0:80|--ADDR 0.0.0.0:9000|' "$HOPSIGNAL_SH"
    sudo systemctl restart hopsignal
    sleep 1
  else
    log "HopSignal already not on :80 (skipping move)"
  fi
else
  echo "HopSignal wrapper $HOPSIGNAL_SH not found" >&2
  exit 1
fi

# ---------- 2. Install websockets (Python 3.10+ compatible) ----------
# Ubuntu 22.04 ships websockets 9.1 which is incompatible with
# Python 3.10 (asyncio.Lock removed the loop kwarg). Install >=10.
if /usr/bin/python3 -c "import websockets; v=int(websockets.__version__.split('.')[0]); assert v>=10" 2>/dev/null; then
  log "websockets >=10 already installed"
else
  log "installing websockets >=10 (Ubuntu 22.04 default is too old for Python 3.10)"
  if ! command -v pip3 >/dev/null 2>&1; then
    sudo DEBIAN_FRONTEND=noninteractive apt-get install -y python3-pip
  fi
  # Older pip (22.0) on Ubuntu 22.04 doesn't recognize
  # --break-system-packages; the env isn't PEP 668-managed anyway.
  sudo pip3 install --quiet "websockets>=10.4" \
    || { echo "pip install websockets failed" >&2; exit 1; }
fi

# ---------- 3. Install relay bridge ----------
log "installing relay_bridge.py -> /opt/"
sudo install -m 755 "$SCRIPT_DIR/relay_bridge.py" /opt/relay_bridge.py

log "installing systemd service"
sudo tee /etc/systemd/system/relay-bridge.service >/dev/null <<EOF
[Unit]
Description=HopToDesk relay bridge (web client <-> Mac TCP)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=caddy
Group=caddy
Environment=RELAY_PUBLIC_IP=${RELAY_PUBLIC_IP}
Environment=RELAY_WS_PORT=${RELAY_WS_PORT}
Environment=RELAY_TCP_PORT=${RELAY_TCP_PORT}
ExecStart=/usr/bin/python3 /opt/relay_bridge.py
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now relay-bridge
sleep 1
sudo systemctl --no-pager status relay-bridge | head -5 || true

# ---------- 4. Download web client assets ----------
log "downloading web client assets"
TMPDIR="$(mktemp -d)"
trap 'rm -rf "$TMPDIR"' EXIT

# Re-populate from scratch every run, so a corrupted root is fixed
# automatically (file_server silently 502s an empty root otherwise).
sudo rm -rf "$WEB_ROOT"
sudo mkdir -p "$WEB_ROOT"

cd "$TMPDIR"
curl -sL https://web.hoptodesk.com/ -o index.html
if [[ ! -s index.html ]]; then
  echo "ERROR: failed to fetch https://web.hoptodesk.com/" >&2
  exit 1
fi

mapfile -t ASSETS < <(grep -oE '(src|href)="[^"]+"' index.html \
  | sed -E 's/^(src|href)="//; s/"$//' \
  | grep -vE '^(#|https?://|data:)' \
  | sort -u)

for asset in "${ASSETS[@]}"; do
  asset="${asset#./}"
  dir="$(dirname "$asset")"
  [[ "$dir" != "." ]] && mkdir -p "$dir"
  if ! curl -sLf "https://web.hoptodesk.com/$asset" -o "$asset"; then
    log "  skip (404?) $asset"
    continue
  fi
  sudo mkdir -p "$WEB_ROOT/$(dirname "$asset")"
  sudo cp "$asset" "$WEB_ROOT/$asset"
done

# Manifest + its icons
curl -sLf https://web.hoptodesk.com/manifest.json -o manifest.json
sudo cp manifest.json "$WEB_ROOT/manifest.json"
while read -r icon; do
  [[ -z "$icon" ]] && continue
  dir="$(dirname "$icon")"
  [[ "$dir" != "." ]] && mkdir -p "$dir"
  curl -sLf "https://web.hoptodesk.com/$icon" -o "$icon" \
    && sudo cp "$icon" "$WEB_ROOT/$icon" || true
done < <(grep -oE '"src": ?"[^"]+"' manifest.json \
  | sed -E 's/"src": ?"([^"]+)"/\1/' | sort -u)

# Patch the JS bundle
JS_BUNDLE=$(find "$WEB_ROOT" -path "*/js/dist/index.*.js" -not -name "*.orig" | head -1)
if [[ -z "$JS_BUNDLE" ]]; then
  echo "ERROR: could not find web client JS bundle under $WEB_ROOT" >&2
  exit 1
fi
log "patching JS bundle: $JS_BUNDLE"
sudo /usr/bin/python3 "$SCRIPT_DIR/patch_web_client.py" "$SIGNAL_HOST" "$WEB_HOST" "$JS_BUNDLE"

sudo chown -R caddy:caddy "$WEB_ROOT"

# ---------- 5. Get cert for ${WEB_HOST} ----------
HOP_CERT_DIR="/etc/letsencrypt/live/${SIGNAL_HOST}"
WEB_CERT_DIR="/etc/letsencrypt/live/${WEB_HOST}"

if [[ ! -s "${WEB_CERT_DIR}/fullchain.pem" ]]; then
  log "obtaining LE cert for ${WEB_HOST} via certbot standalone"
  if ! command -v certbot >/dev/null 2>&1; then
    sudo DEBIAN_FRONTEND=noninteractive apt-get install -y certbot \
      || { echo "certbot install failed" >&2; exit 1; }
  fi
  sudo systemctl stop caddy 2>/dev/null || true
  sleep 1
  sudo certbot certonly --standalone \
    -d "${WEB_HOST}" \
    --agree-tos \
    -m "${LE_EMAIL}" \
    --no-eff-email
fi
# certbot creates 0600 keys; Caddy needs to read them. Either chmod
# or, better, make Caddy part of the ssl-cert group.
sudo chmod 644 "${WEB_CERT_DIR}/fullchain.pem" "${WEB_CERT_DIR}/privkey.pem"
sudo chown caddy:caddy "${WEB_CERT_DIR}/fullchain.pem" "${WEB_CERT_DIR}/privkey.pem"
# Same for the signal cert, in case it changed
sudo chmod 644 "${HOP_CERT_DIR}/fullchain.pem" "${HOP_CERT_DIR}/privkey.pem"
sudo chown caddy:caddy "${HOP_CERT_DIR}/fullchain.pem" "${HOP_CERT_DIR}/privkey.pem"

# ---------- 6. Write Caddyfile ----------
log "writing new Caddyfile"
sudo tee /etc/caddy/Caddyfile >/dev/null <<EOF
{
    admin localhost:2019
    http_port 80
    https_port 443
}

${SIGNAL_HOST}:443 {
    tls ${HOP_CERT_DIR}/fullchain.pem ${HOP_CERT_DIR}/privkey.pem {
        protocols tls1.2
    }
    reverse_proxy 127.0.0.1:9000
}

# Plain-HTTP vhost on :80. The Mac native client uses ws://
# (rendezvous on port 80) because native-tls on macOS chokes on the
# Let's Encrypt YR2 chain. We MUST serve WS on 80 directly (no
# 308->HTTPS redirect), otherwise the Mac gets stuck "Connecting to
# the HopToDesk network...". This vhost reverse-proxies to HopSignal.
${SIGNAL_HOST}:80 {
    reverse_proxy 127.0.0.1:9000
}

# Web client on :80 — only used for ACME HTTP-01 challenges.
# Just serve the static root so the host exists on :80 too.
${WEB_HOST}:80 {
    root * ${WEB_ROOT}
    file_server
}

${WEB_HOST}:443 {
    tls ${WEB_CERT_DIR}/fullchain.pem ${WEB_CERT_DIR}/privkey.pem {
        protocols tls1.2
    }
    root * ${WEB_ROOT}
    route {
        @relay path /relay*
        reverse_proxy @relay 127.0.0.1:${RELAY_WS_PORT} {
            # The bridge uses websockets lib which is HTTP/1.1-only.
            transport http {
                versions 1.1
            }
        }
        file_server
    }
}
EOF

# ---------- 7. Restart Caddy ----------
log "(re)starting caddy"
sudo systemctl start caddy
sleep 2
sudo systemctl --no-pager status caddy | head -5 || true

# ---------- 8. Verify ----------
log "verifying endpoints"
echo
echo "Static page (file_server):"
curl -s -o /dev/null -w "  https://${WEB_HOST}/ -> HTTP %{http_code}\n" \
  -m 5 "https://${WEB_HOST}/"
echo
echo "Relay WS endpoint (no upgrade headers -> expect 426):"
curl -s -o /dev/null -w "  https://${WEB_HOST}/relay/ -> HTTP %{http_code}\n" \
  -m 5 "https://${WEB_HOST}/relay/"
echo
echo "Mac-side TCP port (should be open in the VPS firewall):"
nc -z -v -w2 "${RELAY_PUBLIC_IP}" "${RELAY_TCP_PORT}" 2>&1 | sed 's/^/  /' || true
echo
log "done. open https://${WEB_HOST}/#id=<DEVICE_ID> from the corp PC"
