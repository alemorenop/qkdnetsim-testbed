# QKDNetSim — 6-Host Docker Emulation (Fig. 3 Reproduction)

Este repositorio es una copia de trabajo de **QKDNetSim** (ver más abajo la documentación original del proyecto) a la que se le ha añadido un escenario propio: la reproducción del experimento de emulación de red descrito en la Fig. 3 de

> Mehic, M., Dervisevic, E., Burdiak, P., Lipovac, V., Fazio, P. and Voznak, M., 2024. *Emulation of quantum key distribution networks*. IEEE Network, 39(1), pp.116-123. https://doi.org/10.1109/MNET.2024.3398404

En ese experimento, cada componente de una red QKD (post-processing Alice/Bob, KMS Alice/Bob, apps ETSI 014 Alice/Bob) corre en su **propia máquina**, comunicándose por red real en vez de estar todo simulado dentro de un único proceso ns-3. Aquí se reproduce exactamente esa topología usando **6 contenedores Docker**, cada uno con su propio proceso ns-3 independiente.

```
HOST1 (post-processing Alice) ──sifting──> HOST2 (post-processing Bob)
        │ entrega clave                          entrega clave │
        v                                                       v
HOST3 (KMS Alice) <────────── transform_keys ──────────> HOST4 (KMS Bob)
        │ GET_KEY (ETSI 014)                    GET_KEY (ETSI 014) │
        v                                                       v
HOST5 (ETSI 014 Alice) ──────── tráfico cifrado ──────> HOST6 (ETSI 014 Bob)
```

## Qué se ha añadido sobre QKDNetSim

- **[`contrib/qkdnetsim/examples/paper-6hosts/`](examples/paper-6hosts/)** — 6 programas ns-3 independientes (`host1_pp_alice.cc` ... `host6_etsi014_bob.cc`), uno por rol/contenedor. A diferencia de los ejemplos originales del módulo (que modelan ambos extremos de cada enlace en un único proceso), estos usan la API de bajo nivel de QKDNetSim para que cada rol viva en su propio proceso, comunicándose por red real vía `EmuFdNetDevice`.
- **[`contrib/qkdnetsim/docker/`](docker/)** — imagen Docker compartida (`Dockerfile`), definición de los 6 contenedores y las 7 redes punto a punto entre ellos (`docker-compose.yml`), script de arranque que detecta automáticamente qué interfaz real corresponde a qué rol (`entrypoint.sh`, necesario porque Docker no garantiza el orden de conexión de redes entre reinicios), y un script de verificación end-to-end (`verify.sh`).
- **Fix en [`model/qkd-kms-queue-logic.h`](model/qkd-kms-queue-logic.h)**: `m_numberOfQueues` se usaba sin inicializar en el constructor de `QKDKMSQueueLogic`, causando una reserva de memoria descontrolada (varios GB) al arrancar cualquier KMS. Se le da el valor por defecto documentado (3) directamente en la declaración.
- **[`examples/CMakeLists.txt`](examples/CMakeLists.txt)**: entradas de build para los 6 binarios nuevos.

## Arranque rápido

```bash
cd contrib/qkdnetsim
docker build -t qkdnetsim-6vm-emulation:latest -f docker/Dockerfile .
docker compose -f docker/docker-compose.yml up -d
./docker/verify.sh
```

---

# Documentación original de QKDNetSim

## Quantum Key Distribution Network Simulation Module for NS-3

As research in Quantum Key Distribution (QKD) technology grows larger and more complex, the need for highly accurate and scalable simulation technologies becomes important to assess the practical feasibility and foresee difficulties in the practical implementation of theoretical achievements. Due to the specificity of QKD link which requires optical and Internet connection between the network nodes, it is very costly to deploy a complete testbed containing multiple network hosts and links to validate and verify a certain network algorithm or protocol. The network simulators in these circumstances save a lot of money and time in accomplishing such task. A simulation environment offers the creation of complex network topologies, a high degree of control and repeatable experiments, which in turn allows researchers to conduct exactly the same experiments and confirm their results.

The aim of Quantum Key Distribution Network Simulation Module (QKDNetSim) project was not to develop the entire simulator from scratch but to develop the QKD simulation module in some of the already existing well-proven simulators. QKDNetSim is intended to facilitate additional understanding of QKD technology with respect to the existing network solutions. It seeks to serve as the natural playground for taking the further steps into this research direction (even towards practical exploitation in subsequent projects or product design).

**QKDNetSim implements the full functional Key Management System (KMS) with key-relay functionality supporting ETSI GS QKD 014 and ETSI GS QKD 004 key delivery interfaces.**

## Documentation

The detailed documentation is available on webpage https://www.qkdnetsim.info

## Deployment

 
- The latest version of the code is compatible with NS-3 version 3.46.
- Thus, one should follow installation requirements from the NS-3 official website (https://www.nsnam.org/wiki/Installation).   
- The code has been successfully tested on Ubuntu 22.04. 
- QKDNetSim v2.0 module is ***NOT*** compatible with QKDNetSim version 1.0 (https://v1.qkdnetsim.info). QKDNetSim v2.0 module was written independently and from scratch.


## Installation

QKDNetSim includes QKDEncryptor class that relies on cryptographic algorithms and schemes from Crypto++ open-source C++ class cryptographic library. Currently, QKD crypto supports several cryptographic algorithms and cryptographic hashes, including One-Time Pad (OTP) cipher, Advanced Encryption Standard (AES) block cipher, VMAC message authentication code (MAC) algorithm, and others.
 
1. Install prerequisites libreries:

	```bash
	sudo apt-get install gcc g++ python3 python3-dev mercurial bzr gdb valgrind gsl-bin doxygen graphviz imagemagick -y  && \
	sudo apt-get install libboost-all-dev git flex bison tcpdump sqlite sqlite3 -y   && \
	sudo apt-get install libsqlite3-dev libxml2 libxml2-dev libgtk2.0-0 libgtk2.0-dev uncrustify -y  && \
	sudo apt-get install libcrypto++-dev libcrypto++-doc libcrypto++-utils unzip wget uuid-dev cmake -y
    ```

2. Install the NS-3 of version 3.46 from the

	```bash
	git clone -b ns-3.46 https://gitlab.com/nsnam/ns-3-dev.git
    ```

3. Download qkdnetsim in contrib directory

	```bash
	cd ns-3-dev/contrib
    git clone -b master https://github.com/QKDNetSim/qkdnetsim
    ```

4. Check patches. They should report no error

	```bash
    cd ..
	git apply --check contrib/qkdnetsim/patches/gnuplot_cc.patches
	git apply --check contrib/qkdnetsim/patches/gnuplot_h.patches
    ```

5. Apply patches

	```bash
	git apply  contrib/qkdnetsim/patches/gnuplot_h.patches
	git apply  contrib/qkdnetsim/patches/gnuplot_cc.patches
    ```

6. Configure NS-3 with qkdnetsim

	```bash
	./ns3 configure --enable-mpi --enable-examples
    ```

7. Run qkdnetsim examples

	```bash
	./ns3 run examples_qkdnetsim_etsi_014
	./ns3 run examples_qkdnetsim_etsi_004
	./ns3 run examples_qkdnetsim_secoqc
	./ns3 run examples_qkdnetsim_etsi_combined_input
	./ns3 run examples_qkdnetsim_etsi_014_emulation_tap
    ```

## Authors

QKDNetSim is maintained by:

- Department of Telecommunications (www.tk.etf.unsa.ba)  
  Faculty of Electrical Engineering  
  University of Sarajevo  
  Zmaja od Bosne bb  
  71000 Sarajevo  
  Bosnia and Herzegovina  
- Department of Telecommunications (www.comtech.vsb.cz)
  VSB Technical University of Ostrava  
  17 . listopadu 15/2172  
  Ostrava-Poruba 708 33  
  Czech Republic  

**Main developers:**

- Emir Dervisevic
- Miroslav Voznak
- Miralem Mehic

Contact us via email (miralem[at]mehic.info).

## Cite 

- Dervisevic, E., Voznak, M. and Mehic, M., 2024. Large-Scale Quantum Key Distribution Network Simulator. Journal of Optical Communications and Networking, doi: https://www.doi.org/10.1364/JOCN.503356
- Dervisevic, E., Tankovic, A., Kaljic, E., Voznak, M. and Mehic, M., 2025. Design of a Key Management System for Efficient Key Supply in Quantum Key Distribution Networks. Journal of Optical Communications and Networking, doi: https://www.doi.org/10.1364/JOCN.577670
- Dervisevic, E., Tankovic, A., Fazel, E., Kompella, R., Fazio, P., Voznak, M. and Mehic, M., 2025. Quantum Key Distribution Networks – Key Management: A Survey. ACM Computing Surveys, 57(10), pp. 1–36, doi: https://www.doi.org/10.1145/3730575
- Mehic, M., Dervisevic, E., Burdiak, P., Lipovac, V., Fazio, P. and Voznak, M., 2024. Emulation of quantum key distribution networks. IEEE Network, 39(1), pp.116-123. doi: https://www.doi.org/10.1109/MNET.2024.3398404
- Mehic, M., Dervisevic, E., Fazio, P. and Voznak, M., 2025. Virtual Quantum Key Distribution Network Ecosystem: The National Czech QKD Network. IEEE Network., 39(3), pp.173-179. doi: https://www.doi.org/10.1109/MNET.2025.3540705

## Acknowledgment 

Development of QKDNetSim was supporty within projects #VJ01010008 “Network Cybersecurity in Post-Quantum Era” by the Ministry of the Interior of Czech Republic in program Impakt, Ministry of Science, Higher Education and Youth of Canton Sarajevo, Bosnia and Herzegovina (27-02-35-37082-1/23), NATO SPS G5894 project ”Quantum Cybersecurity in 5G Networks (QUANTUM5)” and H2020 project OPENQKD (No. 857156).

![NESPOQ](https://www.qkdnetsim.info/wp-content/uploads/2025/12/cz.png)
![MONKS](https://www.qkdnetsim.info/wp-content/uploads/2025/12/monks.png)
