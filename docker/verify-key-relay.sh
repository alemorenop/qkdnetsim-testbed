#!/bin/bash
# Verificacion end-to-end del escenario "key relay" (9 hosts).
# Uso: ./docker/verify-key-relay.sh [duracion_muestreo_s]
#   (ejecutar desde contrib/qkdnetsim, con el stack ya levantado)
# Las muestras de tcpdump solo existen temporalmente dentro de los contenedores;
# el script no crea ni copia capturas al repositorio.

set -uo pipefail
# Git Bash convierte automaticamente /tmp/... a una ruta Windows incluso
# cuando el argumento va dirigido a un proceso dentro del contenedor.
# Docker necesita recibir aqui las rutas Linux sin modificar.
export MSYS_NO_PATHCONV=1
COMPOSE="docker compose -f docker/docker-compose.key-relay.yml"
CAPTURE_S="${1:-10}"
failures=0

log_count() {
    local service="$1" pattern="$2" container_id
    container_id=$($COMPOSE ps -q "$service")
    docker exec "$container_id" grep -cE "$pattern" /tmp/qkdnetsim.log 2>/dev/null || true
}

echo "=== 1) Estado de los 9 contenedores ==="
docker ps --filter "name=qkd-relay" --format "table {{.Names}}\t{{.Status}}"
echo

for service in $($COMPOSE config --services); do
    container_id=$($COMPOSE ps -q "$service")
    if [ -z "$container_id" ] || [ "$(docker inspect -f '{{.State.Running}}' "$container_id" 2>/dev/null)" != "true" ]; then
        echo "  [FALLO] $service no esta ejecutandose"
        failures=$((failures + 1))
        continue
    fi
    restarts=$(docker inspect -f '{{.RestartCount}}' "$container_id")
    if [ "$restarts" -ne 0 ]; then
        echo "  [FALLO] $service se ha reiniciado $restarts veces"
        failures=$((failures + 1))
    fi
done
echo

echo "=== 2) Actividad esperada por host (logs del arranque actual) ==="
declare -A PATTERNS=(
    [host1_pp_alice]="Clave entregada a KMS Alice"
    [host2_pp_relay_a]="Clave entregada a KMS Relay"
    [host3_pp_relay_b]="Clave entregada a KMS Relay"
    [host4_pp_bob]="Clave entregada a KMS Bob"
    [host5_kms_alice]="almacena clave|sirve clave"
    [host6_kms_relay]="almacena clave"
    [host7_kms_bob]="almacena clave|sirve clave"
    [host8_etsi014_alice]="Peticion GET_KEY"
    [host9_etsi014_bob]="Peticion GET_KEY"
)

for service in "${!PATTERNS[@]}"; do
    pattern="${PATTERNS[$service]}"
    count=$(log_count "$service" "$pattern")
    if [ "$count" -gt 0 ]; then
        echo "  [OK]    $service: $count lineas ('$pattern')"
    else
        echo "  [VACIO] $service: 0 lineas ('$pattern')"
        failures=$((failures + 1))
    fi
done
echo

echo "=== 3) Confirmacion de que KMS Alice y KMS Bob SIRVEN claves a las apps ETSI014 (no solo relayan/almacenan) ==="
for service in host5_kms_alice host7_kms_bob; do
    count=$(log_count "$service" "sirve clave")
    echo "  $service: $count claves servidas desde el arranque"
    if [ "$count" -eq 0 ]; then
        failures=$((failures + 1))
    fi
done

alice_ids=$(docker exec qkd-relay-host5 sed -n 's/.*sirve clave.*keyId=\([^ ]*\).*/\1/p' /tmp/qkdnetsim.log | sort -u)
bob_ids=$(docker exec qkd-relay-host7 sed -n 's/.*sirve clave.*keyId=\([^ ]*\).*/\1/p' /tmp/qkdnetsim.log | sort -u)
common_ids=$(comm -12 <(printf '%s\n' "$alice_ids") <(printf '%s\n' "$bob_ids") | sed '/^$/d' | wc -l)
if [ "$common_ids" -gt 0 ]; then
    echo "  [OK] HOST5 y HOST7 han servido $common_ids keyId(s) identicos"
else
    echo "  [FALLO] HOST5 y HOST7 no han servido ningun keyId comun"
    failures=$((failures + 1))
fi
echo

echo "=== 4) Muestreo temporal de trafico (${CAPTURE_S}s) ==="

capture() {
    local container="$1" filter="$2" outfile="$3" label="$4"
    echo "  -> $label ($container, filtro: $filter)"
    docker exec "$container" timeout "$CAPTURE_S" tcpdump -U -i any -w "/tmp/$outfile" "$filter" >/dev/null 2>&1
    pkts=$(docker exec "$container" tcpdump -nn -r "/tmp/$outfile" 2>/dev/null | wc -l)
    if [ "$pkts" -gt 0 ]; then
        echo "     [OK] $pkts paquetes observados"
        return 0
    else
        echo "     [FALLO] no hubo trafico durante la ventana de ${CAPTURE_S}s"
        return 1
    fi
}

# NOTA: el trafico APP-KMS (store_key, GET_KEY) usa el puerto 80
# (m_sinkSocket), pero el trafico KMS-KMS (skey_create/transform_keys, el
# relay en si) usa el puerto 8080 (m_sinkSocketKMS, ver PrepareSinkSocket()
# en qkd-key-manager-system-application.cc) -- por eso los enlaces
# KMS<->KMS necesitan "or port 8080" o se pierde justo el trafico que
# se supone que demuestran.
#
# Lanzamos las 4 muestras EN PARALELO para que compartan la misma ventana temporal.
capture_pids=()
capture qkd-relay-host1 "tcp port 80" "01_host1_pp_a_kms_alice.pcap"   "HOST1 -> HOST5 (entrega de clave en bruto, post-processing -> KMS Alice)" & capture_pids+=("$!")
capture qkd-relay-host5 "tcp port 80 or tcp port 8080" "02_host5_kms_alice_relay.pcap" "HOST5 -> HOST6 (relay KMS Alice -> KMS Relay)" & capture_pids+=("$!")
capture qkd-relay-host6 "tcp port 80 or tcp port 8080" "03_host6_kms_relay_bob.pcap"   "HOST6 -> HOST7 (relay KMS Relay -> KMS Bob)" & capture_pids+=("$!")
capture qkd-relay-host8 "tcp port 8081" "04_host8_alice_bob_app.pcap" "HOST8 -> HOST9 (trafico cifrado ETSI014 Alice <-> Bob, el resultado final)" & capture_pids+=("$!")
for pid in "${capture_pids[@]}"; do
    if ! wait "$pid"; then
        failures=$((failures + 1))
    fi
done
echo

relay_payload=$(docker exec qkd-relay-host5 tcpdump -nn -A -r /tmp/02_host5_kms_alice_relay.pcap 2>/dev/null)
if ! grep -q 'skey_create' <<<"$relay_payload"; then
    echo "  [INFO] no hubo una nueva negociacion SKEY_CREATE durante esta ventana"
    echo "         (el keyId comun servido por ambos KMS ya confirma el relay)"
elif grep -qE '"ekey":"[^"]+' <<<"$relay_payload"; then
    echo "  [OK] SKEY_CREATE observado con ekey no vacio"
else
    echo "  [FALLO] SKEY_CREATE observado con ekey vacio en HOST5 -> HOST6"
    failures=$((failures + 1))
fi

app_packets=$(docker exec qkd-relay-host8 tcpdump -nn -r /tmp/04_host8_alice_bob_app.pcap 2>/dev/null | wc -l)
if [ "$app_packets" -le 3 ]; then
    echo "  [INFO] muestra paralela de aplicacion vacia; reintentando 6s en secuencial"
    docker exec qkd-relay-host8 timeout 6 tcpdump -U -i any \
        -w /tmp/04_host8_alice_bob_app.pcap 'tcp port 8081' >/dev/null 2>&1
    app_packets=$(docker exec qkd-relay-host8 tcpdump -nn \
        -r /tmp/04_host8_alice_bob_app.pcap 2>/dev/null | wc -l)
fi
if [ "$app_packets" -gt 3 ]; then
    echo "  [OK] trafico de aplicacion cifrado: $app_packets paquetes capturados"
else
    echo "  [FALLO] trafico de aplicacion insuficiente: $app_packets paquetes"
    failures=$((failures + 1))
fi
echo

echo "=== 5) Muestra en texto del trafico cifrado Alice<->Bob ==="
docker exec qkd-relay-host8 timeout 8 tcpdump -i any -nn -c 8 'tcp port 8081' 2>/dev/null
echo

if [ "$failures" -ne 0 ]; then
    echo "RESULTADO: FALLO ($failures comprobaciones)" >&2
    exit 1
fi
echo "RESULTADO: OK"
