# Imagen base compartida por los escenarios de emulacion (point-to-point y key-relay).
# Build context = raiz de este checkout de qkdnetsim (contrib/qkdnetsim), p.ej.:
#   docker build -t qkdnetsim-6vm-emulation:latest -f docker/Dockerfile .
# (ejecutado desde dentro de contrib/qkdnetsim)

FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive

# --- Dependencias oficiales de QKDNetSim ---
# build-essential + ninja-build: ./ns3 necesita un generador (make o ninja) que
# la imagen base ubuntu:22.04 no trae por defecto (a diferencia de un Ubuntu de escritorio).
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential ninja-build \
    gcc g++ python3 python3-dev mercurial bzr gdb valgrind gsl-bin doxygen graphviz imagemagick \
    libboost-all-dev git flex bison tcpdump sqlite3 \
    libsqlite3-dev libxml2 libxml2-dev libgtk2.0-0 libgtk2.0-dev uncrustify \
    libcrypto++-dev libcrypto++-doc libcrypto++-utils unzip wget uuid-dev cmake \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# --- Extras solo necesarios para el modo emulacion dentro del contenedor ---
# iproute2 -> "ip link set ethX promisc on"   (obligatorio para EmuFdNetDevice)
# ethtool   -> desactivar offload de checksum/segmentacion en la interfaz real
# iputils-ping -> depuracion de conectividad entre contenedores
RUN apt-get update && apt-get install -y --no-install-recommends \
    iproute2 ethtool iputils-ping \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt

# --- ns-3.46 (clon limpio, sin patches todavia) ---
RUN git clone -b ns-3.46 https://gitlab.com/nsnam/ns-3-dev.git

# --- QKDNetSim: se copia el checkout local (ya incluye point-to-point/, key-relay/ y el CMakeLists.txt actualizado) ---
COPY . /opt/ns-3-dev/contrib/qkdnetsim

WORKDIR /opt/ns-3-dev

# --- Patches oficiales sobre src/stats/model/gnuplot.{cc,h} ---
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

# --- Configura y compila SOLO los binarios de nuestros escenarios ---
# (--enable-examples es obligatorio: ahi viven, pero "./ns3 build" a secas
# compilaria tambien el resto de ejemplos de qkdnetsim, y uno de ellos
# [examples_qkdnetsim_etsi_combined_input.cc] requiere --enable-mpi, que no usamos)
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

# --- Entrypoint: pone en promiscuo y limpia la IP de kernel de cada interfaz ---
COPY docker/entrypoint.sh /opt/entrypoint.sh
RUN sed -i 's/\r$//' /opt/entrypoint.sh && chmod +x /opt/entrypoint.sh

WORKDIR /opt/ns-3-dev
ENTRYPOINT ["/opt/entrypoint.sh"]
CMD ["/bin/bash"]
