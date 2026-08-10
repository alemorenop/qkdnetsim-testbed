#!/bin/bash
# End-to-end verification of the 6-container point-to-point scenario.
# Usage: ./docker/verify.sh [capture_seconds]

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

echo "=== 1) Status of the 6 containers ==="
docker ps --filter "name=qkd-p2p-" --format "table {{.Names}}\t{{.Status}}"
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

echo "=== 2) Expected activity per container ==="
declare -A PATTERNS=(
    [pp_alice]="Key delivered to KMS Alice"
    [pp_bob]="Key delivered to KMS Bob"
    [kms_alice]="stores key|serves key"
    [kms_bob]="stores key|serves key"
    [etsi014_alice]="GET_KEY request to KMS Alice"
    [etsi014_bob]="GET_KEY request to KMS Bob"
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

echo "=== 3) Same key served to Alice and Bob ==="
alice_ids=$(docker exec qkd-p2p-kms-alice sed -n 's/.*serves key.*keyId=\([^ ]*\).*/\1/p' /tmp/qkdnetsim.log | sort -u)
bob_ids=$(docker exec qkd-p2p-kms-bob sed -n 's/.*serves key.*keyId=\([^ ]*\).*/\1/p' /tmp/qkdnetsim.log | sort -u)
common_ids=$(comm -12 <(printf '%s\n' "$alice_ids") <(printf '%s\n' "$bob_ids") | sed '/^$/d')
if [ -n "$common_ids" ]; then
    echo "  [OK] common keyId: $(printf '%s' "$common_ids" | tr '\n' ' ')"
else
    echo "  [FAIL] the KMS instances have not served any common keyId"
    failures=$((failures + 1))
fi
echo

echo "=== 4) Encrypted Alice-Bob traffic (${CAPTURE_S}s) ==="
# tcpdump opens the raw socket as root and then drops privileges to a
# dedicated user (Debian/Ubuntu package) before writing -w. If a file with
# that same name is left over from a previous run with different
# owner/permissions, the next attempt fails silently with "Permission
# denied" (exit 1, zero packets), unrelated to whether there is real
# traffic. Remove the file before each attempt so it never depends on its
# previous state.
docker exec qkd-p2p-etsi014-alice rm -f /tmp/p2p-verify-app.pcap
docker exec qkd-p2p-etsi014-alice timeout "$CAPTURE_S" tcpdump -U -i any -s 0 \
    -w /tmp/p2p-verify-app.pcap 'tcp port 8081' >/dev/null 2>&1

app_packets=$(docker exec qkd-p2p-etsi014-alice tcpdump -nn -r /tmp/p2p-verify-app.pcap 2>/dev/null | wc -l)
# Steps 1-3 (grepping logs that already have thousands of lines) can take a
# few seconds, so this capture window may start right in a gap between
# bursts of the periodic send. A retry covers that case without flagging as
# a failure a scenario that is actually sending traffic.
if [ "$app_packets" -le 3 ]; then
    echo "  [INFO] empty sample; retrying for ${CAPTURE_S}s"
    docker exec qkd-p2p-etsi014-alice rm -f /tmp/p2p-verify-app.pcap
    docker exec qkd-p2p-etsi014-alice timeout "$CAPTURE_S" tcpdump -U -i any -s 0 \
        -w /tmp/p2p-verify-app.pcap 'tcp port 8081' >/dev/null 2>&1
    app_packets=$(docker exec qkd-p2p-etsi014-alice tcpdump -nn -r /tmp/p2p-verify-app.pcap 2>/dev/null | wc -l)
fi
wire_text=$(docker exec qkd-p2p-etsi014-alice tcpdump -nn -A -s 0 -r /tmp/p2p-verify-app.pcap 2>/dev/null)
if [ "$app_packets" -gt 3 ]; then
    echo "  [OK] $app_packets application packets captured"
else
    echo "  [FAIL] insufficient traffic: $app_packets packet(s)"
    failures=$((failures + 1))
fi

# GetPacketContent() generates ONLY pure alphanumeric noise as the starting
# plaintext (see model/qkd-app-014.cc) -- it does not insert any text
# marker of its own into the packet. Without encryption, that alphanumeric
# noise travels as-is and shows up as a long readable [[:alnum:]] run in the
# -A dump. With OTP active, those same bytes become non-printable noise.
if grep -qE '[[:alnum:]]{100}' <<<"$wire_text"; then
    echo "  [FAIL] the application body still contains plaintext"
    failures=$((failures + 1))
else
    echo "  [OK] encrypted binary body, no recognizable plaintext"
fi
echo

if [ "$failures" -ne 0 ]; then
    echo "RESULT: FAIL ($failures check(s))" >&2
    exit 1
fi
echo "RESULT: OK"
