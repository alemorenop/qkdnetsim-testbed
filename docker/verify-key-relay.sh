#!/bin/bash
# Verificacion end-to-end del escenario "key relay" (9 hosts).
# Uso: ./docker/verify-key-relay.sh [duracion_captura_s]
#   (ejecutar desde contrib/qkdnetsim, con el stack ya levantado y estable,
#   ver watchdog-all.sh si acabas de hacer "docker compose up -d")
#
# Genera, ademas del chequeo de logs, ficheros .pcap reales (uno por enlace
# clave de la cadena) en docker/captures/, para poder abrirlos con Wireshark
# de verdad en tu maquina y ver el trafico salto a salto.

set -u
COMPOSE="docker compose -f docker/docker-compose.key-relay.yml"
CAPTURE_S="${1:-10}"
OUTDIR="$(dirname "$0")/captures"
mkdir -p "$OUTDIR"

echo "=== 1) Estado de los 9 contenedores ==="
docker ps --filter "name=qkd-relay" --format "table {{.Names}}\t{{.Status}}"
echo

echo "=== 2) Actividad esperada por host (ultimos 2 min de logs) ==="
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
    count=$($COMPOSE logs --since 2m "$service" 2>/dev/null | grep -cE "$pattern")
    if [ "$count" -gt 0 ]; then
        echo "  [OK]    $service: $count lineas ('$pattern')"
    else
        echo "  [VACIO] $service: 0 lineas ('$pattern')"
    fi
done
echo

echo "=== 3) Confirmacion de que KMS Alice y KMS Bob SIRVEN claves a las apps ETSI014 (no solo relayan/almacenan) ==="
for service in host5_kms_alice host7_kms_bob; do
    count=$($COMPOSE logs --since 2m "$service" 2>/dev/null | grep -c "sirve clave")
    echo "  $service: $count claves servidas en los ultimos 2 min"
done
echo

echo "=== 4) Capturas .pcap reales (${CAPTURE_S}s cada una) -- abrelas con Wireshark ==="

capture() {
    local container="$1" filter="$2" outfile="$3" label="$4"
    echo "  -> $label ($container, filtro: $filter)"
    docker exec "$container" timeout "$CAPTURE_S" tcpdump -i any -w "/tmp/$(basename "$outfile")" "$filter" >/dev/null 2>&1
    docker cp "$container:/tmp/$(basename "$outfile")" "$OUTDIR/$outfile" >/dev/null 2>&1
    if [ -s "$OUTDIR/$outfile" ]; then
        pkts=$(docker exec "$container" sh -c "command -v tcpdump >/dev/null && tcpdump -r /tmp/$(basename "$outfile") 2>/dev/null | wc -l" 2>/dev/null || echo "?")
        echo "     guardado en $OUTDIR/$outfile"
    else
        echo "     [AVISO] no se genero captura (revisa si hubo trafico en la ventana de ${CAPTURE_S}s)"
    fi
}

# NOTA: el trafico APP-KMS (store_key, GET_KEY) usa el puerto 80
# (m_sinkSocket), pero el trafico KMS-KMS (skey_create/transform_keys, el
# relay en si) usa el puerto 8080 (m_sinkSocketKMS, ver PrepareSinkSocket()
# en qkd-key-manager-system-application.cc) -- por eso los enlaces
# KMS<->KMS necesitan "or port 8080" o se pierde justo el trafico que
# se supone que demuestran.
#
# Lanzamos las 4 capturas EN PARALELO para que compartan la misma ventana temporal
( capture qkd-relay-host1 "tcp port 80" "01_host1_pp_a_kms_alice.pcap"   "HOST1 -> HOST5 (entrega de clave en bruto, post-processing -> KMS Alice)" ) &
( capture qkd-relay-host5 "tcp port 80 or tcp port 8080" "02_host5_kms_alice_relay.pcap" "HOST5 -> HOST6 (relay KMS Alice -> KMS Relay)" ) &
( capture qkd-relay-host6 "tcp port 80 or tcp port 8080" "03_host6_kms_relay_bob.pcap"   "HOST6 -> HOST7 (relay KMS Relay -> KMS Bob)" ) &
( capture qkd-relay-host8 "tcp port 8081" "04_host8_alice_bob_app.pcap" "HOST8 -> HOST9 (trafico cifrado ETSI014 Alice <-> Bob, el resultado final)" ) &
wait
echo

echo "=== 5) Muestra en texto del trafico cifrado Alice<->Bob (por si no tienes Wireshark a mano) ==="
docker exec qkd-relay-host8 timeout 8 tcpdump -i any -nn -c 8 'tcp port 8081' 2>/dev/null
echo

echo "Listo. Capturas .pcap en: $OUTDIR/"
echo "Cópialas a tu maquina si estas en remoto (scp/docker cp) y abrelas con:  wireshark $OUTDIR/*.pcap"
