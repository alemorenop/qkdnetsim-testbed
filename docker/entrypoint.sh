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

exec "$@" "${extra_args[@]}"
