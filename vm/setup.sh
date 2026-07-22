#!/usr/bin/env bash
# ============================================================================
# Decathlon slot watcher — always-on VM setup (Oracle Cloud Always Free, Ubuntu)
#
# Usage (run on the VM):
#   curl -fsSL https://raw.githubusercontent.com/Ahmed2286/decathlon-slot-watcher/main/vm/setup.sh \
#     | sudo bash -s -- <NTFY_TOPIC> [CMD_TOPIC]
#
#   NTFY_TOPIC  your alert topic (phone pushes), e.g. dcth-rdv-ahmed-k39fq7z2
#   CMD_TOPIC   optional: a second private topic; posting any message to it
#               triggers an immediate on-demand check (phone button, no token)
#
# What it installs:
#   * /opt/decathlon-watch: venv + Playwright Chromium + the watcher script
#   * cron: slot check every 2 minutes (flock so runs never overlap,
#           110s hard timeout so a hung run can't pile up)
#   * cron: daily heartbeat at 07:00 UTC (~09:00 Paris)
#   * systemd service: listens on CMD_TOPIC and runs an on-demand check
#   * 2G swap if the VM has <2G RAM (E2.1.Micro), logrotate for the log
#
# Safe to re-run: it just reinstalls/overwrites its own pieces.
# ============================================================================
set -euo pipefail

NTFY_TOPIC="${1:?Usage: setup.sh NTFY_TOPIC [CMD_TOPIC]}"
CMD_TOPIC="${2:-}"

APP_DIR=/opt/decathlon-watch
RUN_USER="${SUDO_USER:-ubuntu}"
RAW_BASE="https://raw.githubusercontent.com/Ahmed2286/decathlon-slot-watcher/main"

echo "==> [1/7] System packages..."
export DEBIAN_FRONTEND=noninteractive
apt-get update -y -qq
apt-get install -y -qq python3-venv curl >/dev/null

TOTAL_MB=$(awk '/MemTotal/{print int($2/1024)}' /proc/meminfo)
if [ "$TOTAL_MB" -lt 2048 ] && [ ! -f /swapfile ]; then
  echo "==> [2/7] Low RAM (${TOTAL_MB}MB): adding 2G swap..."
  fallocate -l 2G /swapfile
  chmod 600 /swapfile
  mkswap /swapfile >/dev/null
  swapon /swapfile
  echo '/swapfile none swap sw 0 0' >> /etc/fstab
else
  echo "==> [2/7] RAM ${TOTAL_MB}MB: no swap needed."
fi

echo "==> [3/7] Python venv + Playwright Chromium (takes a few minutes)..."
mkdir -p "$APP_DIR/state"
python3 -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/pip" -q install --upgrade pip playwright
# Fixed browser path so cron (non-root) finds the browser installed here (root)
export PLAYWRIGHT_BROWSERS_PATH="$APP_DIR/pw-browsers"
"$APP_DIR/venv/bin/python" -m playwright install --with-deps chromium >/dev/null

echo "==> [4/7] Watcher script + config..."
curl -fsSL "$RAW_BASE/decathlon_watch.py" -o "$APP_DIR/decathlon_watch.py"

cat > "$APP_DIR/env" <<EOF
NTFY_TOPIC=$NTFY_TOPIC
TARGET_BEFORE=2026-09-07
STATE_FILE=$APP_DIR/state/last_earliest.txt
PLAYWRIGHT_BROWSERS_PATH=$APP_DIR/pw-browsers
EOF

cat > "$APP_DIR/run.sh" <<'EOF'
#!/usr/bin/env bash
# One check. flock = no overlapping runs; timeout = hung Chromium can't pile up.
# Arg "wait" (on-demand) waits up to 4 min for the lock instead of skipping.
set -a; source /opt/decathlon-watch/env; set +a
FLOCK_OPTS="-n"
[ "${1:-}" = "wait" ] && FLOCK_OPTS="-w 240"
exec /usr/bin/flock $FLOCK_OPTS /opt/decathlon-watch/.lock \
  /usr/bin/timeout 110 /opt/decathlon-watch/venv/bin/python /opt/decathlon-watch/decathlon_watch.py
EOF
chmod +x "$APP_DIR/run.sh"

echo "==> [5/7] Cron (every 2 min + daily heartbeat 07:00 UTC ~ 09:00 Paris)..."
cat > /etc/cron.d/decathlon-watch <<EOF
SHELL=/bin/bash
*/2 * * * * $RUN_USER /opt/decathlon-watch/run.sh >> /opt/decathlon-watch/watch.log 2>&1
0 7 * * * $RUN_USER MODE=heartbeat /opt/decathlon-watch/run.sh wait >> /opt/decathlon-watch/watch.log 2>&1
EOF

cat > /etc/logrotate.d/decathlon-watch <<EOF
/opt/decathlon-watch/watch.log {
  size 1M
  rotate 3
  missingok
  notifempty
  copytruncate
}
EOF

if [ -n "$CMD_TOPIC" ]; then
  echo "==> [6/7] On-demand listener (topic: $CMD_TOPIC)..."
  cat > "$APP_DIR/listen.sh" <<EOF
#!/usr/bin/env bash
# Streams the private command topic; any message = run an on-demand check now.
while true; do
  curl -sN --max-time 3600 "https://ntfy.sh/$CMD_TOPIC/raw" | while read -r line; do
    [ -n "\$line" ] || continue   # skip keepalive blank lines
    MODE=heartbeat /opt/decathlon-watch/run.sh wait >> /opt/decathlon-watch/watch.log 2>&1
  done
  sleep 2
done
EOF
  chmod +x "$APP_DIR/listen.sh"
  cat > /etc/systemd/system/decathlon-listen.service <<EOF
[Unit]
Description=Decathlon watcher on-demand listener
After=network-online.target
Wants=network-online.target

[Service]
User=$RUN_USER
ExecStart=$APP_DIR/listen.sh
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload
  systemctl enable --now decathlon-listen >/dev/null 2>&1
else
  echo "==> [6/7] No CMD_TOPIC given: skipping on-demand listener."
fi

chown -R "$RUN_USER":"$RUN_USER" "$APP_DIR"

echo "==> [7/7] Test check now (expect a ntfy push in ~1 minute)..."
sudo -u "$RUN_USER" MODE=heartbeat "$APP_DIR/run.sh" wait || true

echo ""
echo "============================================================"
echo " Done. Watching every 2 minutes."
echo "   log:      tail -f $APP_DIR/watch.log"
echo "   config:   $APP_DIR/env"
echo "   update:   re-run this setup one-liner anytime"
echo "============================================================"
