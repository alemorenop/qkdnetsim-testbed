#!/bin/bash
# Verificacion end-to-end del escenario point-to-point de 6 hosts.
# Uso: ./docker/verify.sh [duracion_captura_s]

set -uo pipefail
export MSYS_NO_PATHCONV=1

COMPOSE="docker compose -f docker/docker-compose.yml"
CAPTURE_S="${1:-6}"
failures=0

log_count() {
    local service="$1" pattern="$2" container_id
    container_id=$($COMPOSE ps -q "$service")
    docker exec "$container_id" grep -cE "$pattern" /tmp/qkdnetsim.log 2>/dev/null || true
}

echo "=== 1) Estado de los 6 contenedores ==="
docker ps --filter "name=qkd-host" --format "table {{.Names}}\t{{.Status}}"
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

echo "=== 2) Actividad esperada por host ==="
declare -A PATTERNS=(
    [host1_pp_alice]="Clave entregada a KMS Alice"
    [host2_pp_bob]="Clave entregada a KMS Bob"
    [host3_kms_alice]="almacena clave|sirve clave"
    [host4_kms_bob]="almacena clave|sirve clave"
    [host5_etsi014_alice]="Peticion GET_KEY a KMS Alice"
    [host6_etsi014_bob]="Peticion GET_KEY a KMS Bob"
)

for service in "${!PATTERNS[@]}"; do
    count=$(log_count "$service" "${PATTERNS[$service]}")
    count=${count:-0}
    if [ "$count" -gt 0 ]; then
        echo "  [OK]    $service: $count lineas"
    else
        echo "  [FALLO] $service: sin actividad esperada"
        failures=$((failures + 1))
    fi
done
echo

echo "=== 3) Misma clave servida en Alice y Bob ==="
alice_ids=$(docker exec qkd-host3 sed -n 's/.*sirve clave.*keyId=\([^ ]*\).*/\1/p' /tmp/qkdnetsim.log | sort -u)
bob_ids=$(docker exec qkd-host4 sed -n 's/.*sirve clave.*keyId=\([^ ]*\).*/\1/p' /tmp/qkdnetsim.log | sort -u)
common_ids=$(comm -12 <(printf '%s\n' "$alice_ids") <(printf '%s\n' "$bob_ids") | sed '/^$/d')
if [ -n "$common_ids" ]; then
    echo "  [OK] keyId comun: $(printf '%s' "$common_ids" | tr '\n' ' ')"
else
    echo "  [FALLO] los KMS no han servido ningun keyId comun"
    failures=$((failures + 1))
fi
echo

echo "=== 4) Trafico cifrado Alice-Bob (${CAPTURE_S}s) ==="
docker exec qkd-host5 timeout "$CAPTURE_S" tcpdump -U -i any -s 0 \
    -w /tmp/p2p-verify-app.pcap 'tcp port 8081' >/dev/null 2>&1

app_packets=$(docker exec qkd-host5 tcpdump -nn -r /tmp/p2p-verify-app.pcap 2>/dev/null | wc -l)
wire_text=$(docker exec qkd-host5 tcpdump -nn -A -s 0 -r /tmp/p2p-verify-app.pcap 2>/dev/null)
if [ "$app_packets" -gt 3 ]; then
    echo "  [OK] $app_packets paquetes de aplicacion capturados"
else
    echo "  [FALLO] trafico insuficiente: $app_packets paquetes"
    failures=$((failures + 1))
fi

# Sin UseCrypto, la carga generada tras "Cookie." es una cadena alfanumerica
# larga y visible. Con OTP activo aparecen bytes no imprimibles casi de inmediato.
if grep -qE 'Cookie\.[[:alnum:]]{100}' <<<"$wire_text"; then
    echo "  [FALLO] el cuerpo de aplicacion conserva texto en claro"
    failures=$((failures + 1))
elif grep -q 'Cookie\.' <<<"$wire_text"; then
    echo "  [OK] encabezado identificable y cuerpo binario cifrado"
else
    echo "  [FALLO] no se encontro un mensaje de aplicacion completo"
    failures=$((failures + 1))
fi
echo

if [ "$failures" -ne 0 ]; then
    echo "RESULTADO: FALLO ($failures comprobaciones)" >&2
    exit 1
fi
echo "RESULTADO: OK"
