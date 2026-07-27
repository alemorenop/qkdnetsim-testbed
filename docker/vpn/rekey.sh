#!/bin/bash
# Fetches QKD key material from the local KMS via ETSI GS QKD 004
# (open_connect/get_key) and periodically rekeys the strongSwan PSK with
# it. ETSI 004 is session-based (a single Key_stream_ID, KSID, negotiated
# once) rather than per-key like ETSI 014, so only the initial KSID needs
# to be handed off between the two endpoints -- every subsequent rekey
# cycle calls get_key(KSID) independently on each side and both KMS
# instances hand back byte-identical key material (confirmed against the
# live KMS containers before writing this script).
set -uo pipefail

: "${ROLE:?}"
: "${PEER_IP:?}"
: "${KMS_IP:?}"
: "${OWN_APP_ID:?}"
: "${PEER_APP_ID:?}"
HANDOFF_PORT="${HANDOFF_PORT:-9999}"
KEY_CHUNK_SIZE_BITS="${KEY_CHUNK_SIZE_BITS:-256}"
REKEY_INTERVAL_S="${REKEY_INTERVAL_S:-60}"

STATE_DIR=/var/lib/qkd-vpn
mkdir -p "$STATE_DIR" /etc/ipsec.d
echo 0 > "$STATE_DIR/generation"

# --- One-time session setup (runs once, for the whole container lifetime) ---
#
# Only the KSID crosses this channel -- never key material. It stands in
# for what a production deployment would carry inside the IKEv2
# negotiation itself (see RFC 8784 Postquantum Preshared Keys for the
# closest standardized analogue); this testbed uses a plain one-shot TCP
# push/listen instead, since implementing that inside strongSwan itself is
# out of scope here.
if [ "$ROLE" = "client" ]; then
    echo "[rekey] requesting open_connect (master) from KMS ${KMS_IP}"
    resp=""
    for attempt in 1 2 3 4 5; do
        resp=$(curl -sf -m 10 -X POST -H "Content-Type: application/json" \
            -d "{\"Source\":\"${OWN_APP_ID}\",\"Destination\":\"${PEER_APP_ID}\",\"QoS\":{\"Key_chunk_size\":${KEY_CHUNK_SIZE_BITS}}}" \
            "http://${KMS_IP}/api/v1/keys/${PEER_APP_ID}/open_connect") && break
        echo "[rekey] open_connect attempt ${attempt} failed, retrying in 3s"
        sleep 3
    done
    ksid=$(echo "$resp" | jq -r '.Key_stream_ID // empty')
    if [ -z "$ksid" ]; then
        echo "[rekey] FATAL: open_connect did not return a Key_stream_ID (response: ${resp})"
        exit 1
    fi
    echo "[rekey] got KSID=${ksid}, pushing to peer ${PEER_IP}:${HANDOFF_PORT}"
    pushed=0
    for attempt in $(seq 1 15); do
        if python3 -c "
import socket
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.settimeout(3)
s.connect(('${PEER_IP}', ${HANDOFF_PORT}))
s.sendall(b'${ksid}')
s.close()
" 2>/dev/null; then
            pushed=1
            break
        fi
        echo "[rekey] KSID handoff attempt ${attempt} failed (peer not listening yet?), retrying in 2s"
        sleep 2
    done
    if [ "$pushed" != "1" ]; then
        echo "[rekey] FATAL: could not push KSID to peer after repeated attempts"
        exit 1
    fi
else
    echo "[rekey] waiting for KSID handoff on port ${HANDOFF_PORT}"
    ksid=$(python3 -c "
import socket
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind(('0.0.0.0', ${HANDOFF_PORT}))
s.listen(1)
conn, _ = s.accept()
data = conn.recv(256).decode().strip()
conn.close()
s.close()
print(data)
")
    if [ -z "$ksid" ]; then
        echo "[rekey] FATAL: did not receive a KSID over the handoff channel"
        exit 1
    fi
    echo "[rekey] received KSID=${ksid}, registering (open_connect replica) with KMS ${KMS_IP}"
    if ! curl -sf -m 10 -X POST -H "Content-Type: application/json" \
        -d "{\"Source\":\"${OWN_APP_ID}\",\"Destination\":\"${PEER_APP_ID}\",\"Key_stream_ID\":\"${ksid}\"}" \
        "http://${KMS_IP}/api/v1/keys/${PEER_APP_ID}/open_connect" >/dev/null; then
        echo "[rekey] FATAL: open_connect (replica) failed"
        exit 1
    fi
fi

echo "[rekey] session ready, KSID=${ksid}"

# --- Periodic loop: fetch fresh key material from the same KSID, rotate the IPsec SA ---
#
# Only the client side actively calls "ipsec up": in IKEv2 the responder
# (server) just needs a matching secret loaded and charon running to
# passively accept the negotiation, so it only needs to keep its own
# secret refreshed on the same cadence.
while true; do
    resp=$(curl -sf -m 10 -X POST -H "Content-Type: application/json" \
        -d "{\"Key_stream_ID\":\"${ksid}\"}" \
        "http://${KMS_IP}/api/v1/keys/${ksid}/get_key")
    key=$(echo "$resp" | jq -r '.Key_buffer // empty')

    if [ -z "$key" ]; then
        echo "[rekey] no key available yet from KMS, retrying next cycle"
        sleep "$REKEY_INTERVAL_S"
        continue
    fi

    prev_gen=$(cat "$STATE_DIR/generation")
    gen=$((prev_gen + 1))

    printf '%%any %%any : PSK "%s"\n' "$key" > /etc/ipsec.secrets
    ipsec rereadsecrets

    conn="qkd-${gen}"
    cat > "/etc/ipsec.d/${conn}.conf" << EOF
conn ${conn}
    also=%default
EOF
    ipsec reload

    if [ "$ROLE" = "client" ]; then
        # "ipsec up"'s own exit code only reflects whether it could talk to
        # charon, not whether the connection actually established (it
        # returns 0 even on "no config named X" or an auth failure) --
        # verify explicitly against "ipsec statusall" instead.
        up_ok=0
        for attempt in 1 2 3 4 5; do
            ipsec up "$conn" >/dev/null 2>&1 || true
            if ipsec statusall | grep -q "${conn}\[.*ESTABLISHED"; then
                up_ok=1
                break
            fi
            echo "[rekey] ipsec up ${conn} not yet established (attempt ${attempt}), retrying in 2s"
            sleep 2
        done
        if [ "$up_ok" = "1" ]; then
            echo "$gen" > "$STATE_DIR/generation"
            echo "[rekey] ESTABLISHED ${conn}"
            if [ "$prev_gen" != "0" ]; then
                ipsec down "qkd-${prev_gen}" >/dev/null 2>&1 || true
                rm -f "/etc/ipsec.d/qkd-${prev_gen}.conf"
                ipsec reload
            fi
        else
            echo "[rekey] FAILED to establish ${conn} after 5 attempts, keeping previous session (if any)"
            rm -f "/etc/ipsec.d/${conn}.conf"
            ipsec reload
        fi
    else
        # Server: just keep the secret/conn ready; confirm the peer
        # actually completed a handshake using it before adopting the new
        # generation as current and cleaning up the old one.
        sleep 3
        if ipsec statusall | grep -q "${conn}\[.*ESTABLISHED"; then
            echo "$gen" > "$STATE_DIR/generation"
            echo "[rekey] ESTABLISHED ${conn}"
            if [ "$prev_gen" != "0" ]; then
                rm -f "/etc/ipsec.d/qkd-${prev_gen}.conf"
                ipsec reload
            fi
        else
            echo "[rekey] ${conn} not yet established on peer side, keeping config for next cycle"
        fi
    fi

    sleep "$REKEY_INTERVAL_S"
done
