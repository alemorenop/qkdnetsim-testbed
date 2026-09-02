#!/usr/bin/env bash
# Builds the 4 comparison images:
#   qkdnetsim:base-old        pristine upstream QKDNetSim, pre-PQC / ns-3.46  (commit 1cda34c)
#   qkdnetsim:base-new        pristine upstream QKDNetSim, PQC / ns-3.48      (commit 525e9bf)
#   qkdnetsim-testbed:old     our non-monolithic adaptation, pre-PQC / ns-3.46 (commit 99783ee)
#   qkdnetsim-testbed:new     our non-monolithic adaptation, PQC / ns-3.48     (commit d9f0fc4)
#
# Run from anywhere; paths below are relative to this script's location.
#
#   ./build-all.sh              build all 4
#   ./build-all.sh base-old     build just one (base-old | base-new | testbed-old | testbed-new)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

# Never use branch names here. These four immutable revisions define the two
# comparison pairs even after main, develop or upstream/master move forward.
UPSTREAM_OLD_COMMIT="1cda34cbe75b5cb33e2ffa87081f724d6484199f"
UPSTREAM_NEW_COMMIT="525e9bf7882ff51b7e197a9fd2ca9ba0b19af0c9"
TESTBED_OLD_COMMIT="99783ee7f10314b52af71ce0528db7305f461f44"
TESTBED_NEW_COMMIT="d9f0fc468f18007f1c19c6fd27c57db64896f030"

build_base_old() {
  echo "==> Building qkdnetsim:base-old (pristine, 1cda34c, ns-3.46, no PQC)"
  local ctx
  ctx="$(mktemp -d)"
  trap 'rm -rf "'"${ctx}"'"' RETURN
  git -C "${REPO_ROOT}" archive "${UPSTREAM_OLD_COMMIT}" | tar -x -C "${ctx}"
  docker build -t qkdnetsim:base-old -f "${SCRIPT_DIR}/Dockerfile.base-old" "${ctx}"
}

build_base_new() {
  echo "==> Building qkdnetsim:base-new (pristine, 525e9bf, ns-3.48, PQC via one isolated CMake-wiring fix -- see Dockerfile.base-new)"
  local ctx
  ctx="$(mktemp -d)"
  trap 'rm -rf "'"${ctx}"'"' RETURN
  git -C "${REPO_ROOT}" archive "${UPSTREAM_NEW_COMMIT}" | tar -x -C "${ctx}"
  docker build -t qkdnetsim:base-new -f "${SCRIPT_DIR}/Dockerfile.base-new" "${ctx}"
}

build_testbed_old() {
  echo "==> Building qkdnetsim-testbed:old (our adaptation, 99783ee, ns-3.46, no PQC)"
  git -C "${REPO_ROOT}" archive "${TESTBED_OLD_COMMIT}" | docker build -t qkdnetsim-testbed:old -f docker/Dockerfile -
}

build_testbed_new() {
  echo "==> Building qkdnetsim-testbed:new (our adaptation, d9f0fc4, ns-3.48, PQC)"
  git -C "${REPO_ROOT}" archive "${TESTBED_NEW_COMMIT}" | docker build -t qkdnetsim-testbed:new -f docker/Dockerfile -
}

case "${1:-all}" in
  base-old)     build_base_old ;;
  base-new)     build_base_new ;;
  testbed-old)  build_testbed_old ;;
  testbed-new)  build_testbed_new ;;
  all)
    build_base_old
    build_base_new
    build_testbed_old
    build_testbed_new
    ;;
  *)
    echo "Usage: $0 [base-old|base-new|testbed-old|testbed-new|all]" >&2
    exit 1
    ;;
esac

echo "==> Done. Images:"
docker images --filter=reference='qkdnetsim:base-*'
docker images --filter=reference='qkdnetsim-testbed:*'
