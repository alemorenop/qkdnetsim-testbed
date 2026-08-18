# CORE classical-network integration

CORE emulates the classical path between Alice and Bob in every supported
testbed scenario. The strongSwan consumers run as CORE-managed DockerNode
endpoints. QKD post-processing, KMS synchronization and trusted key relay stay
outside CORE in the Docker Compose infrastructure.

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

The isolated `classical-topology.py` laboratory numbers those links as
`10.252.i.0/30`. The supported VPN runner deliberately uses a separate block,
`10.253.i.0/30`, where `i` is the zero-based link index. With `R` routers there
are `R + 1` links: Alice is `10.253.0.1/30`, and Bob is
`10.253.R.2/30`. Thus a direct run (`--routers 0`) assigns Bob
`10.253.0.2/30`; with three routers Bob becomes `10.253.3.2/30`.

## Step 4: run a supported VPN scenario

CORE is the classical Alice–Bob network in both supported scenarios. Compose
starts the persistent QKD post-processing and KMS infrastructure;
`vpn-topology.py` creates the two transient strongSwan DockerNodes, attaches
`eth0` to their KMS networks and gives `eth1` to CORE. The former ns-3
synthetic-traffic runner is preserved only under
[`historical/toy-traffic/`](../historical/toy-traffic/).

Start CORE and the selected VPN infrastructure:

```bash
docker compose -f docker/docker-compose.core.yml up -d --build core
docker compose -f docker/docker-compose.vpn.yml up -d
```

Run point-to-point QKD with either ETSI interface:

```bash
docker compose -f docker/docker-compose.core.yml exec core \
  /opt/core/venv/bin/python /workspace/core/vpn-topology.py \
  --qkd-topology point-to-point --qkd-interface 004 \
  --routers 0 --delay-ms 5 --bandwidth-mbps 100 --loss-percent 0
```

For trusted-node key relay, start
`docker/docker-compose.key-relay-vpn.yml` instead and pass
`--qkd-topology key-relay`. Both topologies accept ETSI 004 or ETSI 014 and
zero to eight classical routers. Delay, bandwidth and loss apply to each link.

The runner waits for both KMS containers, creates Bob before Alice and relies
on bounded application-level TCP/KMS retries for the remaining readiness
window. No fixed sleep, watchdog or manual endpoint ordering is required. It
requires multiple matching QKD generations, continuous traffic during rekey,
old-IKE-SA retirement, sustained `iperf3` throughput, ESP and no plaintext
ICMP or TCP payload. Key-relay runs additionally verify trusted-node
consumption; ETSI 004 also verifies `new_app`, `register` and `fill`.
Every run emits a machine-readable `[CORE_RESULT]` JSON record and removes
its endpoint containers and temporary capture.

Run the complete supported matrix from the repository root:

```bash
python3 automation/run-regression.py --build
```

It runs the four VPN variants three times by default, then executes the
expected mismatched-key rejection once and writes JSON, CSV and per-run logs
below `results/`. Use `--repetitions 1`, `--case NAME` or `--list` for
shorter workflows.

The Docker Compose and in-container commands are identical on native Linux,
WSL and Windows PowerShell. The host regression launcher uses
`python3 automation/run-regression.py` on Linux/WSL and
`py -3 automation/run-regression.py` on Windows. Docker Desktop must use Linux
containers; on native Linux the commands target the local Docker Engine. The
current CORE package targets x86-64 hosts.

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
- `core/vpn-topology.py` creates the strongSwan Alice/Bob DockerNodes, selects
  ETSI 004 or ETSI 014, verifies rekeys and old-SA retirement, drives ping and
  `iperf3`, and checks ESP with no plaintext application payload.
- `automation/run-regression.py` orchestrates builds, Compose lifecycle,
  repeated scenario execution, negative testing and structured evidence.

The topology runners own the complete endpoint lifecycle: they wait for both
KMS containers, create Bob and Alice, attach `eth0` to Docker KMS networks and
`eth1` to CORE, configure routes and per-link conditions, perform verification
and remove every transient endpoint during shutdown.

Official references:

- <https://coreemu.github.io/core/install_docker.html>
- <https://coreemu.github.io/core/docker.html>
- <https://coreemu.github.io/core/architecture.html>
