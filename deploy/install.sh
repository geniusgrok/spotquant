#!/bin/sh
# Idempotent install for one Ubuntu host. Does not enable live trading.
# Does not touch unrelated services.
set -eu

ROOT=$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)
ENABLE_DEMO=0
if [ "${1:-}" = "--enable-demo" ]; then
  ENABLE_DEMO=1
fi

if ! command -v python3.13 >/dev/null 2>&1; then
  echo "需要 /usr/bin/python3.13。Ubuntu 24.04 可从 deadsnakes 安装，本脚本不会改动其他软件。" >&2
  exit 1
fi

if ! id spotquant >/dev/null 2>&1; then
  useradd --system --create-home --home-dir /var/lib/spotquant --shell /usr/sbin/nologin spotquant
fi

install -d -o root -g spotquant -m 750 /etc/spotquant
install -d -o spotquant -g spotquant -m 750 /var/lib/spotquant/demo /var/lib/spotquant/live
install -d -o spotquant -g spotquant -m 750 /var/backups/spotquant/demo /var/backups/spotquant/live
install -d -o root -g root -m 755 \
  /etc/systemd/system/spotquant-session@live.service.d \
  /etc/systemd/system/spotquant-watch@live.service.d \
  /etc/systemd/system/spotquant-backup@live.service.d
install -m 755 "$ROOT/deploy/sq" /usr/local/bin/sq

if [ "$ROOT" != "/opt/spotquant" ]; then
  install -d -o spotquant -g spotquant -m 755 /opt/spotquant
  tar -C "$ROOT" -cf - --exclude .git --exclude '.git/*' . | tar -C /opt/spotquant -xf -
  chown -R spotquant:spotquant /opt/spotquant
fi

install_unit() {
  install -m 644 "$ROOT/deploy/systemd/$1" "/etc/systemd/system/$1"
}

install_unit spotquant-session@.service
install_unit spotquant-session@.timer
install_unit spotquant-watch@.service
install_unit spotquant-watch@.timer
install_unit spotquant-backup@.service
install_unit spotquant-backup@.timer
install_unit spotquant-failed@.service
for unit in spotquant-session@live.service spotquant-watch@live.service spotquant-backup@live.service; do
  install -m 644 "$ROOT/deploy/systemd/${unit}.d/enable.conf" \
    "/etc/systemd/system/${unit}.d/enable.conf"
done
if SHA=$(git -C "$ROOT" rev-parse HEAD 2>/dev/null); then
  printf '%s\n' "$SHA" > /etc/spotquant/source_sha
  chown root:spotquant /etc/spotquant/source_sha
  chmod 644 /etc/spotquant/source_sha
fi

if [ ! -f /etc/spotquant/demo.json ]; then
  install -m 640 -o root -g spotquant "$ROOT/deploy/demo.json.example" /etc/spotquant/demo.json
fi
if [ ! -f /etc/spotquant/live.json ]; then
  install -m 640 -o root -g spotquant "$ROOT/deploy/live.json.example" /etc/spotquant/live.json
fi
if [ ! -f /etc/spotquant/demo.env ]; then
  install -m 640 -o root -g spotquant "$ROOT/deploy/demo.env.example" /etc/spotquant/demo.env
fi
if [ ! -f /etc/spotquant/live.env ]; then
  install -m 640 -o root -g spotquant "$ROOT/deploy/live.env.example" /etc/spotquant/live.env
fi
if [ ! -f /etc/spotquant/notify.env ]; then
  install -m 640 -o root -g spotquant "$ROOT/deploy/notify.env.example" /etc/spotquant/notify.env
fi

systemctl daemon-reload

if [ "$ENABLE_DEMO" -eq 1 ]; then
  systemctl enable --now spotquant-session@demo.timer spotquant-watch@demo.timer spotquant-backup@demo.timer
fi

echo "安装完成。实盘定时器没有启用。切换步骤见 deploy/README.md。"
