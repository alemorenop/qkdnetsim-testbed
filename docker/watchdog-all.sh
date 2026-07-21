#!/bin/bash
# Watchdog EXTERNO general para los 9 hosts del escenario de key relay.
#
# Ver la nota completa en watchdog-host9.sh: RealtimeSimulatorImpl tiene una
# condicion de carrera de arranque conocida (confirmada con strace) que a
# veces deja el proceso colgado desde el primer instante sin producir salida.
# Un watchdog DENTRO del contenedor que solo reinicia el proceso no basta;
# hay que recrear el CONTENEDOR entero (par veth nuevo) para que tenga una
# oportunidad real de arrancar bien. Este script vigila los 9 contenedores a
# la vez y recrea automaticamente cualquiera que se quede colgado, hasta que
# todos muestren actividad real. Pensado para lanzarse una vez tras el
# despliegue inicial (docker compose up -d) y dejarlo correr en segundo plano.
#
# Uso:
#   ./watchdog-all.sh [timeout_s por intento] [maximo de rondas]
#
# Ejemplo:
#   ./watchdog-all.sh 20 40

set -u

COMPOSE_FILE="$(dirname "$0")/docker-compose.key-relay.yml"
TIMEOUT_S="${1:-20}"
MAX_ROUNDS="${2:-0}" # 0 = sin limite

# service:container:marcador-de-actividad-real
HOSTS=(
    "host1_pp_alice:qkd-relay-host1:Clave entregada"
    "host2_pp_relay_a:qkd-relay-host2:Clave entregada"
    "host3_pp_relay_b:qkd-relay-host3:Clave entregada"
    "host4_pp_bob:qkd-relay-host4:Clave entregada"
    "host5_kms_alice:qkd-relay-host5:almacena clave"
    "host6_kms_relay:qkd-relay-host6:almacena clave"
    "host7_kms_bob:qkd-relay-host7:almacena clave"
    "host8_etsi014_alice:qkd-relay-host8:Peticion GET_KEY"
    "host9_etsi014_bob:qkd-relay-host9:Peticion GET_KEY"
)

is_healthy() {
    local container="$1" marker="$2"
    docker logs "$container" 2>&1 | grep -q "$marker"
}

round=0
while true; do
    round=$((round + 1))
    if [ "$MAX_ROUNDS" -gt 0 ] && [ "$round" -gt "$MAX_ROUNDS" ]; then
        echo "[watchdog-all] limite de rondas (${MAX_ROUNDS}) alcanzado, abandonando" >&2
        exit 1
    fi

    echo "[watchdog-all] ronda ${round}: esperando ${TIMEOUT_S}s antes de comprobar los 9 hosts..."
    sleep "$TIMEOUT_S"

    stuck=()
    for entry in "${HOSTS[@]}"; do
        IFS=':' read -r service container marker <<< "$entry"
        if ! is_healthy "$container" "$marker"; then
            stuck+=("$service")
        fi
    done

    if [ "${#stuck[@]}" -eq 0 ]; then
        echo "[watchdog-all] los 9 hosts muestran actividad real -- listo"
        exit 0
    fi

    echo "[watchdog-all] hosts sin actividad (${#stuck[@]}): ${stuck[*]} -- recreando"
    docker compose -f "$COMPOSE_FILE" up -d --force-recreate "${stuck[@]}" >/dev/null 2>&1
done
