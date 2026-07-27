#!/bin/bash
# End-to-end verification of the point-to-point + VPN scenario: HOST5/HOST6
# run real strongSwan IPsec/IKEv2 endpoints instead of the toy ETSI014 app,
# rekeyed from real ETSI GS QKD 004 key material fetched from HOST3/HOST4.
# Usage: ./docker/verify-vpn.sh [capture_seconds]

set -uo pipefail
export MSYS_NO_PATHCONV=1

COMPOSE="docker compose -f docker/docker-compose.yml -f docker/docker-compose.vpn.yml"
CAPTURE_S="${1:-8}"
failures=0

log_count() {
    local service="$1" pattern="$2" container_id
    container_id=$($COMPOSE ps -q "$service")
    docker exec "$container_id" grep -cE "$pattern" /tmp/qkdnetsim.log 2>/dev/null || true
}

echo "=== 1) Status of the 6 containers ==="
docker ps --filter "name=qkd-host" --format "table {{.Names}}\t{{.Status}}"
echo

for service in $($COMPOSE config --services); do
    container_id=$($COMPOSE ps -q "$service")
    if [ -z "$container_id" ] || [ "$(docker inspect -f '{{.State.Running}}' "$container_id" 2>/dev/null)" != "true" ]; then
        echo "  [FAIL] $service is not running"
        failures=$((failures + 1))
        continue
    fi
    restarts=$(docker inspect -f '{{.RestartCount}}' "$container_id")
    if [ "$restarts" -ne 0 ]; then
        echo "  [FAIL] $service has restarted $restarts time(s)"
        failures=$((failures + 1))
    fi
done
echo

echo "=== 2) Expected activity per host ==="
declare -A PATTERNS=(
    [host1_pp_alice]="Key delivered to KMS Alice"
    [host2_pp_bob]="Key delivered to KMS Bob"
    [host3_kms_alice]="stores key|serves key"
    [host4_kms_bob]="stores key|serves key"
    [host5_etsi014_alice]="got KSID|ESTABLISHED"
    [host6_etsi014_bob]="received KSID|ESTABLISHED"
)

for service in "${!PATTERNS[@]}"; do
    count=$(log_count "$service" "${PATTERNS[$service]}")
    count=${count:-0}
    if [ "$count" -gt 0 ]; then
        echo "  [OK]    $service: $count lines"
    else
        echo "  [FAIL]  $service: no expected activity"
        failures=$((failures + 1))
    fi
done
echo

echo "=== 3) VPN endpoints established a QKD-keyed IPsec SA ==="
host5_gen=$(docker exec qkd-host5 sed -n 's/.*ESTABLISHED qkd-\([0-9]*\).*/\1/p' /tmp/qkdnetsim.log | tail -1)
host6_gen=$(docker exec qkd-host6 sed -n 's/.*ESTABLISHED qkd-\([0-9]*\).*/\1/p' /tmp/qkdnetsim.log | tail -1)
if [ -z "$host5_gen" ] || [ -z "$host6_gen" ]; then
    echo "  [FAIL] one or both endpoints never logged an ESTABLISHED generation"
    failures=$((failures + 1))
else
    echo "  [OK] host5 generation=$host5_gen, host6 generation=$host6_gen"
fi

if docker exec qkd-host5 ipsec statusall 2>/dev/null | grep -q "Security Associations (1 up"; then
    echo "  [OK] host5: IPsec SA ESTABLISHED"
else
    echo "  [FAIL] host5: no ESTABLISHED IPsec SA"
    failures=$((failures + 1))
fi
if docker exec qkd-host6 ipsec statusall 2>/dev/null | grep -q "Security Associations (1 up"; then
    echo "  [OK] host6: IPsec SA ESTABLISHED"
else
    echo "  [FAIL] host6: no ESTABLISHED IPsec SA"
    failures=$((failures + 1))
fi
echo

echo "=== 4) Real encrypted traffic between HOST5 and HOST6 (${CAPTURE_S}s) ==="
# Same privilege-drop/stale-file gotcha as verify.sh: tcpdump opens the raw
# socket as root and drops privileges before writing -w, so a leftover file
# from a previous run can make the capture fail silently.
docker exec qkd-host6 rm -f /tmp/vpn-verify.pcap
docker exec qkd-host6 timeout -s INT "$CAPTURE_S" tcpdump -i any -s 0 \
    -w /tmp/vpn-verify.pcap 'host 192.168.56.5' >/dev/null 2>&1 &
tcpdump_pid=$!
sleep 1
docker exec qkd-host5 ping -c 3 192.168.56.6 >/dev/null 2>&1
wait "$tcpdump_pid" 2>/dev/null

esp_packets=$(docker exec qkd-host6 tcpdump -nn -r /tmp/vpn-verify.pcap 2>/dev/null | grep -c "ESP(")
total_packets=$(docker exec qkd-host6 tcpdump -nn -r /tmp/vpn-verify.pcap 2>/dev/null | wc -l)
if [ "$esp_packets" -gt 0 ]; then
    echo "  [OK] $esp_packets ESP packet(s) observed ($total_packets total captured)"
else
    echo "  [FAIL] no ESP traffic observed ($total_packets total captured)"
    failures=$((failures + 1))
fi

# Real proof of encryption: an ESP payload with no ICMP header ever visible
# on the wire (unlike a plaintext ping, which would show "ICMP echo
# request/reply" in a plain tcpdump decode).
if [ "$total_packets" -gt 0 ] && [ "$esp_packets" -eq "$total_packets" ]; then
    echo "  [OK] all captured packets are ESP -- no plaintext ICMP visible on the wire"
else
    echo "  [FAIL] some captured packets are not ESP (possible plaintext leak)"
    failures=$((failures + 1))
fi
echo

if [ "$failures" -ne 0 ]; then
    echo "RESULT: FAIL ($failures check(s))" >&2
    exit 1
fi
echo "RESULT: OK"
