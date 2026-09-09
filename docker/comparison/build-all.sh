#!/usr/bin/env bash
# Builds the comparison images:
#   qkdnetsim:base-old        upstream model + matched workload, ns-3.46       (commit 1cda34c)
#   qkdnetsim:base-new        upstream model + matched workload, ns-3.48/PQC   (commit 525e9bf)
#   qkdnetsim:base-new-reference  same upstream workload using discrete-event
#                                execution for the Padua statistics profile
#   qkdnetsim-testbed:old     our non-monolithic adaptation, pre-PQC / ns-3.46 (commit 99783ee)
#   qkdnetsim-testbed:new     our non-monolithic adaptation, PQC / ns-3.48     (commit cc6b619)
#
# Run from anywhere; paths below are relative to this script's location.
#
#   ./build-all.sh              build all 4
#   ./build-all.sh base-old     build just one (base-old | base-new | testbed-old | testbed-new)
#   ./build-all.sh check        verify Docker/Python discovery without building

set -euo pipefail

# Non-login Git Bash launched from PowerShell may inherit only the Windows
# PATH. Add its standard Unix tool directories explicitly; this is harmless on
# Linux and WSL.
PATH="/usr/local/bin:/usr/bin:/bin:${PATH}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

# PowerShell's `bash` command commonly enters WSL. Docker Desktop may then be
# available only as the Windows executable even when WSL integration is off.
# Prefer a native Docker CLI, but fall back to the standard Docker Desktop
# installation reached through WSL or Git Bash filesystem mounts.
resolve_docker() {
  if command -v docker >/dev/null 2>&1; then
    command -v docker
    return
  fi

  local candidate
  for candidate in \
    /mnt/c/Users/*/AppData/Local/Programs/DockerDesktop/resources/bin/docker.exe \
    /c/Users/*/AppData/Local/Programs/DockerDesktop/resources/bin/docker.exe; do
    if [[ -f "${candidate}" && -x "${candidate}" ]]; then
      printf '%s\n' "${candidate}"
      return
    fi
  done

  echo "Docker CLI not found. Start Docker Desktop or enable its WSL integration." >&2
  return 1
}

DOCKER_BIN="$(resolve_docker)"
docker() {
  "${DOCKER_BIN}" "$@"
}

resolve_python() {
  local candidate
  for candidate in python3 python; do
    if command -v "${candidate}" >/dev/null 2>&1 && \
       "${candidate}" -c 'import sys; assert sys.version_info >= (3, 10)' \
         >/dev/null 2>&1; then
      printf '%s\n' "${candidate}"
      return
    fi
  done

  # The Windows launcher may exist even when it has no registered interpreter,
  # so test it before selecting it.
  for candidate in /c/Windows/py.exe /mnt/c/Windows/py.exe; do
    if [[ -f "${candidate}" && -x "${candidate}" ]] && \
       "${candidate}" -3 -c 'import sys; assert sys.version_info >= (3, 10)' \
         >/dev/null 2>&1; then
      printf '%s\n' "${candidate} -3"
      return
    fi
  done

  # Codex Desktop bundles Python in its local runtime. This fallback makes the
  # developer checkout immediately usable; ordinary Windows installations
  # should still install Python 3.10+ system-wide.
  for candidate in \
    /c/Users/*/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe \
    /mnt/c/Users/*/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe; do
    if [[ -f "${candidate}" && -x "${candidate}" ]] && \
       "${candidate}" -c 'import sys; assert sys.version_info >= (3, 10)' \
         >/dev/null 2>&1; then
      printf '%s\n' "${candidate}"
      return
    fi
  done

  echo "Python 3.10+ not found. Install it before building comparison images." >&2
  return 1
}

read -r -a PYTHON_CMD <<< "$(resolve_python)"
python3() {
  "${PYTHON_CMD[@]}" "$@"
}

# Never use branch names here. These four immutable revisions define the two
# comparison pairs even after main, develop or upstream/master move forward.
UPSTREAM_OLD_COMMIT="1cda34cbe75b5cb33e2ffa87081f724d6484199f"
UPSTREAM_NEW_COMMIT="525e9bf7882ff51b7e197a9fd2ca9ba0b19af0c9"
TESTBED_OLD_COMMIT="99783ee7f10314b52af71ce0528db7305f461f44"
TESTBED_NEW_COMMIT="cc6b619a4474913468722ab16000adbcadccce3f"

build_base_old() {
  echo "==> Building qkdnetsim:base-old (upstream model, matched workload, 1cda34c)"
  local ctx
  ctx="$(mktemp -d)"
  trap 'rm -rf "'"${ctx}"'"' RETURN
  git -C "${REPO_ROOT}" archive "${UPSTREAM_OLD_COMMIT}" | tar -x -C "${ctx}"
  python3 "${SCRIPT_DIR}/instrument-baseline.py" "${ctx}"
  docker build -t qkdnetsim:base-old -f "${SCRIPT_DIR}/Dockerfile.base-old" "${ctx}"
}

build_base_new() {
  echo "==> Building qkdnetsim:base-new (upstream model, matched workload, 525e9bf)"
  local ctx
  ctx="$(mktemp -d)"
  trap 'rm -rf "'"${ctx}"'"' RETURN
  git -C "${REPO_ROOT}" archive "${UPSTREAM_NEW_COMMIT}" | tar -x -C "${ctx}"
  python3 "${SCRIPT_DIR}/instrument-baseline.py" "${ctx}"
  docker build -t qkdnetsim:base-new -f "${SCRIPT_DIR}/Dockerfile.base-new" "${ctx}"
}

build_base_new_reference() {
  echo "==> Building qkdnetsim:base-new-reference (upstream model, discrete-event Padua reference)"
  local ctx
  ctx="$(mktemp -d)"
  trap 'rm -rf "'"${ctx}"'"' RETURN
  git -C "${REPO_ROOT}" archive "${UPSTREAM_NEW_COMMIT}" | tar -x -C "${ctx}"
  python3 "${SCRIPT_DIR}/instrument-baseline.py" "${ctx}" default
  docker build -t qkdnetsim:base-new-reference -f "${SCRIPT_DIR}/Dockerfile.base-new" "${ctx}"
}

build_testbed_old() {
  echo "==> Building qkdnetsim-testbed:old (our adaptation, 99783ee, ns-3.46, no PQC)"
  local ctx
  ctx="$(mktemp -d)"
  trap 'rm -rf "'"${ctx}"'"' RETURN
  git -C "${REPO_ROOT}" archive "${TESTBED_OLD_COMMIT}" | tar -x -C "${ctx}"
  python3 "${SCRIPT_DIR}/instrument-testbed.py" "${ctx}"
  docker build \
    --label org.qkdnetsim.comparison.revision="${TESTBED_OLD_COMMIT}" \
    --label org.qkdnetsim.comparison.instrumentation="matched-app-traces-v7" \
    -t qkdnetsim-testbed:old -f "${ctx}/docker/Dockerfile" "${ctx}"
}

build_testbed_new() {
  echo "==> Building qkdnetsim-testbed:new (our adaptation, cc6b619, ns-3.48, PQC)"
  local ctx
  ctx="$(mktemp -d)"
  trap 'rm -rf "'"${ctx}"'"' RETURN
  git -C "${REPO_ROOT}" archive "${TESTBED_NEW_COMMIT}" | tar -x -C "${ctx}"
  python3 "${SCRIPT_DIR}/instrument-testbed.py" "${ctx}"
  docker build \
    --label org.qkdnetsim.comparison.revision="${TESTBED_NEW_COMMIT}" \
    --label org.qkdnetsim.comparison.instrumentation="matched-app-traces-v8" \
    -t qkdnetsim-testbed:new -f "${ctx}/docker/Dockerfile" "${ctx}"
}

case "${1:-all}" in
  check)
    echo "Docker: $(docker version --format '{{.Client.Version}}')"
    echo "Python: $(python3 --version)"
    exit 0
    ;;
  base-old)     build_base_old ;;
  base-new)     build_base_new ;;
  base-new-reference) build_base_new_reference ;;
  testbed-old)  build_testbed_old ;;
  testbed-new)  build_testbed_new ;;
  all)
    build_base_old
    build_base_new
    build_base_new_reference
    build_testbed_old
    build_testbed_new
    ;;
  *)
    echo "Usage: $0 [check|base-old|base-new|base-new-reference|testbed-old|testbed-new|all]" >&2
    exit 1
    ;;
esac

echo "==> Done. Images:"
docker images --filter=reference='qkdnetsim:base-*'
docker images --filter=reference='qkdnetsim-testbed:*'
