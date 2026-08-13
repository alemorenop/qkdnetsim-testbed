# CORE classical-network integration

CORE emulates the classical path between Alice and Bob in every testbed
scenario. Both the ns-3 traffic consumers and the strongSwan consumers run as
CORE-managed DockerNode endpoints. QKD post-processing, KMS synchronization
and trusted key relay stay outside CORE in the Docker Compose infrastructure.

## Step 1: CORE runtime

The image follows CORE's complete official Ubuntu 22.04 Docker build and pins
the corrected CORE 9.2.1 release. It includes OSPF-MDR, the EMANE runtime and
EMANE Python bindings even though the current classical topologies use only Linux
namespaces, veth, bridges and NetEm. Keeping the complete runtime avoids a base
image rebuild if later experiments add wireless models or CORE's NRL routing
services. Docker CLI and additional network utilities are installed on top for
this testbed's Docker-node integration.

The CORE container uses the host Docker Engine through
`/var/run/docker.sock`. `privileged: true` allows it to create and configure
network namespaces, while `pid: host` allows CORE to enter the namespaces of
the Docker nodes created through that engine. This is Docker-outside-of-Docker,
not a nested Docker daemon.

There are two distinct management entry points inside the container. The
scenario scripts instantiate an embedded `CoreEmu` session directly and own
its complete lifecycle. In parallel, `core-daemon` exposes the external gRPC
management API on port 50051 for tools that need it; the current topology
runners do not use that API. Neither management path carries Alice–Bob data:
application and IPsec packets stay on the veth, bridge/router and NetEm links
created for the CORE data plane.

> [!WARNING]
> Access to the Docker socket is effectively administrative access to the
> Docker host. Run only the version-controlled scripts and images belonging to
> this testbed through the CORE container.

Start it from the repository root:

```bash
docker compose -f docker/docker-compose.core.yml up -d --build
docker compose -f docker/docker-compose.core.yml ps
```

Run the initial wired-network smoke test:

```bash
docker compose -f docker/docker-compose.core.yml exec core \
  /opt/core/venv/bin/python /workspace/core/smoke-test.py
```

The expected final marker is:

```text
[CORE_SMOKE] OK direct link delayUs=5000 bandwidthBps=100000000
```

Stop the runtime with:

```bash
docker compose -f docker/docker-compose.core.yml down
```

## Step 2: Docker-node integration

CORE runs the strongSwan and ns-3 application endpoints as Docker nodes. Build
the minimal validation image, which includes the networking tools required by
CORE's Docker-node support:

```bash
docker compose -f docker/docker-compose.core.yml --profile node-image build
```

The `node-image` profile is build-only and is not started with the CORE daemon.
Start CORE and run the Docker-node smoke test:

```bash
docker compose -f docker/docker-compose.core.yml up -d
docker compose -f docker/docker-compose.core.yml exec core \
  /opt/core/venv/bin/python /workspace/core/docker-node-smoke-test.py
```

The test asks CORE to create two sibling Docker containers with `--net=none`,
attach their only non-loopback interfaces to a 100 Mbit/s link with 5 ms
one-way delay, assign `10.251.0.1/24` and `10.251.0.2/24`, and verify lossless
connectivity and the expected RTT. The session shutdown must remove both
containers. Successful execution ends with:

```text
[CORE_DOCKER_NODE_SMOKE] OK image=qkdnetsim-core-node:22.04 delayUs=5000 bandwidthBps=100000000
[CORE_DOCKER_NODE_SMOKE] cleanup=OK
```

CORE's documentation recommends globally disabling Docker's default bridge and
iptables handling when Docker is dedicated to CORE. This testbed deliberately
does not change the Docker Engine's global configuration: its existing Compose
networks require normal Docker networking, while CORE already creates its
managed nodes with `--net=none`.

## Step 3: configurable classical path

The validated Docker endpoints can be connected directly or through up to
eight lightweight CORE routers. Every link receives its own delay, bandwidth
and optional loss configuration. For a direct path:

```bash
docker compose -f docker/docker-compose.core.yml up -d
docker compose -f docker/docker-compose.core.yml exec core \
  /opt/core/venv/bin/python /workspace/core/classical-topology.py \
  --routers 0 --delay-ms 5 --bandwidth-mbps 100
```

For a three-router path:

```bash
docker compose -f docker/docker-compose.core.yml exec core \
  /opt/core/venv/bin/python /workspace/core/classical-topology.py \
  --routers 3 --delay-ms 5 --bandwidth-mbps 100
```

`--delay-ms` is the one-way delay of each individual link, not the total path
delay. With `R` routers there are `R + 1` links, so the expected propagation
component of RTT is approximately:

```text
RTT = 2 * (routers + 1) * delayPerLink
```

The script assigns a different `/30` network to each hop, enables IPv4
forwarding on the routers, installs the required endpoint and transit routes,
checks connectivity and RTT, and removes the Docker endpoints on shutdown.
Additional options are available through `--help`, including per-link packet
loss and the ping sample count.

## Step 4: run any testbed scenario

CORE is the classical Alice–Bob network in all four scenarios. Compose starts
the persistent QKD post-processing and KMS infrastructure; a runner creates
the two transient application DockerNodes, connects `eth0` to their KMS
networks and gives `eth1` to CORE. Compose contains no application endpoints
or Alice–Bob data network: CORE is the only implementation of that layer.

Start CORE once:

```bash
docker compose -f docker/docker-compose.core.yml up -d --build core
```

Build the two endpoint images used by the runners:

```bash
docker build -t qkdnetsim-testbed:latest -f docker/Dockerfile .
docker build -t qkdnetsim-vpn-endpoint:latest -f docker/vpn/Dockerfile.vpn .
```

For point-to-point QKD, start its four infrastructure services:

```bash
docker compose -f docker/docker-compose.yml up -d
```

Run the ns-3 ETSI 014 traffic consumer or the strongSwan consumer:

```bash
docker compose -f docker/docker-compose.core.yml exec core \
  /opt/core/venv/bin/python /workspace/core/traffic-topology.py \
  --qkd-topology point-to-point --routers 0 --delay-ms 5 \
  --bandwidth-mbps 100 --loss-percent 0

docker compose -f docker/docker-compose.vpn.yml up -d
docker compose -f docker/docker-compose.core.yml exec core \
  /opt/core/venv/bin/python /workspace/core/vpn-topology.py \
  --qkd-topology point-to-point --qkd-interface 004 \
  --routers 0 --delay-ms 5 --bandwidth-mbps 100 --loss-percent 0
```

For trusted-node key relay, use the corresponding infrastructure and select
`key-relay` in exactly the same runners:

```bash
docker compose -f docker/docker-compose.key-relay.yml up -d
docker compose -f docker/docker-compose.core.yml exec core \
  /opt/core/venv/bin/python /workspace/core/traffic-topology.py \
  --qkd-topology key-relay --routers 3 --delay-ms 5 \
  --bandwidth-mbps 100 --loss-percent 0

docker compose -f docker/docker-compose.key-relay-vpn.yml up -d
docker compose -f docker/docker-compose.core.yml exec core \
  /opt/core/venv/bin/python /workspace/core/vpn-topology.py \
  --qkd-topology key-relay --qkd-interface 014 \
  --routers 3 --delay-ms 5 --bandwidth-mbps 100 --loss-percent 0
```

Only one QKD infrastructure project should be active at a time. For the VPN,
`--qkd-interface` accepts `004` or `014`. Both runners accept zero to eight
routers. `--delay-ms`, `--bandwidth-mbps` and `--loss-percent` apply to every
individual link.

The KMS-facing interfaces use fixed, locally administered MAC addresses.
QKDNetSim's `EmuFdNetDevice` keeps an ARP cache across experiments; a new
random MAC for the same endpoint IP would make later TCP replies target the
removed container. The native traffic runner additionally installs a host
route in the ns-3 endpoint stack when routers exist. A Linux route alone would
not affect packets produced by ns-3.

Both runners explicitly wait for the Alice-side and Bob-side KMS containers
to report `healthy` before creating either endpoint. They then create both
network namespaces and start Bob before Alice. Application-level TCP/KMS
retries cover the remaining key-availability window, so no fixed sleep,
watchdog or manual container ordering is required.

The traffic runner requires key requests at both consumers and encrypted
TCP/8081 traffic. The VPN runner requires matching generations and
fingerprints, established strongSwan state, successful ping, outbound ESP and
zero plaintext ICMP. Both remove their endpoint containers automatically. The
KMS retains accepted APP–KMS sockets while active and removes them and any
partial HTTP buffer when the peer closes, allowing repeated experiments
without restarting QKDNetSim.

Stop both runtimes afterwards:

```bash
docker compose -f docker/docker-compose.core.yml down
docker compose -f docker/docker-compose.vpn.yml down
docker compose -f docker/docker-compose.yml down
docker compose -f docker/docker-compose.key-relay.yml down
docker compose -f docker/docker-compose.key-relay-vpn.yml down
```

The first build compiles EMANE's Python bindings and OSPF-MDR, so it takes
longer than subsequent builds, which reuse Docker's cached layers.

The commands are identical on native Linux, WSL and Windows PowerShell. On
Windows they target Docker Desktop's Linux engine; on native Linux they target
the local Docker Engine. Docker Desktop must be running Linux containers, and
the current CORE package targets x86-64 hosts. The smoke tests isolate the
runtime and Docker-node requirements, while the VPN test verifies the complete
strongSwan, KMS and routed-classical-path integration.

## Files and responsibilities

- `docker/docker-compose.core.yml` starts the privileged CORE daemon and
  exposes the host Docker Engine needed to create sibling DockerNodes.
- `docker/core/Dockerfile.core` builds the complete CORE 9.2.1 runtime.
- `docker/core/entrypoint-core.sh` prepares and starts the CORE daemon inside
  that image.
- `docker/core/Dockerfile.node` builds the small generic image used only by
  the DockerNode smoke test.
- `core/smoke-test.py` validates a basic CORE wired link and its NetEm delay.
- `core/docker-node-smoke-test.py` validates creation, networking and cleanup
  of real DockerNodes.
- `core/classical-topology.py` is an isolated classical-network laboratory for
  direct and multi-router paths without QKDNetSim or strongSwan.
- `core/traffic-topology.py` creates the ETSI 014 ns-3 Alice/Bob DockerNodes,
  attaches them to the selected point-to-point or key-relay KMS infrastructure
  and verifies encrypted TCP/8081 traffic.
- `core/vpn-topology.py` creates the strongSwan Alice/Bob DockerNodes, selects
  ETSI 004 or ETSI 014, establishes the IPsec tunnel and verifies ESP with no
  plaintext ICMP.

The topology runners own the complete endpoint lifecycle: they wait for both
KMS containers, create Bob and Alice, attach `eth0` to Docker KMS networks and
`eth1` to CORE, configure routes and per-link conditions, perform verification
and remove every transient endpoint during shutdown.

Official references:

- <https://coreemu.github.io/core/install_docker.html>
- <https://coreemu.github.io/core/docker.html>
- <https://coreemu.github.io/core/architecture.html>
