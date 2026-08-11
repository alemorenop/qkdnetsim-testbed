#!/usr/bin/env bash
set -euo pipefail

export MSYS_NO_PATHCONV=1

docker_bin="${DOCKER_BIN:-docker}"
source docker/verify-link-budget.sh
expected_interface="${QKD_INTERFACE:-004}"
startup_timeout="${1:-360}"
rotation_timeout="${2:-180}"
capture=/tmp/qkd-relay-vpn-verify.pcap
containers=(
    qkd-relay-vpn-pp-alice
    qkd-relay-vpn-pp-a
    qkd-relay-vpn-pp-b
    qkd-relay-vpn-pp-bob
    qkd-relay-vpn-kms-alice
    qkd-relay-vpn-kms-trusted
    qkd-relay-vpn-kms-bob
    qkd-relay-vpn-alice
    qkd-relay-vpn-bob
)

cleanup() {
    "$docker_bin" exec qkd-relay-vpn-alice sh -c \
        "rm -f '$capture' /tmp/qkd-relay-vpn-tcpdump.log" \
        >/dev/null 2>&1 || true
}
trap cleanup EXIT

fail() {
    echo "[FAIL] $*" >&2
    exit 1
}

[ "$expected_interface" = "004" ] || [ "$expected_interface" = "014" ] ||
    fail "QKD_INTERFACE must be 004 or 014"

state_value() {
    local container="$1" field="$2"
    "$docker_bin" exec "$container" python3 -c \
        'import json,sys; value=json.load(open("/run/qkd-vpn/state.json")).get(sys.argv[1]); print("" if value is None else value)' \
        "$field" | tr -d '\r'
}

wait_for_health() {
    local container="$1" deadline=$((SECONDS + startup_timeout)) status=""
    while (( SECONDS < deadline )); do
        status=$("$docker_bin" inspect -f \
            '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' \
            "$container" 2>/dev/null || true)
        [ "$status" = "healthy" ] && return 0
        sleep 3
    done
    fail "$container did not become healthy within ${startup_timeout}s (last status: ${status:-missing})"
}

assert_stable_containers() {
    local container running restarts
    for container in "${containers[@]}"; do
        running=$("$docker_bin" inspect -f '{{.State.Running}}' "$container")
        restarts=$("$docker_bin" inspect -f '{{.RestartCount}}' "$container")
        [ "$running" = "true" ] || fail "$container is not running"
        [ "$restarts" = "0" ] || fail "$container restarted $restarts time(s)"
    done
}

assert_peer_state_matches() {
    local alice_generation bob_generation alice_key_id bob_key_id
    local alice_ksid bob_ksid alice_index bob_index
    local alice_fingerprint bob_fingerprint alice_interface bob_interface
    local key_reference

    alice_generation=$(state_value qkd-relay-vpn-alice generation)
    bob_generation=$(state_value qkd-relay-vpn-bob generation)
    alice_key_id=$(state_value qkd-relay-vpn-alice key_id)
    bob_key_id=$(state_value qkd-relay-vpn-bob key_id)
    alice_ksid=$(state_value qkd-relay-vpn-alice ksid)
    bob_ksid=$(state_value qkd-relay-vpn-bob ksid)
    alice_index=$(state_value qkd-relay-vpn-alice key_index)
    bob_index=$(state_value qkd-relay-vpn-bob key_index)
    alice_fingerprint=$(state_value qkd-relay-vpn-alice key_fingerprint)
    bob_fingerprint=$(state_value qkd-relay-vpn-bob key_fingerprint)
    alice_interface=$(state_value qkd-relay-vpn-alice qkd_interface)
    bob_interface=$(state_value qkd-relay-vpn-bob qkd_interface)

    [ "$alice_interface" = "$expected_interface" ] &&
        [ "$bob_interface" = "$expected_interface" ] ||
        fail "VPN endpoints use ETSI $alice_interface/$bob_interface; expected ETSI $expected_interface"
    [ "$alice_generation" = "$bob_generation" ] ||
        fail "generation mismatch: Alice=$alice_generation Bob=$bob_generation"

    if [ "$expected_interface" = "004" ]; then
        [ -n "$alice_ksid" ] && [ "$alice_ksid" = "$bob_ksid" ] ||
            fail "ETSI 004 KSID mismatch"
        [ -n "$alice_index" ] && [ "$alice_index" = "$bob_index" ] ||
            fail "ETSI 004 stream-index mismatch"
        key_reference="$alice_index"
    else
        [ -n "$alice_key_id" ] && [ "$alice_key_id" = "$bob_key_id" ] ||
            fail "ETSI 014 key_ID mismatch"
        key_reference="$alice_key_id"
    fi

    [ -n "$alice_fingerprint" ] &&
        [ "$alice_fingerprint" = "$bob_fingerprint" ] ||
        fail "QKD key fingerprint mismatch"

    echo "$alice_generation|$key_reference"
}

assert_current_ike_sa() {
    local container="$1" generation="$2" status
    status=$("$docker_bin" exec "$container" ipsec statusall 2>&1)
    grep -Eq "qkd-${generation}\\[[0-9]+\\]: ESTABLISHED" <<<"$status" ||
        fail "$container has no qkd-${generation} IKE_SA"
}

assert_kms_served_reference() {
    local reference="$1"
    "$docker_bin" logs qkd-relay-vpn-kms-alice 2>&1 |
        grep "serves key.*keyId=${reference}" >/dev/null ||
        fail "KMS Alice did not report serving ETSI $expected_interface reference=$reference"
    "$docker_bin" logs qkd-relay-vpn-kms-bob 2>&1 |
        grep "serves key.*keyId=${reference}" >/dev/null ||
        fail "KMS Bob did not report serving ETSI $expected_interface reference=$reference"
}

echo "=== 1) Waiting for the initial relayed QKD-backed VPN ==="
wait_for_health qkd-relay-vpn-alice
wait_for_health qkd-relay-vpn-bob
assert_stable_containers
verify_qkd_link_budget_pair "$docker_bin" qkd-relay-vpn-pp-alice qkd-relay-vpn-pp-a alice-relay ||
    fail "the Alice-relay QKD endpoints do not use the same link budget"
verify_qkd_link_budget_pair "$docker_bin" qkd-relay-vpn-pp-b qkd-relay-vpn-pp-bob relay-bob ||
    fail "the relay-Bob QKD endpoints do not use the same link budget"
initial_state=$(assert_peer_state_matches)
initial_generation=${initial_state%%|*}
initial_reference=${initial_state#*|}
[ "$initial_generation" -ge 1 ] ||
    fail "the first committed generation was not reached"
assert_current_ike_sa qkd-relay-vpn-alice "$initial_generation"
assert_current_ike_sa qkd-relay-vpn-bob "$initial_generation"
assert_kms_served_reference "$initial_reference"
echo "[OK] generation $initial_generation uses the same relayed ETSI $expected_interface reference and fingerprint"

echo
echo "=== 2) Waiting for a real IKE PSK rotation ==="
target_generation=$((initial_generation + 1))
deadline=$((SECONDS + rotation_timeout))
while (( SECONDS < deadline )); do
    assert_stable_containers
    alice_generation=$(state_value qkd-relay-vpn-alice generation)
    bob_generation=$(state_value qkd-relay-vpn-bob generation)
    if [ "$alice_generation" -ge "$target_generation" ] &&
        [ "$bob_generation" -ge "$target_generation" ]; then
        break
    fi
    sleep 3
done

current_state=$(assert_peer_state_matches)
current_generation=${current_state%%|*}
current_reference=${current_state#*|}
[ "$current_generation" -ge "$target_generation" ] ||
    fail "no committed rotation within ${rotation_timeout}s (still at generation $current_generation)"
assert_current_ike_sa qkd-relay-vpn-alice "$current_generation"
assert_current_ike_sa qkd-relay-vpn-bob "$current_generation"
assert_kms_served_reference "$current_reference"
echo "[OK] both peers committed qkd-$current_generation with relayed ETSI $expected_interface reference=$current_reference"

echo
echo "=== 3) Sending real traffic and checking the outer interface ==="
cleanup
data_dev=$("$docker_bin" exec qkd-relay-vpn-alice sh -c \
    "ip route get 192.168.121.9 | sed -n 's/.* dev \\([^ ]*\\).*/\\1/p' | head -n1" |
    tr -d '\r')
[ -n "$data_dev" ] || fail "could not resolve Alice's VPN data interface"

"$docker_bin" exec qkd-relay-vpn-alice sh -c "
    timeout 7 tcpdump -Z root -U -Q out -i '$data_dev' -s 128 \
        -w '$capture' 'host 192.168.121.9 and (ip proto 50 or icmp)' \
        >/tmp/qkd-relay-vpn-tcpdump.log 2>&1 &
    capture_pid=\$!
    sleep 1
    ping -c 3 -W 2 192.168.121.9 >/dev/null
    wait \$capture_pid || true
    test -s '$capture'
" || fail "ping or packet capture failed"

esp_packets=$("$docker_bin" exec qkd-relay-vpn-alice sh -c \
    "tcpdump -nn -r '$capture' 'ip proto 50' 2>/dev/null | wc -l" |
    tr -d '\r ')
plaintext_icmp=$("$docker_bin" exec qkd-relay-vpn-alice sh -c \
    "tcpdump -nn -r '$capture' 'icmp' 2>/dev/null | wc -l" |
    tr -d '\r ')
[ "$esp_packets" -gt 0 ] || fail "no outbound ESP traffic was captured"
[ "$plaintext_icmp" -eq 0 ] ||
    fail "$plaintext_icmp outbound plaintext ICMP packet(s) were visible"
echo "[OK] ping succeeded; outer traffic contained ESP and no plaintext ICMP"

cleanup
"$docker_bin" exec qkd-relay-vpn-alice sh -c "test ! -e '$capture'" ||
    fail "temporary capture cleanup failed"
trap - EXIT

echo
echo "ETSI $expected_interface key-relay VPN scenario verified successfully."
