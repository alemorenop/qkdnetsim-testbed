# Base image shared by the emulation scenarios (point-to-point and key-relay).
# Build context = root of this qkdnetsim checkout (contrib/qkdnetsim), e.g.:
#   docker build -t qkdnetsim-testbed:latest -f docker/Dockerfile .
# (run from inside contrib/qkdnetsim)

FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive

# --- Official QKDNetSim dependencies ---
# build-essential + ninja-build: ./ns3 needs a generator (make or ninja) that
# the ubuntu:22.04 base image doesn't ship by default (unlike a desktop Ubuntu).
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential ninja-build \
    gcc g++ python3 python3-dev python3-pip mercurial bzr gdb valgrind gsl-bin doxygen graphviz imagemagick \
    libboost-all-dev git flex bison tcpdump sqlite3 \
    libsqlite3-dev libxml2 libxml2-dev libgtk2.0-0 libgtk2.0-dev uncrustify \
    libcrypto++-dev libcrypto++-doc libcrypto++-utils unzip wget uuid-dev cmake \
    ca-certificates libssl-dev \
    && rm -rf /var/lib/apt/lists/*

# ns-3.48 requires CMake >= 3.25. Ubuntu 22.04 ships 3.22, so install a
# current CMake in /usr/local/bin while retaining the distro package for its
# ancillary files and dependency compatibility.
RUN python3 -m pip install --no-cache-dir "cmake>=3.25,<4"

# --- Reproducible PQC toolchain ---
# liboqs provides ML-KEM-512 and liboqs-cpp is the header-only C++ wrapper used
# by QKDNetSim's KMS. PQC remains a runtime option, but is compiled into the
# common image so the same artifact can run QKD-only and QKD+PQC scenarios.
# liboqs-cpp 0.12.0 performs a runtime version check against liboqs. Keep the
# two layers on the same release to avoid silently testing an unsupported ABI
# combination.
ARG LIBOQS_VERSION=0.12.0
ARG LIBOQS_CPP_COMMIT=2d5703f5028e55a777caf68ebab32b8c5475f3a7
RUN git clone --branch ${LIBOQS_VERSION} --depth 1 https://github.com/open-quantum-safe/liboqs.git /opt/liboqs && \
    cmake -S /opt/liboqs -B /opt/liboqs/build -GNinja \
      -DBUILD_SHARED_LIBS=ON -DOQS_BUILD_ONLY_LIB=ON \
      -DOQS_MINIMAL_BUILD="KEM_ml_kem_512" && \
    cmake --build /opt/liboqs/build && \
    cmake --install /opt/liboqs/build && \
    git clone https://github.com/open-quantum-safe/liboqs-cpp.git /opt/liboqs-cpp && \
    git -C /opt/liboqs-cpp checkout ${LIBOQS_CPP_COMMIT} && \
    cmake -S /opt/liboqs-cpp -B /opt/liboqs-cpp/build -GNinja && \
    cmake --build /opt/liboqs-cpp/build --target install && \
    ldconfig && \
    rm -rf /opt/liboqs /opt/liboqs-cpp

# --- Extras only needed for the emulation mode inside the container ---
# iproute2 -> "ip link set ethX promisc on"   (required for EmuFdNetDevice)
# ethtool   -> disable checksum/segmentation offload on the real interface
# iputils-ping -> connectivity debugging between containers
RUN apt-get update && apt-get install -y --no-install-recommends \
    iproute2 ethtool iputils-ping \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt

# --- ns-3.48, required by the current official QKDNetSim baseline ---
RUN git clone -b ns-3.48 https://gitlab.com/nsnam/ns-3-dev.git

# --- QKDNetSim: copies the local checkout (already includes point-to-point/, key-relay/ and the updated CMakeLists.txt) ---
COPY . /opt/ns-3-dev/contrib/qkdnetsim

WORKDIR /opt/ns-3-dev

# --- Official patches on src/stats/model/gnuplot.{cc,h} ---
RUN sed -i 's/\r$//' \
        contrib/qkdnetsim/patches/gnuplot_cc.patches \
        contrib/qkdnetsim/patches/gnuplot_h.patches \
        contrib/qkdnetsim/patches/realtime-simulator-clamp.patch && \
    git apply --check contrib/qkdnetsim/patches/gnuplot_cc.patches && \
    git apply --check contrib/qkdnetsim/patches/gnuplot_h.patches && \
    git apply --check contrib/qkdnetsim/patches/realtime-simulator-clamp.patch && \
    git apply contrib/qkdnetsim/patches/gnuplot_h.patches && \
    git apply contrib/qkdnetsim/patches/gnuplot_cc.patches && \
    git apply contrib/qkdnetsim/patches/realtime-simulator-clamp.patch

# --- Configure and build ONLY the binaries for our scenarios ---
# (--enable-examples is mandatory: that's where they live, but a plain
# "./ns3 build" would also build the rest of qkdnetsim's examples, and one of
# them [examples_qkdnetsim_etsi_combined_input.cc] requires --enable-mpi,
# which we don't use)
RUN ./ns3 configure --enable-examples -- -DQKDNETSIM_WITH_PQC=ON && \
    ./ns3 build \
        p2p_pp_alice \
        p2p_pp_bob \
        p2p_kms_alice \
        p2p_kms_bob \
        relay_pp_alice \
        relay_pp_relay_a \
        relay_pp_relay_b \
        relay_pp_bob \
        relay_kms_alice \
        relay_kms_trusted \
        relay_kms_bob \
        p2p_etsi014_alice \
        p2p_etsi014_bob \
        relay_etsi014_alice \
        relay_etsi014_bob \
        distributed_secoqc_site

# --- Entrypoint: sets each interface to promiscuous mode and clears its kernel IP ---
COPY docker/entrypoint.sh /opt/entrypoint.sh
RUN sed -i 's/\r$//' /opt/entrypoint.sh && chmod +x /opt/entrypoint.sh

WORKDIR /opt/ns-3-dev
ENTRYPOINT ["/opt/entrypoint.sh"]
CMD ["/bin/bash"]
