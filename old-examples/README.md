# Old examples

This directory preserves the two first distributed QKDNetSim prototypes. They
use QKDNetSim's C++ `QKDApp014` consumers and synthetic TCP traffic instead of
the strongSwan VPN endpoints used by the active scenarios.

The archive is organized by responsibility:

- `core/traffic-topology.py` creates the two application DockerNodes and their
  classical path;
- `examples/point-to-point/` and `examples/key-relay/` contain the four
  scenario-specific C++ consumers;
- `diagrams/` contains the corresponding architectural diagrams and their
  editable HTML sources.

The PP and KMS programs are not duplicated here because the old and current
scenarios execute exactly the same active sources under `examples/`. The
standard QKDNetSim image nevertheless compiles the four archived consumer
targets so that these examples remain reproducible. They are intentionally
excluded from `automation/run-regression.py`.

## Build

From the repository root:

```bash
docker build -t qkdnetsim-testbed:latest -f docker/Dockerfile .
docker compose -f docker/docker-compose.core.yml up -d --build core
```

## Point-to-point synthetic traffic

```bash
docker compose -f docker/docker-compose.yml up -d
docker compose -f docker/docker-compose.core.yml exec core \
  /opt/core/venv/bin/python /workspace/old-examples/core/traffic-topology.py \
  --qkd-topology point-to-point --routers 0 --delay-ms 5 \
  --bandwidth-mbps 100 --loss-percent 0
docker compose -f docker/docker-compose.yml down
```

## Trusted-node synthetic traffic

```bash
docker compose -f docker/docker-compose.key-relay.yml up -d
docker compose -f docker/docker-compose.core.yml exec core \
  /opt/core/venv/bin/python /workspace/old-examples/core/traffic-topology.py \
  --qkd-topology key-relay --routers 0 --delay-ms 5 \
  --bandwidth-mbps 100 --loss-percent 0
docker compose -f docker/docker-compose.key-relay.yml down
```

The runner reports success only after observing matching ETSI 014 key requests
and encrypted QKDNetSim application traffic. These examples are retained for
historical reproduction and library regression analysis; they are not active
VPN scenarios or performance baselines.
