#!/usr/bin/env bash
# One-time (and repeatable) server setup for the AABA trading agent on Ubuntu 24.04.
#   sudo bash deploy/bootstrap.sh            # from a clone of the repository, or:
#   curl -fsSL https://raw.githubusercontent.com/<owner>/<repo>/main/deploy/bootstrap.sh | sudo bash
# It installs packages, Python 3.11 (via uv), the code in /opt/aaba, two service users and the
# systemd units. It does NOT write secrets and does NOT start the scheduler: see deploy/README.md.
set -euo pipefail

REPO_URL="${AABA_REPO_URL:-https://github.com/Arunima-Dutta-fsm/Trading-Agent.git}"
BRANCH="${AABA_BRANCH:-main}"
APP=/opt/aaba
STATE=/var/lib/aaba
LOGS=/var/log/aaba
ETC=/etc/aaba

[ "$(id -u)" -eq 0 ] || { echo "run with sudo"; exit 1; }
export DEBIAN_FRONTEND=noninteractive

echo "== packages"
apt-get update -qq
apt-get install -y -qq git sqlite3 curl ca-certificates unattended-upgrades >/dev/null
systemctl enable --now unattended-upgrades >/dev/null 2>&1 || true

echo "== swap (only on small machines)"
if [ "$(awk '/MemTotal/{print int($2/1024)}' /proc/meminfo)" -lt 2500 ] && ! swapon --show | grep -q /swapfile; then
  fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile >/dev/null && swapon /swapfile
  grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

echo "== users and folders"
getent group aabadata >/dev/null || groupadd --system aabadata
id aaba >/dev/null 2>&1 || useradd --system --create-home --home-dir /home/aaba --shell /usr/sbin/nologin aaba
id aabaweb >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin aabaweb
usermod -aG aabadata aaba
usermod -aG aabadata aabaweb
install -d -o aaba -g aabadata -m 2775 "$STATE" "$STATE/data" "$STATE/model-run" "$STATE/reports" "$LOGS"
install -d -o aaba -g aaba -m 2770 "$STATE/backups"            # backups: trading user only
install -d -o aabaweb -g aabadata -m 0750 "$STATE/web"          # dashboard user's home (Streamlit files)
install -d -o root -g aaba -m 750 "$ETC"

echo "== code"
if [ -d "$APP/.git" ]; then
  sudo -u aaba git -C "$APP" fetch -q origin "$BRANCH"
  sudo -u aaba git -C "$APP" checkout -q "$BRANCH"
  sudo -u aaba git -C "$APP" merge -q --ff-only "origin/$BRANCH"
else
  install -d -o aaba -g aaba -m 755 "$APP"
  sudo -u aaba git clone -q --branch "$BRANCH" "$REPO_URL" "$APP"
fi
chmod -R o+rX "$APP"                          # the dashboard user reads the code

echo "== python 3.11 + dependencies (uv)"
if ! command -v uv >/dev/null; then
  curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin UV_NO_MODIFY_PATH=1 sh >/dev/null
fi
export UV_PYTHON_INSTALL_DIR=/opt/uv-python
uv python install 3.11 >/dev/null
[ -x "$APP/.venv/bin/python" ] || sudo -u aaba env UV_PYTHON_INSTALL_DIR=$UV_PYTHON_INSTALL_DIR uv venv -q --python 3.11 "$APP/.venv"
sudo -u aaba env UV_PYTHON_INSTALL_DIR=$UV_PYTHON_INSTALL_DIR UV_CACHE_DIR=/home/aaba/.cache/uv \
  uv pip install -q --python "$APP/.venv/bin/python" -r "$APP/requirements.txt"
chmod -R o+rX /opt/uv-python "$APP/.venv"

echo "== settings and services"
[ -f "$ETC/engine.env" ] || install -o root -g aaba -m 640 /dev/null "$ETC/engine.env"
install -o root -g root -m 644 "$APP/deploy/dashboard.env" "$ETC/dashboard.env"
install -o root -g root -m 644 "$APP"/deploy/systemd/*.service "$APP"/deploy/systemd/*.timer /etc/systemd/system/
install -o root -g root -m 755 "$APP/deploy/aaba" /usr/local/bin/aaba
systemctl daemon-reload
systemctl enable --now aaba-backup.timer >/dev/null
systemctl enable aaba-scheduler.service aaba-dashboard.service >/dev/null

echo
echo "Done. Python: $("$APP/.venv/bin/python" --version). Next (deploy/README.md):"
echo "  1. put the secrets in $ETC/engine.env  (never in Git)"
echo "  2. sudo aaba check"
echo "  3. sudo systemctl start aaba-dashboard aaba-scheduler"
