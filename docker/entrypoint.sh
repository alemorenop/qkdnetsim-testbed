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
all_ifaces=()

for iface in $(ls /sys/class/net | grep -v '^lo$'); do
    all_ifaces+=("$iface")
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

# ARRANQUE ESCALONADO (opt-in via STARTUP_JITTER_MAX_MS, 0/sin definir =
# desactivado): mitigacion para la carrera de arranque de RealtimeSimulatorImpl
# descrita en el bloque de WATCHDOG mas abajo. Al lanzar los 9 contenedores a
# la vez, los 9 procesos crean sus hilos de RealtimeSimulatorImpl exactamente
# en el mismo instante real, maximizando la contencion de CPU/scheduler justo
# en la ventana mas sensible a la carrera. Un retraso aleatorio (distinto por
# contenedor, ya que cada bash tiene su propia semilla de $RANDOM) antes de
# arrancar el binario real reduce esa simultaneidad sin tocar ns-3.
if [ -n "${STARTUP_JITTER_MAX_MS:-}" ] && [ "${STARTUP_JITTER_MAX_MS}" -gt 0 ] 2>/dev/null; then
    jitter_ms=$(( RANDOM % STARTUP_JITTER_MAX_MS ))
    jitter_s=$(awk "BEGIN { printf \"%.3f\", ${jitter_ms}/1000 }")
    echo "[entrypoint] arranque escalonado: esperando ${jitter_ms}ms antes de lanzar ns-3"
    sleep "$jitter_s"
fi

# WATCHDOG (opt-in via WATCHDOG_TIMEOUT, en segundos, 0/sin definir = desactivado):
# RealtimeSimulatorImpl tiene una condicion de carrera de arranque conocida
# (confirmada con strace: bucle de futex entre 2 hilos sin avanzar el reloj
# simulado ni realizar NINGUNA syscall de red) que ocasionalmente deja el
# proceso colgado desde el primer instante, sin producir salida jamas. No es
# un bug de nuestro codigo ni de qkdnetsim - es interno al nucleo de ns-3 - y
# tocar realtime-simulator-impl.cc/wall-clock-synchronizer.cc es demasiado
# arriesgado (lo usan TODOS los escenarios en tiempo real). En vez de eso,
# detectamos el sintoma exacto (cero bytes de salida tras WATCHDOG_TIMEOUT
# segundos) y reiniciamos el binario dentro del propio contenedor hasta que
# arranque con normalidad.
if [ -n "${WATCHDOG_TIMEOUT:-}" ] && [ "${WATCHDOG_TIMEOUT}" -gt 0 ] 2>/dev/null; then
    binary="$1"
    shift
    logfile=$(mktemp)
    trap 'rm -f "$logfile"' EXIT

    while true; do
        "$binary" "$@" "${extra_args[@]}" > "$logfile" 2>&1 &
        pid=$!

        alive=1
        for _ in $(seq 1 "$WATCHDOG_TIMEOUT"); do
            if ! kill -0 "$pid" 2>/dev/null; then
                alive=0
                break
            fi
            if [ -s "$logfile" ]; then
                break
            fi
            sleep 1
        done

        if [ "$alive" -eq 1 ] && kill -0 "$pid" 2>/dev/null && [ ! -s "$logfile" ]; then
            echo "[entrypoint] watchdog: sin salida tras ${WATCHDOG_TIMEOUT}s, proceso probablemente colgado (carrera de arranque de RealtimeSimulatorImpl) -- reiniciando" >&2
            kill -9 "$pid" 2>/dev/null || true
            wait "$pid" 2>/dev/null || true
            : > "$logfile"
            # El SIGKILL no le da chance a EmuFdNetDevice de cerrar limpiamente
            # su socket raw (m_stop/join en FdReader, ver unix-fd-reader.cc).
            # Volvemos a poner las interfaces en modo promiscuo y vaciar su IP
            # por si el kernel dejo algo en un estado raro tras el kill -9.
            for iface in "${all_ifaces[@]}"; do
                ip link set "$iface" promisc on 2>/dev/null || true
                ip addr flush dev "$iface" 2>/dev/null || true
            done
            continue
        fi

        tail -n +1 -f --pid="$pid" "$logfile" &
        tailpid=$!
        wait "$pid"
        exitcode=$?
        wait "$tailpid" 2>/dev/null || true
        exit "$exitcode"
    done
else
    exec "$@" "${extra_args[@]}"
fi
