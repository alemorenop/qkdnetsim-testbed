#!/usr/bin/env bash
set -euo pipefail

# CORE creates Docker nodes with this keepalive command and configures their
# interfaces afterwards. Do not start strongSwan until the CORE topology runner
# explicitly invokes this entrypoint with the required environment.
if [ "${1:-}" = "tail" ] && [ "${2:-}" = "-f" ]; then
    exec "$@"
fi

required_vars=(
    VPN_ROLE
    OWN_IP
    PEER_IP
    KMS_IP
    OWN_APP_ID
    PEER_APP_ID
)

for var in "${required_vars[@]}"; do
    if [ -z "${!var:-}" ]; then
        echo "[vpn] missing required environment variable: ${var}" >&2
        exit 1
    fi
done

mkdir -p /run/qkd-vpn /etc/ipsec.d
chmod 0700 /run/qkd-vpn

# Native Linux TCP packets reach ns-3 before Docker's virtual offload
# metadata is finalized.  Disable offloads so EmuFdNetDevice sees valid
# checksums and segmentation.
for iface in $(ls /sys/class/net | grep -v '^lo$'); do
    ethtool -K "$iface" tx off rx off sg off tso off gso off gro off lro off \
        >/dev/null 2>&1 || true
done

exec > >(tee -a /tmp/qkdnetsim.log) 2>&1

cat > /etc/ipsec.conf << EOF
config setup
    uniqueids=yes

conn %default
    keyexchange=ikev2
    authby=psk
    type=transport
    left=${OWN_IP}
    leftid=${OWN_IP}
    right=${PEER_IP}
    rightid=${PEER_IP}
    ike=aes256-sha256-modp2048!
    esp=aes256-sha256!
    mobike=no
    keyingtries=1
    auto=add

include /etc/ipsec.d/*.conf
EOF

: > /etc/ipsec.secrets
chmod 0600 /etc/ipsec.secrets

echo "[vpn] starting strongSwan (role=${VPN_ROLE})"
ipsec start

for _ in $(seq 1 50); do
    if ipsec status >/dev/null 2>&1; then
        exec /opt/qkd-vpn.py
    fi
    sleep 0.2
done

echo "[vpn] strongSwan did not become ready" >&2
exit 1
