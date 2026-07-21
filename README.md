# QKDNetSim — Laboratorio de escenarios de emulación en Docker

Este repositorio es un entorno de pruebas (testbed) construido sobre **QKDNetSim** (ver más abajo la documentación original del proyecto) al que se le van añadiendo distintos **escenarios de emulación en red real**, cada uno con sus propios contenedores Docker, uno por nodo/rol, comunicándose por red de verdad en vez de estar todo simulado dentro de un único proceso ns-3.

## Escenarios

### 1. Point-to-point — enlace directo Alice-Bob (`examples/point-to-point/`)

Adaptación a contenedores del experimento de emulación de red descrito en la Fig. 3 de:

> Mehic, M., Dervisevic, E., Burdiak, P., Lipovac, V., Fazio, P. and Voznak, M., 2024. *Emulation of quantum key distribution networks*. IEEE Network, 39(1), pp.116-123. https://doi.org/10.1109/MNET.2024.3398404

El paper describe el experimento simulando ambos extremos de cada enlace dentro de un único proceso ns-3; aquí cada rol vive en su propio contenedor Docker, comunicándose por red real (`EmuFdNetDevice`) en vez de estar todo dentro del mismo proceso — mismo escenario lógico, pero desplegado como 6 nodos independientes de verdad.

6 contenedores: post-processing Alice/Bob, KMS Alice/Bob, apps ETSI 014 Alice/Bob, con Alice y Bob comunicándose directamente (sin nodo intermedio).

```
HOST1 (post-processing Alice) ──sifting──> HOST2 (post-processing Bob)
        │ entrega clave                          entrega clave │
        v                                                       v
HOST3 (KMS Alice) <────────── transform_keys ──────────> HOST4 (KMS Bob)
        │ GET_KEY (ETSI 014)                    GET_KEY (ETSI 014) │
        v                                                       v
HOST5 (ETSI 014 Alice) ──────── tráfico cifrado ──────> HOST6 (ETSI 014 Bob)
```

| Host | Rol | IPs (red ↔ vecino) |
|---|---|---|
| HOST1 | post-processing Alice | 192.168.11.1 (↔H2), 192.168.13.1 (↔H3) |
| HOST2 | post-processing Bob | 192.168.11.2 (↔H1), 192.168.24.2 (↔H4) |
| HOST3 | KMS Alice | 192.168.13.3 (↔H1), 192.168.34.3 (↔H4), 192.168.35.3 (↔H5) |
| HOST4 | KMS Bob | 192.168.24.4 (↔H2), 192.168.34.4 (↔H3), 192.168.46.4 (↔H6) |
| HOST5 | ETSI014 Alice | 192.168.35.5 (↔H3), 192.168.56.5 (↔H6) |
| HOST6 | ETSI014 Bob | 192.168.46.6 (↔H4), 192.168.56.6 (↔H5) |

Arranque rápido:
```bash
cd contrib/qkdnetsim
docker build -t qkdnetsim-6vm-emulation:latest -f docker/Dockerfile .
docker compose -f docker/docker-compose.yml up -d
./docker/verify.sh
```

### 2. Key relay — nodo intermedio sin enlace directo Alice-Bob (`examples/key-relay/`)

Variante en la que Alice y Bob no tienen enlace QKD directo, sino que pasan por un nodo intermedio ("trusted node") con su propio KMS, que hace de relay entre ambos usando la funcionalidad de relay ya integrada en `QKDKeyManagerSystemApplication` (`RelayConsumption`/`WasteRelay`, `ConfigureRSBuffers`, etc.), más un mecanismo de cifrado salto-a-salto (`skey_create`, tipo OTP) para que Alice y Bob acaben compartiendo una clave real sin que ninguno de los dos vea la clave "en crudo" del otro tramo. Funciona de extremo a extremo, con tráfico cifrado real fluyendo entre HOST8 y HOST9 (verificado con `tcpdump`).

Topología (9 hosts):

```
HOST1 (PP Alice) ──qkd──> HOST2 (PP Relay-A)          HOST3 (PP Relay-B) <──qkd── HOST4 (PP Bob)
       │                                                                                  │
       v                                                                                  v
HOST5 (KMS Alice) ─────────────── relay ───────────> HOST6 (KMS Relay) <─── relay ─────── HOST7 (KMS Bob)
       │                                                                                  │
       v                                                                                  v
HOST8 (ETSI014 Alice) ─────────────────── tráfico cifrado ───────────────────> HOST9 (ETSI014 Bob)
```

| Host | Rol | IPs (red ↔ vecino) |
|---|---|---|
| HOST1 | PP Alice | 192.168.111.1 (↔H2), 192.168.112.1 (↔H5) |
| HOST2 | PP Relay-A | 192.168.111.2 (↔H1), 192.168.113.2 (↔H6) |
| HOST3 | PP Relay-B | 192.168.114.3 (↔H4), 192.168.115.3 (↔H6) |
| HOST4 | PP Bob | 192.168.114.4 (↔H3), 192.168.116.4 (↔H7) |
| HOST5 | **KMS Alice** | 192.168.112.5 (↔H1), 192.168.117.5 (↔H6), 192.168.119.5 (↔H8) |
| HOST6 | **KMS Relay** (nodo intermedio) | 192.168.113.6 (↔H2), 192.168.115.6 (↔H3), 192.168.117.6 (↔H5), 192.168.118.6 (↔H7) |
| HOST7 | **KMS Bob** | 192.168.116.7 (↔H4), 192.168.118.7 (↔H6), 192.168.120.7 (↔H9) |
| HOST8 | ETSI014 Alice | 192.168.119.8 (↔H5), 192.168.121.8 (↔H9) |
| HOST9 | ETSI014 Bob | 192.168.120.9 (↔H7), 192.168.121.9 (↔H8) |

Arranque:
```bash
cd contrib/qkdnetsim
docker build -t qkdnetsim-6vm-emulation:latest -f docker/Dockerfile .
docker compose -f docker/docker-compose.key-relay.yml up -d
```
Con el `depends_on` de más abajo, `up -d` ya intenta arrancar los 9 hosts en el orden correcto por si solo (ver "Arranque ordenado" más abajo); aun así, si algún contenedor se queda colgado sin dar señales de vida (la otra carrera conocida, ver caveat), lanza el watchdog externo para que lo recree:
```bash
./docker/watchdog-all.sh 30 15   # 30s por ronda: hay hosts con appStartTime=20, un timeout mas corto los recrea sin necesidad (ver leccion de watchdog-all.sh mas abajo)
```

#### Cómo comprobar que funciona de verdad

No basta con ver los 9 contenedores en `Up` — eso solo dice que el proceso no ha muerto. Para una comprobación real (logs esperados por host + una muestra de tráfico) usa:
```bash
./docker/verify-key-relay.sh [segundos_de_captura]   # por defecto 10s
```
Esto imprime, por cada uno de los 9 hosts, si está generando la actividad esperada (`Clave entregada`, `almacena clave`, `sirve clave`, `Peticion GET_KEY`, según el rol), confirma que HOST5/HOST7 están **sirviendo** claves a las apps (no solo relayando), y genera capturas `.pcap` reales en `docker/captures/` para 4 enlaces clave de la cadena (HOST1→HOST5, HOST5→HOST6, HOST6→HOST7, HOST8↔HOST9) que puedes abrir con Wireshark de verdad:
```bash
wireshark docker/captures/*.pcap
```
El HOST8↔HOST9 es el que demuestra el resultado final: tráfico TCP cifrado con secuencias creciendo de forma continua entre las dos apps ETSI014, que es justo lo que Alice y Bob consiguen sin tener enlace QKD directo entre ellos.

#### Bugs de la librería encontrados y corregidos para que este escenario funcione

- **Socket TCP roto en silencio, sin reintento.** `Connect()` se llama una única vez al arrancar; si la conexión inicial pierde la carrera contra el listener remoto (que puede no estar escuchando todavía), el socket queda muerto para siempre sin que ningún callback de error se dispare — la app sigue "enviando" en bucle sin que llegue un solo paquete real al otro lado. Confirmado con `strace` (cero `write()`/`read()` reales pese a actividad interna continua) en **tres puntos distintos** del pipeline, cada uno arreglado con un watchdog de aplicación que detecta la ausencia de confirmación de conexión/respuesta y fuerza un socket nuevo:
  - App ETSI014 (`QKDApp014`) → su KMS local (`qkd-app-014.{h,cc}`).
  - KMS → KMS del siguiente salto en el relay (`qkd-key-manager-system-application.{h,cc}`).
  - Post-processing (`QKDPostprocessingApplication`) → su KMS local (`qkd-postprocessing-application.{h,cc}`).
- **Contabilidad de bits atascada en READY.** En `Relay()`, `StoreKey(key,true)` seguido de `MarkKey(id,INIT)` tiene efecto neto cero sobre `m_currentKeyBit`, con lo que `CheckState()` deja de reflejar el vaciado real del buffer de relay una vez cruzado el umbral por primera vez. Mitigado comprobando también `GetSBitCount()` (el recuento real, no el acumulado histórico) en `SBufferClientCheck`.
- **`skey_create` asumía una única clave de tamaño exacto.** El material de cifrado salto-a-salto se buscaba como una sola clave de tamaño exacto en bits, pero el buffer solo contiene claves de tamaño por defecto (2048 bits); se sustituyó por el mismo patrón de fusión de varias claves (`GetTransformCandidate`) que ya se usaba en otras partes del fichero.
- **Bookkeeping de peticiones HTTP desbalanceado.** El reenvío multi-salto de `skey_create` mandaba la petición al siguiente salto sin registrarla en `m_httpRequestsQueryKMS`, y la respuesta hacía `pop()` de una cola vacía → **caída del proceso** (`NS_FATAL_ERROR("HTTP query for this KMS is empty!")`).
- **Watchdog de relay con condición de carrera propia.** El watchdog del socket KMS↔KMS (primer punto de la lista, tramo relay) podía disparar sobre un intento ya completado si una respuesta real llegaba justo antes de que venciera su timeout de 5s y ya había arrancado un intento nuevo, intentando marcar como obsoleta una clave que ya no existía → **otra caída del proceso** (`NS_FATAL_ERROR("Key not found for marking!")`). Arreglado con un contador de generación por destino: el watchdog comprueba si sigue siendo el intento vigente antes de tocar nada.

**Requisito de temporización:** `--appStartTime=20` en HOST8/HOST9 (no baja, ver comentarios en `docker-compose.key-relay.yml`) — `QKDApp014` conecta con su KMS una sola vez al arrancar; con menos margen puede intentarlo antes de que el KMS esté escuchando, perdiendo la carrera igual que los sockets de arriba.

#### Arranque ordenado (`depends_on`) para reducir la carrera de conexión

Los watchdogs de arriba son la solución **reactiva** (detectar el silencio y reintentar) a un problema de fondo que es siempre el mismo: no había ninguna garantía de que un KMS estuviera ya en `Listen()` antes de que su(s) cliente(s) intentaran `Connect()` — `docker-compose.key-relay.yml` no tenía `depends_on`, y dentro de ns-3 tanto los KMS como las apps de post-processing arrancan en el instante 0 de simulación, exactamente a la vez que sus clientes.

Para atacarlo de forma **proactiva**, además de los watchdogs (que se mantienen como red de seguridad, no se han quitado), se añadió:
- Dos trazas nuevas — `ListenReady` en `QKDKeyManagerSystemApplication` y `AppListenReady` en `QKDApp014` — que se disparan justo cuando los sockets de escucha relevantes ya están en `Bind()+Listen()`.
- Un marcador de texto por host enganchado a esas trazas (p.ej. `"[HOST5] KMS Alice escuchando"`), igual que los marcadores `almacena clave`/`sirve clave` que ya existían.
- `entrypoint.sh` ahora duplica todo su stdout a `/tmp/qkdnetsim.log` **dentro de cada contenedor** — necesario porque un `HEALTHCHECK` de Docker se ejecuta en el namespace del propio contenedor y no tiene visibilidad sobre `docker logs` (eso es una vista del lado del host sobre el log driver).
- Un `HEALTHCHECK` por host (HOST5, HOST6, HOST7, HOST9) que hace `grep` de su marcador en ese fichero, y una cadena de `depends_on: condition: service_healthy` en `docker-compose.key-relay.yml` que sigue la topología real (grafo acíclico, sin ciclos):

```
HOST7 (raiz, sin dependencias)
  ← HOST6            ← HOST2, HOST3
  ← HOST4
  ← HOST9            ← HOST8 (tambien depende de HOST5)
       ← HOST5        ← HOST1, HOST8
```

Con esto, `docker compose up -d` arranca HOST7 primero, espera a que confirme que escucha, luego HOST6/HOST4/HOST9, y así sucesivamente — en vez de lanzar los 9 a ciegas y confiar en que la suerte (o el reintento del watchdog) lo arregle después. En las pruebas de esta sesión, con `depends_on` activo los hosts que antes necesitaban el watchdog de reconexión (HOST1-4, sobre el enlace post-processing→KMS) arrancaron con **0 reconexiones** — la conexión se estableció bien a la primera siempre.

**Importante — esto NO sustituye a `watchdog-all.sh`.** `depends_on` solo ordena *cuándo* arranca cada contenedor; no puede hacer nada contra la otra carrera (`RealtimeSimulatorImpl`, ver caveat justo abajo), que cuelga un proceso *antes* de que intente conectar con nadie. De hecho, durante las pruebas de esta misma sesión, con `depends_on` ya funcionando, HOST1 se quedó colgado por esa segunda carrera varias veces en despliegues distintos — en unos casos recuperado por `watchdog-all.sh`, en otro se recuperó solo tras arrancar más lento de lo normal. Los dos mecanismos son complementarios: uno reduce la carrera de conexión, el otro sigue haciendo falta para la carrera de arranque del hilo de `RealtimeSimulatorImpl`.

#### Caveat conocido, no arreglado: carrera de arranque de `RealtimeSimulatorImpl`

Confirmada con `gdb`/`strace`: dos hilos en bucle de futex sin avanzar el reloj simulado ni hacer ninguna syscall de red, que a veces deja algún contenedor colgado desde el primer instante, sin producir ninguna salida (ni siquiera el marcador `ListenReady` de arriba, porque ocurre antes de que el proceso llegue a montar ningún socket). No es específico de este escenario, de estos fixes ni del `depends_on` — es interno al núcleo de ns-3 (`realtime-simulator-impl.cc`/`wall-clock-synchronizer.cc`), usado por todos los escenarios en tiempo real, y tocarlo ahí es demasiado arriesgado. Se mitiga (no se corrige de raíz) con dos mecanismos combinados:
- `STARTUP_JITTER_MAX_MS` (variable de entorno en `docker-compose.key-relay.yml`, ya activada por defecto para HOST1-7): retraso aleatorio antes de arrancar cada binario, para no lanzar todos los procesos exactamente en el mismo instante y reducir la contención que dispara la carrera.
- `./docker/watchdog-all.sh [timeout_s] [rondas]`: vigila los 9 contenedores tras el `docker compose up -d` y **recrea el contenedor entero** (no solo el proceso — un simple restart del binario dentro del mismo contenedor casi nunca recupera el hilo colgado) de cualquiera que no muestre actividad real dentro del timeout. `watchdog-host9.sh` es la misma lógica aplicada a un único host/contenedor, útil para recrear uno concreto suelto sin tocar el resto.

**Lección de `watchdog-all.sh`:** el `timeout_s` por ronda tiene que ser mayor que el `appStartTime` más alto en juego (20s para HOST8/HOST9). Con un timeout más corto (p.ej. 15s, usado en pruebas iniciales de esta sesión) el watchdog recreaba HOST8/HOST9 en bucle **sin necesidad** — no porque estuvieran colgados, sino porque su marcador de actividad (`Peticion GET_KEY`) no puede aparecer antes de que pase su `appStartTime`, y cada recreación reinicia esa cuenta desde cero. Por eso el ejemplo de arriba usa `30`, no `15`.

**Lección operativa (recreación parcial):** si necesitas recrear contenedores a mano para depurar algo, hazlo con `docker compose down --remove-orphans && docker compose up -d` (todos a la vez), no con `--force-recreate hostX` sobre unos pocos. Un peer que se queda vivo mientras el otro extremo de su enlace se recrea puede quedarse con una conexión TCP apuntando al contenedor viejo (ya destruido), y sigue "recibiendo" sin error aparente mientras en realidad no llega nada — mismo síntoma que los bugs de arriba, pero causado por el despliegue, no por el código. En las pruebas de esta sesión, un caso de un único host (HOST9, luego también HOST1) atascado en la carrera de `RealtimeSimulatorImpl` tras varias recreaciones individuales solo se resolvió con un reset completo (`down` + `up` de los 9 a la vez) — recrear una y otra vez el mismo contenedor en solitario, mientras sus 8 hermanos llevan minutos corriendo, no parece ayudar a esquivar la carrera.

## Qué se ha añadido sobre QKDNetSim

- **[`contrib/qkdnetsim/examples/point-to-point/`](examples/point-to-point/)** — 6 programas ns-3 independientes (`host1_pp_alice.cc` ... `host6_etsi014_bob.cc`), uno por rol/contenedor (escenario 1, adaptación en contenedores de la Fig. 3 del paper). A diferencia de los ejemplos originales del módulo (que modelan ambos extremos de cada enlace en un único proceso), estos usan la API de bajo nivel de QKDNetSim para que cada rol viva en su propio proceso, comunicándose por red real vía `EmuFdNetDevice`.
- **[`contrib/qkdnetsim/examples/key-relay/`](examples/key-relay/)** — escenario 2, mismo patrón aplicado a una topología de 3 sitios con relay (9 binarios, ver `examples/CMakeLists.txt` y las reglas de compilación añadidas en `docker/Dockerfile`).
- **[`contrib/qkdnetsim/docker/`](docker/)** — imagen Docker compartida (`Dockerfile`), definición de los 6 contenedores y las 7 redes punto a punto entre ellos del escenario 1 (`docker-compose.yml`), script de arranque que detecta automáticamente qué interfaz real corresponde a qué rol (`entrypoint.sh`, necesario porque Docker no garantiza el orden de conexión de redes entre reinicios), y un script de verificación end-to-end (`verify.sh`).
- **[`docker-compose.key-relay.yml`](docker/docker-compose.key-relay.yml)** — definición de los 9 contenedores y las 10 redes del escenario 2 (proyecto Docker Compose separado del escenario 1, ver comentario en el propio archivo), incluida la cadena `depends_on`/`healthcheck` descrita más arriba.
- **`entrypoint.sh` ampliado** con: (a) dos mecanismos opt-in (activados vía variables de entorno, sin efecto si no se definen) para mitigar la carrera de arranque de `RealtimeSimulatorImpl` que afecta al escenario 2 por tener 9 procesos arrancando a la vez en vez de 6 — arranque escalonado con retraso aleatorio (`STARTUP_JITTER_MAX_MS`) y un watchdog interno que reinicia el binario si no produce salida a tiempo (`WATCHDOG_TIMEOUT`) —; y (b) duplicado de todo el stdout a `/tmp/qkdnetsim.log` dentro del contenedor, para que los `HEALTHCHECK` de Docker (que no ven `docker logs`) puedan comprobar los marcadores de arranque.
- **[`watchdog-all.sh`](docker/watchdog-all.sh) / [`watchdog-host9.sh`](docker/watchdog-host9.sh)** — watchdogs *externos* (se ejecutan en el host, no dentro del contenedor) para la misma carrera de arranque: cuando el `WATCHDOG_TIMEOUT` interno no basta porque el hilo colgado no responde ni a un reinicio del proceso, estos recrean el contenedor entero (par veth nuevo) hasta que arranca con normalidad. `watchdog-all.sh` vigila los 9 a la vez; `watchdog-host9.sh` apunta a uno solo.
- **[`verify-key-relay.sh`](docker/verify-key-relay.sh)** — comprobación end-to-end del escenario 2 (mismo espíritu que `verify.sh` del escenario 1, adaptado a 9 hosts): chequeo de logs esperados por host, confirmación de que HOST5/HOST7 sirven claves, y capturas `.pcap` reales de 4 enlaces de la cadena en `docker/captures/` para inspeccionar con Wireshark.
- **Trazas `ListenReady`** (en [`model/qkd-key-manager-system-application.{h,cc}`](model/qkd-key-manager-system-application.h)) y **`AppListenReady`** (en [`model/qkd-app-014.{h,cc}`](model/qkd-app-014.h)) — se disparan cuando los sockets de escucha relevantes ya están activos; alimentan los marcadores que usan los `HEALTHCHECK` de `depends_on`.
- **Fix en [`model/qkd-kms-queue-logic.h`](model/qkd-kms-queue-logic.h)**: `m_numberOfQueues` se usaba sin inicializar en el constructor de `QKDKMSQueueLogic`, causando una reserva de memoria descontrolada (varios GB) al arrancar cualquier KMS. Se le da el valor por defecto documentado (3) directamente en la declaración.
- **[`examples/CMakeLists.txt`](examples/CMakeLists.txt)**: entradas de build para los binarios de cada escenario.

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
