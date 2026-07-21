/*
 * HOST6 - KMS RELAY (nodo intermedio, "trusted node")
 *
 * No tiene aplicacion ETSI014 propia: su unico trabajo es servir de puente
 * entre KMS Alice y KMS Bob. Tiene DOS enlaces QKD directos (uno con cada
 * lado), cada uno con su propio Q-Buffer/S-Buffer LOCAL:
 *   - Lado Alice: alimentado por HOST2, LOCAL_SBUFFER indexado por el ID de KMS Alice
 *   - Lado Bob:   alimentado por HOST3, LOCAL_SBUFFER indexado por el ID de KMS Bob
 *
 * No necesita ningun S-Buffer de tipo RELAY propio (eso es solo para los
 * extremos, Alice y Bob) - el reenvio real lo hace la funcion interna
 * Relay()/ProcessRelayRequest usando directamente estos dos buffers LOCALes,
 * disparado automaticamente cuando llega trafico de relay real.
 *
 * Truco de "master" en dos roles a la vez (ver comentario en host5_kms_alice.cc):
 *   - Lado Alice: Relay debe ser "slave" (Alice ya es master en su propio script)
 *     -> el placeholder de Alice se crea DESPUES del nodo real (orden normal)
 *   - Lado Bob: Relay debe ser "master" (Bob es "slave" en su propio script)
 *     -> el placeholder de Bob se crea ANTES del nodo real
 *
 *   --devPPA  / --myIpPPA   enlace hacia HOST2 (post-processing Relay-A)
 *   --devPPB  / --myIpPPB   enlace hacia HOST3 (post-processing Relay-B)
 *   --devKmsA / --myIpKmsA  enlace hacia HOST5 (KMS Alice)
 *   --devKmsB / --myIpKmsB  enlace hacia HOST7 (KMS Bob)
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

NS_LOG_COMPONENT_DEFINE("HOST6_KMS_RELAY");

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

// BUG estructural (no de timing): QKDKeyManagerSystemApplication::ProcessQKD014()
// solo auto-repone el LOCAL_SBUFFER de un enlace al recibir STORE_KEY si
// GetNode()->GetId() > dstNodeId ("isMaster"). Con nuestro esquema de IDs
// (dummy=0, Bob=1, Relay=2, Alice=3) el Relay SIEMPRE es "slave" hacia Alice
// (2 < 3), asi que su S-Buffer local hacia Alice nunca se auto-repone y el
// primer salto del relay (Alice->Relay) falla siempre con 400 Bad Request
// ("no encuentra el key_ID en el buffer 'dec'"), sin importar cuanto se
// espere. Se compensa con esta comprobacion periodica manual (mismo patron
// que PeriodicRelayCheck en host5_kms_alice.cc/host7_kms_bob.cc).
static void
PeriodicLocalBufferCheck(Ptr<QKDKeyManagerSystemApplication> kms, uint32_t peerId, Time period, Time stopTime)
{
    if(Simulator::Now() >= stopTime)
        return;
    kms->CheckBufferReplenishment(peerId);
    Simulator::Schedule(period, &PeriodicLocalBufferCheck, kms, peerId, period, stopTime);
}

int
main(int argc, char* argv[])
{
    GlobalValue::Bind("SimulatorImplementationType", StringValue("ns3::RealtimeSimulatorImpl"));
    GlobalValue::Bind("ChecksumEnabled", BooleanValue(true));

    // ---- Interfaces reales de esta VM (ajusta a tu laboratorio) ----
    std::string devPPA   = "eth0";
    std::string devPPB   = "eth1";
    std::string devKmsA  = "eth2";
    std::string devKmsB  = "eth3";
    std::string myIpPPA  = "192.168.113.6";
    std::string myIpPPB  = "192.168.115.6";
    std::string myIpKmsA = "192.168.117.6";
    std::string myIpKmsB = "192.168.118.6";

    // ---- Peers (IPs reales de las otras VMs) ----
    std::string peerKmsAliceIp = "192.168.117.5"; // HOST5
    std::string peerKmsBobIp   = "192.168.118.7"; // HOST7

    // ---- Contrato de identificadores compartido entre VMs ----
    std::string ppRelayAId = "cccccccc-0000-0000-0000-000000000002"; // modulo de HOST2
    std::string ppRelayBId = "dddddddd-0000-0000-0000-000000000001"; // modulo de HOST3

    // ---- Configuracion del Q-Buffer ----
    uint32_t qbMin = 1024;
    uint32_t qbThr = 51200;
    uint32_t qbMax = 500000000;
    // DEBE coincidir con qbDefaultKeyBits en host5_kms_alice.cc y host7_kms_bob.cc
    // (2048 = ppKeySize*8 del lado post-processing): el protocolo de relay asume
    // que todos los KM usan el mismo tamano de clave por defecto (ver comentario
    // "Keys must be in default size!" en Relay(), qkd-key-manager-system-application.cc).
    // Un valor distinto aqui provoca NS_FATAL_ERROR "key size != input size" en
    // QKDEncryptor::COTP() al re-cifrar la clave relayada con un tamano incompatible.
    uint32_t qbDefaultKeyBits = 2048;

    uint32_t simulationTime = 5000;

    CommandLine cmd;
    cmd.AddValue("devPPA", "NIC real hacia HOST2", devPPA);
    cmd.AddValue("devPPB", "NIC real hacia HOST3", devPPB);
    cmd.AddValue("devKmsA", "NIC real hacia HOST5", devKmsA);
    cmd.AddValue("devKmsB", "NIC real hacia HOST7", devKmsB);
    cmd.AddValue("myIpPPA", "IP local en el enlace hacia HOST2", myIpPPA);
    cmd.AddValue("myIpPPB", "IP local en el enlace hacia HOST3", myIpPPB);
    cmd.AddValue("myIpKmsA", "IP local en el enlace hacia HOST5", myIpKmsA);
    cmd.AddValue("myIpKmsB", "IP local en el enlace hacia HOST7", myIpKmsB);
    cmd.AddValue("peerKmsAliceIp", "IP real de HOST5 (KMS Alice)", peerKmsAliceIp);
    cmd.AddValue("peerKmsBobIp", "IP real de HOST7 (KMS Bob)", peerKmsBobIp);
    cmd.AddValue("ppRelayAId", "UUID del modulo de post-processing de HOST2", ppRelayAId);
    cmd.AddValue("ppRelayBId", "UUID del modulo de post-processing de HOST3", ppRelayBId);
    cmd.AddValue("simTime", "Duracion de la simulacion (s)", simulationTime);
    cmd.Parse(argc, argv);

    // IMPORTANTE (ver nota completa en host5_kms_alice.cc): el protocolo de
    // relay mete IDs de nodo ns-3 en crudo en los mensajes JSON, asi que los
    // 3 procesos deben coincidir en el mismo esquema global: (dummy)=0, Bob=1,
    // Relay=2, Alice=3. El "dummy" inicial es obligatorio (el ID 0 esta
    // reservado como "campo vacio" en ProcessRelayRequest). Este orden de
    // creacion cumple ademas el "master" local (Relay > Bob = master hacia
    // Bob; Relay < Alice = slave hacia Alice, ya que Alice es master en su
    // propio script).
    CreateObject<Node>();                          // ID 0 = (sin usar, reservado)
    Ptr<Node> bobHandle = CreateObject<Node>();    // ID 1 = Bob

    NodeContainer self;
    self.Create(1);                                // ID 2 = Relay (self)
    Ptr<Node> node = self.Get(0);

    Ptr<Node> aliceHandle = CreateObject<Node>();  // ID 3 = Alice

    InternetStackHelper internet;
    internet.Install(self);

    MobilityHelper mobility;
    mobility.SetMobilityModel("ns3::ConstantPositionMobilityModel");
    mobility.Install(self);

    AddEmuInterface(node, devPPA, myIpPPA, "00:00:00:00:16:01");
    AddEmuInterface(node, devPPB, myIpPPB, "00:00:00:00:16:02");
    AddEmuInterface(node, devKmsA, myIpKmsA, "00:00:00:00:16:03");
    AddEmuInterface(node, devKmsB, myIpKmsB, "00:00:00:00:16:04");

    QKDLinkHelper QLinkHelper;
    QKDAppHelper QAHelper;

    // BUG de la libreria (ver comentario completo en host5_kms_alice.cc): sin
    // este SetDefault, todo S-Buffer creado (incluidos los LOCAL_SBUFFER que usa
    // este nodo) queda con KeySize=512 fijo por SBuffer::DoInitialize(), sin
    // importar lo que se pase a ConfigureQBuffers().
    Config::SetDefault("ns3::SBuffer::SMinimal", UintegerValue(qbMin));
    Config::SetDefault("ns3::SBuffer::SThreshold", UintegerValue(qbThr));
    Config::SetDefault("ns3::SBuffer::SMaximal", UintegerValue(qbMax));
    Config::SetDefault("ns3::SBuffer::SDefaultKeySize", UintegerValue(qbDefaultKeyBits));

    Ptr<QKDControl> control = QLinkHelper.InstallQKDNController(node);
    QLinkHelper.ConfigureQBuffers({control}, qbMin, qbThr, qbMax, qbDefaultKeyBits);

    // El KMS Relay escucha en el puerto 80; usamos la interfaz hacia KMS Bob como
    // direccion "publica" (misma logica que en el escenario 1: basta una).
    QAHelper.InstallKeyManager(node, Ipv4Address(myIpKmsB.c_str()), 80, control);
    Ptr<QKDKeyManagerSystemApplication> kms = control->GetKeyManagerSystemApplication(node);

    uint32_t aliceId = aliceHandle->GetId();
    uint32_t bobId   = bobHandle->GetId();

    // --- Enlace LOCAL directo hacia Alice (alimentado por HOST2) ---
    kms->CreateQBuffer(aliceId, control->GetQBufferConf(aliceId));
    kms->SetPeerKmAddress(aliceId, Ipv4Address(peerKmsAliceIp.c_str()));
    kms->RegisterQKDModule(aliceId, ppRelayAId);

    // --- Enlace LOCAL directo hacia Bob (alimentado por HOST3) ---
    kms->CreateQBuffer(bobId, control->GetQBufferConf(bobId));
    kms->SetPeerKmAddress(bobId, Ipv4Address(peerKmsBobIp.c_str()));
    kms->RegisterQKDModule(bobId, ppRelayBId);

    // --- Rutas: ambos vecinos son directos (1 salto) desde el punto de vista del Relay ---
    control->AddRouteEntry(QKDLocationRegisterEntry(
        aliceId, Ipv4Address(peerKmsAliceIp.c_str()), 1,
        aliceId, Ipv4Address(peerKmsAliceIp.c_str()), "kms-alice"
    ));
    control->AddRouteEntry(QKDLocationRegisterEntry(
        bobId, Ipv4Address(peerKmsBobIp.c_str()), 1,
        bobId, Ipv4Address(peerKmsBobIp.c_str()), "kms-bob"
    ));

    // Comprobacion periodica manual de ambos S-Buffers LOCALes (ver comentario
    // completo junto a PeriodicLocalBufferCheck): el lado Alice NUNCA se
    // autorrepone (Relay es "slave" ahi, 2 < 3) y se quedaria vacio para
    // siempre sin esto. Se aplica tambien al lado Bob por simetria/seguridad,
    // aunque ese lado si se autorrepone via "isMaster" en STORE_KEY.
    Simulator::Schedule(Seconds(1.0), &PeriodicLocalBufferCheck, kms, aliceId, Seconds(1.0), Seconds(simulationTime));
    Simulator::Schedule(Seconds(1.0), &PeriodicLocalBufferCheck, kms, bobId, Seconds(1.0), Seconds(simulationTime));

    Ipv4GlobalRoutingHelper::PopulateRoutingTables();

    Config::Connect("/NodeList/*/ApplicationList/*/$ns3::QKDKeyManagerSystemApplication/QKDKeyGenerated",
                     MakeCallback(+[](std::string ctx, const std::string& appId, const std::string& keyId, const uint32_t& bits) {
                         std::cout << "[HOST6] KMS Relay almacena clave appId=" << appId << " keyId=" << keyId << " bits=" << bits << std::endl;
                     }));
    Config::Connect("/NodeList/*/ApplicationList/*/$ns3::QKDKeyManagerSystemApplication/RelayConsumption",
                     MakeCallback(+[](std::string ctx, const uint32_t& node, const uint32_t& src, const uint32_t& dst, const uint32_t& amount) {
                         std::cout << "[HOST6] Relay consumido src=" << src << " dst=" << dst << " bits=" << amount << std::endl;
                     }));
    Config::Connect("/NodeList/*/ApplicationList/*/$ns3::QKDKeyManagerSystemApplication/WasteRelay",
                     MakeCallback(+[](std::string ctx, const uint32_t& src, const uint32_t& dst, const uint32_t& amount) {
                         std::cout << "[HOST6] Relay DESPERDICIADO src=" << src << " dst=" << dst << " bits=" << amount << std::endl;
                     }));
    Config::Connect("/NodeList/*/ApplicationList/*/$ns3::QKDKeyManagerSystemApplication/ListenReady",
                     MakeCallback(+[](std::string ctx, const uint32_t& node) {
                         std::cout << "[HOST6] KMS Relay escuchando" << std::endl;
                     }));

    Simulator::Stop(Seconds(simulationTime));
    Simulator::Run();
    Simulator::Destroy();
    return 0;
}
