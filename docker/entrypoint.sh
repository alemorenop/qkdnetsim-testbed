#!/bin/bash
set -e

# Docker no garantiza que el orden en que se conectan las redes de un
# servicio se traduzca siempre en el mismo eth0/eth1/eth2 dentro del
# contenedor entre un "up" y otro. Para no depender de ese orden,
# detectamos aqui que interfaz real corresponde a que rol MIRANDO LA IP
# que Docker le asigno (antes de vaciarla) y comparandola contra las
# variables de entorno DEV_MAP_<ROL>=<prefijo /24>, p.ej.:
#   DEV_MAP_PP=192.168.13
#   DEV_MAP_KMS=192.168.34
#   DEV_MAP_ETSI=192.168.35
# Con eso construimos los --devPP=ethX / --devKms=ethY / --devEtsi=ethZ
# correctos y se los anadimos al comando antes de arrancar ns-3.

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

    # Modo promiscuo + vaciado de IP, igual que antes (independiente del
    # descubrimiento de arriba, que ya leyo la IP que necesitaba)
    ip link set "$iface" promisc on
    ip addr flush dev "$iface"
done

# Duplicamos toda la salida (de aqui en adelante) a un fichero fijo dentro
# del contenedor, ademas del stdout normal. Necesario para el HEALTHCHECK de
# docker-compose.key-relay.yml: un HEALTHCHECK se ejecuta DENTRO del propio
# contenedor y no tiene acceso a "docker logs" (eso es una vista del lado del
# host sobre el log driver), asi que necesita un fichero local que pueda
# grepear para saber si este nodo ya imprimio su marcador de "listo para
# aceptar conexiones" (ver traza ListenReady/AppListenReady en el modelo).
exec > >(tee -a /tmp/qkdnetsim.log) 2>&1

# EmuFdNetDevice opens the Docker veth immediately after the kernel addresses
# above are flushed. Give that interface state a short, deterministic settling
# period when requested by the scenario. Unlike the old random jitter, this
# does not change the dependency order between hosts.
if [ -n "${NETWORK_SETTLE_MS:-}" ] && [ "${NETWORK_SETTLE_MS}" -gt 0 ] 2>/dev/null; then
    settle_s=$(awk "BEGIN { printf \"%.3f\", ${NETWORK_SETTLE_MS}/1000 }")
    echo "[entrypoint] esperando ${NETWORK_SETTLE_MS}ms para estabilizar interfaces"
    sleep "$settle_s"
fi

exec "$@" "${extra_args[@]}"
