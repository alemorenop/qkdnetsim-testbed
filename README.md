# QKDNetSim Testbed

This repository is a testbed built on top of **QKDNetSim** (see the original project documentation below). It provides real-network emulation scenarios in which every node or role runs in its own Docker container and communicates over actual network interfaces instead of being simulated inside a single ns-3 process.

## Scenarios

### 1. Point-to-point — direct Alice–Bob link (`examples/point-to-point/`)

Container-based adaptation of the network emulation experiment shown in Fig. 3 of:

> Mehic, M., Dervisevic, E., Burdiak, P., Lipovac, V., Fazio, P. and Voznak, M., 2024. *Emulation of quantum key distribution networks*. IEEE Network, 39(1), pp.116-123. https://doi.org/10.1109/MNET.2024.3398404

The paper simulates both ends of every link inside a single ns-3 process. In this testbed, every role runs in a separate Docker container and communicates through real network interfaces using `EmuFdNetDevice`. The logical scenario is the same, but it is deployed as six independent nodes.

The six containers run Alice/Bob post-processing, Alice/Bob KMS, and Alice/Bob ETSI 014 applications. Alice and Bob communicate directly, without an intermediate node.

```
HOST1 (post-processing Alice) ──sifting──> HOST2 (post-processing Bob)
        │ key delivery                            key delivery │
        v                                                       v
HOST3 (KMS Alice) <────────── transform_keys ──────────> HOST4 (KMS Bob)
        │ GET_KEY (ETSI 014)                    GET_KEY (ETSI 014) │
        v                                                       v
HOST5 (ETSI 014 Alice) ─────── encrypted traffic ─────> HOST6 (ETSI 014 Bob)
```

| Host | Role | IPs (network ↔ peer) |
|---|---|---|
| HOST1 | post-processing Alice | 192.168.11.1 (↔H2), 192.168.13.1 (↔H3) |
| HOST2 | post-processing Bob | 192.168.11.2 (↔H1), 192.168.24.2 (↔H4) |
| HOST3 | KMS Alice | 192.168.13.3 (↔H1), 192.168.34.3 (↔H4), 192.168.35.3 (↔H5) |
| HOST4 | KMS Bob | 192.168.24.4 (↔H2), 192.168.34.4 (↔H3), 192.168.46.4 (↔H6) |
| HOST5 | ETSI014 Alice | 192.168.35.5 (↔H3), 192.168.56.5 (↔H6) |
| HOST6 | ETSI014 Bob | 192.168.46.6 (↔H4), 192.168.56.6 (↔H5) |

Quick start:

```bash
cd contrib/qkdnetsim-testbed
docker build -t qkdnetsim-6vm-emulation:latest -f docker/Dockerfile .
docker compose -f docker/docker-compose.yml up -d
./docker/verify.sh
```

Container creation order does not affect the scenario. Readiness traces from the PP, KMS, and ETSI 014 applications ensure that listeners start before their clients. The dependency chain is HOST4 → HOST3/HOST2/HOST6 → HOST1/HOST5. The first PP link may take about one minute to converge because of ns-3 TCP backoff; no container restart is required.

`verify.sh` requires all six processes to be running with zero restarts, checks that both KMS instances served the same `keyId`, and temporarily samples port 8081 to verify that application traffic exists and the body is no longer visible as plaintext. HOST5 and HOST6 use OTP with `useCrypto=1`.

### 2. Key relay — intermediate node without a direct Alice–Bob link (`examples/key-relay/`)

In this scenario, Alice and Bob do not have a direct QKD link. They communicate through a trusted intermediate node with its own KMS. The node relays keys using the functionality already provided by `QKDKeyManagerSystemApplication` (`RelayConsumption`, `WasteRelay`, `ConfigureRSBuffers`, and related methods), together with hop-by-hop OTP encryption through `skey_create`. Alice and Bob ultimately share actual key material without either endpoint seeing the raw key from the other QKD segment. End-to-end encrypted traffic flows between HOST8 and HOST9 and is verified with temporary `tcpdump` samples.

Topology (9 hosts):

```
HOST1 (PP Alice) ──qkd──> HOST2 (PP Relay-A)          HOST3 (PP Relay-B) <──qkd── HOST4 (PP Bob)
       │                                                                                  │
       v                                                                                  v
HOST5 (KMS Alice) ─────────────── relay ───────────> HOST6 (KMS Relay) <─── relay ─────── HOST7 (KMS Bob)
       │                                                                                  │
       v                                                                                  v
HOST8 (ETSI014 Alice) ───────────────── encrypted traffic ─────────────────> HOST9 (ETSI014 Bob)
```

| Host | Role | IPs (network ↔ peer) |
|---|---|---|
| HOST1 | PP Alice | 192.168.111.1 (↔H2), 192.168.112.1 (↔H5) |
| HOST2 | PP Relay-A | 192.168.111.2 (↔H1), 192.168.113.2 (↔H6) |
| HOST3 | PP Relay-B | 192.168.114.3 (↔H4), 192.168.115.3 (↔H6) |
| HOST4 | PP Bob | 192.168.114.4 (↔H3), 192.168.116.4 (↔H7) |
| HOST5 | **KMS Alice** | 192.168.112.5 (↔H1), 192.168.117.5 (↔H6), 192.168.119.5 (↔H8) |
| HOST6 | **KMS Relay** (intermediate node) | 192.168.113.6 (↔H2), 192.168.115.6 (↔H3), 192.168.117.6 (↔H5), 192.168.118.6 (↔H7) |
| HOST7 | **KMS Bob** | 192.168.116.7 (↔H4), 192.168.118.7 (↔H6), 192.168.120.7 (↔H9) |
| HOST8 | ETSI014 Alice | 192.168.119.8 (↔H5), 192.168.121.8 (↔H9) |
| HOST9 | ETSI014 Bob | 192.168.120.9 (↔H7), 192.168.121.9 (↔H8) |

Start the scenario:

```bash
cd contrib/qkdnetsim-testbed
docker build -t qkdnetsim-6vm-emulation:latest -f docker/Dockerfile .
docker compose -f docker/docker-compose.key-relay.yml up -d
```

`depends_on` and the health checks start hosts according to the readiness of their actual listeners. PP links use TCP reconnection and may take about one minute to converge because of ns-3 TCP backoff. Do not restart containers during this window.

#### End-to-end verification

Seeing all nine containers in the `Up` state only proves that their processes have not exited. To check the expected activity for every host and sample the relevant traffic, run:

```bash
./docker/verify-key-relay.sh [sample_seconds]   # default: 10s
```

The verifier reports whether all nine hosts produce the expected role-specific activity (`Clave entregada`, `almacena clave`, `sirve clave`, or `Peticion GET_KEY`), confirms that HOST5 and HOST7 serve the same `keyId` to the applications, and temporarily samples the four relevant links. Packet captures remain under `/tmp` inside the containers and are neither copied to nor stored in the repository. Traffic between HOST8 and HOST9 confirms the final result between the ETSI 014 applications.

#### Library issues fixed for this scenario

- **TCP sockets and startup ordering.** PP, KMS, and ETSI listeners publish readiness traces used by the health checks. `QKDPostprocessingApplication` preserves its socket during `SYN_SENT`, allows TCP backoff to proceed, and reconnects after a failure or closure. Periodically destroying a socket during the handshake caused `TcpSocketBase` assertions when delayed packets arrived.
- **Incorrect framing of the post-processing TCP stream.** The receiver assumed that every `Recv()` call contained exactly one sent packet and constructed a `std::string` without an explicit length. TCP fragmentation or coalescing consequently caused `JSON parse error` and terminated the process. The receiver now keeps a buffer per connection, extracts frames delimited by `;`, preserves incomplete fragments, and discards malformed JSON without aborting the container.
- **Empty key material in `skey_create`.** `mergedKey` was consumed while splitting the local response and was then reused empty for the relay operation. An exact copy is now preserved, its size is validated, and the key is encrypted hop by hop. HOST5 and HOST7 serve the same 6400-bit `keyId`.
- **Uninitialized ETSI 014 state.** `m_isSignalingConnectedToApp` and `m_isDataConnectedToApp` were read before initialization, causing some runs to skip socket creation. Both now start explicitly as `false`.
- **Encryption disabled in the scenario.** HOST8 and HOST9 used `useCrypto=0`, so traffic described as OTP-protected was actually plaintext. The key-relay scenario now uses `useCrypto=1`.
- **Bit accounting stuck in READY.** In `Relay()`, `StoreKey(key,true)` followed by `MarkKey(id,INIT)` has a net-zero effect on `m_currentKeyBit`. As a result, `CheckState()` stopped reflecting actual relay-buffer depletion after the threshold was crossed for the first time. `SBufferClientCheck` now also checks `GetSBitCount()`, which is the current count rather than historical accumulated state.
- **`skey_create` assumed one exact-size key.** Hop-by-hop encryption material was requested as a single key with an exact bit length, while the buffer only contained default 2048-bit keys. It now uses the same multi-key merge pattern through `GetTransformCandidate` that is already used elsewhere in the implementation.
- **Unbalanced HTTP request bookkeeping.** Multi-hop forwarding of `skey_create` sent the request to the next hop without adding it to `m_httpRequestsQueryKMS`. Processing the response then attempted to `pop()` an empty queue and terminated the process with `NS_FATAL_ERROR("HTTP query for this KMS is empty!")`.

**Timing requirement:** keep `--appStartTime=20` for HOST8 and HOST9 (see the comments in `docker-compose.key-relay.yml`). `QKDApp014` establishes its KMS connection during startup; reducing the delay may make it connect before the KMS listener is ready.

#### Ordered startup with `depends_on`

Originally, there was no guarantee that a server had reached `Listen()` before its client called `Connect()`. The following mechanisms now enforce the real dependency graph:

- Readiness traces in `QKDKeyManagerSystemApplication`, `QKDPostprocessingApplication`, and `QKDApp014`, emitted after `Bind()` and `Listen()` complete.
- A host-specific text marker attached to each trace, such as `"[HOST5] KMS Alice escuchando"`, alongside the existing `almacena clave` and `sirve clave` markers.
- `entrypoint.sh` mirrors stdout to `/tmp/qkdnetsim.log` inside each container. Docker health checks run inside the container namespace and cannot read the host-side `docker logs` view provided by the logging driver.
- Health checks on HOST2, HOST4, HOST5, HOST6, HOST7, and HOST9, plus a `depends_on: condition: service_healthy` chain that follows the actual topology:

```
HOST7
  ├─ HOST6
  │    ├─ HOST2 ─ HOST1
  │    └─ HOST5 ─ HOST8
  ├─ HOST4 ─ HOST3
  └─ HOST9 ─ HOST8
```

All four PP applications schedule a lightweight event every 100 ms so that `RealtimeSimulatorImpl` always has a nearby event. The entrypoint applies a fixed veth stabilization delay. There is no random jitter and no watchdog that terminates processes or recreates containers.

The image also applies `patches/realtime-simulator-clamp.patch` to ns-3.46. Under sustained load, the external `FdNetDevice` thread may observe a timestamp a few ticks behind `m_currentTs`. The original implementation aborts with `schedule for time < m_currentTs`; the patch schedules an already-late frame at the current valid timestamp and prevents the corresponding exit-code-139 failure.

PP convergence may take approximately one minute because ns-3 backs off after the initial lost SYN packets. Run the verifier after HOST1–HOST4 begin reporting `Clave entregada`.

## Testbed additions to QKDNetSim

- **[`examples/point-to-point/`](examples/point-to-point/)** — six independent ns-3 programs (`host1_pp_alice.cc` through `host6_etsi014_bob.cc`), one per role and container. Unlike the original module examples, which model both ends of every link in one process, these programs use the low-level QKDNetSim API and communicate over real networks through `EmuFdNetDevice`.
- **[`examples/key-relay/`](examples/key-relay/)** — the same pattern applied to a three-site relay topology with nine binaries. See `examples/CMakeLists.txt` and the build targets in `docker/Dockerfile`.
- **[`docker/`](docker/)** — the shared image, the six-container point-to-point definition and its seven networks, interface-aware entrypoint, and end-to-end verifier.
- **[`docker-compose.key-relay.yml`](docker/docker-compose.key-relay.yml)** — the nine-container, ten-network key-relay definition, maintained as a separate Compose project and including the readiness dependency chain described above.
- **`entrypoint.sh`** — detects interfaces by subnet, normalizes veth devices, applies a fixed `NETWORK_SETTLE_MS`, and mirrors stdout to `/tmp/qkdnetsim.log` for health checks. It contains no watchdog or random jitter.
- **[`verify-key-relay.sh`](docker/verify-key-relay.sh)** — checks that all scenario processes are running without restarts, validates role-specific logs, confirms that HOST5 and HOST7 serve the same `keyId`, and samples traffic without storing captures in the repository.
- **Readiness traces** in KMS, post-processing, and ETSI 014 applications — emitted when their relevant listener sockets are active and consumed by Compose health checks.
- **[`model/qkd-kms-queue-logic.h`](model/qkd-kms-queue-logic.h) fix** — initializes `m_numberOfQueues` to its documented default of 3. Previously, the uninitialized value could cause multi-gigabyte allocations while starting any KMS.
- **[`examples/CMakeLists.txt`](examples/CMakeLists.txt)** — build entries for every scenario binary.

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
