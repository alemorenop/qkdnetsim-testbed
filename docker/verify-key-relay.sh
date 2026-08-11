#!/bin/bash
# End-to-end verification of the "key relay" scenario (9 containers).
# Usage: ./docker/verify-key-relay.sh [sample_seconds]
#   (run from contrib/qkdnetsim, with the stack already up)
# tcpdump samples only exist temporarily inside the containers; this script
# does not create or copy captures into the repository.

set -uo pipefail
# Git Bash automatically converts /tmp/... to a Windows path even when the
# argument is meant for a process inside the container. Docker needs to
# receive the unmodified Linux paths here.
export MSYS_NO_PATHCONV=1
source docker/verify-link-budget.sh
COMPOSE="docker compose -f docker/docker-compose.key-relay.yml"
CAPTURE_S="${1:-10}"
READY_TIMEOUT_S="${READY_TIMEOUT_S:-120}"
failures=0

log_count() {
    local service="$1" pattern="$2" container_id
    container_id=$($COMPOSE ps -q "$service")
    docker exec "$container_id" grep -cE "$pattern" /tmp/qkdnetsim.log 2>/dev/null || true
}

common_served_key_count() {
    local alice_ids bob_ids
    alice_ids=$(docker exec qkd-relay-kms-alice sed -n 's/.*serves key.*keyId=\([^ ]*\).*/\1/p' /tmp/qkdnetsim.log 2>/dev/null | sort -u)
    bob_ids=$(docker exec qkd-relay-kms-bob sed -n 's/.*serves key.*keyId=\([^ ]*\).*/\1/p' /tmp/qkdnetsim.log 2>/dev/null | sort -u)
    comm -12 <(printf '%s\n' "$alice_ids") <(printf '%s\n' "$bob_ids") | sed '/^$/d' | wc -l
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

echo "=== QKD link budgets ==="
if ! verify_qkd_link_budget_pair docker qkd-relay-pp-alice qkd-relay-pp-a alice-relay; then
    failures=$((failures + 1))
fi
if ! verify_qkd_link_budget_pair docker qkd-relay-pp-b qkd-relay-pp-bob relay-bob; then
    failures=$((failures + 1))
fi
echo

# The two physical QKD links become ready before the end-to-end relay buffer.
# On a cold start the first 6400-bit application key may therefore take tens
# of seconds to traverse both hops. Wait for the functional condition instead
# of treating that normal warm-up as a startup failure.
echo "Waiting up to ${READY_TIMEOUT_S}s for the first key served at both ends..."
deadline=$((SECONDS + READY_TIMEOUT_S))
while [ "$(common_served_key_count)" -eq 0 ] && [ "$SECONDS" -lt "$deadline" ]; do
    sleep 2
done
echo

echo "=== 2) Expected activity per container (current run's logs) ==="
declare -A PATTERNS=(
    [pp_alice]="Key delivered to KMS Alice"
    [pp_relay_a]="Key delivered to KMS Relay"
    [pp_relay_b]="Key delivered to KMS Relay"
    [pp_bob]="Key delivered to KMS Bob"
    [kms_alice]="stores key|serves key"
    [kms_trusted]="stores key"
    [kms_bob]="stores key|serves key"
    [etsi014_alice]="GET_KEY request"
    [etsi014_bob]="GET_KEY request"
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
for service in kms_alice kms_bob; do
    count=$(log_count "$service" "serves key")
    echo "  $service: $count key(s) served since startup"
    if [ "$count" -eq 0 ]; then
        failures=$((failures + 1))
    fi
done

common_ids=$(common_served_key_count)
if [ "$common_ids" -gt 0 ]; then
    echo "  [OK] KMS Alice and KMS Bob served $common_ids identical keyId(s)"
else
    echo "  [FAIL] KMS Alice and KMS Bob have not served any common keyId"
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
capture qkd-relay-pp-alice "tcp port 80" "01_pp_alice_kms_alice.pcap"   "PP Alice -> KMS Alice (raw key delivery, post-processing -> KMS Alice)" & capture_pids+=("$!")
capture qkd-relay-kms-alice "tcp port 80 or tcp port 8080" "02_kms_alice_relay.pcap" "KMS Alice -> trusted KMS (relay KMS Alice -> KMS Relay)" & capture_pids+=("$!")
capture qkd-relay-kms-trusted "tcp port 80 or tcp port 8080" "03_kms_trusted_bob.pcap"   "trusted KMS -> KMS Bob (relay KMS Relay -> KMS Bob)" & capture_pids+=("$!")
capture qkd-relay-etsi014-alice "tcp port 8081" "04_etsi014_alice_bob.pcap" "ETSI 014 Alice <-> Bob (encrypted ETSI014 Alice <-> Bob traffic, the final result)" & capture_pids+=("$!")
for pid in "${capture_pids[@]}"; do
    if ! wait "$pid"; then
        failures=$((failures + 1))
    fi
done
echo

relay_payload=$(docker exec qkd-relay-kms-alice tcpdump -nn -A -r /tmp/02_kms_alice_relay.pcap 2>/dev/null)
if ! grep -q 'skey_create' <<<"$relay_payload"; then
    echo "  [INFO] no new SKEY_CREATE negotiation happened during this window"
    echo "         (the common keyId already served by both KMS instances already confirms the relay)"
elif grep -qE '"ekey":"[^"]+' <<<"$relay_payload"; then
    echo "  [OK] SKEY_CREATE observed with a non-empty ekey"
else
    echo "  [FAIL] SKEY_CREATE observed with an empty ekey on KMS Alice -> trusted KMS"
    failures=$((failures + 1))
fi

app_packets=$(docker exec qkd-relay-etsi014-alice tcpdump -nn -r /tmp/04_etsi014_alice_bob.pcap 2>/dev/null | wc -l)
if [ "$app_packets" -le 3 ]; then
    echo "  [INFO] parallel application sample empty; retrying sequentially for 6s"
    docker exec qkd-relay-etsi014-alice rm -f /tmp/04_etsi014_alice_bob.pcap
    docker exec qkd-relay-etsi014-alice timeout 6 tcpdump -U -i any \
        -w /tmp/04_etsi014_alice_bob.pcap 'tcp port 8081' >/dev/null 2>&1
    app_packets=$(docker exec qkd-relay-etsi014-alice tcpdump -nn \
        -r /tmp/04_etsi014_alice_bob.pcap 2>/dev/null | wc -l)
fi
if [ "$app_packets" -gt 3 ]; then
    echo "  [OK] encrypted application traffic: $app_packets packet(s) captured"
else
    echo "  [FAIL] insufficient application traffic: $app_packets packet(s)"
    failures=$((failures + 1))
fi
echo

echo "=== 5) Text sample of the encrypted Alice<->Bob traffic ==="
docker exec qkd-relay-etsi014-alice timeout 8 tcpdump -i any -nn -c 8 'tcp port 8081' 2>/dev/null
echo

if [ "$failures" -ne 0 ]; then
    echo "RESULT: FAIL ($failures check(s))" >&2
    exit 1
fi
echo "RESULT: OK"
