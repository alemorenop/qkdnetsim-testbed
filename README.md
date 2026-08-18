# QKDNetSim Testbed

This repository is a testbed built on top of
[QKDNetSim](https://www.qkdnetsim.info/). It provides real-network emulation
scenarios in which every node or role runs in its own Docker container and
communicates over actual network interfaces instead of placing all logical
roles inside one conventional ns-3 simulation process.

## Contents

- [Architecture and motivation](#architecture-and-motivation)
  - [Research basis and testbed extensions](#research-basis-and-testbed-extensions)
- [Installation and execution](#installation-and-execution)
  - [Automated regression matrix](#automated-regression-matrix)
- [Distance-aware QKD link budget](#distance-aware-qkd-link-budget)
- [CORE classical-network integration](#core-classical-network-integration)
- [Scenarios](#scenarios)
  - [Historical prototypes with synthetic traffic](#historical-toy-traffic-prototypes)
  - [1. Point-to-point QKD-backed VPN](#scenario-point-to-point-vpn)
  - [2. Key-relay QKD-backed VPN](#scenario-key-relay-vpn)
- [Testbed additions to QKDNetSim](#testbed-additions-to-qkdnetsim)
- [External documentation](#external-documentation)

## Architecture and motivation

This testbed turns QKDNetSim scenarios into distributed emulation
environments. QKD post-processing, KMS and application roles execute as
independent processes with real Linux network interfaces, while Docker
Compose provides the persistent QKD/KMS infrastructure and CORE creates the
classical Alice-Bob topology and its transient endpoints. The result combines
QKDNetSim's repeatable key-generation and key-management models with real
network stacks, strongSwan and configurable direct or multi-hop classical
paths.

### Research basis and testbed extensions

The two supported scenarios combine two published reference architectures
with extensions developed for this testbed. The earlier synthetic-traffic
prototypes are retained separately as project history.

| Testbed scenario | Research basis | Extension in this repository |
|---|---|---|
| Point-to-point VPN | QKD-fed strongSwan architecture from *Virtual Quantum Key Distribution Network Ecosystem: The National Czech QKD Network* | Reproducible Docker/CORE deployment with selectable ETSI 004 or ETSI 014 consumption |
| Key-relay VPN | Containerized key-relay topology and point-to-point strongSwan integration above | QKD-backed IPsec extended across the trusted-node path for both ETSI interfaces; this combined scenario is specific to this testbed |

The reference emulation architecture in Mehic et al.,
[*Emulation of Quantum Key Distribution Networks*](https://doi.org/10.1109/MNET.2024.3398404),
places QKD components in separate virtual machines. This testbed applies the
same principle of distributing roles across isolated network hosts, but uses
Linux containers instead of one complete guest operating system per role.
Each container retains its own process tree, network namespace, interfaces,
routes and sockets; QKDNetSim reaches the corresponding Docker veth through
`EmuFdNetDevice`, and native VPN endpoints use the Linux network stack
directly.

The process boundary is therefore preserved rather than introduced by the
container conversion: the paper's point-to-point emulation assigns its six
roles to six VMs, each with an independent QKDNetSim/ns-3 execution. The first
prototype in this repository reproduced those six independent roles with
containers and QKDNetSim's synthetic consumers. That prototype is now
historical. In the supported VPN scenarios, the QKD post-processing and KMS
roles still run as independent ns-3 processes, while the application
containers run native strongSwan and the Python QKD key consumer. This differs
from conventional all-in-one QKDNetSim examples, where several simulated
nodes and applications can belong to one ns-3 process.

The VPN integration is based on Mehic et al.,
[*Virtual Quantum Key Distribution Network Ecosystem: The National Czech QKD Network*](https://doi.org/10.1109/MNET.2025.3540705),
where strongSwan consumes QKD-derived key material. That work provides the
application-integration pattern; this repository adapts it to the common
Docker/CORE architecture and then applies it to both the direct and trusted
key-relay QKD topologies.

The geographical and transport basis of the Czech experiment is not
arbitrary. It comes from
[CESNET3](https://www.cesnet.cz/en/sit-cesnet3-eng), the operational Czech
national research and education network. The paper uses data from its
deployed optical routes: 304 km and 70 dB between Prague and Brno, and 257 km
and 62 dB between Brno and Ostrava. Over that physical basis it anticipates
the Prague--Brno--Ostrava QKD backbone proposed by
[CZQCI](https://www.cesnet.cz/en/science).

CZQCI was still under consideration when the article was prepared and the
final QKD equipment was not known. The authors therefore represent the route
as five virtual QKD links joined by trusted nodes, divide it into nominal
100 km and 85 km segments, estimate the secret-key rates from optical and
device assumptions, and emulate the full Qiskit--QKDNetSim--strongSwan stack
on one host. Names such as `Prague1`, `Prague2` and `Brno1` denote logical
QKD roles rather than confirmed institutions or physical sites. The result
is a model calibrated against real research-network routes, not an exact
digital twin.

The distinction is also confirmed by the later deployment. The Czech
cybersecurity authority
[reported in July 2026](https://nukib.gov.cz/cs/kyberneticka-bezpecnost/nkc/projekty-nkc/)
that physical implementation had finished on 30 June 2026 with approximately
600 km and six QKD segments between Prague, Brno and Ostrava, plus commercial
and experimental metropolitan branches. The article's five-link topology is
therefore best understood as a pre-deployment experimental abstraction of the
national architecture that was subsequently built.

Containers were selected for this implementation because they provide:

- lower CPU, memory and storage overhead than a deployment based on one VM per
  role;
- faster creation and cleanup, which supports repeated experiments and larger
  topologies;
- reproducible images, dependencies, interface assignments and startup
  conditions through Docker and Compose;
- identical scenario definitions on native Linux and Docker Desktop; and
- direct integration with CORE's DockerNode lifecycle and per-link NetEm
  configuration.

This is an implementation choice rather than a claim that containers and VMs
are equivalent. Containers share the host Linux kernel and therefore provide
less isolation and do not reproduce hypervisor or guest-OS overhead. VMs
remain preferable when an experiment requires different operating systems,
stronger host boundaries, virtual-hardware effects or deployment across
independent hypervisors. The container approach is intended to study the QKD,
KMS, application and network behaviour of this testbed with lower operational
cost and greater experimental repeatability.

## Installation and execution

### Requirements

The recommended workflow runs the complete testbed in Linux containers and
requires:

- Git;
- Docker Engine with the Docker Compose plugin;
- Python 3.10 or newer for the host-side regression orchestrator;
- an x86-64 Linux host, or Docker Desktop configured to use Linux containers
  on Windows. Commands may be issued from PowerShell, WSL or a Linux shell.

A pre-existing ns-3 or QKDNetSim installation is not required for this Docker
workflow. This repository contains the QKDNetSim module together with the
testbed changes. During the image build,
[`docker/Dockerfile`](docker/Dockerfile) clones ns-3.46, copies this checkout to
`contrib/qkdnetsim`, applies the required patches and compiles the scenario
binaries.

Clone the repository in any working directory:

```bash
git clone https://github.com/alemorenop/qkdnetsim-testbed.git
cd qkdnetsim-testbed
```

### Initial build

Build the QKDNetSim QKD/KMS image and the strongSwan VPN endpoint image:

```bash
docker build -t qkdnetsim-testbed:latest -f docker/Dockerfile .
docker build -t qkdnetsim-vpn-endpoint:latest -f docker/vpn/Dockerfile.vpn .
```

Build and start the shared CORE runtime:

```bash
docker compose -f docker/docker-compose.core.yml up -d --build core
```

The first build takes longer because ns-3, QKDNetSim, CORE, EMANE and the
routing components are compiled. Docker reuses these layers on subsequent
builds.

### Running a scenario

CORE is shared by both supported scenarios and normally remains running between
experiments. Each experiment then consists of two steps:

1. Start the selected persistent QKD/KMS infrastructure with its Compose
   file.
2. Execute `vpn-topology.py` to create and verify the strongSwan tunnel inside
   the CORE container.

For example, run the point-to-point VPN with ETSI 014 over a direct classical
link:

```bash
docker compose -f docker/docker-compose.vpn.yml up -d

docker compose -f docker/docker-compose.core.yml exec core \
  /opt/core/venv/bin/python /workspace/core/vpn-topology.py \
  --qkd-topology point-to-point --qkd-interface 014 \
  --routers 0 --delay-ms 5 \
  --bandwidth-mbps 100 --loss-percent 0
```

The runner waits for the infrastructure, creates the transient Alice and Bob
DockerNodes, verifies the end-to-end result and removes those endpoints when
the experiment finishes. Detailed commands for every VPN variant
are provided in the corresponding [scenario sections](#scenarios), while all
CORE topology parameters and smoke tests are documented in the
[local CORE integration guide](core/README.md).

Stop the example infrastructure and the shared CORE runtime when they are no
longer required:

```bash
docker compose -f docker/docker-compose.vpn.yml down
docker compose -f docker/docker-compose.core.yml down
```

### Automated regression matrix

[`automation/run-regression.py`](automation/run-regression.py) is the
recommended way to validate the complete testbed. It builds the images when
requested, starts CORE, selects and recreates the required Compose project,
runs the corresponding topology runner, saves the evidence, and tears down
both persistent infrastructure and transient CORE endpoints. Before every
case it also removes only stale containers whose names begin with
`qkd-core-vpn-`; an interrupted experiment therefore
cannot retain a fixed endpoint IP and contaminate the next run.

Run the complete campaign from the repository root:

```bash
python3 automation/run-regression.py --build
```

On Windows, the equivalent command is `py -3 automation/run-regression.py
--build`. The default campaign runs each of the four supported variants three
times:

1. point-to-point VPN with ETSI 004;
2. point-to-point VPN with ETSI 014;
3. key-relay VPN with ETSI 004; and
4. key-relay VPN with ETSI 014.

It then runs one expected-failure test in which Bob deliberately derives a
different test key. That case passes only when the consumers report `KMS
streams diverged` and refuse to establish the tunnel. The fault hook is
disabled unless the runner receives `--fault-mode bob-key-mismatch`.

The VPN cases require at least two different QKD generations, continuous ping
during rekey, retirement of the previous IKE SA, sustained `iperf3` traffic,
ESP on the exterior path, no clear ICMP or TCP payload, and explicit relay
evidence. ETSI 004 relay verification additionally requires the routed
`new_app`, `register`, and `fill` control operations.

Each run is written below `results/regression-<UTC timestamp>/` unless
`--output-dir` is supplied. `summary.json` contains the complete structured
campaign, `summary.csv` provides a compact table, and every case has its own
`runner.log`, `infrastructure.log`, and `result.json`. Infrastructure logs
also contribute the applied `[QKD_LINK_BUDGET]` lines. These result
directories are ignored by Git and contain no stored packet captures.

Useful shorter invocations are:

```bash
# One pass through all four variants plus the negative test
python3 automation/run-regression.py --repetitions 1

# One selected case; repeat --case to select several
python3 automation/run-regression.py --case vpn-key-relay-etsi004

# Display the matrix without accessing Docker
python3 automation/run-regression.py --list
```

Network conditions and acceptance limits are configurable with `--routers`,
`--delay-ms`, `--bandwidth-mbps`, `--loss-percent`, `--traffic-duration`,
`--min-generations`, `--rekey-interval`, and
`--max-rekey-loss-percent`. Use `--help` for the complete interface.

### Optional native ns-3 development

Docker is the supported execution path for the emulation testbed. To compile
the module directly instead, use an ns-3.46 source tree and place this
repository at `ns-3-dev/contrib/qkdnetsim`. Native compilation also requires
the QKDNetSim system dependencies listed in its
[official documentation](https://www.qkdnetsim.info/). Do not clone the
upstream QKDNetSim module first: this repository already contains that module
and the testbed-specific changes.

## Distance-aware QKD link budget

All four testbed scenarios derive the average secret-key generation rate of
each physical QKD link from its fiber length and attenuation. QKDNetSim
abstracts the quantum channel, so the calculation is performed before the
post-processing application starts and its result is assigned to the
`QKDPostprocessingApplication::KeyRate` attribute.

For a fiber of length $L$ and attenuation coefficient $\alpha$, the total
channel loss is:

$$
A_{\mathrm{dB}}(L) = \alpha L
$$

The corresponding fiber transmittance is:

$$
\eta(L) = 10^{-A_{\mathrm{dB}}(L)/10}
        = 10^{-\alpha L/10}
$$

The testbed uses a loss-budget-based key-rate model in which the rate scales
linearly with this transmittance:

$$
R_{\mathrm{model}}(L) = R_0\eta(L)
                       = R_0 10^{-\alpha L/10}
$$

where $L$ is expressed in kilometres, $\alpha$ in dB/km, $A_{\mathrm{dB}}$
in dB, $\eta$ is dimensionless, and $R_0$ is the configurable secret-key rate
before fiber loss.

The implementation uses general link-budget parameter names. Its current
default zero-loss rate is `17,094,000 bit/s`, calibrated from the equipment
and processing factors used to estimate the reference network links:

$$
\begin{aligned}
R_0 &= 10^9 \times 0.42 \times 0.50 \times 0.25
       \times 0.88 \times 0.37 \\
    &= 17{,}094{,}000\ \mathrm{bit/s}
\end{aligned}
$$

This model is supported by the following sources:

- Mehic et al., *Virtual Quantum Key Distribution Network Ecosystem: The
  National Czech QKD Network*, use the 85 km and 100 km reference links and
  report approximately 150 kbit/s and 85.083 kbit/s respectively
  ([IEEE Network, 2025](https://doi.org/10.1109/MNET.2025.3540705)).
- The source cited by that paper, Andrew Shields' *Performance Limits for
  Quantum Key Distribution Networks*, explicitly presents the equipment-factor
  product above, includes channel transmittance $\eta$, and identifies the
  loss-budget-dominated operating regime
  ([ITU-T workshop presentation, 2019](https://www.itu.int/en/ITU-T/Workshops-and-Seminars/2019060507/Documents/Andrew_Shields_Presentation.pdf)).
- Peer-reviewed QKD analyses use the same conversion from fiber distance and
  attenuation to transmittance, $T=10^{-\alpha L/10}$
  ([Scientific Reports, 2021](https://doi.org/10.1038/s41598-021-90055-3)).
- Fundamental rate-loss studies show that repeaterless optical QKD rates decay
  exponentially with distance and scale linearly with transmittance in the
  high-loss limit
  ([Nature Communications, 2014](https://doi.org/10.1038/ncomms6235);
  [Nature Communications, 2017](https://doi.org/10.1038/ncomms15043)).

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

### Model scope

$R_{\mathrm{model}}=R_0\eta$ is a calibrated link-budget approximation, not a
universal or protocol-specific secret-key-rate equation. It is suitable for
the current experiments because it provides a transparent, reproducible way
to map fiber distance to QKDNetSim's average `KeyRate` and reproduces the
selected reference results.

It does not model QBER, detector dark counts, Raman noise, optical
misalignment, finite-key effects, or processing saturation as functions of
distance. A detailed BB84, decoy-state, CV-QKD, or other implementation would
need its own security and device model. The approximation must also not be
applied unchanged to protocols with different rate-loss scaling, such as
Twin-Field QKD, whose ideal scaling can approach $\sqrt{\eta}$ rather than
$\eta$. The configurable $R_0$ and attenuation parameters allow the current
model to be recalibrated, while a future protocol-specific model can replace
the calculation without changing the scenario architecture.

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
docker compose -f docker/docker-compose.vpn.yml up -d
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
the two active Docker QKD/KMS topologies described below. It was nevertheless updated
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
that value is treated as an explicitly configured rate and no distance calculation is
performed. The link-budget calculation changes only QKD key generation; it
does not yet emulate classical propagation delay. Classical delay will be
introduced separately with Linux `tc netem` or CORE so the optical model
and classical-network model can be varied independently.

## CORE classical-network integration

The CORE runtime is available under [`core/`](core/README.md). CORE is the
classical Alice–Bob network for every supported scenario. Post-processing, KMS
synchronization, physical QKD links and trusted key relay remain in the
QKDNetSim/Docker infrastructure; the two application endpoints are always
Docker nodes managed by a CORE session.

### From a Docker bridge to CORE

The initial testbed connected Alice and Bob directly through one shared
Docker bridge network: `192.168.56.0/24` in the point-to-point scenarios and
`192.168.121.0/24` in the key-relay scenarios. Docker provided one veth per
endpoint, so the classical path was always a single direct Ethernet segment.
This was sufficient to exchange synthetic application traffic or establish
the VPN, but it could not represent intermediate classical routers or assign
different network conditions to individual hops.

The current architecture keeps Docker only for the endpoint-to-KMS connection
on `eth0`. The second endpoint interface, `eth1`, is created and connected by
CORE. A CORE session can model either a direct Alice–Bob link or a path with
up to eight routers and applies independently configurable one-way delay,
bandwidth and packet loss to every link. This separates experiments on the
optical QKD distance/key rate from experiments on the classical path. The old
Docker bridge implementation is described here only as architectural history;
it is no longer present as an executable scenario.

The integration provides a reproducible, complete CORE 9.2.1 container with
EMANE, its Python bindings and OSPF-MDR. Its smoke tests validate Linux
namespaces, real Docker nodes, veth connectivity, NetEm delay and automatic
container cleanup. The classical topology laboratory provides selectable
direct or multi-router paths with independent per-link delay, bandwidth and
loss. The supported `vpn-topology.py` runner places the native strongSwan
consumers on that path and selects point-to-point QKD or trusted-node key
relay without
changing the classical topology. Compose defines only persistent QKD/KMS
infrastructure; CORE is the sole owner of application endpoint creation,
classical links and cleanup. Commands and verification criteria are documented
in [`core/README.md`](core/README.md).

The architecture diagrams separate the classical data plane from its control
and orchestration plane. Each topology runner creates an embedded `CoreEmu`
session and uses the host Docker Engine, exposed through `docker.sock`, to
create the sibling endpoint containers. The simultaneously available
`core-daemon` exposes CORE's external gRPC management API on port 50051, but
the current runners do not send endpoint traffic—or their embedded session—
through that daemon. Application traffic traverses only the CORE-created veth,
bridge/router and NetEm path shown in the data plane.

The canonical editable diagram sources live in
[`diagrams/src/`](diagrams/src/) as self-contained HTML documents created with
the testbed's `diagram-design` profile, selected by
[`.diagram-design`](.diagram-design). Each HTML file contains its CSS and the
complete inline SVG, so it can be opened in a browser and edited or rendered
without a separate drawing application. The adjacent SVG is the generated,
diagram-only publication artifact: Markdown can embed it directly and LaTeX
can convert it without including the HTML page wrapper. Keeping both therefore
separates an editable source from a portable output; changes should be made in
the HTML source and exported to the SVG rather than maintained independently.
Their project-specific palette is intentionally
independent of the visual styling of the reference papers. The figures retain
the implementation-level information of the original drawings: every
process boundary, `EmuFdNetDevice`, Linux veth/`AF_PACKET` boundary, Docker
subnet and endpoint address, ETSI operation, CORE component and data-plane
address is shown next to the component or connector to which it belongs.

### Addressing contract

Compose assigns a `.254` gateway to every persistent `/24` Docker network.
The addresses below are the static interface values shared by Compose, the
C++ defaults, `core/vpn-topology.py` and the active diagrams.

| Point-to-point subnet | Endpoint A | Endpoint B |
|---|---|---|
| `192.168.11.0/24` | PP Alice `192.168.11.1` | PP Bob `192.168.11.2` |
| `192.168.13.0/24` | PP Alice `192.168.13.1` | KMS Alice `192.168.13.3` |
| `192.168.24.0/24` | PP Bob `192.168.24.2` | KMS Bob `192.168.24.4` |
| `192.168.34.0/24` | KMS Alice `192.168.34.3` | KMS Bob `192.168.34.4` |
| `192.168.35.0/24` | KMS Alice `192.168.35.3` | Alice VPN `192.168.35.5` |
| `192.168.46.0/24` | KMS Bob `192.168.46.4` | Bob VPN `192.168.46.6` |

| Key-relay subnet | Endpoint A | Endpoint B |
|---|---|---|
| `192.168.111.0/24` | PP Alice `192.168.111.1` | PP Relay-A `192.168.111.2` |
| `192.168.112.0/24` | PP Alice `192.168.112.1` | KMS Alice `192.168.112.5` |
| `192.168.113.0/24` | PP Relay-A `192.168.113.2` | KMS Trusted `192.168.113.6` |
| `192.168.114.0/24` | PP Relay-B `192.168.114.3` | PP Bob `192.168.114.4` |
| `192.168.115.0/24` | PP Relay-B `192.168.115.3` | KMS Trusted `192.168.115.6` |
| `192.168.116.0/24` | PP Bob `192.168.116.4` | KMS Bob `192.168.116.7` |
| `192.168.117.0/24` | KMS Alice `192.168.117.5` | KMS Trusted `192.168.117.6` |
| `192.168.118.0/24` | KMS Trusted `192.168.118.6` | KMS Bob `192.168.118.7` |
| `192.168.119.0/24` | KMS Alice `192.168.119.5` | Alice VPN `192.168.119.8` |
| `192.168.120.0/24` | KMS Bob `192.168.120.7` | Bob VPN `192.168.120.9` |

The CORE VPN path uses one `/30` per classical link. With `R` routers,
`vpn-topology.py` creates `10.253.i.0/30` for `i = 0 ... R`. Alice is always
`10.253.0.1/30`; Bob is `10.253.R.2/30`. Consequently the direct case
(`R = 0`) uses `10.253.0.1/30` and `10.253.0.2/30`. Router interfaces occupy
the `.2` address of the link on their Alice-facing side and the `.1` address
of the following link. The notation `10.253.R.2` in the diagrams is therefore
parameterized, not a literal IPv4 address.

## Scenarios

All commands in this section are run from the repository root.

The active architecture diagrams use role names such as `PP Alice`,
`KMS Trusted` and `Alice VPN endpoint`; they do not require the reader to map
the design through opaque host numbers. Historical figures, source comments
and some diagnostic tables may retain `H1`–`H9` as compact deployment
identifiers. Each identifier represents a Docker container, but its role may
be an ns-3 post-processing node, a KMS node, or a native Linux VPN endpoint.
Current internal names are role-based as well: for example, Compose uses
`kms_trusted`, the corresponding container is `qkd-relay-kms-trusted`, the
binary is `relay_kms_trusted`, and its log marker is
`[RELAY_KMS_TRUSTED]`.

<a id="historical-toy-traffic-prototypes"></a>

### Historical prototypes with synthetic traffic

Before introducing strongSwan, the testbed was developed through two
QKDNetSim-native prototypes. They are no longer supported executions and do
not appear in the build or regression matrix, but they established the
distributed architecture and exposed library faults that also affected the
QKD/KMS pipeline now used by the VPN scenarios.

Their detailed figures remain available as
[point-to-point](historical/toy-traffic/diagrams/point-to-point.svg) and
[trusted-node key relay](historical/toy-traffic/diagrams/key-relay.svg)
architectures. They use the same project palette and component grammar as the
active figures, while retaining the historical H1-H9 identifiers, exact IP
addresses and C++ `QKDApp014`/OTP data plane. Editable HTML sources are stored
beside them under
[`historical/toy-traffic/diagrams/src/`](historical/toy-traffic/diagrams/src/).

#### Direct point-to-point prototype

The first prototype separated the six roles of the reference emulation into
six independent ns-3 processes: Alice/Bob post-processing, Alice/Bob KMS and
two `QKDApp014` consumers. The consumers requested matching ETSI 014 keys and
used QKDNetSim's OTP mode to protect synthetic TCP/8081 traffic over the CORE
classical path. It demonstrated that:

- `EmuFdNetDevice` could connect independent ns-3 processes through Docker
  veth interfaces instead of keeping all roles in one simulation;
- the PP pair delivered correlated material to two independent KMSs;
- the ETSI 014 `enc_keys`/`dec_keys` flow returned the same key at both
  endpoints; and
- CORE could create the application endpoints and a direct or routed
  Alice-Bob path independently of the QKD link.

#### Trusted-node key-relay prototype

The second prototype extended the same pattern to nine independent roles:
four post-processing processes, Alice/trusted/Bob KMSs and two ETSI 014
consumers. It demonstrated that two independent QKD links could feed a
trusted KMS, that `skey_create` could transform material hop by hop, that the
endpoint KMSs served the same end-to-end key ID, and that the resulting key
could protect the synthetic application flow. Most of the difficult defects
were exposed here because requests, buffers and responses crossed three KMS
processes rather than one direct pair.

#### QKDNetSim and ns-3 issues exposed by the prototypes

The prototypes exposed two different classes of defects. The first group is
still exercised by the current VPN scenarios because their PP/KMS containers
use the same QKDNetSim classes and relay machinery. The second group belongs
to the retired C++ `QKDApp014` data-plane consumer: its corrections remain in
the library, but the active VPN uses `qkd-vpn.py` and strongSwan instead and
does not execute those paths.

##### Corrections exercised by the active VPN scenarios

- **TCP sockets and startup ordering.** PP and KMS listeners now publish
  readiness traces consumed by Compose health checks. A connecting
  `QKDPostprocessingApplication` preserves its socket while it is in
  `SYN_SENT`, lets TCP backoff proceed and reconnects only after a confirmed
  failure or closure. Repeatedly destroying a socket during the handshake had
  produced `TcpSocketBase` assertions when delayed packets arrived.
- **Incorrect framing of the post-processing TCP stream.** A `Recv()` callback
  was incorrectly treated as exactly one sent JSON message and a
  `std::string` was constructed without its explicit byte length. TCP
  fragmentation or coalescing therefore caused `JSON parse error` and process
  termination. The receiver now maintains a buffer per connection, extracts
  `;`-delimited frames, preserves incomplete suffixes and rejects malformed
  JSON without aborting the container.
- **Empty key material in `skey_create`.** `mergedKey` was consumed when the
  local response was split and then reused empty for relay. The implementation
  now preserves an exact copy, validates its size and encrypts that material
  hop by hop, allowing both endpoint KMSs to serve the same key ID.
- **Relay bit accounting stuck in `READY`.** In `Relay()`, the combination of
  `StoreKey(key, true)` and `MarkKey(id, INIT)` had a net-zero effect on
  `m_currentKeyBit`; after crossing the threshold once, `CheckState()` no
  longer represented current depletion. `SBufferClientCheck` now also uses
  `GetSBitCount()`, which reports the present content rather than historical
  accumulated state.
- **`skey_create` assumed one exact-size hop key.** Relay protection requested
  a single key of the exact required length while buffers contained default
  2048-bit objects. It now uses `GetTransformCandidate()` to merge several
  keys, following the multi-key pattern used elsewhere in QKDNetSim.
- **Unbalanced HTTP request bookkeeping.** A forwarded multi-hop
  `skey_create` request was not inserted into `m_httpRequestsQueryKMS`.
  Processing its response then attempted to `pop()` an empty queue and
  terminated with `NS_FATAL_ERROR("HTTP query for this KMS is empty!")`.
  Forwarded requests are now registered and matched to their responses.
- **Late external frames in the real-time scheduler.** Under load, the
  `FdNetDevice` thread could supply a timestamp just behind `m_currentTs`, and
  ns-3 aborted with `schedule for time < m_currentTs`. The scoped
  `realtime-simulator-clamp.patch` schedules an already-late external frame at
  the current valid time without changing future-event ordering, preventing
  the observed process termination reported by Docker as exit code 139.

The TCP, framing, readiness and real-time corrections are exercised by both
point-to-point and key-relay VPNs. Relay-buffer accounting is exercised by the
key-relay infrastructure with either ETSI interface. The preserved relay
material, multi-key hop protection and forwarded HTTP bookkeeping are
functionally required by the ETSI 014 key-relay path; they are harmless but
not traversed by the direct point-to-point flow.

##### Corrections retained for the historical QKDApp consumers

- **Uninitialized ETSI 014 application state.** `QKDApp014` read
  `m_isSignalingConnectedToApp` and `m_isDataConnectedToApp` before
  initialization, so some executions skipped socket creation. Both flags now
  start explicitly as `false`.
- **Encryption disabled in the relay prototype.** Its C++ consumers used
  `useCrypto=0`, although the traffic was described as OTP-protected. The
  prototype was corrected to `useCrypto=1`. This discovery also motivated the
  active VPN acceptance rule: verify ESP and the absence of plaintext rather
  than accepting key requests or TCP connectivity alone.
- **Uninitialized bytes serialized by `QKDAppHeader`.** Its fixed 32-byte
  authentication-tag field was only partially written by `SetAuthTag()`, so a
  disabled authenticator could expose unrelated bytes from a reused ns-3
  buffer. The setter now pads the whole field with leading `0` characters.

These three fixes are compiled as part of QKDNetSim but are not runtime
dependencies of the current VPN: `qkd-vpn.py` consumes ETSI 004/014 from the
KMS and strongSwan/IPsec owns the encrypted data plane, so no `QKDApp014` or
`QKDAppHeader` object is instantiated.

##### Endpoint timing and infrastructure readiness

In the historical deployment, H8 and H9 no longer depended on a fixed
`appStartTime`: the runner waited for healthy KMS containers, created the
endpoints, started Bob before Alice and observed key requests and application
traffic. The names H8/H9 now exist only in the archived diagrams and sources.

The replacement is still required by the active VPN architecture. Readiness
follows the actual dependency graph rather than a guessed container delay:
listeners emit role-specific markers after `Bind()` and `Listen()`;
`entrypoint.sh` mirrors them to `/tmp/qkdnetsim.log`; Compose waits for healthy
PP/KMS dependencies; and `core/vpn-topology.py` waits for the QKD/KMS
infrastructure, starts the Bob VPN endpoint before Alice, and then waits for a
committed common generation and an installed IPsec tunnel. TCP, KMS and peer
API retries absorb the remaining convergence window. There is no random
startup jitter, watchdog or fixed application start delay.

The active readiness chain is split by responsibility:

- `QKDKeyManagerSystemApplication` and
  `QKDPostprocessingApplication` emit role-specific listener markers after
  `Bind()` and `Listen()`; Compose turns those markers into health checks and
  `depends_on: condition: service_healthy` dependencies.
- The former `QKDApp014` readiness trace belongs only to the archived C++
  consumer. Current endpoints expose `/health` and `/status` through
  `qkd-vpn.py`, and the runner verifies their committed key generation and
  strongSwan state.
- Every active PP process schedules a lightweight event every 100 ms so that
  `RealtimeSimulatorImpl` has a nearby event, while `entrypoint.sh` applies a
  fixed veth stabilization interval before starting ns-3.

If an initial PP SYN is lost, ns-3 TCP backoff can still make convergence take
approximately one minute. This is expected: do not manually restart the
containers during that interval, because doing so discards the connection
state and restarts convergence. The CORE runner waits automatically and no
separate verifier or manual delay is required.

##### End-to-end verification in the current VPN

The retired runner waited for ETSI requests and encrypted synthetic TCP/8081
traffic. Its current replacement applies a stronger VPN-specific condition:
`core/vpn-topology.py` requires matching key generations, the expected current
IKE SA, retirement of the previous SA after rekey, successful ping, sustained
`iperf3` traffic, ESP on the exterior path and no plaintext ICMP or TCP
payload. Relay cases additionally require evidence from both QKD links and
the trusted KMS. Packet inspection is transient; captures and CORE endpoints
are deleted after the run and no capture is stored in the repository.

The retired endpoint sources, former traffic runner and diagrams are retained
under `historical/toy-traffic/`. The shared PP/KMS programs remain in their
normal active directories because the two VPN scenarios below still execute
them.

<a id="scenario-point-to-point"></a>

### 1. Point-to-point QKD-backed VPN — ETSI 004 and ETSI 014 (`docker/docker-compose.vpn.yml`)

This scenario applies the strongSwan integration pattern from the Czech QKD
network paper to the distributed point-to-point QKD/KMS infrastructure.
The Alice and Bob native Ubuntu endpoints run real strongSwan IPsec/IKEv2 VPN
and periodically obtain
real key material from KMS Alice and KMS Bob and hand it to strongSwan as
the connection's pre-shared key. The reference architecture—a client/server
pair of strongSwan encryptors fed by a periodic key-fetch script—is described
in:

> Mehic, M., Dervisevic, E., Fazio, P. and Voznak, M., 2025. *Virtual Quantum Key Distribution Network Ecosystem: The National Czech QKD Network*. IEEE Network. https://doi.org/10.1109/MNET.2025.3540705

The scenario supports both ETSI GS QKD 004 and ETSI GS QKD 014 so that
the two application interfaces can be compared over the same QKD link,
strongSwan configuration, key size and rekey interval. The use of ETSI GS
QKD 004 for session-based VPN key retrieval follows:

> Buruaga, J.S., Brunner, H.H., Fung, F., Peev, M., Pastor, A., López, D.R., Ortiz, L., Martín, V. and Brito, J.P., 2023. *VPN Protection with QKD-Derived Keys Using Standard Interfaces*. In 2023 23rd International Conference on Transparent Optical Networks (ICTON). IEEE. https://doi.org/10.1109/ICTON59386.2023.10207212

The two PP and two KMS ns-3 processes and their networks are unchanged. The
standalone Compose project starts only that QKD/KMS infrastructure in the
normal workflow. CORE
creates role-named `qkd-core-vpn-alice-<PID>` and
`qkd-core-vpn-bob-<PID>` transient DockerNode endpoints, connects their
`eth0` interfaces to the KMS networks and manages their classical `eth1`
interfaces.

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
scenario 2. That case adds an internal KMS-to-KMS control plane because ETSI
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
docker build -t qkdnetsim-testbed:latest -f docker/Dockerfile .
docker build -t qkdnetsim-vpn-endpoint:latest -f docker/vpn/Dockerfile.vpn .
docker compose -f docker/docker-compose.vpn.yml up -d
docker compose -f docker/docker-compose.core.yml up -d --build core
docker compose -f docker/docker-compose.core.yml exec core \
  /opt/core/venv/bin/python /workspace/core/vpn-topology.py \
  --qkd-topology point-to-point --qkd-interface 004 \
  --routers 0 --delay-ms 5 --bandwidth-mbps 100 --loss-percent 0
```

Run the equivalent point-to-point VPN with ETSI 014 after the first run
finishes and removes its transient endpoints:

```bash
docker compose -f docker/docker-compose.core.yml exec core \
  /opt/core/venv/bin/python /workspace/core/vpn-topology.py \
  --qkd-topology point-to-point --qkd-interface 014 \
  --routers 0 --delay-ms 5 --bandwidth-mbps 100 --loss-percent 0
```

The runner verifies the selected interface, multiple matching generations
with different key fingerprints, traffic continuity during rekey, retirement
of the previous IKE SA, successful ping, sustained `iperf3` throughput, ESP,
and zero plaintext ICMP or TCP payload. Its transient endpoints and temporary
capture are deleted after each run, while the QKD/KMS infrastructure may
remain active for comparisons.

**Real (non-ns-3) client ↔ ns-3 KMS interoperability fix.** The Alice/Bob VPN endpoints talk to their KMS nodes over ordinary kernel TCP/IP, not `EmuFdNetDevice` — this exposed a checksum-offload interoperability gap that never mattered for the rest of the testbed (the other ns-3 containers communicate with another ns-3/`EmuFdNetDevice` process, never with a native kernel network stack). A real client's outgoing TCP segments are marked for hardware checksum offload, which never gets filled in over a Docker veth pair; with `ChecksumEnabled=true`, ns-3 sees an invalid checksum on every segment and silently drops it (`TcpL4Protocol: Bad checksum, dropping packet!`), which looks like a hung TCP handshake from the outside even though ARP/ICMP work fine. [`entrypoint.sh`](docker/entrypoint.sh) and [`entrypoint-vpn.sh`](docker/vpn/entrypoint-vpn.sh) now both disable checksum/segmentation offload (`ethtool -K ... off`) on their managed interfaces — the Dockerfile had `ethtool` installed for exactly this purpose already, it just was never invoked.

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

<a id="scenario-key-relay-vpn"></a>

### 2. Key-relay QKD-backed VPN — ETSI 004 and ETSI 014 (`docker/docker-compose.key-relay-vpn.yml`)

This scenario combines the distributed trusted-node QKD/KMS infrastructure
with native strongSwan endpoints. It is the testbed-specific extension of the
point-to-point VPN integration across a key-relay path; neither reference
paper defines this complete topology:

![Trusted-node QKD-backed IPsec/IKEv2 VPN architecture](diagrams/key-relay-vpn.svg)

The four PP and three KMS ns-3 processes are extended unchanged from
`docker-compose.key-relay.yml`. CORE creates the two strongSwan DockerNode
endpoints and reuses the same interface-aware VPN consumer as scenario 1.
`--qkd-interface 004` selects the stream-oriented flow and `014` selects the
key-oriented flow without changing the topology or strongSwan parameters.

#### Relayed key acquisition

The existing QKDNetSim relay mechanism first supplies matching end-to-end key
objects to the `RELAY_SBUFFER`s at KMS Alice and KMS Bob. KMS Trusted is the
trusted node:
it decrypts and re-encrypts the key material with independent hop keys while
forwarding it from KMS Alice to KMS Bob. The VPN can consume that common material
through either application interface.

In ETSI 004 mode:

1. The Alice VPN endpoint calls `open_connect` on KMS Alice. KMS Alice creates
   the master stream and sends an internal `new_app` control request through
   KMS Trusted. KMS Bob creates the replica association with the same KSID.
   Alice receives the KSID only
   after that operation succeeds end to end.
2. Alice sends the KSID to the Bob VPN endpoint once. Bob calls
   `open_connect(KSID)` on KMS Bob, which sends an internal `register` request
   back through KMS Trusted.
3. Once both SAEs are registered, KMS Alice reserves complete key objects from
   its end-to-end relay buffer and sends a `fill` request containing their
   identifiers—not their secret values—to KMS Bob.
4. KMS Bob resolves those identifiers from its matching relay buffer and
   inserts the material into its replica stream. Its acknowledgement causes
   KMS Alice to commit the same objects to the master stream; rejected objects
   are returned to the relay buffer.
5. Each VPN generation uses the normal ETSI 004 `get_key(KSID)` endpoint.
   Alice and Bob compare the returned stream index and fingerprint before
   installing the PSK and performing the transactional IKE cutover.

The `new_app`, `register` and `fill` messages use the private
`/api/v1/associations/relay004/<request-id>` KMS endpoint. KMS Trusted is only a
hop-by-hop control proxy and keeps no ETSI 004 association. Each request
carries its origin, final KMS and previous hop, so responses follow the
reverse path and duplicate fills cannot overlap. This endpoint is an
internal QKDNetSim trusted-network extension, not an additional ETSI
SAE-to-KMS operation.

In ETSI 014 mode:

1. Alice requests one 256-bit key from KMS Alice with
   `enc_keys/<Bob SAE>/number/1/size/256`.
2. QKDNetSim creates and relays the end-to-end key through
   KMS Alice → KMS Trusted → KMS Bob using the existing trusted-node `skey_create`
   mechanism and hop-by-hop QKD protection.
3. Alice sends only the returned `key_ID` to Bob's coordination API.
4. Bob requests that exact identifier from KMS Bob with
   `dec_keys/<Alice SAE>`.
5. Both endpoints compare `key_ID` and key fingerprint, install the result as
   the strongSwan PSK, and perform the same transactional IKE cutover.

Raw key material never travels between the Alice and Bob VPN endpoints. The recurring
`key_ID` coordination is necessary because ETSI 014 is key-oriented rather
than stream-oriented; ETSI 004 only coordinates its KSID during association
setup and thereafter identifies each chunk by its monotonically increasing
stream index. Prepare requests are idempotent, so a lost response does not
make Bob consume another key.

#### Start and verify

Stop any previously running QKD infrastructure before starting this scenario:

```bash
docker build -t qkdnetsim-testbed:latest -f docker/Dockerfile .
docker build -t qkdnetsim-vpn-endpoint:latest -f docker/vpn/Dockerfile.vpn .
docker compose -f docker/docker-compose.key-relay-vpn.yml up -d
docker compose -f docker/docker-compose.core.yml up -d --build core
docker compose -f docker/docker-compose.core.yml exec core \
  /opt/core/venv/bin/python /workspace/core/vpn-topology.py \
  --qkd-topology key-relay --qkd-interface 004 \
  --routers 0 --delay-ms 5 --bandwidth-mbps 100 --loss-percent 0
```

Run the same topology with ETSI 014 after the ETSI 004 run finishes:

```bash
docker compose -f docker/docker-compose.core.yml exec core \
  /opt/core/venv/bin/python /workspace/core/vpn-topology.py \
  --qkd-topology key-relay --qkd-interface 014 \
  --routers 0 --delay-ms 5 --bandwidth-mbps 100 --loss-percent 0
```

As in scenario 1, the runner confirms multiple matching fingerprints and
either ETSI 004 KSID/index or ETSI 014 `key_ID`, validates the exact current
IKE SA and retirement of its predecessor, and requires sustained traffic to
produce ESP with no plaintext application payload. It also requires relay
consumption plus both endpoint deliveries; ETSI 004 additionally checks the
routed `new_app`, `register`, and `fill` transactions.

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

- **[`examples/point-to-point/`](examples/point-to-point/)** — four independent
  role-named ns-3 programs (`pp_alice.cc`, `pp_bob.cc`, `kms_alice.cc`, and
  `kms_bob.cc`) that provide the active point-to-point VPN's QKD/KMS
  infrastructure through `EmuFdNetDevice`.
- **[`examples/key-relay/`](examples/key-relay/)** — seven persistent
  role-named ns-3 programs implementing the two post-processing links and
  three KMS roles used by the active trusted-node VPN.
- **[`docker/`](docker/)** — the shared images, Compose definitions for persistent QKD/KMS infrastructure, and interface-aware entrypoints. Application endpoint topology and verification live exclusively under `core/`.
- **[`diagrams/`](diagrams/)** — accessible SVG architecture diagrams used by
  the documentation, with their editable, self-contained HTML sources under
  [`diagrams/src/`](diagrams/src/) and a project-specific visual profile
  selected by [`.diagram-design`](.diagram-design). The retired toy-traffic
  equivalents and their HTML sources are kept under
  [`historical/toy-traffic/diagrams/`](historical/toy-traffic/diagrams/).
- **[`docker-compose.yml`](docker/docker-compose.yml)** — the four-service
  point-to-point QKD/KMS infrastructure. It deliberately contains neither
  Alice/Bob application services nor a classical data network.
- **[`docker-compose.key-relay.yml`](docker/docker-compose.key-relay.yml)** — the seven-service trusted-node QKD/KMS infrastructure and its readiness dependency chain. The two application endpoints are added by CORE at run time.
- **[`entrypoint.sh`](docker/entrypoint.sh)** — detects interfaces by subnet,
  normalizes veth devices, applies a fixed `NETWORK_SETTLE_MS`, and mirrors
  stdout to `/tmp/qkdnetsim.log` for health checks. It contains no watchdog or
  random jitter.
- **Readiness traces** in KMS and post-processing programs — emitted when
  their relevant listener sockets are active and consumed by Compose health
  checks.
- **[`model/qkd-kms-queue-logic.h`](model/qkd-kms-queue-logic.h) fix** — initializes `m_numberOfQueues` to its documented default of 3. Previously, the uninitialized value could cause multi-gigabyte allocations while starting any KMS.
- **[`model/qkd-key-manager-system-application.cc`](model/qkd-key-manager-system-application.cc)**
  — preserves the original direct ETSI 004 path and adds routed
  `new_app`/`register`/`fill` control transactions for multi-hop
  associations. Secret material is moved by the existing trusted-node relay;
  the new control messages carry only KSID, routing metadata and key IDs.
- **[`model/s-buffer.cc`](model/s-buffer.cc)** — maintains monotonically
  increasing ETSI 004 stream indices after a completely consumed stream is
  refilled.
- **[`examples/CMakeLists.txt`](examples/CMakeLists.txt)** — build entries for
  the active QKD/KMS infrastructure binaries; retired synthetic consumers are
  intentionally excluded.
- **[`examples/qkd-link-budget.h`](examples/qkd-link-budget.h)** — shared,
  validated fiber-loss model used by the point-to-point, key-relay and
  topology-input post-processing applications. Compose passes the same
  physical parameters to both ends of every distributed QKD link.
- **[`docker/vpn/`](docker/vpn/)** — the strongSwan endpoint image, its
  interface-aware entrypoint, and the transactional ETSI 004/014 consumer
  (`qkd-vpn.py`).
- **[`docker-compose.core.yml`](docker/docker-compose.core.yml)** and
  **[`core/`](core/README.md)** — the common classical network used by all
  scenarios, including direct or routed paths with configurable per-link
  delay, bandwidth and loss, transient endpoint lifecycle, and end-to-end
  verification. The CORE README documents every runtime, smoke-test and
  topology-runner file individually.
- **[`docker-compose.vpn.yml`](docker/docker-compose.vpn.yml)** — the
  point-to-point VPN QKD/KMS infrastructure. The CORE VPN runner verifies
  synchronized key generations, real IKE PSK rotation and encrypted ESP
  traffic.
- **[`docker-compose.key-relay-vpn.yml`](docker/docker-compose.key-relay-vpn.yml)**
  — the selectable ETSI 004/014 seven-service key-relay VPN infrastructure. CORE
  supplies its two application endpoints and performs the end-to-end
  association, synchronized rotation and ESP verification.
- **[`automation/run-regression.py`](automation/run-regression.py)** — the
  host-side four-variant VPN regression orchestrator. It owns image/Compose
  lifecycle, repeated execution, expected negative testing, stale endpoint
  cleanup, and JSON/CSV/log evidence collection.

---

## External documentation

This document is limited to the architecture, scenarios and implementation
details introduced by this testbed. General information about QKDNetSim,
including its architecture, installation, QKD models, KMS and ETSI GS QKD
004/014 interfaces, is available in the
[official QKDNetSim documentation](https://www.qkdnetsim.info/).

CORE's general architecture, installation procedures, node creation,
services, APIs and network-emulation facilities are described in the
[official CORE documentation](https://coreemu.github.io/core/). Details that
are specific to this repository—its container runtime, DockerNode lifecycle,
topology runners and verification commands—remain documented in the
[local CORE integration guide](core/README.md).
