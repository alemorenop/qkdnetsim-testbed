# Imagen base compartida por los 6 hosts del escenario del paper.
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

# --- QKDNetSim: se copia el checkout local (ya incluye paper-6hosts/ y el CMakeLists.txt actualizado) ---
COPY . /opt/ns-3-dev/contrib/qkdnetsim

WORKDIR /opt/ns-3-dev

# --- Patches oficiales sobre src/stats/model/gnuplot.{cc,h} ---
RUN git apply --check contrib/qkdnetsim/patches/gnuplot_cc.patches && \
    git apply --check contrib/qkdnetsim/patches/gnuplot_h.patches && \
    git apply contrib/qkdnetsim/patches/gnuplot_h.patches && \
    git apply contrib/qkdnetsim/patches/gnuplot_cc.patches

# --- Configura y compila SOLO nuestros 6 binarios ---
# (--enable-examples es obligatorio: ahi viven, pero "./ns3 build" a secas
# compilaria tambien el resto de ejemplos de qkdnetsim, y uno de ellos
# [examples_qkdnetsim_etsi_combined_input.cc] requiere --enable-mpi, que no usamos)
RUN ./ns3 configure --enable-examples && \
    ./ns3 build \
        paper_host1_pp_alice \
        paper_host2_pp_bob \
        paper_host3_kms_alice \
        paper_host4_kms_bob \
        paper_host5_etsi014_alice \
        paper_host6_etsi014_bob

# --- Entrypoint: pone en promiscuo y limpia la IP de kernel de cada interfaz ---
COPY docker/entrypoint.sh /opt/entrypoint.sh
RUN chmod +x /opt/entrypoint.sh

WORKDIR /opt/ns-3-dev
ENTRYPOINT ["/opt/entrypoint.sh"]
CMD ["/bin/bash"]
