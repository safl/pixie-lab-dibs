#!/bin/sh
# SPDX-FileCopyrightText: Simon A. F. Lund <os@safl.dk>
#
# SPDX-License-Identifier: BSD-3-Clause
#
# Install the built wheel as the real systemd service, then register, log
# in and claim against it. This is the path the unit tests stub out, and
# where dibs once broke: under systemd, ssh-keygen must be able to look its
# own user up. Needs root through sudo and a running systemd; CI's runner
# has both. Run from the repository root after "make build".
set -eu

wheel=$(ls dist/*.whl)
tmp=$(mktemp -d)
trap 'sudo dibs server uninstall >/dev/null 2>&1 || true; rm -rf "$tmp"' EXIT

sudo env "PATH=$PATH" UV_TOOL_DIR=/opt/uv-tools UV_TOOL_BIN_DIR=/usr/local/bin \
    uv tool install --python /usr/bin/python3 "$wheel"

sudo mkdir -p /etc/dibs
printf '[[thing]]\nname = "box"\nmac = "aa:bb:cc:dd:ee:ff"\n' | sudo tee /etc/dibs/dibs.toml >/dev/null
sudo /usr/local/bin/dibs server install --port 8090

export DIBS_URL=http://127.0.0.1:8090 XDG_CONFIG_HOME="$tmp/config"
for _ in $(seq 1 30); do curl -fsS "$DIBS_URL/healthz" >/dev/null 2>&1 && break; sleep 1; done

ssh-keygen -q -t ed25519 -N '' -f "$tmp/key"
dibs register ci -i "$tmp/key.pub"
sudo dibs admin approve ci
dibs login
dibs claim box --for smoke
dibs list | grep -qx "'ci' has claimed 'box' for 'smoke' expires in: 'never'"
dibs release --mine

# Installing again, as an upgrade does, must replace the running process.
before=$(systemctl show -p MainPID --value dibs.service)
sudo /usr/local/bin/dibs server install --port 8090
after=$(systemctl show -p MainPID --value dibs.service)
[ "$before" != "$after" ] || { echo "install did not restart the running service" >&2; exit 1; }
for _ in $(seq 1 30); do curl -fsS "$DIBS_URL/healthz" >/dev/null 2>&1 && break; sleep 1; done
dibs list | grep -qx "'box' is free"
echo "service smoke test passed"
