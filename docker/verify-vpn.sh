#!/usr/bin/env bash
set -euo pipefail

export MSYS_NO_PATHCONV=1

docker_bin="${DOCKER_BIN:-docker}"
startup_timeout="${1:-240}"
rotation_timeout="${2:-120}"
capture=/tmp/qkd-vpn-verify.pcap
compose=(
    "$docker_bin" compose
    -f docker/docker-compose.vpn.yml
)
services=(
    host1_pp_alice
    host2_pp_bob
    host3_kms_alice
    host4_kms_bob
    host5_vpn_alice
    host6_vpn_bob
)
containers=(
    qkd-vpn-host1
    qkd-vpn-host2
    qkd-vpn-host3
    qkd-vpn-host4
    qkd-vpn-host5
    qkd-vpn-host6
)

cleanup() {
    "$docker_bin" exec qkd-vpn-host5 sh -c \
        "rm -f '$capture' /tmp/qkd-vpn-tcpdump.log" >/dev/null 2>&1 || true
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

assert_running_without_restarts() {
    local container running restarts
    for container in "${containers[@]}"; do
        running=$("$docker_bin" inspect -f '{{.State.Running}}' "$container")
        restarts=$("$docker_bin" inspect -f '{{.RestartCount}}' "$container")
        [ "$running" = "true" ] || fail "$container is not running"
        [ "$restarts" = "0" ] || fail "$container restarted $restarts time(s)"
    done
}

assert_peer_state_matches() {
    local alice_generation bob_generation alice_ksid bob_ksid
    local alice_index bob_index alice_fingerprint bob_fingerprint

    alice_generation=$(state_value qkd-vpn-host5 generation)
    bob_generation=$(state_value qkd-vpn-host6 generation)
    alice_ksid=$(state_value qkd-vpn-host5 ksid)
    bob_ksid=$(state_value qkd-vpn-host6 ksid)
    alice_index=$(state_value qkd-vpn-host5 key_index)
    bob_index=$(state_value qkd-vpn-host6 key_index)
    alice_fingerprint=$(state_value qkd-vpn-host5 key_fingerprint)
    bob_fingerprint=$(state_value qkd-vpn-host6 key_fingerprint)

    [ -n "$alice_ksid" ] && [ "$alice_ksid" = "$bob_ksid" ] ||
        fail "VPN peers do not report the same ETSI 004 KSID"
    [ "$alice_generation" = "$bob_generation" ] ||
        fail "generation mismatch: Alice=$alice_generation Bob=$bob_generation"
    [ -n "$alice_index" ] && [ "$alice_index" = "$bob_index" ] ||
        fail "ETSI 004 key-index mismatch: Alice=$alice_index Bob=$bob_index"
    [ -n "$alice_fingerprint" ] &&
        [ "$alice_fingerprint" = "$bob_fingerprint" ] ||
        fail "QKD key fingerprint mismatch"

    echo "$alice_generation"
}

assert_current_ike_sa() {
    local container="$1" generation="$2" status
    status=$("$docker_bin" exec "$container" ipsec statusall 2>&1)
    grep -Eq "qkd-${generation}\\[[0-9]+\\]: ESTABLISHED" <<<"$status" ||
        fail "$container has no qkd-${generation} IKE_SA"
}

echo "=== 1) Waiting for the initial QKD-backed VPN ==="
wait_for_health qkd-vpn-host5
wait_for_health qkd-vpn-host6
assert_running_without_restarts
initial_generation=$(assert_peer_state_matches)
[ "$initial_generation" -ge 1 ] ||
    fail "the first committed generation was not reached"
assert_current_ike_sa qkd-vpn-host5 "$initial_generation"
assert_current_ike_sa qkd-vpn-host6 "$initial_generation"
echo "[OK] six containers are stable; generation $initial_generation uses the same KSID, key index and fingerprint"

echo
echo "=== 2) Waiting for a real IKE PSK rotation ==="
target_generation=$((initial_generation + 1))
deadline=$((SECONDS + rotation_timeout))
while (( SECONDS < deadline )); do
    assert_running_without_restarts
    alice_generation=$(state_value qkd-vpn-host5 generation)
    bob_generation=$(state_value qkd-vpn-host6 generation)
    if [ "$alice_generation" -ge "$target_generation" ] &&
        [ "$bob_generation" -ge "$target_generation" ]; then
        break
    fi
    sleep 3
done

current_generation=$(assert_peer_state_matches)
[ "$current_generation" -ge "$target_generation" ] ||
    fail "no committed rotation within ${rotation_timeout}s (still at generation $current_generation)"
assert_current_ike_sa qkd-vpn-host5 "$current_generation"
assert_current_ike_sa qkd-vpn-host6 "$current_generation"
echo "[OK] both peers committed generation $current_generation as a new IKE_SA"

echo
echo "=== 3) Sending real traffic and checking the outer interface ==="
cleanup
data_dev=$("$docker_bin" exec qkd-vpn-host5 sh -c \
    "ip route get 192.168.56.6 | sed -n 's/.* dev \\([^ ]*\\).*/\\1/p' | head -n1" |
    tr -d '\r')
[ -n "$data_dev" ] || fail "could not resolve Alice's VPN data interface"

"$docker_bin" exec qkd-vpn-host5 sh -c "
    timeout 7 tcpdump -Z root -U -Q out -i '$data_dev' -s 128 \
        -w '$capture' 'host 192.168.56.6 and (ip proto 50 or icmp)' \
        >/tmp/qkd-vpn-tcpdump.log 2>&1 &
    capture_pid=\$!
    sleep 1
    ping -c 3 -W 2 192.168.56.6 >/dev/null
    wait \$capture_pid || true
    test -s '$capture'
" || fail "ping or packet capture failed"

esp_packets=$("$docker_bin" exec qkd-vpn-host5 sh -c \
    "tcpdump -nn -r '$capture' 'ip proto 50' 2>/dev/null | wc -l" |
    tr -d '\r ')
plaintext_icmp=$("$docker_bin" exec qkd-vpn-host5 sh -c \
    "tcpdump -nn -r '$capture' 'icmp' 2>/dev/null | wc -l" |
    tr -d '\r ')
[ "$esp_packets" -gt 0 ] || fail "no outbound ESP traffic was captured"
[ "$plaintext_icmp" -eq 0 ] ||
    fail "$plaintext_icmp outbound plaintext ICMP packet(s) were visible"
echo "[OK] ping succeeded; outer traffic contained ESP and no plaintext ICMP"

cleanup
"$docker_bin" exec qkd-vpn-host5 sh -c "test ! -e '$capture'" ||
    fail "temporary capture cleanup failed"
trap - EXIT

echo
echo "VPN point-to-point scenario verified successfully."
