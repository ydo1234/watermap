#!/bin/bash
set -euo pipefail
for unit in watermap-pull-backup.service watermap-pull-backup.timer watermap-check.service watermap-check.timer; do
    install -m 0644 "/home/yann/watermap-deployment/$unit" "/etc/systemd/system/$unit"
done
systemctl daemon-reload
systemctl enable --now watermap-pull-backup.timer watermap-check.timer
systemctl start watermap-pull-backup.service watermap-check.service
systemctl list-timers watermap-pull-backup.timer watermap-check.timer --no-pager
