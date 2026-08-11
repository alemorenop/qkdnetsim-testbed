# QKDNetSim Testbed

This repository is a testbed built on top of **QKDNetSim** (see the original project documentation below). It provides real-network emulation scenarios in which every node or role runs in its own Docker container and communicates over actual network interfaces instead of being simulated inside a single ns-3 process.

## Distance-aware QKD link budget

All four testbed scenarios derive the average secret-key generation rate of
each physical QKD link from its fiber length and attenuation. QKDNetSim
abstracts the quantum channel, so the calculation is performed before the
post-processing application starts and its result is assigned to the
`QKDPostprocessingApplication::KeyRate` attribute:

```text
totalLossDb = fiberLengthKm * fiberAttenuationDbPerKm
keyRateBps  = zeroLossKeyRateBps * 10^(-totalLossDb / 10)
```

The implementation uses general link-budget parameter names. Its current
default zero-loss rate is `17,094,000 bit/s`, calibrated from the equipment
and processing factors used to estimate the reference network links:

```text
1e9 * 0.42 * 0.5 * 0.25 * 0.88 * 0.37 = 17,094,000 bit/s
```

The model and the 85 km/100 km reference links come from:

> Mehic, M., Dervisevic, E., Fazio, P. and Voznak, M., 2025. *Virtual Quantum Key Distribution Network Ecosystem: The National Czech QKD Network*. IEEE Network, 39(3), pp.173-179. https://doi.org/10.1109/MNET.2025.3540705

With the published link parameters, the implementation reproduces the paper's
reported estimates:

| Fiber length | Attenuation | Total loss | Calculated `keyRate` |
|---:|---:|---:|---:|
| 85 km | 0.2417 dB/km | 20.5445 dB | 150,797 bit/s |
| 100 km | 0.2303 dB/km | 23.03 dB | 85,083 bit/s |

The paper's prose lists a 20% detector efficiency, while its reported rates
are reproduced by the 25% factor shown above. The implementation follows the
published rates and exposes `QKD_ZERO_LOSS_KEY_RATE_BPS` so experiments can
select a different equipment model explicitly instead of hiding that
assumption.

The two post-processing processes at the ends of one QKD link must receive
identical parameters. Compose enforces this automatically. Point-to-point and
point-to-point VPN scenarios use:

- `QKD_FIBER_LENGTH_KM` (default `85`)
- `QKD_FIBER_ATTENUATION_DB_PER_KM` (default `0.2417`)
- `QKD_ZERO_LOSS_KEY_RATE_BPS` (default `17094000`)

For example, run any point-to-point variant with the reference 100 km profile:

```bash
QKD_FIBER_LENGTH_KM=100 \
QKD_FIBER_ATTENUATION_DB_PER_KM=0.2303 \
docker compose -f docker/docker-compose.vpn.yml up -d --build
```

Key-relay and key-relay VPN scenarios configure both physical QKD segments
independently:

- `QKD_ALICE_RELAY_FIBER_LENGTH_KM` and
  `QKD_ALICE_RELAY_ATTENUATION_DB_PER_KM` (defaults: 85 km, 0.2417 dB/km)
- `QKD_RELAY_BOB_FIBER_LENGTH_KM` and
  `QKD_RELAY_BOB_ATTENUATION_DB_PER_KM` (defaults: 100 km, 0.2303 dB/km)
- the common `QKD_ZERO_LOSS_KEY_RATE_BPS`

Each post-processing process prints a `[QKD_LINK_BUDGET]` line containing the
fiber length, attenuation, total loss and resulting rate. This makes the
physical assumptions recorded in the container logs and allows a verifier or
experiment harness to confirm that both ends used the same link profile.

The original `examples_qkdnetsim_etsi_combined_input` scenario is not used by
the four Docker scenario families described below. It was nevertheless updated
because it is the generic topology-from-JSON entry point and already exposes
`srcDstDistance`; leaving it unchanged would make distance affect the Docker
links while being ignored by configurable JSON topologies. It now uses the
same calculation when a `qkd_links` entry contains
`fiberAttenuationDbPerKm`. In that JSON schema, `srcDstDistance` remains in
meters and `zeroLossKeyRateBps` is optional:

```json
{
  "srcDstDistance": 100000,
  "fiberAttenuationDbPerKm": 0.2303,
  "zeroLossKeyRateBps": 17094000
}
```

Old web-generated JSON files containing only `keyRate` remain supported, but
that value is treated as a legacy explicit rate and no distance calculation is
performed. The link-budget calculation changes only QKD key generation; it
does not yet emulate classical propagation delay. Classical delay will be
introduced separately with Linux `tc netem` or COREEMU so the optical model
and classical-network model can be varied independently.

## Scenarios

The diagrams and tables use `H1`–`H9` as compact deployment identifiers. Each
identifier represents a Docker container, but its architectural role may be
an ns-3 node, a KMS node, a synthetic QKD application, or a native Linux VPN
endpoint. Internal names are role-based as well: for example, Compose uses
`kms_trusted`, the corresponding container is `qkd-relay-kms-trusted`, the
binary is `relay_kms_trusted`, and its log marker is
`[RELAY_KMS_TRUSTED]`.

### 1. Point-to-point — direct Alice–Bob link (`examples/point-to-point/`)

Container-based adaptation of the network emulation experiment shown in Fig. 3 of:

> Mehic, M., Dervisevic, E., Burdiak, P., Lipovac, V., Fazio, P. and Voznak, M., 2024. *Emulation of quantum key distribution networks*. IEEE Network, 39(1), pp.116-123. https://doi.org/10.1109/MNET.2024.3398404

The paper simulates both ends of every link inside a single ns-3 process. In this testbed, every role runs in a separate Docker container. Each ns-3 `EmuFdNetDevice` opens an `AF_PACKET` raw socket bound to a Linux `ethX` interface backed by a Docker veth; this veth is the equivalent of the paper diagrams' “Real Net Device”. Native VPN endpoints do not use ns-3 or `EmuFdNetDevice`: strongSwan and the VPN consumer use the Linux network stack and Docker veth directly. The logical scenario is the same, but it is deployed as six independent nodes.

The six containers run Alice/Bob post-processing, Alice/Bob KMS, and Alice/Bob ETSI 014 applications. Alice and Bob communicate directly, without an intermediate node.

![Point-to-point QKDNetSim testbed architecture](diagrams/point-to-point.svg)

| Node ID | Container role | IPs (network ↔ peer) |
|---|---|---|
| H1 | post-processing Alice (ns-3 node) | 192.168.11.1 (↔H2), 192.168.13.1 (↔H3) |
| H2 | post-processing Bob (ns-3 node) | 192.168.11.2 (↔H1), 192.168.24.2 (↔H4) |
| H3 | KMS Alice (ns-3 node) | 192.168.13.3 (↔H1), 192.168.34.3 (↔H4), 192.168.35.3 (↔H5) |
| H4 | KMS Bob (ns-3 node) | 192.168.24.4 (↔H2), 192.168.34.4 (↔H3), 192.168.46.4 (↔H6) |
| H5 | ETSI 014 Alice (ns-3 application node) | 192.168.35.5 (↔H3), 192.168.56.5 (↔H6) |
| H6 | ETSI 014 Bob (ns-3 application node) | 192.168.46.6 (↔H4), 192.168.56.6 (↔H5) |

Quick start:

```bash
cd contrib/qkdnetsim-testbed
docker build -t qkdnetsim-6vm-emulation:latest -f docker/Dockerfile .
docker compose -f docker/docker-compose.yml up -d
./docker/verify.sh
```

Container creation order does not affect the scenario. Readiness traces from the PP, KMS, and ETSI 014 applications ensure that listeners start before their clients. The dependency chain is H4 → H3/H2/H6 → H1/H5. The first PP link may take about one minute to converge because of ns-3 TCP backoff; no container restart is required.

`verify.sh` requires all six processes to be running with zero restarts, checks that both KMS instances served the same `keyId`, and temporarily samples port 8081 to verify that application traffic exists and the body is no longer visible as plaintext. The H5 and H6 application nodes use OTP with `useCrypto=1`.

### 2. Key relay — intermediate node without a direct Alice–Bob link (`examples/key-relay/`)

In this scenario, Alice and Bob do not have a direct QKD link. They communicate through a trusted intermediate node with its own KMS. The node relays keys using the functionality already provided by `QKDKeyManagerSystemApplication` (`RelayConsumption`, `WasteRelay`, `ConfigureRSBuffers`, and related methods), together with hop-by-hop OTP encryption through `skey_create`. Alice and Bob ultimately share actual key material without either endpoint seeing the raw key from the other QKD segment. End-to-end encrypted traffic flows between the H8 and H9 application nodes and is verified with temporary `tcpdump` samples.

Topology (9 Docker containers):

![Trusted-node key-relay QKDNetSim testbed architecture](diagrams/key-relay.svg)

| Node ID | Container role | IPs (network ↔ peer) |
|---|---|---|
| H1 | PP Alice (ns-3 node) | 192.168.111.1 (↔H2), 192.168.112.1 (↔H5) |
| H2 | PP Relay-A (ns-3 node) | 192.168.111.2 (↔H1), 192.168.113.2 (↔H6) |
| H3 | PP Relay-B (ns-3 node) | 192.168.114.3 (↔H4), 192.168.115.3 (↔H6) |
| H4 | PP Bob (ns-3 node) | 192.168.114.4 (↔H3), 192.168.116.4 (↔H7) |
| H5 | **KMS Alice** (ns-3 node) | 192.168.112.5 (↔H1), 192.168.117.5 (↔H6), 192.168.119.5 (↔H8) |
| H6 | **KMS Relay** (trusted intermediate ns-3 node) | 192.168.113.6 (↔H2), 192.168.115.6 (↔H3), 192.168.117.6 (↔H5), 192.168.118.6 (↔H7) |
| H7 | **KMS Bob** (ns-3 node) | 192.168.116.7 (↔H4), 192.168.118.7 (↔H6), 192.168.120.7 (↔H9) |
| H8 | ETSI 014 Alice (ns-3 application node) | 192.168.119.8 (↔H5), 192.168.121.8 (↔H9) |
| H9 | ETSI 014 Bob (ns-3 application node) | 192.168.120.9 (↔H7), 192.168.121.9 (↔H8) |

Start the scenario:

```bash
cd contrib/qkdnetsim-testbed
docker build -t qkdnetsim-6vm-emulation:latest -f docker/Dockerfile .
docker compose -f docker/docker-compose.key-relay.yml up -d
```

`depends_on` and the health checks start containers according to the readiness of their actual listeners. PP links use TCP reconnection and may take about one minute to converge because of ns-3 TCP backoff. Do not restart containers during this window.

#### End-to-end verification

Seeing all nine containers in the `Up` state only proves that their processes have not exited. To check the expected activity for every container and sample the relevant traffic, run:

```bash
./docker/verify-key-relay.sh [sample_seconds]   # default: 10s
```

The verifier reports whether all nine containers produce the expected role-specific activity (`Key delivered`, `stores key`, `serves key`, or `GET_KEY request`), confirms that the H5 and H7 KMS nodes serve the same `keyId` to the applications, and temporarily samples the four relevant links. Packet captures remain under `/tmp` inside the containers and are neither copied to nor stored in the repository. Traffic between the H8 and H9 application nodes confirms the final result between the ETSI 014 applications.

#### Library issues fixed for this scenario

- **TCP sockets and startup ordering.** PP, KMS, and ETSI listeners publish readiness traces used by the health checks. `QKDPostprocessingApplication` preserves its socket during `SYN_SENT`, allows TCP backoff to proceed, and reconnects after a failure or closure. Periodically destroying a socket during the handshake caused `TcpSocketBase` assertions when delayed packets arrived.
- **Incorrect framing of the post-processing TCP stream.** The receiver assumed that every `Recv()` call contained exactly one sent packet and constructed a `std::string` without an explicit length. TCP fragmentation or coalescing consequently caused `JSON parse error` and terminated the process. The receiver now keeps a buffer per connection, extracts frames delimited by `;`, preserves incomplete fragments, and discards malformed JSON without aborting the container.
- **Empty key material in `skey_create`.** `mergedKey` was consumed while splitting the local response and was then reused empty for the relay operation. An exact copy is now preserved, its size is validated, and the key is encrypted hop by hop. The H5 and H7 KMS nodes serve the same 6400-bit `keyId`.
- **Uninitialized ETSI 014 state.** `m_isSignalingConnectedToApp` and `m_isDataConnectedToApp` were read before initialization, causing some runs to skip socket creation. Both now start explicitly as `false`.
- **Encryption disabled in the scenario.** The H8 and H9 application nodes used `useCrypto=0`, so traffic described as OTP-protected was actually plaintext. The key-relay scenario now uses `useCrypto=1`.
- **Uninitialized bytes leaked into the wire (applies to both scenarios).** `QKDAppHeader::GetSerializedSize()` reserves a fixed 32 bytes for the authentication tag field, but `SetAuthTag()` wrote exactly `value.size()` bytes with no padding. With authentication disabled (`authenticationType=0`, the default in both scenarios), `QKDEncryptor::Authenticate()` returns an empty string, so the remaining 32 bytes of that reservation were left untouched — whatever the ns-3 `Buffer` previously held (in one observed case, a literal fragment of an unrelated HTTP response, `"Vary: Accept-Encoding, Cookie"`) was sent on the wire as-is. `SetAuthTag()` now pads with leading `'0'` characters the same way `SetEncryptionKeyId()`/`SetAuthenticationKeyId()` already did.
- **Bit accounting stuck in READY.** In `Relay()`, `StoreKey(key,true)` followed by `MarkKey(id,INIT)` has a net-zero effect on `m_currentKeyBit`. As a result, `CheckState()` stopped reflecting actual relay-buffer depletion after the threshold was crossed for the first time. `SBufferClientCheck` now also checks `GetSBitCount()`, which is the current count rather than historical accumulated state.
- **`skey_create` assumed one exact-size key.** Hop-by-hop encryption material was requested as a single key with an exact bit length, while the buffer only contained default 2048-bit keys. It now uses the same multi-key merge pattern through `GetTransformCandidate` that is already used elsewhere in the implementation.
- **Unbalanced HTTP request bookkeeping.** Multi-hop forwarding of `skey_create` sent the request to the next hop without adding it to `m_httpRequestsQueryKMS`. Processing the response then attempted to `pop()` an empty queue and terminated the process with `NS_FATAL_ERROR("HTTP query for this KMS is empty!")`.

**Timing requirement:** keep `--appStartTime=20` for the H8 and H9 application nodes (see the comments in `docker-compose.key-relay.yml`). `QKDApp014` establishes its KMS connection during startup; reducing the delay may make it connect before the KMS listener is ready.

#### Ordered startup with `depends_on`

Originally, there was no guarantee that a server had reached `Listen()` before its client called `Connect()`. The following mechanisms now enforce the real dependency graph:

- Readiness traces in `QKDKeyManagerSystemApplication`, `QKDPostprocessingApplication`, and `QKDApp014`, emitted after `Bind()` and `Listen()` complete.
- A role-specific readiness marker attached to each trace, for example `"[RELAY_KMS_ALICE] KMS Alice listening"`, alongside the existing `stores key` and `serves key` markers.
- `entrypoint.sh` mirrors stdout to `/tmp/qkdnetsim.log` inside each container. Docker health checks run inside the container namespace and cannot read the host-side `docker logs` view provided by the logging driver.
- Health checks on H2, H4, H5, H6, H7, and H9, plus a `depends_on: condition: service_healthy` chain that follows the actual topology:

```
H7
  ├─ H6
  │    ├─ H2 ─ H1
  │    └─ H5 ─ H8
  ├─ H4 ─ H3
  └─ H9 ─ H8
```

All four PP applications schedule a lightweight event every 100 ms so that `RealtimeSimulatorImpl` always has a nearby event. The entrypoint applies a fixed veth stabilization delay. There is no random jitter and no watchdog that terminates processes or recreates containers.

The image also applies `patches/realtime-simulator-clamp.patch` to ns-3.46. Under sustained load, the external `FdNetDevice` thread may observe a timestamp a few ticks behind `m_currentTs`. The original implementation aborts with `schedule for time < m_currentTs`; the patch schedules an already-late frame at the current valid timestamp and prevents the corresponding exit-code-139 failure.

PP convergence may take approximately one minute because ns-3 backs off after the initial lost SYN packets. Run the verifier after the H1–H4 PP nodes begin reporting `Key delivered`.

### 3. Point-to-point QKD-backed VPN — ETSI 004 and ETSI 014 (`docker/docker-compose.vpn.yml`)

Replaces the synthetic ETSI 014 application nodes at H5/H6 with real strongSwan IPsec/IKEv2 VPN endpoints. Instead of an ns-3 process encrypting synthetic traffic by hand, the H5 and H6 endpoints are native Ubuntu containers that periodically fetch real key material from the H3 and H4 KMS nodes and hand it to strongSwan as the connection's pre-shared key — the QKD stack's only remaining job is producing the key that protects a real IPsec tunnel. The overall architecture (a client/server pair of strongSwan encryptors fed by a periodic key-fetch script) follows:

> Mehic, M., Dervisevic, E., Fazio, P. and Voznak, M., 2025. *Virtual Quantum Key Distribution Network Ecosystem: The National Czech QKD Network*. IEEE Network. https://doi.org/10.1109/MNET.2025.3540705

The scenario supports both ETSI GS QKD 004 and ETSI GS QKD 014 so that
the two application interfaces can be compared over the same QKD link,
strongSwan configuration, key size and rekey interval. The use of ETSI GS
QKD 004 for session-based VPN key retrieval follows:

> Buruaga, J.S., Brunner, H.H., Fung, F., Peev, M., Pastor, A., López, D.R., Ortiz, L., Martín, V. and Brito, J.P., 2023. *VPN Protection with QKD-Derived Keys Using Standard Interfaces*. In 2023 23rd International Conference on Transparent Optical Networks (ICTON). IEEE. https://doi.org/10.1109/ICTON59386.2023.10207212

The H1–H4 ns-3 nodes and their networks are unchanged. The VPN definition is a
standalone Compose project that extends those four base services and replaces
the application endpoints with the `vpn_alice` and `vpn_bob` services.
Keeping it standalone prevents Compose from merging
the ETSI 014 dependency chain into this scenario.

![Point-to-point QKD-backed IPsec/IKEv2 VPN architecture](diagrams/point-to-point-vpn.svg)

**Interface selection.** ETSI 004 negotiates one `Key_stream_ID` (KSID) for
the complete VPN session. Alice sends that KSID once to Bob, which registers
the replica association with its own KMS. ETSI 014 instead obtains every new
key with `enc_keys`; Alice sends its `key_ID` to Bob, which retrieves the
matching material with `dec_keys`. Raw key material never crosses the endpoint
coordination channel in either mode. `QKD_INTERFACE=004` is the default;
setting it to `014` selects the second flow without changing the topology or
VPN parameters.

The same endpoint API is also available over the trusted-node path in
scenario 4. That case adds an internal KMS-to-KMS control plane because ETSI
GS QKD 004 defines the SAE-to-KMS stream API, but does not define how several
KMSs must construct one association across a trusted-node network. The
extension is described with the key-relay scenario below; it does not change
the ETSI 004 requests made by the VPN consumer.

**Transactional rekey cycle.** Alice drives each generation. With ETSI 004 it
obtains one `get_key(KSID)` result; with ETSI 014 it obtains one `enc_keys`
result and Bob retrieves its `key_ID` through `dec_keys`. Repeated control
requests are idempotent, so a lost response cannot consume an extra key on
only one side. The peers compare the ETSI 004 key index or ETSI 014 `key_ID`,
together with a SHA-256 fingerprint, before installing the secret.

For generation 2 and later, both peers temporarily remove the previous
strongSwan connection definition before initiation. This is required because
strongSwan otherwise reuses the previous IKE_SA and creates only another
CHILD_SA, which would not authenticate with the new QKD PSK. Alice closes the
old IKE_SA, initiates `qkd-N`, verifies that both endpoints report that exact
IKE_SA as `ESTABLISHED`, and commits the generation. This creates a short,
controlled cutover. If it fails, both endpoints restore the previous PSK and
connection and Alice re-establishes the previous generation.

The operation repeats every `REKEY_INTERVAL_S` (60 seconds by default).
`/run/qkd-vpn/state.json` contains only the KSID, generation, key index,
truncated fingerprint and status; raw keys are never written to state or logs.

Quick start with ETSI 004:

```bash
cd contrib/qkdnetsim-testbed
docker compose -f docker/docker-compose.yml down
docker build -t qkdnetsim-6vm-emulation:latest -f docker/Dockerfile .
docker compose -f docker/docker-compose.vpn.yml up -d --build
bash ./docker/verify-vpn.sh
```

Run the equivalent point-to-point VPN with ETSI 014 after stopping the first
run:

```bash
docker compose -f docker/docker-compose.vpn.yml down
QKD_INTERFACE=014 docker compose -f docker/docker-compose.vpn.yml up -d --build
QKD_INTERFACE=014 bash ./docker/verify-vpn.sh
```

Keep `QKD_INTERFACE` identical for `docker compose` and the verifier. For
repeatable measurements, bring the project down between interface changes so
that both tests start with empty KMS/VPN state.

`verify-vpn.sh` first verifies the initial VPN and then waits for a later
committed generation, proving that a new IKE_SA was authenticated with a new
QKD PSK. It requires all six containers to have zero restarts, verifies the
selected interface, compares generation, fingerprint and either KSID/key index
(ETSI 004) or `key_ID` (ETSI 014), and checks the exact `qkd-N` IKE_SA on both
peers. In ETSI 014 mode it also confirms that both KMSs reported serving that
`key_ID`. Finally it sends a real ping and captures only outbound ESP or ICMP:
ESP must be present and plaintext ICMP absent. The temporary capture is always
deleted.

**Real (non-ns-3) client ↔ ns-3 KMS interoperability fix.** The H5/H6 VPN endpoints talk to their KMS nodes over ordinary kernel TCP/IP, not `EmuFdNetDevice` — this exposed a checksum-offload interoperability gap that never mattered for the rest of the testbed (the other ns-3 containers communicate with another ns-3/`EmuFdNetDevice` process, never with a native kernel network stack). A real client's outgoing TCP segments are marked for hardware checksum offload, which never gets filled in over a Docker veth pair; with `ChecksumEnabled=true`, ns-3 sees an invalid checksum on every segment and silently drops it (`TcpL4Protocol: Bad checksum, dropping packet!`), which looks like a hung TCP handshake from the outside even though ARP/ICMP work fine. `entrypoint.sh` and `entrypoint-vpn.sh` now both disable checksum/segmentation offload (`ethtool -K ... off`) on their managed interfaces — the Dockerfile had `ethtool` installed for exactly this purpose already, it just was never invoked.

#### How the VPN endpoints are implemented

`docker/vpn/Dockerfile.vpn` builds a small Ubuntu image with strongSwan,
Python and network diagnostics; it contains no ns-3 build. The entrypoint
disables veth offloads, renders the fixed IKEv2 transport-mode configuration
and starts strongSwan. `qkd-vpn.py` then implements the persistent KMS
connection, KSID registration, idempotent prepare/activate/commit/rollback
protocol, secret installation and exact IKE_SA validation.

The cipher suites are pinned to plugins available in Ubuntu 22.04:
`ike=aes256-sha256-modp2048!` and `esp=aes256-sha256!`. The program validates
`ipsec statusall` rather than trusting the exit code of `ipsec up`, because
that command can return success even when no matching IKE_SA was established.

### 4. Key-relay QKD-backed VPN — ETSI 004 and ETSI 014 (`docker/docker-compose.key-relay-vpn.yml`)

This scenario keeps the complete trusted-node topology from scenario 2 and
replaces only the synthetic H8/H9 ETSI 014 application nodes with native
strongSwan endpoints:

![Trusted-node QKD-backed IPsec/IKEv2 VPN architecture](diagrams/key-relay-vpn.svg)

The H1–H7 ns-3 nodes are extended unchanged from
`docker-compose.key-relay.yml`. The standalone project defines correctly
named `vpn_alice` and `vpn_bob` services and reuses the same
interface-aware VPN consumer as scenario 3. `QKD_INTERFACE=004` is the
default; setting it to `014` selects the key-oriented flow without changing
the topology, strongSwan configuration or rotation interval.

#### Relayed key acquisition

The existing QKDNetSim relay mechanism first supplies matching end-to-end key
objects to the `RELAY_SBUFFER`s at the H5 and H7 KMS nodes. H6 is a trusted KMS node:
it decrypts and re-encrypts the key material with independent hop keys while
forwarding it from H5 to H7. The VPN can consume that common material
through either application interface.

In ETSI 004 mode:

1. The H8 VPN endpoint calls `open_connect` on the H5 KMS node. H5 creates the master stream and
   sends an internal `new_app` control request through the H6 trusted KMS node. H7 creates
   the replica association with the same KSID. H8 receives the KSID only
   after that operation succeeds end to end.
2. H8 sends the KSID to the H9 VPN endpoint once. H9 calls `open_connect(KSID)` on
   the H7 KMS node, which sends an internal `register` request back through H6.
3. Once both SAEs are registered, H5 reserves complete key objects from
   its end-to-end relay buffer and sends a `fill` request containing their
   identifiers—not their secret values—to H7.
4. H7 resolves those identifiers from its matching relay buffer and
   inserts the material into its replica stream. Its acknowledgement causes
   H5 to commit the same objects to the master stream; rejected objects
   are returned to the relay buffer.
5. Each VPN generation uses the normal ETSI 004 `get_key(KSID)` endpoint.
   Alice and Bob compare the returned stream index and fingerprint before
   installing the PSK and performing the transactional IKE cutover.

The `new_app`, `register` and `fill` messages use the private
`/api/v1/associations/relay004/<request-id>` KMS endpoint. H6 is only a
hop-by-hop control proxy and keeps no ETSI 004 association. Each request
carries its origin, final KMS and previous hop, so responses follow the
reverse path and duplicate fills cannot overlap. This endpoint is an
internal QKDNetSim trusted-network extension, not an additional ETSI
SAE-to-KMS operation.

In ETSI 014 mode:

1. Alice requests one 256-bit key from the H5 KMS node with
   `enc_keys/<Bob SAE>/number/1/size/256`.
2. QKDNetSim creates and relays the end-to-end key through
   H5 → H6 → H7 using the existing trusted-node `skey_create`
   mechanism and hop-by-hop QKD protection.
3. Alice sends only the returned `key_ID` to Bob's coordination API.
4. Bob requests that exact identifier from the H7 KMS node with
   `dec_keys/<Alice SAE>`.
5. Both endpoints compare `key_ID` and key fingerprint, install the result as
   the strongSwan PSK, and perform the same transactional IKE cutover.

Raw key material never travels between the H8 and H9 VPN endpoints. The recurring
`key_ID` coordination is necessary because ETSI 014 is key-oriented rather
than stream-oriented; ETSI 004 only coordinates its KSID during association
setup and thereafter identifies each chunk by its monotonically increasing
stream index. Prepare requests are idempotent, so a lost response does not
make Bob consume another key.

#### Start and verify

The synthetic and VPN key-relay projects use the same explicit subnets, so
stop scenario 2 before starting scenario 4:

```bash
cd contrib/qkdnetsim-testbed
docker compose -f docker/docker-compose.key-relay.yml down
docker build -t qkdnetsim-6vm-emulation:latest -f docker/Dockerfile .
docker compose -f docker/docker-compose.key-relay-vpn.yml up -d --build
bash ./docker/verify-key-relay-vpn.sh
```

Run the same topology with ETSI 014 after stopping the ETSI 004 run:

```bash
docker compose -f docker/docker-compose.key-relay-vpn.yml down
QKD_INTERFACE=014 docker compose -f docker/docker-compose.key-relay-vpn.yml up -d --build
QKD_INTERFACE=014 bash ./docker/verify-key-relay-vpn.sh
```

As in scenario 3, keep `QKD_INTERFACE` identical for Compose and the
verifier, and bring the project down before changing interfaces.

The verifier requires all nine containers to remain running with zero
restarts. It confirms matching fingerprints and either ETSI 004 KSID/index
or ETSI 014 `key_ID` at the VPN endpoints, checks the corresponding H5
and H7 KMS traces, validates the exact IKE_SA on both sides, and waits for
at least one later committed generation. A real ping must produce outbound
ESP and no plaintext ICMP. Its temporary capture is always deleted.

#### Stream-index continuity

`SBuffer::InsertKeyToStreamSession()` now distinguishes a never-used stream
from an empty stream whose earlier chunks were consumed. Previously, an
empty buffer reused `m_currentStreamIndex`, causing a fast-consuming endpoint
to receive index `0` repeatedly while its peer continued with `1`, `2`, and
so on. The last assigned index is now retained and the next refill starts at
`last + 1`; this applies to both direct and relayed ETSI 004 associations.
After stream-buffer initialization the KMS also restores the
`Key_chunk_size` negotiated by `open_connect`, because the generic S-buffer
initializer otherwise replaced it with the relay-storage default. Thus both
VPN interfaces now supply the configured 256-bit PSK material even though
the relay layer transports larger storage blocks internally.

## Testbed additions to QKDNetSim

- **[`examples/point-to-point/`](examples/point-to-point/)** — six independent role-named ns-3 programs (`pp_alice.cc`, `pp_bob.cc`, `kms_alice.cc`, `kms_bob.cc`, `etsi014_alice.cc`, and `etsi014_bob.cc`), one per role and container. Unlike the original module examples, which model both ends of every link in one process, these programs use the low-level QKDNetSim API and communicate over real networks through `EmuFdNetDevice`.
- **[`examples/key-relay/`](examples/key-relay/)** — the same pattern applied to a three-site relay topology with nine role-named programs: `pp_alice.cc`, `pp_relay_a.cc`, `pp_relay_b.cc`, `pp_bob.cc`, `kms_alice.cc`, `kms_trusted.cc`, `kms_bob.cc`, `etsi014_alice.cc`, and `etsi014_bob.cc`. See `examples/CMakeLists.txt` and the build targets in `docker/Dockerfile`.
- **[`docker/`](docker/)** — the shared image, the six-container point-to-point definition and its seven networks, interface-aware entrypoint, and end-to-end verifier.
- **[`docker-compose.key-relay.yml`](docker/docker-compose.key-relay.yml)** — the nine-container, ten-network key-relay definition, maintained as a separate Compose project and including the readiness dependency chain described above.
- **`entrypoint.sh`** — detects interfaces by subnet, normalizes veth devices, applies a fixed `NETWORK_SETTLE_MS`, and mirrors stdout to `/tmp/qkdnetsim.log` for health checks. It contains no watchdog or random jitter.
- **[`verify-key-relay.sh`](docker/verify-key-relay.sh)** — checks that all scenario processes are running without restarts, validates role-specific logs, confirms that the H5 and H7 KMS nodes serve the same `keyId`, and samples traffic without storing captures in the repository. Because the two physical links become ready before the end-to-end relay buffer, it waits up to 120 seconds for the first common served key instead of assuming that listener readiness also means immediate application-key readiness. Both this script and `verify.sh` remove any pre-existing capture file before writing a new one — `tcpdump` opens the raw socket as root and then drops privileges to a dedicated user before writing `-w`, so a leftover file from a previous run with different ownership made the capture fail silently (`Permission denied`, zero packets) regardless of whether traffic was actually flowing. `verify.sh`'s plaintext check no longer looks for a specific marker string — that check happened to depend on the same uninitialized-memory leak described above, not on anything the application actually sends — and instead only asserts the absence of any long readable run in the captured payload.
- **Readiness traces** in KMS, post-processing, and ETSI 014 applications — emitted when their relevant listener sockets are active and consumed by Compose health checks.
- **[`model/qkd-kms-queue-logic.h`](model/qkd-kms-queue-logic.h) fix** — initializes `m_numberOfQueues` to its documented default of 3. Previously, the uninitialized value could cause multi-gigabyte allocations while starting any KMS.
- **[`model/qkd-key-manager-system-application.cc`](model/qkd-key-manager-system-application.cc)**
  — preserves the original direct ETSI 004 path and adds routed
  `new_app`/`register`/`fill` control transactions for multi-hop
  associations. Secret material is moved by the existing trusted-node relay;
  the new control messages carry only KSID, routing metadata and key IDs.
- **[`model/s-buffer.cc`](model/s-buffer.cc)** — maintains monotonically
  increasing ETSI 004 stream indices after a completely consumed stream is
  refilled.
- **[`examples/CMakeLists.txt`](examples/CMakeLists.txt)** — build entries for every scenario binary.
- **[`examples/qkd-link-budget.h`](examples/qkd-link-budget.h)** — shared,
  validated fiber-loss model used by the point-to-point, key-relay and
  topology-input post-processing applications. Compose passes the same
  physical parameters to both ends of every distributed QKD link.
- **[`docker/vpn/`](docker/vpn/)** — the strongSwan endpoint image, its
  interface-aware entrypoint, and the transactional ETSI 004/014 consumer
  (`qkd-vpn.py`).
- **[`docker-compose.vpn.yml`](docker/docker-compose.vpn.yml)** and
  **[`verify-vpn.sh`](docker/verify-vpn.sh)** — the standalone six-node VPN
  scenario and its verifier for synchronized key generations, real IKE PSK
  rotation, stable containers and encrypted ESP traffic.
- **[`docker-compose.key-relay-vpn.yml`](docker/docker-compose.key-relay-vpn.yml)**
  and **[`verify-key-relay-vpn.sh`](docker/verify-key-relay-vpn.sh)** — the
  selectable ETSI 004/014 nine-node key-relay VPN and its end-to-end
  association, synchronized rotation and ESP verifier.

---

# Original QKDNetSim documentation

## Quantum Key Distribution Network Simulation Module for NS-3

As research in Quantum Key Distribution (QKD) technology grows larger and more complex, the need for highly accurate and scalable simulation technologies becomes important to assess the practical feasibility and foresee difficulties in the practical implementation of theoretical achievements. Due to the specificity of QKD link which requires optical and Internet connection between the network nodes, it is very costly to deploy a complete testbed containing multiple network hosts and links to validate and verify a certain network algorithm or protocol. The network simulators in these circumstances save a lot of money and time in accomplishing such task. A simulation environment offers the creation of complex network topologies, a high degree of control and repeatable experiments, which in turn allows researchers to conduct exactly the same experiments and confirm their results.

The aim of Quantum Key Distribution Network Simulation Module (QKDNetSim) project was not to develop the entire simulator from scratch but to develop the QKD simulation module in some of the already existing well-proven simulators. QKDNetSim is intended to facilitate additional understanding of QKD technology with respect to the existing network solutions. It seeks to serve as the natural playground for taking the further steps into this research direction (even towards practical exploitation in subsequent projects or product design).

**QKDNetSim implements the full functional Key Management System (KMS) with key-relay functionality supporting ETSI GS QKD 014 and ETSI GS QKD 004 key delivery interfaces.**

## Documentation

The detailed documentation is available on webpage https://www.qkdnetsim.info

## Deployment

 
- The latest version of the code is compatible with NS-3 version 3.46.
- Thus, one should follow installation requirements from the NS-3 official website (https://www.nsnam.org/wiki/Installation).   
- The code has been successfully tested on Ubuntu 22.04. 
- QKDNetSim v2.0 module is ***NOT*** compatible with QKDNetSim version 1.0 (https://v1.qkdnetsim.info). QKDNetSim v2.0 module was written independently and from scratch.


## Installation

QKDNetSim includes QKDEncryptor class that relies on cryptographic algorithms and schemes from Crypto++ open-source C++ class cryptographic library. Currently, QKD crypto supports several cryptographic algorithms and cryptographic hashes, including One-Time Pad (OTP) cipher, Advanced Encryption Standard (AES) block cipher, VMAC message authentication code (MAC) algorithm, and others.
 
1. Install prerequisites libreries:

	```bash
	sudo apt-get install gcc g++ python3 python3-dev mercurial bzr gdb valgrind gsl-bin doxygen graphviz imagemagick -y  && \
	sudo apt-get install libboost-all-dev git flex bison tcpdump sqlite sqlite3 -y   && \
	sudo apt-get install libsqlite3-dev libxml2 libxml2-dev libgtk2.0-0 libgtk2.0-dev uncrustify -y  && \
	sudo apt-get install libcrypto++-dev libcrypto++-doc libcrypto++-utils unzip wget uuid-dev cmake -y
    ```

2. Install the NS-3 of version 3.46 from the

	```bash
	git clone -b ns-3.46 https://gitlab.com/nsnam/ns-3-dev.git
    ```

3. Download qkdnetsim in contrib directory

	```bash
	cd ns-3-dev/contrib
    git clone -b master https://github.com/QKDNetSim/qkdnetsim
    ```

4. Check patches. They should report no error

	```bash
    cd ..
	git apply --check contrib/qkdnetsim/patches/gnuplot_cc.patches
	git apply --check contrib/qkdnetsim/patches/gnuplot_h.patches
    ```

5. Apply patches

	```bash
	git apply  contrib/qkdnetsim/patches/gnuplot_h.patches
	git apply  contrib/qkdnetsim/patches/gnuplot_cc.patches
    ```

6. Configure NS-3 with qkdnetsim

	```bash
	./ns3 configure --enable-mpi --enable-examples
    ```

7. Run qkdnetsim examples

	```bash
	./ns3 run examples_qkdnetsim_etsi_014
	./ns3 run examples_qkdnetsim_etsi_004
	./ns3 run examples_qkdnetsim_secoqc
	./ns3 run examples_qkdnetsim_etsi_combined_input
	./ns3 run examples_qkdnetsim_etsi_014_emulation_tap
    ```

## Authors

QKDNetSim is maintained by:

- Department of Telecommunications (www.tk.etf.unsa.ba)  
  Faculty of Electrical Engineering  
  University of Sarajevo  
  Zmaja od Bosne bb  
  71000 Sarajevo  
  Bosnia and Herzegovina  
- Department of Telecommunications (www.comtech.vsb.cz)
  VSB Technical University of Ostrava  
  17 . listopadu 15/2172  
  Ostrava-Poruba 708 33  
  Czech Republic  

**Main developers:**

- Emir Dervisevic
- Miroslav Voznak
- Miralem Mehic

Contact us via email (miralem[at]mehic.info).

## Cite 

- Dervisevic, E., Voznak, M. and Mehic, M., 2024. Large-Scale Quantum Key Distribution Network Simulator. Journal of Optical Communications and Networking, doi: https://www.doi.org/10.1364/JOCN.503356
- Dervisevic, E., Tankovic, A., Kaljic, E., Voznak, M. and Mehic, M., 2025. Design of a Key Management System for Efficient Key Supply in Quantum Key Distribution Networks. Journal of Optical Communications and Networking, doi: https://www.doi.org/10.1364/JOCN.577670
- Dervisevic, E., Tankovic, A., Fazel, E., Kompella, R., Fazio, P., Voznak, M. and Mehic, M., 2025. Quantum Key Distribution Networks – Key Management: A Survey. ACM Computing Surveys, 57(10), pp. 1–36, doi: https://www.doi.org/10.1145/3730575
- Mehic, M., Dervisevic, E., Burdiak, P., Lipovac, V., Fazio, P. and Voznak, M., 2024. Emulation of quantum key distribution networks. IEEE Network, 39(1), pp.116-123. doi: https://www.doi.org/10.1109/MNET.2024.3398404
- Mehic, M., Dervisevic, E., Fazio, P. and Voznak, M., 2025. Virtual Quantum Key Distribution Network Ecosystem: The National Czech QKD Network. IEEE Network., 39(3), pp.173-179. doi: https://www.doi.org/10.1109/MNET.2025.3540705

## Acknowledgment 

Development of QKDNetSim was supporty within projects #VJ01010008 “Network Cybersecurity in Post-Quantum Era” by the Ministry of the Interior of Czech Republic in program Impakt, Ministry of Science, Higher Education and Youth of Canton Sarajevo, Bosnia and Herzegovina (27-02-35-37082-1/23), NATO SPS G5894 project ”Quantum Cybersecurity in 5G Networks (QUANTUM5)” and H2020 project OPENQKD (No. 857156).

![NESPOQ](https://www.qkdnetsim.info/wp-content/uploads/2025/12/cz.png)
![MONKS](https://www.qkdnetsim.info/wp-content/uploads/2025/12/monks.png)
