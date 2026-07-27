#!/bin/bash
set -e

# A real (non-ns-3) kernel TCP stack marks outgoing segments for checksum
# offload, expecting a NIC to fill in the real checksum before the packet
# leaves. Docker veth pairs have no such NIC, so the ns-3 KMS on the other
# end (ChecksumEnabled=true) sees a bogus checksum and silently drops every
# segment, which looks like a hung TCP handshake from here. Confirmed fix:
# disable offload on this side too (the KMS side already does the same in
# docker/entrypoint.sh).
for iface in $(ls /sys/class/net | grep -v '^lo$'); do
    ethtool -K "$iface" tx off rx off sg off tso off gso off gro off >/dev/null 2>&1 || true
done

# Mirror all output to a fixed file inside the container, same convention
# as docker/entrypoint.sh: HEALTHCHECK runs inside the container's own
# namespace and has no access to "docker logs".
exec > >(tee -a /tmp/qkdnetsim.log) 2>&1

: "${ROLE:?ROLE must be set to client or server}"
: "${OWN_IP:?}"
: "${PEER_IP:?}"

# strongSwan transport-mode connection directly between the two existing
# host IPs -- a drop-in encrypted replacement for the plaintext socket the
# toy ETSI014 app used to open between these same two addresses. No new
# Docker subnets/routes needed.
cat > /etc/ipsec.conf << EOF
config setup

conn %default
    keyexchange=ikev2
    authby=psk
    type=transport
    left=${OWN_IP}
    right=${PEER_IP}
    # Pinned to a known-available suite (aes/sha2/gmp plugins, confirmed
    # loaded on this image) instead of relying on default proposal
    # negotiation, which failed with NO_PROPOSAL_CHOSEN between two
    # instances of this same image (missing curve25519/openssl plugins
    # that Ubuntu's strongswan package doesn't enable by default).
    ike=aes256-sha256-modp2048!
    esp=aes256-sha256!
    auto=add

include /etc/ipsec.d/*.conf
EOF

: > /etc/ipsec.secrets

echo "[entrypoint] starting charon (role=${ROLE})"
ipsec start

# Give charon a moment to come up before the rekey loop starts issuing
# ipsec commands against it.
sleep 2

exec /opt/rekey.sh
