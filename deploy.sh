#!/usr/bin/env bash
set -euo pipefail

SRC="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
DEST=/opt/plainapp-orb
SVC=plainapp
WAIT=20   # seconds service must stay up before enabling

fail() {
  echo
  echo "DEPLOY FAILED: $1 (NOT enabled)"
  systemctl status "$SVC" --no-pager || true
  sudo journalctl -u "$SVC" -n 30 --no-pager || true
  sudo systemctl stop "$SVC" || true
  exit 1
}

# copy project to /opt
sudo systemctl stop "$SVC" || true
sudo rm -f "$DEST"/plainwatch.db-wal "$DEST"/plainwatch.db-shm
sudo mkdir -p "$DEST"
sudo cp -r "$SRC"/. "$DEST"/
sudo chown -R opc:opc "$DEST"
sudo restorecon -RF "$DEST"

# fresh venv + packages
rm -rf "$DEST/.venv"
python3 -m venv "$DEST/.venv"
"$DEST/.venv/bin/pip" install -r "$DEST/requirements.txt"

# install unit, reload, start
sudo cp "$DEST/plainapp.service" /etc/systemd/system/plainapp.service
sudo restorecon -F /etc/systemd/system/plainapp.service
sudo systemctl daemon-reload
sudo systemctl restart "$SVC" || fail "service failed to start"

echo
echo "=== Status after start ==="
systemctl status "$SVC" --no-pager || true

# wait: must stay active for WAIT seconds
echo
echo "Waiting ${WAIT}s to confirm service stays up..."
for ((i = 1; i <= WAIT; i++)); do
  sleep 1
  systemctl is-active --quiet "$SVC" || fail "service stopped after ${i}s"
done
[[ "$(systemctl show -p NRestarts --value "$SVC")" == "0" ]] || fail "service restarted during wait"

# stable: enable at boot
sudo systemctl enable "$SVC"

echo
echo "=== Final status ==="
systemctl status "$SVC" --no-pager || true
echo
echo "ENABLED: $SVC is running and will start at boot"