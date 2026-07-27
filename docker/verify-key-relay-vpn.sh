#!/usr/bin/env bash
set -euo pipefail

export MSYS_NO_PATHCONV=1

docker_bin="${DOCKER_BIN:-docker}"
startup_timeout="${1:-360}"
rotation_timeout="${2:-180}"
capture=/tmp/qkd-relay-vpn-verify.pcap
containers=(
    qkd-relay-vpn-host1
    qkd-relay-vpn-host2
    qkd-relay-vpn-host3
    qkd-relay-vpn-host4
    qkd-relay-vpn-host5
    qkd-relay-vpn-host6
    qkd-relay-vpn-host7
    qkd-relay-vpn-host8
    qkd-relay-vpn-host9
)

cleanup() {
    "$docker_bin" exec qkd-relay-vpn-host8 sh -c \
        "rm -f '$capture' /tmp/qkd-relay-vpn-tcpdump.log" \
        >/dev/null 2>&1 || true
}
trap cleanup EXIT

fail() {
    echo "[FAIL] $*" >&2
    exit 1
}

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
    local alice_fingerprint bob_fingerprint alice_interface bob_interface

    alice_generation=$(state_value qkd-relay-vpn-host8 generation)
    bob_generation=$(state_value qkd-relay-vpn-host9 generation)
    alice_key_id=$(state_value qkd-relay-vpn-host8 key_id)
    bob_key_id=$(state_value qkd-relay-vpn-host9 key_id)
    alice_fingerprint=$(state_value qkd-relay-vpn-host8 key_fingerprint)
    bob_fingerprint=$(state_value qkd-relay-vpn-host9 key_fingerprint)
    alice_interface=$(state_value qkd-relay-vpn-host8 qkd_interface)
    bob_interface=$(state_value qkd-relay-vpn-host9 qkd_interface)

    [ "$alice_interface" = "014" ] && [ "$bob_interface" = "014" ] ||
        fail "one or both VPN endpoints are not using ETSI 014"
    [ "$alice_generation" = "$bob_generation" ] ||
        fail "generation mismatch: Alice=$alice_generation Bob=$bob_generation"
    [ -n "$alice_key_id" ] && [ "$alice_key_id" = "$bob_key_id" ] ||
        fail "ETSI 014 key_ID mismatch"
    [ -n "$alice_fingerprint" ] &&
        [ "$alice_fingerprint" = "$bob_fingerprint" ] ||
        fail "QKD key fingerprint mismatch"

    echo "$alice_generation|$alice_key_id"
}

assert_current_ike_sa() {
    local container="$1" generation="$2" status
    status=$("$docker_bin" exec "$container" ipsec statusall 2>&1)
    grep -Eq "qkd-${generation}\\[[0-9]+\\]: ESTABLISHED" <<<"$status" ||
        fail "$container has no qkd-${generation} IKE_SA"
}

assert_kms_served_key() {
    local key_id="$1"
    "$docker_bin" logs qkd-relay-vpn-host5 2>&1 |
        grep "serves key.*keyId=${key_id}" >/dev/null ||
        fail "KMS Alice did not report serving key_ID=$key_id"
    "$docker_bin" logs qkd-relay-vpn-host7 2>&1 |
        grep "serves key.*keyId=${key_id}" >/dev/null ||
        fail "KMS Bob did not report serving key_ID=$key_id"
}

echo "=== 1) Waiting for the initial relayed QKD-backed VPN ==="
wait_for_health qkd-relay-vpn-host8
wait_for_health qkd-relay-vpn-host9
assert_stable_containers
initial_state=$(assert_peer_state_matches)
initial_generation=${initial_state%%|*}
initial_key_id=${initial_state#*|}
[ "$initial_generation" -ge 1 ] ||
    fail "the first committed generation was not reached"
assert_current_ike_sa qkd-relay-vpn-host8 "$initial_generation"
assert_current_ike_sa qkd-relay-vpn-host9 "$initial_generation"
assert_kms_served_key "$initial_key_id"
echo "[OK] generation $initial_generation uses the same relayed ETSI 014 key_ID and fingerprint"

echo
echo "=== 2) Waiting for a real IKE PSK rotation ==="
target_generation=$((initial_generation + 1))
deadline=$((SECONDS + rotation_timeout))
while (( SECONDS < deadline )); do
    assert_stable_containers
    alice_generation=$(state_value qkd-relay-vpn-host8 generation)
    bob_generation=$(state_value qkd-relay-vpn-host9 generation)
    if [ "$alice_generation" -ge "$target_generation" ] &&
        [ "$bob_generation" -ge "$target_generation" ]; then
        break
    fi
    sleep 3
done

current_state=$(assert_peer_state_matches)
current_generation=${current_state%%|*}
current_key_id=${current_state#*|}
[ "$current_generation" -ge "$target_generation" ] ||
    fail "no committed rotation within ${rotation_timeout}s (still at generation $current_generation)"
assert_current_ike_sa qkd-relay-vpn-host8 "$current_generation"
assert_current_ike_sa qkd-relay-vpn-host9 "$current_generation"
assert_kms_served_key "$current_key_id"
echo "[OK] both peers committed qkd-$current_generation with relayed key_ID=$current_key_id"

echo
echo "=== 3) Sending real traffic and checking the outer interface ==="
cleanup
data_dev=$("$docker_bin" exec qkd-relay-vpn-host8 sh -c \
    "ip route get 192.168.121.9 | sed -n 's/.* dev \\([^ ]*\\).*/\\1/p' | head -n1" |
    tr -d '\r')
[ -n "$data_dev" ] || fail "could not resolve Alice's VPN data interface"

"$docker_bin" exec qkd-relay-vpn-host8 sh -c "
    timeout 7 tcpdump -Z root -U -Q out -i '$data_dev' -s 128 \
        -w '$capture' 'host 192.168.121.9 and (ip proto 50 or icmp)' \
        >/tmp/qkd-relay-vpn-tcpdump.log 2>&1 &
    capture_pid=\$!
    sleep 1
    ping -c 3 -W 2 192.168.121.9 >/dev/null
    wait \$capture_pid || true
    test -s '$capture'
" || fail "ping or packet capture failed"

esp_packets=$("$docker_bin" exec qkd-relay-vpn-host8 sh -c \
    "tcpdump -nn -r '$capture' 'ip proto 50' 2>/dev/null | wc -l" |
    tr -d '\r ')
plaintext_icmp=$("$docker_bin" exec qkd-relay-vpn-host8 sh -c \
    "tcpdump -nn -r '$capture' 'icmp' 2>/dev/null | wc -l" |
    tr -d '\r ')
[ "$esp_packets" -gt 0 ] || fail "no outbound ESP traffic was captured"
[ "$plaintext_icmp" -eq 0 ] ||
    fail "$plaintext_icmp outbound plaintext ICMP packet(s) were visible"
echo "[OK] ping succeeded; outer traffic contained ESP and no plaintext ICMP"

cleanup
"$docker_bin" exec qkd-relay-vpn-host8 sh -c "test ! -e '$capture'" ||
    fail "temporary capture cleanup failed"
trap - EXIT

echo
echo "Key-relay VPN scenario verified successfully."
