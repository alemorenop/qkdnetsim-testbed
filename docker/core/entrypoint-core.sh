#!/usr/bin/env bash
set -euo pipefail

if [ ! -S /var/run/docker.sock ]; then
    echo "[core] /var/run/docker.sock is not mounted" >&2
    exit 1
fi

if ! docker version --format '{{.Server.Version}}' >/dev/null 2>&1; then
    echo "[core] the Docker Engine is not reachable through docker.sock" >&2
    exit 1
fi

if [ ! -r /proc/1/ns/net ]; then
    echo "[core] the host PID namespace is not visible" >&2
    exit 1
fi

echo "[core] CORE $(dpkg-query -W -f='${Version}' core)"
echo "[core] EMANE $(emane --version 2>&1 | head -n 1)"
echo "[core] OSPF-MDR $(command -v ospfd)"
echo "[core] Docker Engine $(docker version --format '{{.Server.Version}}')"
echo "[core] starting core-daemon"

exec "$@"
