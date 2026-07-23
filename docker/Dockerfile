# Base image shared by the emulation scenarios (point-to-point and key-relay).
# Build context = root of this qkdnetsim checkout (contrib/qkdnetsim), e.g.:
#   docker build -t qkdnetsim-6vm-emulation:latest -f docker/Dockerfile .
# (run from inside contrib/qkdnetsim)

FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive

# --- Official QKDNetSim dependencies ---
# build-essential + ninja-build: ./ns3 needs a generator (make or ninja) that
# the ubuntu:22.04 base image doesn't ship by default (unlike a desktop Ubuntu).
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential ninja-build \
    gcc g++ python3 python3-dev mercurial bzr gdb valgrind gsl-bin doxygen graphviz imagemagick \
    libboost-all-dev git flex bison tcpdump sqlite3 \
    libsqlite3-dev libxml2 libxml2-dev libgtk2.0-0 libgtk2.0-dev uncrustify \
    libcrypto++-dev libcrypto++-doc libcrypto++-utils unzip wget uuid-dev cmake \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# --- Extras only needed for the emulation mode inside the container ---
# iproute2 -> "ip link set ethX promisc on"   (required for EmuFdNetDevice)
# ethtool   -> disable checksum/segmentation offload on the real interface
# iputils-ping -> connectivity debugging between containers
RUN apt-get update && apt-get install -y --no-install-recommends \
    iproute2 ethtool iputils-ping \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt

# --- ns-3.46 (clean clone, no patches yet) ---
RUN git clone -b ns-3.46 https://gitlab.com/nsnam/ns-3-dev.git

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
RUN ./ns3 configure --enable-examples && \
    ./ns3 build \
        p2p_host1_pp_alice \
        p2p_host2_pp_bob \
        p2p_host3_kms_alice \
        p2p_host4_kms_bob \
        p2p_host5_etsi014_alice \
        p2p_host6_etsi014_bob \
        relay_host1_pp_alice \
        relay_host2_pp_relay_a \
        relay_host3_pp_relay_b \
        relay_host4_pp_bob \
        relay_host5_kms_alice \
        relay_host6_kms_relay \
        relay_host7_kms_bob \
        relay_host8_etsi014_alice \
        relay_host9_etsi014_bob

# --- Entrypoint: sets each interface to promiscuous mode and clears its kernel IP ---
COPY docker/entrypoint.sh /opt/entrypoint.sh
RUN sed -i 's/\r$//' /opt/entrypoint.sh && chmod +x /opt/entrypoint.sh

WORKDIR /opt/ns-3-dev
ENTRYPOINT ["/opt/entrypoint.sh"]
CMD ["/bin/bash"]
