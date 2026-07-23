#!/bin/bash
# End-to-end verification of the "key relay" scenario (9 hosts).
# Usage: ./docker/verify-key-relay.sh [sample_seconds]
#   (run from contrib/qkdnetsim, with the stack already up)
# tcpdump samples only exist temporarily inside the containers; this script
# does not create or copy captures into the repository.

set -uo pipefail
# Git Bash automatically converts /tmp/... to a Windows path even when the
# argument is meant for a process inside the container. Docker needs to
# receive the unmodified Linux paths here.
export MSYS_NO_PATHCONV=1
COMPOSE="docker compose -f docker/docker-compose.key-relay.yml"
CAPTURE_S="${1:-10}"
failures=0

log_count() {
    local service="$1" pattern="$2" container_id
    container_id=$($COMPOSE ps -q "$service")
    docker exec "$container_id" grep -cE "$pattern" /tmp/qkdnetsim.log 2>/dev/null || true
}

echo "=== 1) Status of the 9 containers ==="
docker ps --filter "name=qkd-relay" --format "table {{.Names}}\t{{.Status}}"
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

echo "=== 2) Expected activity per host (current run's logs) ==="
declare -A PATTERNS=(
    [host1_pp_alice]="Key delivered to KMS Alice"
    [host2_pp_relay_a]="Key delivered to KMS Relay"
    [host3_pp_relay_b]="Key delivered to KMS Relay"
    [host4_pp_bob]="Key delivered to KMS Bob"
    [host5_kms_alice]="stores key|serves key"
    [host6_kms_relay]="stores key"
    [host7_kms_bob]="stores key|serves key"
    [host8_etsi014_alice]="GET_KEY request"
    [host9_etsi014_bob]="GET_KEY request"
)

for service in "${!PATTERNS[@]}"; do
    pattern="${PATTERNS[$service]}"
    count=$(log_count "$service" "$pattern")
    if [ "$count" -gt 0 ]; then
        echo "  [OK]    $service: $count line(s) ('$pattern')"
    else
        echo "  [EMPTY] $service: 0 lines ('$pattern')"
        failures=$((failures + 1))
    fi
done
echo

echo "=== 3) Confirm that KMS Alice and KMS Bob SERVE keys to the ETSI014 apps (not just relay/store) ==="
for service in host5_kms_alice host7_kms_bob; do
    count=$(log_count "$service" "serves key")
    echo "  $service: $count key(s) served since startup"
    if [ "$count" -eq 0 ]; then
        failures=$((failures + 1))
    fi
done

alice_ids=$(docker exec qkd-relay-host5 sed -n 's/.*serves key.*keyId=\([^ ]*\).*/\1/p' /tmp/qkdnetsim.log | sort -u)
bob_ids=$(docker exec qkd-relay-host7 sed -n 's/.*serves key.*keyId=\([^ ]*\).*/\1/p' /tmp/qkdnetsim.log | sort -u)
common_ids=$(comm -12 <(printf '%s\n' "$alice_ids") <(printf '%s\n' "$bob_ids") | sed '/^$/d' | wc -l)
if [ "$common_ids" -gt 0 ]; then
    echo "  [OK] HOST5 and HOST7 served $common_ids identical keyId(s)"
else
    echo "  [FAIL] HOST5 and HOST7 have not served any common keyId"
    failures=$((failures + 1))
fi
echo

echo "=== 4) Temporary traffic sampling (${CAPTURE_S}s) ==="

capture() {
    local container="$1" filter="$2" outfile="$3" label="$4"
    echo "  -> $label ($container, filter: $filter)"
    # tcpdump opens the raw socket as root and drops privileges before
    # writing -w; a file left over from a previous run with different
    # owner/permissions makes the attempt fail silently ("Permission
    # denied", 0 packets), unrelated to whether there was real traffic.
    docker exec "$container" rm -f "/tmp/$outfile"
    docker exec "$container" timeout "$CAPTURE_S" tcpdump -U -i any -w "/tmp/$outfile" "$filter" >/dev/null 2>&1
    pkts=$(docker exec "$container" tcpdump -nn -r "/tmp/$outfile" 2>/dev/null | wc -l)
    if [ "$pkts" -gt 0 ]; then
        echo "     [OK] $pkts packet(s) observed"
        return 0
    else
        echo "     [FAIL] no traffic during the ${CAPTURE_S}s window"
        return 1
    fi
}

# NOTE: APP-KMS traffic (store_key, GET_KEY) uses port 80 (m_sinkSocket),
# but KMS-KMS traffic (skey_create/transform_keys, the relay itself) uses
# port 8080 (m_sinkSocketKMS, see PrepareSinkSocket() in
# qkd-key-manager-system-application.cc) -- that's why the KMS<->KMS links
# need "or port 8080", otherwise exactly the traffic they are meant to
# demonstrate gets missed.
#
# Run the 4 samples IN PARALLEL so they share the same time window.
capture_pids=()
capture qkd-relay-host1 "tcp port 80" "01_host1_pp_a_kms_alice.pcap"   "HOST1 -> HOST5 (raw key delivery, post-processing -> KMS Alice)" & capture_pids+=("$!")
capture qkd-relay-host5 "tcp port 80 or tcp port 8080" "02_host5_kms_alice_relay.pcap" "HOST5 -> HOST6 (relay KMS Alice -> KMS Relay)" & capture_pids+=("$!")
capture qkd-relay-host6 "tcp port 80 or tcp port 8080" "03_host6_kms_relay_bob.pcap"   "HOST6 -> HOST7 (relay KMS Relay -> KMS Bob)" & capture_pids+=("$!")
capture qkd-relay-host8 "tcp port 8081" "04_host8_alice_bob_app.pcap" "HOST8 -> HOST9 (encrypted ETSI014 Alice <-> Bob traffic, the final result)" & capture_pids+=("$!")
for pid in "${capture_pids[@]}"; do
    if ! wait "$pid"; then
        failures=$((failures + 1))
    fi
done
echo

relay_payload=$(docker exec qkd-relay-host5 tcpdump -nn -A -r /tmp/02_host5_kms_alice_relay.pcap 2>/dev/null)
if ! grep -q 'skey_create' <<<"$relay_payload"; then
    echo "  [INFO] no new SKEY_CREATE negotiation happened during this window"
    echo "         (the common keyId already served by both KMS instances already confirms the relay)"
elif grep -qE '"ekey":"[^"]+' <<<"$relay_payload"; then
    echo "  [OK] SKEY_CREATE observed with a non-empty ekey"
else
    echo "  [FAIL] SKEY_CREATE observed with an empty ekey on HOST5 -> HOST6"
    failures=$((failures + 1))
fi

app_packets=$(docker exec qkd-relay-host8 tcpdump -nn -r /tmp/04_host8_alice_bob_app.pcap 2>/dev/null | wc -l)
if [ "$app_packets" -le 3 ]; then
    echo "  [INFO] parallel application sample empty; retrying sequentially for 6s"
    docker exec qkd-relay-host8 rm -f /tmp/04_host8_alice_bob_app.pcap
    docker exec qkd-relay-host8 timeout 6 tcpdump -U -i any \
        -w /tmp/04_host8_alice_bob_app.pcap 'tcp port 8081' >/dev/null 2>&1
    app_packets=$(docker exec qkd-relay-host8 tcpdump -nn \
        -r /tmp/04_host8_alice_bob_app.pcap 2>/dev/null | wc -l)
fi
if [ "$app_packets" -gt 3 ]; then
    echo "  [OK] encrypted application traffic: $app_packets packet(s) captured"
else
    echo "  [FAIL] insufficient application traffic: $app_packets packet(s)"
    failures=$((failures + 1))
fi
echo

echo "=== 5) Text sample of the encrypted Alice<->Bob traffic ==="
docker exec qkd-relay-host8 timeout 8 tcpdump -i any -nn -c 8 'tcp port 8081' 2>/dev/null
echo

if [ "$failures" -ne 0 ]; then
    echo "RESULT: FAIL ($failures check(s))" >&2
    exit 1
fi
echo "RESULT: OK"
