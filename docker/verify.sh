#!/bin/bash
# Verificacion end-to-end del escenario de 6 hosts.
# Uso: ./docker/verify.sh   (ejecutar desde contrib/qkdnetsim, con el stack ya levantado)

set -u
COMPOSE="docker compose -f docker/docker-compose.yml"

echo "=== 1) Estado de los contenedores ==="
docker ps --filter "name=qkd-host" --format "table {{.Names}}\t{{.Status}}"
echo

echo "=== 2) Actividad esperada por host (ultimos 2 min de logs) ==="
declare -A PATTERNS=(
    [host1_pp_alice]="Clave entregada a KMS Alice"
    [host2_pp_bob]="Clave entregada a KMS Bob"
    [host3_kms_alice]="almacena clave|sirve clave"
    [host4_kms_bob]="almacena clave|sirve clave"
    [host5_etsi014_alice]="Peticion GET_KEY a KMS Alice"
    [host6_etsi014_bob]="Peticion GET_KEY a KMS Bob"
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

echo "=== 3) Confirmacion de que KMS Alice y KMS Bob SIRVEN claves (no solo almacenan) ==="
for service in host3_kms_alice host4_kms_bob; do
    count=$($COMPOSE logs --since 2m "$service" 2>/dev/null | grep -c "sirve clave")
    echo "  $service: $count claves servidas en los ultimos 2 min"
done
echo

echo "=== 4) Muestra de trafico HTTP real (10s) en HOST3 (nuestros scripts usan el puerto 80 para todo: STORE_KEY, GET_KEY y transform_keys) ==="
docker exec qkd-host3 timeout 10 tcpdump -i any -A -s 0 'tcp port 80' -c 6 2>/dev/null
