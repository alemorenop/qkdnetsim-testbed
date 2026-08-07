#!/usr/bin/env python3
"""ETSI QKD 004/014 consumer and transactional strongSwan PSK rotator."""

from __future__ import annotations

import hashlib
import base64
import http.client
import http.server
import json
import os
import pathlib
import re
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable


ROLE = os.environ["VPN_ROLE"].lower()
QKD_INTERFACE = os.getenv("QKD_INTERFACE", "004")
OWN_IP = os.environ["OWN_IP"]
PEER_IP = os.environ["PEER_IP"]
KMS_IP = os.environ["KMS_IP"]
OWN_APP_ID = os.environ["OWN_APP_ID"]
PEER_APP_ID = os.environ["PEER_APP_ID"]
CONTROL_PORT = int(os.getenv("CONTROL_PORT", "9090"))
KEY_CHUNK_SIZE_BITS = int(os.getenv("KEY_CHUNK_SIZE_BITS", "256"))
REKEY_INTERVAL_S = int(os.getenv("REKEY_INTERVAL_S", "60"))
RETRY_INTERVAL_S = float(os.getenv("RETRY_INTERVAL_S", "2"))
RETRY_LIMIT = int(os.getenv("RETRY_LIMIT", "60"))

RUN_DIR = pathlib.Path("/run/qkd-vpn")
STATE_FILE = RUN_DIR / "state.json"
SECRETS_FILE = pathlib.Path("/etc/ipsec.secrets")
CONNECTION_DIR = pathlib.Path("/etc/ipsec.d")


def log(message: str) -> None:
    print(f"[vpn-{ROLE}] {message}", flush=True)


def atomic_write(path: pathlib.Path, content: str, mode: int = 0o600) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    os.chmod(temporary, mode)
    os.replace(temporary, path)


def run_ipsec(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["ipsec", *args],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    output = result.stdout.strip()
    if output and (result.returncode or args[0] in {"up", "down"}):
        log(output)
    if check and result.returncode:
        raise RuntimeError(
            f"ipsec {' '.join(args)} failed with status {result.returncode}"
        )
    return result


def retry(operation: Callable[[], Any], description: str) -> Any:
    last_error: Exception | None = None
    for attempt in range(1, RETRY_LIMIT + 1):
        try:
            return operation()
        except Exception as error:
            last_error = error
            if attempt == 1 or attempt % 10 == 0:
                log(f"{description} not ready (attempt {attempt}): {error}")
            time.sleep(RETRY_INTERVAL_S)
    raise RuntimeError(f"{description} failed after {RETRY_LIMIT} attempts") from last_error


class PersistentKmsClient:
    """Keep the ns-3 TCP socket alive across ETSI API requests."""

    def __init__(self, host: str) -> None:
        self.host = host
        self.connection: http.client.HTTPConnection | None = None
        self.lock = threading.Lock()

    def reset(self) -> None:
        if self.connection is not None:
            try:
                self.connection.close()
            except OSError:
                pass
        self.connection = None

    def request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        body = (
            json.dumps(payload, separators=(",", ":"))
            if payload is not None
            else None
        )
        headers = {
            "Accept": "application/json",
            "Connection": "keep-alive",
            "User-Agent": f"QKDVPN-{QKD_INTERFACE}-{ROLE}",
        }
        if payload is not None:
            headers["Content-Type"] = "application/json"
        with self.lock:
            if self.connection is None:
                self.connection = http.client.HTTPConnection(
                    self.host, 80, timeout=10
                )
            try:
                self.connection.request(method, path, body=body, headers=headers)
                response = self.connection.getresponse()
                response_body = response.read()
                if response.status != 200:
                    raise RuntimeError(
                        f"KMS HTTP {response.status}: "
                        f"{response_body.decode(errors='replace')}"
                    )
                if not response_body:
                    return {}
                return json.loads(response_body.decode())
            except (OSError, http.client.HTTPException):
                self.reset()
                raise

    def post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self.request("POST", path, payload)

    def get(self, path: str) -> dict[str, Any]:
        return self.request("GET", path)


KMS = PersistentKmsClient(KMS_IP)


@dataclass(frozen=True)
class KeyVersion:
    generation: int
    index: int | None
    key_id: str | None
    value: str
    digest: str


def parse_key_response(response: dict[str, Any], generation: int) -> KeyVersion:
    if "index" not in response or not response.get("Key_buffer"):
        raise ValueError("KMS response has no index or Key_buffer")
    value = str(response["Key_buffer"])
    actual_bits = len(value.encode()) * 8
    if actual_bits != KEY_CHUNK_SIZE_BITS:
        raise ValueError(
            f"ETSI 004 returned {actual_bits} bits, "
            f"expected {KEY_CHUNK_SIZE_BITS}"
        )
    return KeyVersion(
        generation=generation,
        index=int(response["index"]),
        key_id=None,
        value=value,
        digest=hashlib.sha256(value.encode()).hexdigest(),
    )


def parse_etsi014_key(
    response: dict[str, Any], generation: int
) -> KeyVersion:
    keys = response.get("keys")
    if not isinstance(keys, list) or len(keys) != 1:
        raise ValueError("ETSI 014 response does not contain exactly one key")
    key_id = str(keys[0].get("key_ID", ""))
    encoded = str(keys[0].get("key", "")).strip()
    if not key_id or not encoded:
        raise ValueError("ETSI 014 response has no key_ID or key")
    material = base64.b64decode(encoded, validate=True)
    if len(material) * 8 != KEY_CHUNK_SIZE_BITS:
        raise ValueError(
            f"ETSI 014 returned {len(material) * 8} bits, "
            f"expected {KEY_CHUNK_SIZE_BITS}"
        )
    return KeyVersion(
        generation=generation,
        index=None,
        key_id=key_id,
        value=f"0x{material.hex()}",
        digest=hashlib.sha256(material).hexdigest(),
    )


def open_master_session() -> str:
    response = KMS.post(
        f"/api/v1/keys/{PEER_APP_ID}/open_connect",
        {
            "Source": OWN_APP_ID,
            "Destination": PEER_APP_ID,
            "QoS": {"Key_chunk_size": KEY_CHUNK_SIZE_BITS},
        },
    )
    ksid = str(response.get("Key_stream_ID", ""))
    if not ksid:
        raise ValueError("open_connect returned no Key_stream_ID")
    return ksid


def register_replica_session(ksid: str) -> None:
    KMS.post(
        f"/api/v1/keys/{PEER_APP_ID}/open_connect",
        {
            "Source": OWN_APP_ID,
            "Destination": PEER_APP_ID,
            "Key_stream_ID": ksid,
        },
    )


def fetch_etsi004_key(ksid: str, generation: int) -> KeyVersion:
    response = KMS.post(
        f"/api/v1/keys/{ksid}/get_key",
        {"Key_stream_ID": ksid},
    )
    return parse_key_response(response, generation)


def fetch_etsi014_enc_key(generation: int) -> KeyVersion:
    response = KMS.get(
        f"/api/v1/keys/{PEER_APP_ID}/enc_keys"
        f"/number/1/size/{KEY_CHUNK_SIZE_BITS}"
    )
    return parse_etsi014_key(response, generation)


def fetch_etsi014_dec_key(key_id: str, generation: int) -> KeyVersion:
    response = KMS.post(
        f"/api/v1/keys/{PEER_APP_ID}/dec_keys",
        {"key_IDs": [{"key_ID": key_id}]},
    )
    key = parse_etsi014_key(response, generation)
    if key.key_id != key_id:
        raise ValueError(
            f"ETSI 014 returned key_ID={key.key_id}, expected {key_id}"
        )
    return key


def escape_ipsec_secret(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def install_secret(key: KeyVersion | None) -> None:
    content = ""
    if key is not None:
        if key.value.startswith("0x"):
            content = f"%any %any : PSK {key.value}\n"
        else:
            content = f'%any %any : PSK "{escape_ipsec_secret(key.value)}"\n'
    atomic_write(SECRETS_FILE, content, 0o600)
    run_ipsec("rereadsecrets")


def key_reference(key: KeyVersion) -> str:
    if key.key_id is not None:
        return f"key_ID={key.key_id}"
    return f"index={key.index}"


def connection_path(generation: int) -> pathlib.Path:
    return CONNECTION_DIR / f"qkd-{generation}.conf"


def install_connection(generation: int) -> None:
    atomic_write(
        connection_path(generation),
        f"conn qkd-{generation}\n    also=%default\n",
        0o600,
    )
    run_ipsec("reload")


def remove_connection(generation: int) -> None:
    try:
        connection_path(generation).unlink()
    except FileNotFoundError:
        return
    run_ipsec("reload")


def tunnel_is_up(generation: int) -> bool:
    result = run_ipsec("statusall", check=False)
    return bool(
        re.search(
            rf"^\s*qkd-{generation}\[\d+\]: ESTABLISHED",
            result.stdout,
            flags=re.MULTILINE,
        )
    )


class SharedState:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.ksid: str | None = None
        self.session_ready = False
        self.current: KeyVersion | None = None
        self.pending: KeyVersion | None = None
        self.tunnel_up = False
        self.last_error: str | None = None

    def publish(self) -> None:
        with self.lock:
            state = {
                "role": ROLE,
                "qkd_interface": QKD_INTERFACE,
                "ksid": self.ksid,
                "generation": self.current.generation if self.current else 0,
                "key_index": self.current.index if self.current else None,
                "key_id": self.current.key_id if self.current else None,
                "key_fingerprint": (
                    self.current.digest[:16] if self.current else None
                ),
                "pending_generation": (
                    self.pending.generation if self.pending else None
                ),
                "tunnel_up": self.tunnel_up,
                "last_error": self.last_error,
            }
        atomic_write(STATE_FILE, json.dumps(state, sort_keys=True) + "\n", 0o600)


STATE = SharedState()


def peer_request(
    path: str,
    payload: dict[str, Any] | None = None,
    timeout: float = 130,
) -> dict[str, Any]:
    data = None
    method = "GET"
    headers = {"Accept": "application/json", "Connection": "close"}
    if payload is not None:
        method = "POST"
        data = json.dumps(payload, separators=(",", ":")).encode()
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        f"http://{PEER_IP}:{CONTROL_PORT}{path}",
        data=data,
        headers=headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode() or "{}")
    except urllib.error.HTTPError as error:
        body = error.read().decode(errors="replace")
        raise RuntimeError(f"peer HTTP {error.code}: {body}") from error


def prepare_bob(
    generation: int, key_id: str | None = None
) -> KeyVersion:
    with STATE.lock:
        if STATE.pending and STATE.pending.generation == generation:
            return STATE.pending
        if STATE.current and STATE.current.generation == generation:
            return STATE.current
        expected = (STATE.current.generation if STATE.current else 0) + 1
        if generation != expected:
            raise ValueError(
                f"generation {generation} is invalid; expected {expected}"
            )
        if not STATE.session_ready:
            raise RuntimeError(f"ETSI {QKD_INTERFACE} session is not ready")

        if QKD_INTERFACE == "004":
            if not STATE.ksid:
                raise RuntimeError("ETSI 004 session has no KSID")
            key = retry(
                lambda: fetch_etsi004_key(STATE.ksid or "", generation),
                f"get_key generation {generation}",
            )
        else:
            if not key_id:
                raise ValueError("ETSI 014 prepare has no key_ID")
            key = retry(
                lambda: fetch_etsi014_dec_key(key_id, generation),
                f"dec_keys generation {generation}",
            )
        install_connection(generation)
        install_secret(key)
        STATE.pending = key
        STATE.last_error = None
        STATE.publish()
        log(
            f"PREPARED generation={generation} {key_reference(key)} "
            f"fingerprint={key.digest[:16]}"
        )
        return key


def commit_bob(generation: int) -> None:
    with STATE.lock:
        if STATE.current and STATE.current.generation == generation:
            STATE.tunnel_up = tunnel_is_up(generation)
            STATE.publish()
            return
        if not STATE.pending or STATE.pending.generation != generation:
            raise ValueError(f"generation {generation} is not pending")
        if not tunnel_is_up(generation):
            raise RuntimeError(f"qkd-{generation} is not established")

        previous = STATE.current
        STATE.current = STATE.pending
        STATE.pending = None
        STATE.tunnel_up = True
        STATE.last_error = None
        STATE.publish()
        if previous:
            remove_connection(previous.generation)
        log(f"COMMITTED generation={generation}")


def activate_bob(generation: int) -> None:
    """Leave only the pending connection eligible for the next IKE_SA."""
    with STATE.lock:
        if not STATE.pending or STATE.pending.generation != generation:
            raise ValueError(f"generation {generation} is not pending")
        if STATE.current:
            remove_connection(STATE.current.generation)
        STATE.publish()
        log(f"ACTIVATED generation={generation} for IKE cutover")


def rollback_bob(generation: int) -> None:
    with STATE.lock:
        if STATE.pending and STATE.pending.generation == generation:
            remove_connection(generation)
            STATE.pending = None
        if STATE.current:
            install_connection(STATE.current.generation)
        install_secret(STATE.current)
        STATE.tunnel_up = bool(
            STATE.current and tunnel_is_up(STATE.current.generation)
        )
        STATE.publish()
        log(f"ROLLED BACK generation={generation}")


class ControlHandler(http.server.BaseHTTPRequestHandler):
    server_version = "QKDVPN/1.0"

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_payload(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        return json.loads(self.rfile.read(length) or b"{}")

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/health":
            self.send_json(200, {"ready": True})
            return
        if parsed.path == "/status":
            generation = int(
                urllib.parse.parse_qs(parsed.query).get("generation", ["0"])[0]
            )
            self.send_json(
                200,
                {
                    "generation": generation,
                    "established": tunnel_is_up(generation),
                },
            )
            return
        self.send_json(404, {"error": "not found"})

    def do_POST(self) -> None:
        try:
            payload = self.read_payload()
            if self.path == "/session":
                interface = str(payload.get("qkd_interface", ""))
                if interface != QKD_INTERFACE:
                    raise ValueError(
                        f"peer requested ETSI {interface}, "
                        f"this endpoint uses ETSI {QKD_INTERFACE}"
                    )
                ksid = str(payload.get("ksid", ""))
                with STATE.lock:
                    if QKD_INTERFACE == "004":
                        if not ksid:
                            raise ValueError("ETSI 004 session has no KSID")
                        if STATE.ksid and STATE.ksid != ksid:
                            raise RuntimeError(
                                "a different KSID is already registered"
                            )
                        if not STATE.ksid:
                            retry(
                                lambda: register_replica_session(ksid),
                                "replica open_connect",
                            )
                            STATE.ksid = ksid
                            log(f"REGISTERED KSID={ksid}")
                    STATE.session_ready = True
                    STATE.publish()
                response = {
                    "qkd_interface": QKD_INTERFACE,
                    "ksid": STATE.ksid,
                }
            elif self.path == "/prepare":
                key = prepare_bob(
                    int(payload["generation"]),
                    str(payload.get("key_id", "")) or None,
                )
                response = {
                    "generation": key.generation,
                    "index": key.index,
                    "key_id": key.key_id,
                    "digest": key.digest,
                }
            elif self.path == "/commit":
                generation = int(payload["generation"])
                commit_bob(generation)
                response = {"generation": generation, "committed": True}
            elif self.path == "/activate":
                generation = int(payload["generation"])
                activate_bob(generation)
                response = {"generation": generation, "activated": True}
            elif self.path == "/rollback":
                generation = int(payload["generation"])
                rollback_bob(generation)
                response = {"generation": generation, "rolled_back": True}
            elif self.path == "/restored":
                with STATE.lock:
                    STATE.tunnel_up = bool(
                        STATE.current
                        and tunnel_is_up(STATE.current.generation)
                    )
                    STATE.publish()
                response = {"restored": STATE.tunnel_up}
            else:
                self.send_json(404, {"error": "not found"})
                return
            self.send_json(200, response)
        except Exception as error:
            log(f"control request {self.path} failed: {error}")
            self.send_json(503, {"error": str(error)})


def initiate(generation: int) -> None:
    for attempt in range(1, 6):
        run_ipsec("up", f"qkd-{generation}", check=False)
        if tunnel_is_up(generation):
            return
        log(
            f"qkd-{generation} is not established "
            f"(attempt {attempt}/5)"
        )
        time.sleep(2)
    raise RuntimeError(f"failed to establish qkd-{generation}")


def require_peer_tunnel(generation: int) -> dict[str, Any]:
    response = peer_request(f"/status?generation={generation}")
    if not response.get("established"):
        raise RuntimeError("peer has not established the new IKE_SA")
    return response


def rollback_alice(failed: KeyVersion, previous: KeyVersion | None) -> None:
    try:
        retry(
            lambda: peer_request(
                "/rollback", {"generation": failed.generation}
            ),
            f"peer rollback generation {failed.generation}",
        )
    finally:
        remove_connection(failed.generation)
        if previous:
            install_connection(previous.generation)
        install_secret(previous)
        with STATE.lock:
            STATE.pending = None
            STATE.current = previous
            STATE.tunnel_up = False
            STATE.publish()

    if previous is not None:
        initiate(previous.generation)
        with STATE.lock:
            STATE.tunnel_up = True
            STATE.publish()
        retry(
            lambda: peer_request("/restored", {}),
            "peer rollback confirmation",
        )
        log(f"RESTORED generation={previous.generation}")


def rotate_alice() -> None:
    with STATE.lock:
        previous = STATE.current
        generation = (previous.generation if previous else 0) + 1
        ksid = STATE.ksid
        session_ready = STATE.session_ready
    if not session_ready:
        raise RuntimeError(f"ETSI {QKD_INTERFACE} session is not ready")

    if QKD_INTERFACE == "004":
        if not ksid:
            raise RuntimeError("ETSI 004 session has no KSID")
        key = retry(
            lambda: fetch_etsi004_key(ksid, generation),
            f"get_key generation {generation}",
        )
    else:
        key = retry(
            lambda: fetch_etsi014_enc_key(generation),
            f"enc_keys generation {generation}",
        )

    prepare_payload: dict[str, Any] = {"generation": generation}
    if key.key_id is not None:
        prepare_payload["key_id"] = key.key_id
    peer = retry(
        lambda: peer_request("/prepare", prepare_payload),
        f"peer prepare generation {generation}",
    )
    peer_reference_matches = (
        str(peer.get("key_id", "")) == key.key_id
        if key.key_id is not None
        else int(peer.get("index", -1)) == key.index
    )
    if not peer_reference_matches or peer.get("digest") != key.digest:
        retry(
            lambda: peer_request("/rollback", {"generation": generation}),
            f"peer rollback mismatched generation {generation}",
        )
        raise RuntimeError(
            f"KMS streams diverged at generation {generation}: "
            f"local {key_reference(key)}, "
            f"peer index={peer.get('index')} key_ID={peer.get('key_id')}"
        )

    install_connection(generation)
    install_secret(key)
    with STATE.lock:
        STATE.pending = key
        STATE.tunnel_up = bool(previous and tunnel_is_up(previous.generation))
        STATE.last_error = None
    STATE.publish()
    log(
        f"PREPARED generation={generation} {key_reference(key)} "
        f"fingerprint={key.digest[:16]}"
    )

    try:
        if previous is not None:
            retry(
                lambda: peer_request(
                    "/activate", {"generation": generation}
                ),
                f"peer activate generation {generation}",
            )
            run_ipsec("down", f"qkd-{previous.generation}", check=False)
            remove_connection(previous.generation)
            with STATE.lock:
                STATE.tunnel_up = False
                STATE.publish()

        initiate(generation)
        retry(
            lambda: require_peer_tunnel(generation),
            f"peer qkd-{generation} status",
        )
        retry(
            lambda: peer_request("/commit", {"generation": generation}),
            f"peer commit generation {generation}",
        )
    except Exception:
        log(f"cutover to generation={generation} failed; rolling back")
        rollback_alice(key, previous)
        raise

    with STATE.lock:
        STATE.current = key
        STATE.pending = None
        STATE.tunnel_up = True
        STATE.last_error = None
        STATE.publish()
    if previous:
        remove_connection(previous.generation)
    log(
        f"COMMITTED generation={generation} {key_reference(key)}; "
        "new IKE_SA authenticated with the QKD PSK"
    )


def run_bob() -> None:
    STATE.publish()
    server = http.server.ThreadingHTTPServer(
        ("0.0.0.0", CONTROL_PORT), ControlHandler
    )
    log(f"coordination API listening on {OWN_IP}:{CONTROL_PORT}")
    server.serve_forever()


def run_alice() -> None:
    STATE.publish()
    ksid: str | None = None
    if QKD_INTERFACE == "004":
        ksid = retry(open_master_session, "master open_connect")
    response = retry(
        lambda: peer_request(
            "/session",
            {
                "qkd_interface": QKD_INTERFACE,
                "ksid": ksid,
            },
        ),
        f"ETSI {QKD_INTERFACE} session setup",
    )
    if response.get("qkd_interface") != QKD_INTERFACE:
        raise RuntimeError("peer acknowledged a different QKD interface")
    if QKD_INTERFACE == "004" and response.get("ksid") != ksid:
        raise RuntimeError("peer acknowledged a different KSID")
    with STATE.lock:
        STATE.ksid = ksid
        STATE.session_ready = True
        STATE.publish()
    if ksid:
        log(f"ETSI 004 SESSION READY KSID={ksid}")
    else:
        log("ETSI 014 SESSION READY")

    while True:
        try:
            rotate_alice()
        except Exception as error:
            with STATE.lock:
                STATE.last_error = str(error)
                STATE.publish()
            log(f"rotation failed: {error}")
        time.sleep(REKEY_INTERVAL_S)


def main() -> None:
    if ROLE not in {"alice", "bob"}:
        raise SystemExit("VPN_ROLE must be alice or bob")
    if QKD_INTERFACE not in {"004", "014"}:
        raise SystemExit("QKD_INTERFACE must be 004 or 014")
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    CONNECTION_DIR.mkdir(parents=True, exist_ok=True)
    if ROLE == "bob":
        run_bob()
    else:
        run_alice()


if __name__ == "__main__":
    main()
