#!/bin/bash
# Watchdog EXTERNO (se ejecuta en el host, no dentro del contenedor).
#
# RealtimeSimulatorImpl tiene una condicion de carrera de arranque conocida
# (confirmada con strace: bucle de futex entre 2 hilos, sin avanzar el reloj
# simulado ni hacer NINGUNA syscall de red) que a veces deja el proceso
# colgado desde el primer instante. Un watchdog DENTRO del contenedor que solo
# mata y reinicia el PROCESO ns-3 (mismo contenedor, mismo par veth) no basta:
# en la practica el proceso vuelve a colgarse casi siempre. Lo que si funciona
# de forma fiable es recrear el CONTENEDOR entero (par veth nuevo), que es
# exactamente lo que hace este script.
#
# Uso:
#   ./watchdog-host9.sh <servicio> <contenedor> <marcador-de-exito> [timeout_s] [max_intentos]
#
# Ejemplo:
#   ./watchdog-host9.sh host9_etsi014_bob qkd-relay-host9 "Peticion GET_KEY" 20 60

set -u

COMPOSE_FILE="$(dirname "$0")/docker-compose.key-relay.yml"
SERVICE="${1:?falta nombre del servicio (p.ej. host9_etsi014_bob)}"
CONTAINER="${2:?falta nombre del contenedor (p.ej. qkd-relay-host9)}"
MARKER="${3:?falta cadena que confirme actividad real (p.ej. 'Peticion GET_KEY')}"
TIMEOUT_S="${4:-20}"
MAX_ATTEMPTS="${5:-0}" # 0 = sin limite

attempt=0
while true; do
    attempt=$((attempt + 1))
    if [ "$MAX_ATTEMPTS" -gt 0 ] && [ "$attempt" -gt "$MAX_ATTEMPTS" ]; then
        echo "[watchdog-host9] limite de intentos (${MAX_ATTEMPTS}) alcanzado, abandonando" >&2
        exit 1
    fi

    echo "[watchdog-host9] intento ${attempt}: esperando ${TIMEOUT_S}s a que '${CONTAINER}' muestre actividad real..."
    ok=0
    for _ in $(seq 1 "$TIMEOUT_S"); do
        if docker logs "$CONTAINER" 2>&1 | grep -q "$MARKER"; then
            ok=1
            break
        fi
        sleep 1
    done

    if [ "$ok" -eq 1 ]; then
        echo "[watchdog-host9] '${CONTAINER}' activo tras ${attempt} intento(s) -- listo"
        exit 0
    fi

    echo "[watchdog-host9] sin actividad tras ${TIMEOUT_S}s, recreando contenedor completo..."
    docker compose -f "$COMPOSE_FILE" up -d --force-recreate "$SERVICE" >/dev/null 2>&1
done
