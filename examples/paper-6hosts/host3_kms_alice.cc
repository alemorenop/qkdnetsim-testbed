/*
 * HOST3 - KMS ALICE
 *
 * Un unico nodo real con tres interfaces bridgeadas al mundo real:
 *   --devPP   / --myIpPP    enlace hacia HOST1 (post-processing Alice)
 *   --devKms  / --myIpKms   enlace hacia HOST4 (KMS Bob)          [relay/transform_keys, puerto 8080]
 *   --devEtsi / --myIpEtsi  enlace hacia HOST5 (ETSI014 Alice)    [GET_KEY, puerto 80]
 *
 * "KMS Bob" (HOST4) no se instancia aqui: solo se crea un Node local vacio
 * (kmsBobHandle) que sirve como identificador interno (GetId()) para las
 * estructuras de QKDControl/QKDKeyManagerSystemApplication. El trafico
 * real de relay va por --peerKmsBobIp sobre el EmuFdNetDevice.
 *
 * Contrato compartido con las demas VMs (deben coincidir exactamente):
 *   ppAliceId   -> debe coincidir con el "SetId" que usa HOST1
 *   etsiAliceId -> debe coincidir con el "appId" que usa HOST5
 *   etsiBobId   -> debe coincidir con el "appId" que usa HOST6
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

NS_LOG_COMPONENT_DEFINE("HOST3_KMS_ALICE");

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

int
main(int argc, char* argv[])
{
    GlobalValue::Bind("SimulatorImplementationType", StringValue("ns3::RealtimeSimulatorImpl"));
    GlobalValue::Bind("ChecksumEnabled", BooleanValue(true));

    // ---- Interfaces reales de esta VM (ajusta a tu laboratorio) ----
    std::string devPP    = "eth0";
    std::string devKms   = "eth1";
    std::string devEtsi  = "eth2";
    std::string myIpPP   = "192.168.13.3";
    std::string myIpKms  = "192.168.34.3";
    std::string myIpEtsi = "192.168.35.3";

    // ---- Peers (IPs reales de las otras VMs) ----
    std::string peerKmsBobIp = "192.168.34.4"; // HOST4, enlace KM-KM

    // ---- Contrato de identificadores compartido entre VMs ----
    std::string ppAliceId   = "aaaaaaaa-0000-0000-0000-000000000001"; // modulo de HOST1
    std::string etsiAliceId = "bbbbbbbb-0000-0000-0000-000000000001"; // app de HOST5
    std::string etsiBobId   = "bbbbbbbb-0000-0000-0000-000000000002"; // app de HOST6

    // ---- Configuracion del Q-Buffer (mismos valores que en los ejemplos) ----
    uint32_t qbMin = 1024;
    uint32_t qbThr = 51200;
    uint32_t qbMax = 500000000;
    uint32_t qbDefaultKeyBits = 512;

    uint32_t simulationTime = 5000;

    CommandLine cmd;
    cmd.AddValue("devPP", "NIC real hacia HOST1", devPP);
    cmd.AddValue("devKms", "NIC real hacia HOST4", devKms);
    cmd.AddValue("devEtsi", "NIC real hacia HOST5", devEtsi);
    cmd.AddValue("myIpPP", "IP local en el enlace hacia HOST1", myIpPP);
    cmd.AddValue("myIpKms", "IP local en el enlace hacia HOST4", myIpKms);
    cmd.AddValue("myIpEtsi", "IP local en el enlace hacia HOST5", myIpEtsi);
    cmd.AddValue("peerKmsBobIp", "IP real de HOST4 (KMS Bob)", peerKmsBobIp);
    cmd.AddValue("ppAliceId", "UUID del modulo de post-processing de HOST1", ppAliceId);
    cmd.AddValue("etsiAliceId", "UUID de la app ETSI014 de HOST5", etsiAliceId);
    cmd.AddValue("etsiBobId", "UUID de la app ETSI014 de HOST6", etsiBobId);
    cmd.AddValue("simTime", "Duracion de la simulacion (s)", simulationTime);
    cmd.Parse(argc, argv);

    // Se crea ANTES que el nodo real para que este ultimo quede con un
    // Node::GetId() mas alto: QKDKeyManagerSystemApplication decide internamente
    // quien es "master" de cada pareja KMS-KMS comparando GetNode()->GetId() >
    // dstNodeId (ver ProcessRequest/SBufferClientCheck) - ese "master" es quien
    // dispara la reposicion del buffer de claves transformadas. Con HOST3 como
    // "master" y HOST4 (host4_kms_bob.cc, que crea su nodo real primero, sin
    // este truco) como "slave", queda exactamente un lado en cada rol.
    Ptr<Node> kmsBobHandle = CreateObject<Node>();

    NodeContainer self;
    self.Create(1);
    Ptr<Node> node = self.Get(0);

    InternetStackHelper internet;
    internet.Install(self);

    MobilityHelper mobility;
    mobility.SetMobilityModel("ns3::ConstantPositionMobilityModel");
    mobility.Install(self);

    AddEmuInterface(node, devPP, myIpPP, "00:00:00:00:03:01");
    AddEmuInterface(node, devKms, myIpKms, "00:00:00:00:03:02");
    AddEmuInterface(node, devEtsi, myIpEtsi, "00:00:00:00:03:03");

    QKDLinkHelper QLinkHelper;
    QKDAppHelper QAHelper;

    Ptr<QKDControl> control = QLinkHelper.InstallQKDNController(node);
    QLinkHelper.ConfigureQBuffers({control}, qbMin, qbThr, qbMax, qbDefaultKeyBits);

    // El KMS escucha en el puerto 80 sobre la interfaz que da a HOST4/HOST5
    // (en este experimento punto a punto basta una direccion "publica" para el KMS;
    // usamos la interfaz hacia HOST4 para las peticiones de relay y la de HOST5 para GET_KEY,
    // ambas atienden sobre la misma aplicacion KMS).
    QAHelper.InstallKeyManager(node, Ipv4Address(myIpKms.c_str()), 80, control);
    Ptr<QKDKeyManagerSystemApplication> kms = control->GetKeyManagerSystemApplication(node);

    // kmsBobHandle ya se creo al principio de main() (ver comentario ahi). Nunca
    // corre codigo, solo da un GetId() estable para referenciar a "KMS Bob".
    uint32_t kmsBobId = kmsBobHandle->GetId();

    kms->CreateQBuffer(kmsBobId, control->GetQBufferConf(kmsBobId));
    kms->SetPeerKmAddress(kmsBobId, Ipv4Address(peerKmsBobIp.c_str()));

    // Entrada de ruta directa (1 salto, sin relay) hacia KMS Bob. Sin esto,
    // QKDControl::GetRoute() no encuentra nada en la tabla de rutas (vacia) y
    // devuelve una QKDLocationRegisterEntry por defecto con campos sin inicializar,
    // lo que acaba provocando un puntero nulo al procesar la primera peticion GET_KEY.
    control->AddRouteEntry(QKDLocationRegisterEntry(
        kmsBobId,                              // nextHopKmNodeId (salto directo)
        Ipv4Address(peerKmsBobIp.c_str()),     // nextHopKmNodeAddress
        1,                                      // hops
        kmsBobId,                               // dstKmNodeId
        Ipv4Address(peerKmsBobIp.c_str()),      // dstKmAddress
        "kms-bob"                               // dstKmId (solo descriptivo)
    ));

    // El modulo de post-processing de HOST1 alimenta el buffer que sirve para llegar a KMS Bob
    kms->RegisterQKDModule(kmsBobId, ppAliceId);

    // Registra el par de apps ETSI014 (HOST5 <-> HOST6); desde el punto de vista de
    // KMS Alice, la app remota (HOST6) se alcanza "via KMS Bob"
    control->RegisterQKDApplicationPair(etsiAliceId, etsiBobId, kmsBobHandle);

    Ipv4GlobalRoutingHelper::PopulateRoutingTables();

    Config::Connect("/NodeList/*/ApplicationList/*/$ns3::QKDKeyManagerSystemApplication/QKDKeyGenerated",
                     MakeCallback(+[](std::string ctx, const std::string& appId, const std::string& keyId, const uint32_t& bits) {
                         std::cout << "[HOST3] KMS Alice almacena clave appId=" << appId << " keyId=" << keyId << " bits=" << bits << std::endl;
                     }));
    Config::Connect("/NodeList/*/ApplicationList/*/$ns3::QKDKeyManagerSystemApplication/KeyServed",
                     MakeCallback(+[](std::string ctx, const std::string& appId, const std::string& keyId, const uint32_t& bits) {
                         std::cout << "[HOST3] KMS Alice sirve clave appId=" << appId << " keyId=" << keyId << " bits=" << bits << std::endl;
                     }));

    Simulator::Stop(Seconds(simulationTime));
    Simulator::Run();
    Simulator::Destroy();
    return 0;
}
