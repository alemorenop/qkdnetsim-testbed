#!/bin/bash
set -e

# Docker does not guarantee that the order in which a service's networks
# are attached translates into the same eth0/eth1/eth2 inside the container
# between one "up" and the next. To avoid depending on that order, we
# detect here which real interface corresponds to which role by LOOKING AT
# THE IP Docker assigned to it (before flushing it) and comparing it
# against the DEV_MAP_<ROLE>=<prefix /24> environment variables, e.g.:
#   DEV_MAP_PP=192.168.13
#   DEV_MAP_KMS=192.168.34
#   DEV_MAP_ETSI=192.168.35
# From that we build the correct --devPP=ethX / --devKms=ethY /
# --devEtsi=ethZ flags and append them to the command before starting ns-3.

declare -A ROLE_TO_FLAG=(
    [PP]=devPP
    [KMS]=devKms
    [ETSI]=devEtsi
    [SIFT]=devSift
    [DATA]=devData
    [PPA]=devPPA
    [PPB]=devPPB
    [KMSA]=devKmsA
    [KMSB]=devKmsB
)

extra_args=()

for iface in $(ls /sys/class/net | grep -v '^lo$'); do
    ip4=$(ip -4 -o addr show dev "$iface" | awk '{print $4}' | cut -d/ -f1)
    if [ -n "$ip4" ]; then
        prefix=$(echo "$ip4" | cut -d. -f1-3)
        for var in $(compgen -v DEV_MAP_); do
            role=${var#DEV_MAP_}
            if [ "${!var}" = "$prefix" ] && [ -n "${ROLE_TO_FLAG[$role]:-}" ]; then
                extra_args+=("--${ROLE_TO_FLAG[$role]}=${iface}")
                echo "[entrypoint] $iface ($ip4) -> --${ROLE_TO_FLAG[$role]}=${iface}"
            fi
        done
    fi

    # Promiscuous mode + IP flush, same as before (independent of the
    # discovery above, which already read the IP it needed)
    ip link set "$iface" promisc on
    ip addr flush dev "$iface"
done

# Mirror all output (from here on) to a fixed file inside the container, in
# addition to normal stdout. Required for the HEALTHCHECK in
# docker-compose.key-relay.yml: a HEALTHCHECK runs INSIDE the container
# itself and has no access to "docker logs" (that's a host-side view over
# the logging driver), so it needs a local file it can grep to know
# whether this node has already printed its "ready to accept connections"
# marker (see the ListenReady/AppListenReady trace in the model).
exec > >(tee -a /tmp/qkdnetsim.log) 2>&1

# EmuFdNetDevice opens the Docker veth immediately after the kernel addresses
# above are flushed. Give that interface state a short, deterministic settling
# period when requested by the scenario. Unlike the old random jitter, this
# does not change the dependency order between hosts.
if [ -n "${NETWORK_SETTLE_MS:-}" ] && [ "${NETWORK_SETTLE_MS}" -gt 0 ] 2>/dev/null; then
    settle_s=$(awk "BEGIN { printf \"%.3f\", ${NETWORK_SETTLE_MS}/1000 }")
    echo "[entrypoint] waiting ${NETWORK_SETTLE_MS}ms to stabilize interfaces"
    sleep "$settle_s"
fi

exec "$@" "${extra_args[@]}"
