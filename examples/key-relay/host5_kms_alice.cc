/*
 * HOST5 - KMS ALICE (escenario key relay)
 *
 * A diferencia del escenario 1 (enlace directo), aqui KMS Alice NO tiene a
 * KMS Bob como vecino directo - solo alcanza a KMS Relay directamente, y a
 * KMS Bob a traves de el (2 saltos). Necesita por tanto:
 *   - Un Q-Buffer/S-Buffer LOCAL hacia el Relay (alimentado por HOST1)
 *   - Un S-Buffer de tipo RELAY hacia Bob (creado explicitamente con
 *     BootstrapRelaySBuffer, ver comentario en qkd-key-manager-system-application.h)
 *   - Una entrada de ruta directa hacia el Relay (1 salto) y otra hacia Bob
 *     via Relay (2 saltos)
 *   - Una comprobacion periodica (CheckBufferReplenishment) que mantenga
 *     relleno el buffer de relay hacia Bob, ya que QKDApp014 nunca dispara
 *     esa reposicion por si sola (no manda GET_STATUS)
 *
 *   --devPP   / --myIpPP    enlace hacia HOST1 (post-processing Alice)
 *   --devKms  / --myIpKms   enlace hacia HOST6 (KMS Relay)          [relay/transform_keys]
 *   --devEtsi / --myIpEtsi  enlace hacia HOST8 (ETSI014 Alice)      [GET_KEY]
 *
 * Contrato compartido con las demas VMs (deben coincidir exactamente):
 *   ppAliceId   -> debe coincidir con el "SetId" que usa HOST1
 *   etsiAliceId -> debe coincidir con el "appId" que usa HOST8
 *   etsiBobId   -> debe coincidir con el "appId" que usa HOST9
 */

#include "ns3/core-module.h"
#include "ns3/network-module.h"
#include "ns3/internet-module.h"
#include "ns3/applications-module.h"
#include "ns3/mobility-module.h"
#include "ns3/fd-net-device-module.h"

#include "ns3/qkd-link-helper.h"
#include "ns3/qkd-app-helper.h"
#include "ns3/qkd-control.h"
#include "ns3/qkd-key-manager-system-application.h"
#include "ns3/qkd-location-register-entry.h"
#include "ns3/q-buffer.h"

using namespace ns3;

NS_LOG_COMPONENT_DEFINE("HOST5_KMS_ALICE");

static void
AddEmuInterface(Ptr<Node> node, std::string devName, std::string ip, std::string mac)
{
    EmuFdNetDeviceHelper emu;
    emu.SetDeviceName(devName);
    NetDeviceContainer dev = emu.Install(node);
    Ptr<NetDevice> d = dev.Get(0);

    Mac48Address addr(mac.c_str());
    d->SetAttribute("Address", Mac48AddressValue(addr));

    Ptr<Ipv4> ipv4 = node->GetObject<Ipv4>();
    uint32_t ifIndex = ipv4->AddInterface(d);
    ipv4->AddAddress(ifIndex, Ipv4InterfaceAddress(Ipv4Address(ip.c_str()), Ipv4Mask("255.255.255.0")));
    ipv4->SetMetric(ifIndex, 1);
    ipv4->SetUp(ifIndex);
}

static void
PeriodicRelayCheck(Ptr<QKDKeyManagerSystemApplication> kms, uint32_t peerId, Time period, Time stopTime)
{
    if(Simulator::Now() >= stopTime)
        return;
    kms->CheckBufferReplenishment(peerId);
    Simulator::Schedule(period, &PeriodicRelayCheck, kms, peerId, period, stopTime);
}

int
main(int argc, char* argv[])
{
    GlobalValue::Bind("SimulatorImplementationType", StringValue("ns3::RealtimeSimulatorImpl"));
    GlobalValue::Bind("ChecksumEnabled", BooleanValue(true));

    // ---- Interfaces reales de esta VM (ajusta a tu laboratorio) ----
    std::string devPP    = "eth0";
    std::string devKms   = "eth1";
    std::string devEtsi  = "eth2";
    std::string myIpPP   = "192.168.112.5";
    std::string myIpKms  = "192.168.117.5";
    std::string myIpEtsi = "192.168.119.5";

    // ---- Peers (IPs reales de las otras VMs) ----
    std::string peerKmsRelayIp = "192.168.117.6"; // HOST6, enlace KMS Alice <-> KMS Relay

    // ---- Contrato de identificadores compartido entre VMs ----
    std::string ppAliceId   = "cccccccc-0000-0000-0000-000000000001"; // modulo de HOST1
    std::string etsiAliceId = "eeeeeeee-0000-0000-0000-000000000001"; // app de HOST8
    std::string etsiBobId   = "eeeeeeee-0000-0000-0000-000000000002"; // app de HOST9

    // ---- Configuracion del Q-Buffer ----
    uint32_t qbMin = 1024;
    uint32_t qbThr = 51200;
    uint32_t qbMax = 500000000;
    // DEBE coincidir con ppKeySize*8 (256 bytes = 2048 bits) del lado post-processing
    // (ver host1_pp_alice.cc): GetDefaultKeyCount() en s-buffer.cc solo cuenta como
    // "disponible" una clave si su tamano coincide EXACTAMENTE con GetKeySize() del
    // S-Buffer local. Si aqui se pone un valor distinto (p.ej. 512 o 4096), el conteo
    // da siempre 0 y el relay nunca arranca ("insufficient amount of key material").
    // Con 2048 el techo de Relay() (20*qbDefaultKeyBits bits/tick, 1 tick/s) queda en
    // 40960 bps, con margen sobre la demanda de GET_KEY del lado ETSI014 (~6400 bps).
    uint32_t qbDefaultKeyBits = 2048;

    // ---- Periodo de reposicion del buffer de relay hacia Bob ----
    // NOTA: bajar este periodo NO ayuda - SBufferClientCheck()/Relay() usa un guard
    // IsRelayActive() que descarta llamadas solapadas, asi que un periodo demasiado
    // corto (probado con 0.02s) deja el guard bloqueado permanentemente y el relay
    // nunca vuelve a dispararse. El suministro real se sube via qbDefaultKeyBits
    // (cada tick mueve como mucho 20*qbDefaultKeyBits bits, ver Relay() en
    // qkd-key-manager-system-application.cc).
    double relayCheckPeriodSec = 1.0;

    uint32_t simulationTime = 5000;

    CommandLine cmd;
    cmd.AddValue("devPP", "NIC real hacia HOST1", devPP);
    cmd.AddValue("devKms", "NIC real hacia HOST6", devKms);
    cmd.AddValue("devEtsi", "NIC real hacia HOST8", devEtsi);
    cmd.AddValue("myIpPP", "IP local en el enlace hacia HOST1", myIpPP);
    cmd.AddValue("myIpKms", "IP local en el enlace hacia HOST6", myIpKms);
    cmd.AddValue("myIpEtsi", "IP local en el enlace hacia HOST8", myIpEtsi);
    cmd.AddValue("peerKmsRelayIp", "IP real de HOST6 (KMS Relay)", peerKmsRelayIp);
    cmd.AddValue("ppAliceId", "UUID del modulo de post-processing de HOST1", ppAliceId);
    cmd.AddValue("etsiAliceId", "UUID de la app ETSI014 de HOST8", etsiAliceId);
    cmd.AddValue("etsiBobId", "UUID de la app ETSI014 de HOST9", etsiBobId);
    cmd.AddValue("simTime", "Duracion de la simulacion (s)", simulationTime);
    cmd.Parse(argc, argv);

    // IMPORTANTE: el protocolo de relay de qkdnetsim mete IDs de nodo ns-3 "en
    // crudo" dentro de los mensajes JSON (source_node_id, destination_node_id),
    // asumiendo que todos los KMS comparten la misma numeracion global (cierto
    // en un unico proceso, falso en el nuestro: cada proceso tiene su propio
    // contador empezando en 0). Para que HOST6 y HOST7 interpreten correctamente
    // los IDs que les mandamos, los 3 procesos deben generar el MISMO ID para
    // la MISMA entidad conceptual, y en el mismo orden en los 3 sitios:
    //   (dummy)=0, Bob=1, Relay=2, Alice(self)=3
    // (host6_kms_relay.cc y host7_kms_bob.cc ya siguen este mismo orden). El
    // "dummy" inicial es obligatorio: ProcessRelayRequest() usa
    // NS_ASSERT(srcNodeId && dstNodeId), es decir, el ID 0 esta reservado como
    // "campo vacio" y ningun nodo real puede tenerlo.
    //
    // De paso, crear el placeholder de Relay ANTES que el nodo real logra que
    // Alice quede con GetId() mas alto: QKDKeyManagerSystemApplication decide
    // quien es "master" del enlace LOCAL Alice-Relay comparando
    // GetNode()->GetId() > dstNodeId. Con Alice de master, es ELLA quien
    // dispara la reposicion del S-Buffer local cuando llega clave nueva de
    // HOST1 (mismo patron que en el escenario 1).
    CreateObject<Node>();                          // ID 0 = (sin usar, reservado)
    Ptr<Node> bobHandle = CreateObject<Node>();    // ID 1 = Bob
    Ptr<Node> relayHandle = CreateObject<Node>();  // ID 2 = Relay

    NodeContainer self;
    self.Create(1);                                // ID 3 = Alice (self)
    Ptr<Node> node = self.Get(0);

    InternetStackHelper internet;
    internet.Install(self);

    MobilityHelper mobility;
    mobility.SetMobilityModel("ns3::ConstantPositionMobilityModel");
    mobility.Install(self);

    AddEmuInterface(node, devPP, myIpPP, "00:00:00:00:15:01");
    AddEmuInterface(node, devKms, myIpKms, "00:00:00:00:15:02");
    AddEmuInterface(node, devEtsi, myIpEtsi, "00:00:00:00:15:03");

    QKDLinkHelper QLinkHelper;
    QKDAppHelper QAHelper;

    // BUG de la libreria (ver s-buffer.cc, SBuffer::DoInitialize()): al crear un
    // S-Buffer nuevo, Object::Initialize() pisa lo que ConfigureRSBuffers() haya
    // fijado (via Init()/Configure()) con sus PROPIOS atributos ns-3
    // ("SMinimal"=10, "SMaximal"=128000, "SThreshold"=32000, "SDefaultKeySize"=512),
    // que nadie mas toca. Sin este SetDefault, todo S-Buffer de tipo RELAY se crea
    // con Mmax=128000 y KeySize=512 pase lo que pase en ConfigureRSBuffers(), lo que
    // deja el suministro de relay fijo en como mucho 20*512=10240 bits por tick.
    Config::SetDefault("ns3::SBuffer::SMinimal", UintegerValue(qbMin));
    Config::SetDefault("ns3::SBuffer::SThreshold", UintegerValue(qbThr));
    Config::SetDefault("ns3::SBuffer::SMaximal", UintegerValue(qbMax));
    Config::SetDefault("ns3::SBuffer::SDefaultKeySize", UintegerValue(qbDefaultKeyBits));

    Ptr<QKDControl> control = QLinkHelper.InstallQKDNController(node);
    QLinkHelper.ConfigureQBuffers({control}, qbMin, qbThr, qbMax, qbDefaultKeyBits);
    // Obligatorio antes de BootstrapRelaySBuffer(): CreateRSBuffer() usa
    // internamente m_rsbuffer_config, que solo se inicializa aqui.
    QLinkHelper.ConfigureRSBuffers({control}, qbMin, qbThr, qbMax, 0, qbDefaultKeyBits);

    QAHelper.InstallKeyManager(node, Ipv4Address(myIpKms.c_str()), 80, control);
    Ptr<QKDKeyManagerSystemApplication> kms = control->GetKeyManagerSystemApplication(node);

    uint32_t relayId = relayHandle->GetId();

    // bobHandle ya se creo al principio de main() (ver comentario ahi) - solo
    // alcanzable por relay, pero necesitamos su GetId() aqui.
    uint32_t bobId = bobHandle->GetId();

    // --- Enlace LOCAL directo hacia el Relay (alimentado por HOST1) ---
    kms->CreateQBuffer(relayId, control->GetQBufferConf(relayId));
    kms->SetPeerKmAddress(relayId, Ipv4Address(peerKmsRelayIp.c_str()));
    kms->RegisterQKDModule(relayId, ppAliceId);

    // --- Rutas: Relay a 1 salto (directo), Bob a 2 saltos (via Relay) ---
    control->AddRouteEntry(QKDLocationRegisterEntry(
        relayId, Ipv4Address(peerKmsRelayIp.c_str()), 1,
        relayId, Ipv4Address(peerKmsRelayIp.c_str()), "kms-relay"
    ));
    control->AddRouteEntry(QKDLocationRegisterEntry(
        relayId, Ipv4Address(peerKmsRelayIp.c_str()), 2,   // nextHop = Relay, 2 saltos
        bobId, Ipv4Address("0.0.0.0"), "kms-bob"            // dst = Bob (no directamente alcanzable)
    ));

    // --- Buffer de RELAY hacia Bob (bootstrap manual, ver cabecera del fichero) ---
    kms->BootstrapRelaySBuffer(bobId);
    Simulator::Schedule(Seconds(1.0), &PeriodicRelayCheck, kms, bobId,
                         Seconds(relayCheckPeriodSec), Seconds(simulationTime));

    // --- Par de apps ETSI014 (HOST8 <-> HOST9); desde KMS Alice, HOST9 se alcanza via Relay ---
    control->RegisterQKDApplicationPair(etsiAliceId, etsiBobId, bobHandle);

    Ipv4GlobalRoutingHelper::PopulateRoutingTables();

    Config::Connect("/NodeList/*/ApplicationList/*/$ns3::QKDKeyManagerSystemApplication/QKDKeyGenerated",
                     MakeCallback(+[](std::string ctx, const std::string& appId, const std::string& keyId, const uint32_t& bits) {
                         std::cout << "[HOST5] KMS Alice almacena clave appId=" << appId << " keyId=" << keyId << " bits=" << bits << std::endl;
                     }));
    Config::Connect("/NodeList/*/ApplicationList/*/$ns3::QKDKeyManagerSystemApplication/KeyServed",
                     MakeCallback(+[](std::string ctx, const std::string& appId, const std::string& keyId, const uint32_t& bits) {
                         std::cout << "[HOST5] KMS Alice sirve clave appId=" << appId << " keyId=" << keyId << " bits=" << bits << std::endl;
                     }));
    Config::Connect("/NodeList/*/ApplicationList/*/$ns3::QKDKeyManagerSystemApplication/RelayConsumption",
                     MakeCallback(+[](std::string ctx, const uint32_t& node, const uint32_t& src, const uint32_t& dst, const uint32_t& amount) {
                         std::cout << "[HOST5] Relay consumido src=" << src << " dst=" << dst << " bits=" << amount << std::endl;
                     }));
    Config::Connect("/NodeList/*/ApplicationList/*/$ns3::QKDKeyManagerSystemApplication/ListenReady",
                     MakeCallback(+[](std::string ctx, const uint32_t& node) {
                         std::cout << "[HOST5] KMS Alice escuchando" << std::endl;
                     }));

    Simulator::Stop(Seconds(simulationTime));
    Simulator::Run();
    Simulator::Destroy();
    return 0;
}
