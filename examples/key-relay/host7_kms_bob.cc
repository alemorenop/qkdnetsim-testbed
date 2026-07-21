/*
 * HOST7 - KMS BOB (escenario key relay)
 *
 * Simetrico a HOST5 (KMS Alice) - ver comentarios ahi para el detalle del
 * porque hace falta el bootstrap manual del S-Buffer de relay y la
 * comprobacion periodica.
 *
 *   --devPP   / --myIpPP    enlace hacia HOST4 (post-processing Bob)
 *   --devKms  / --myIpKms   enlace hacia HOST6 (KMS Relay)          [relay/transform_keys]
 *   --devEtsi / --myIpEtsi  enlace hacia HOST9 (ETSI014 Bob)        [GET_KEY]
 *
 * Contrato compartido con las demas VMs (deben coincidir exactamente):
 *   ppBobId     -> debe coincidir con el "SetId" que usa HOST4
 *   etsiBobId   -> debe coincidir con el "appId" que usa HOST9
 *   etsiAliceId -> debe coincidir con el "appId" que usa HOST8
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

NS_LOG_COMPONENT_DEFINE("HOST7_KMS_BOB");

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
    std::string myIpPP   = "192.168.116.7";
    std::string myIpKms  = "192.168.118.7";
    std::string myIpEtsi = "192.168.120.7";

    // ---- Peers (IPs reales de las otras VMs) ----
    std::string peerKmsRelayIp = "192.168.118.6"; // HOST6, enlace KMS Bob <-> KMS Relay

    // ---- Contrato de identificadores compartido entre VMs ----
    std::string ppBobId     = "dddddddd-0000-0000-0000-000000000002"; // modulo de HOST4
    std::string etsiAliceId = "eeeeeeee-0000-0000-0000-000000000001"; // app de HOST8
    std::string etsiBobId   = "eeeeeeee-0000-0000-0000-000000000002"; // app de HOST9

    // ---- Configuracion del Q-Buffer ----
    uint32_t qbMin = 1024;
    uint32_t qbThr = 51200;
    uint32_t qbMax = 500000000;
    // Ver nota completa en host5_kms_alice.cc: DEBE coincidir con ppKeySize*8 del
    // lado post-processing (host4_pp_bob.cc), o GetDefaultKeyCount() nunca contara
    // ninguna clave como disponible y el relay se queda permanentemente sin arrancar.
    uint32_t qbDefaultKeyBits = 2048;

    // ---- Periodo de reposicion del buffer de relay hacia Alice ----
    // Ver nota completa en host5_kms_alice.cc: NO bajar este periodo (rompe el
    // guard IsRelayActive() de Relay()). El suministro se ajusta via qbDefaultKeyBits.
    double relayCheckPeriodSec = 1.0;

    uint32_t simulationTime = 5000;

    CommandLine cmd;
    cmd.AddValue("devPP", "NIC real hacia HOST4", devPP);
    cmd.AddValue("devKms", "NIC real hacia HOST6", devKms);
    cmd.AddValue("devEtsi", "NIC real hacia HOST9", devEtsi);
    cmd.AddValue("myIpPP", "IP local en el enlace hacia HOST4", myIpPP);
    cmd.AddValue("myIpKms", "IP local en el enlace hacia HOST6", myIpKms);
    cmd.AddValue("myIpEtsi", "IP local en el enlace hacia HOST9", myIpEtsi);
    cmd.AddValue("peerKmsRelayIp", "IP real de HOST6 (KMS Relay)", peerKmsRelayIp);
    cmd.AddValue("ppBobId", "UUID del modulo de post-processing de HOST4", ppBobId);
    cmd.AddValue("etsiAliceId", "UUID de la app ETSI014 de HOST8", etsiAliceId);
    cmd.AddValue("etsiBobId", "UUID de la app ETSI014 de HOST9", etsiBobId);
    cmd.AddValue("simTime", "Duracion de la simulacion (s)", simulationTime);
    cmd.Parse(argc, argv);

    // IMPORTANTE (ver nota completa en host5_kms_alice.cc): el protocolo de
    // relay mete IDs de nodo ns-3 en crudo en los mensajes JSON, asi que los
    // 3 procesos deben coincidir en el mismo esquema global: (dummy)=0, Bob=1,
    // Relay=2, Alice=3. El "dummy" inicial es obligatorio (el ID 0 esta
    // reservado como "campo vacio" en ProcessRelayRequest). Orden NORMAL (self
    // primero tras el dummy) cumple ademas el "master" local (Bob < Relay =
    // Bob es "slave" en ese enlace, ya que el Relay es "master" hacia Bob, ver
    // host6_kms_relay.cc).
    CreateObject<Node>();                          // ID 0 = (sin usar, reservado)
    NodeContainer self;
    self.Create(1);                                // ID 1 = Bob (self)
    Ptr<Node> node = self.Get(0);

    InternetStackHelper internet;
    internet.Install(self);

    MobilityHelper mobility;
    mobility.SetMobilityModel("ns3::ConstantPositionMobilityModel");
    mobility.Install(self);

    AddEmuInterface(node, devPP, myIpPP, "00:00:00:00:17:01");
    AddEmuInterface(node, devKms, myIpKms, "00:00:00:00:17:02");
    AddEmuInterface(node, devEtsi, myIpEtsi, "00:00:00:00:17:03");

    QKDLinkHelper QLinkHelper;
    QKDAppHelper QAHelper;

    // BUG de la libreria (ver comentario completo en host5_kms_alice.cc): sin
    // este SetDefault, SBuffer::DoInitialize() pisa cualquier S-Buffer de tipo
    // RELAY con Mmax=128000 y KeySize=512 fijos, ignorando ConfigureRSBuffers().
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

    // Identificador local para "KMS Relay" (HOST6) - vecino directo
    Ptr<Node> relayHandle = CreateObject<Node>();  // ID 2 = Relay
    uint32_t relayId = relayHandle->GetId();

    // Identificador local para "KMS Alice" (HOST5) - solo alcanzable por relay
    Ptr<Node> aliceHandle = CreateObject<Node>();  // ID 3 = Alice
    uint32_t aliceId = aliceHandle->GetId();

    // --- Enlace LOCAL directo hacia el Relay (alimentado por HOST4) ---
    kms->CreateQBuffer(relayId, control->GetQBufferConf(relayId));
    kms->SetPeerKmAddress(relayId, Ipv4Address(peerKmsRelayIp.c_str()));
    kms->RegisterQKDModule(relayId, ppBobId);

    // --- Rutas: Relay a 1 salto (directo), Alice a 2 saltos (via Relay) ---
    control->AddRouteEntry(QKDLocationRegisterEntry(
        relayId, Ipv4Address(peerKmsRelayIp.c_str()), 1,
        relayId, Ipv4Address(peerKmsRelayIp.c_str()), "kms-relay"
    ));
    control->AddRouteEntry(QKDLocationRegisterEntry(
        relayId, Ipv4Address(peerKmsRelayIp.c_str()), 2,   // nextHop = Relay, 2 saltos
        aliceId, Ipv4Address("0.0.0.0"), "kms-alice"        // dst = Alice (no directamente alcanzable)
    ));

    // --- Buffer de RELAY hacia Alice (bootstrap manual) ---
    kms->BootstrapRelaySBuffer(aliceId);
    Simulator::Schedule(Seconds(1.0), &PeriodicRelayCheck, kms, aliceId,
                         Seconds(relayCheckPeriodSec), Seconds(simulationTime));

    // BUG estructural (ver comentario completo en host6_kms_relay.cc,
    // PeriodicLocalBufferCheck): el LOCAL_SBUFFER hacia el Relay (relayId=2)
    // solo se autorrepone al recibir STORE_KEY si GetNode()->GetId() > relayId.
    // Como Bob(self)=1 < Relay=2, Bob SIEMPRE es "slave" en ese enlace y nunca
    // se autorrepone. Reutilizamos PeriodicRelayCheck (generico: solo llama a
    // CheckBufferReplenishment, que despacha por tipo de buffer) para cubrirlo.
    Simulator::Schedule(Seconds(1.0), &PeriodicRelayCheck, kms, relayId,
                         Seconds(relayCheckPeriodSec), Seconds(simulationTime));

    // --- Par de apps ETSI014 (HOST9 <-> HOST8); desde KMS Bob, HOST8 se alcanza via Relay ---
    control->RegisterQKDApplicationPair(etsiBobId, etsiAliceId, aliceHandle);

    Ipv4GlobalRoutingHelper::PopulateRoutingTables();

    Config::Connect("/NodeList/*/ApplicationList/*/$ns3::QKDKeyManagerSystemApplication/QKDKeyGenerated",
                     MakeCallback(+[](std::string ctx, const std::string& appId, const std::string& keyId, const uint32_t& bits) {
                         std::cout << "[HOST7] KMS Bob almacena clave appId=" << appId << " keyId=" << keyId << " bits=" << bits << std::endl;
                     }));
    Config::Connect("/NodeList/*/ApplicationList/*/$ns3::QKDKeyManagerSystemApplication/KeyServed",
                     MakeCallback(+[](std::string ctx, const std::string& appId, const std::string& keyId, const uint32_t& bits) {
                         std::cout << "[HOST7] KMS Bob sirve clave appId=" << appId << " keyId=" << keyId << " bits=" << bits << std::endl;
                     }));
    Config::Connect("/NodeList/*/ApplicationList/*/$ns3::QKDKeyManagerSystemApplication/RelayConsumption",
                     MakeCallback(+[](std::string ctx, const uint32_t& node, const uint32_t& src, const uint32_t& dst, const uint32_t& amount) {
                         std::cout << "[HOST7] Relay consumido src=" << src << " dst=" << dst << " bits=" << amount << std::endl;
                     }));
    Config::Connect("/NodeList/*/ApplicationList/*/$ns3::QKDKeyManagerSystemApplication/ListenReady",
                     MakeCallback(+[](std::string ctx, const uint32_t& node) {
                         std::cout << "[HOST7] KMS Bob escuchando" << std::endl;
                     }));

    Simulator::Stop(Seconds(simulationTime));
    Simulator::Run();
    Simulator::Destroy();
    return 0;
}
