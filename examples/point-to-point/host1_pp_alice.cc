/*
 * HOST1 - QKD Post-processing ALICE (master)
 *
 * Un unico nodo ns-3 real, con dos interfaces bridgeadas al mundo real:
 *   --devSift / --myIpSift   enlace hacia HOST2 (post-processing Bob)   [sifting, UDP+TCP]
 *   --devKms  / --myIpKms    enlace hacia HOST3 (KMS Alice)             [entrega de claves]
 *
 * "Bob" (HOST2) NO se instancia aqui: solo se crea un Node local vacio
 * (bobHandle) que sirve de identificador para SetDst(); el trafico real
 * de sifting viaja por la direccion Remote/Remote_Sifting (IP real de HOST2)
 * sobre el EmuFdNetDevice.
 *
 * Contrato compartido con las demas VMs (deben coincidir exactamente):
 *   ppAliceId  -> tambien usado en HOST3 (KMS Alice) al registrar el modulo
 *   ppBobId    -> debe coincidir con el "SetId" que use HOST2
 */

#include "ns3/core-module.h"
#include "ns3/network-module.h"
#include "ns3/internet-module.h"
#include "ns3/applications-module.h"
#include "ns3/mobility-module.h"
#include "ns3/fd-net-device-module.h"

#include "ns3/qkd-postprocessing-application.h"

using namespace ns3;

NS_LOG_COMPONENT_DEFINE("HOST1_PP_ALICE");

static void
KeepAlive(Time period, Time stopTime)
{
    if(Simulator::Now() < stopTime)
        Simulator::Schedule(period, &KeepAlive, period, stopTime);
}

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
    std::string devSift  = "eth0";
    std::string devKms   = "eth1";
    std::string myIpSift = "192.168.11.1";
    std::string myIpKms  = "192.168.13.1";
    uint16_t    siftPort = 7102;

    // ---- Peers (IPs reales de las otras VMs) ----
    std::string peerBobIp  = "192.168.11.2"; // HOST2, mismo enlace de sifting
    std::string kmsAliceIp = "192.168.13.3"; // HOST3 (KMS Alice)

    // ---- Contrato de identificadores compartido entre VMs ----
    std::string ppAliceId = "aaaaaaaa-0000-0000-0000-000000000001"; // debe coincidir con HOST3
    std::string ppBobId   = "aaaaaaaa-0000-0000-0000-000000000002"; // debe coincidir con HOST2/HOST4

    // ---- Parametros del enlace QKD (valores del paper) ----
    uint32_t ppKeySize     = 256;    // bytes (2048 bits)
    uint32_t ppKeyRateBps  = 150000; // 150 kbps
    uint32_t ppPacketSize  = 300;    // bytes
    uint32_t ppDataRateBps = 2000;   // 2 kbps

    uint32_t qkdStartTime  = 0;
    uint32_t simulationTime = 5000;

    CommandLine cmd;
    cmd.AddValue("devSift", "NIC real hacia HOST2", devSift);
    cmd.AddValue("devKms", "NIC real hacia HOST3", devKms);
    cmd.AddValue("myIpSift", "IP local en el enlace hacia HOST2", myIpSift);
    cmd.AddValue("myIpKms", "IP local en el enlace hacia HOST3", myIpKms);
    cmd.AddValue("peerBobIp", "IP real de HOST2", peerBobIp);
    cmd.AddValue("kmsAliceIp", "IP real de HOST3 (KMS Alice)", kmsAliceIp);
    cmd.AddValue("ppAliceId", "UUID de este modulo (debe coincidir con HOST3)", ppAliceId);
    cmd.AddValue("ppBobId", "UUID del modulo par en HOST2", ppBobId);
    cmd.AddValue("ppKeySize", "Tamano de clave (bytes)", ppKeySize);
    cmd.AddValue("ppKeyRateBps", "Tasa media de generacion de claves (bps)", ppKeyRateBps);
    cmd.AddValue("simTime", "Duracion de la simulacion (s)", simulationTime);
    cmd.Parse(argc, argv);

    NodeContainer self;
    self.Create(1);
    Ptr<Node> node = self.Get(0);

    InternetStackHelper internet;
    internet.Install(self);

    MobilityHelper mobility;
    mobility.SetMobilityModel("ns3::ConstantPositionMobilityModel");
    mobility.Install(self);

    AddEmuInterface(node, devSift, myIpSift, "00:00:00:00:01:01");
    AddEmuInterface(node, devKms, myIpKms, "00:00:00:00:01:02");

    InetSocketAddress selfSiftAddr(Ipv4Address(myIpSift.c_str()), siftPort);
    InetSocketAddress peerSiftAddr(Ipv4Address(peerBobIp.c_str()), siftPort);
    InetSocketAddress kmsAddr(Ipv4Address(kmsAliceIp.c_str()), 80);

    Ptr<QKDPostprocessingApplication> app = CreateObject<QKDPostprocessingApplication>();
    app->SetAttribute("Local", AddressValue(selfSiftAddr));
    app->SetAttribute("Local_Sifting", AddressValue(selfSiftAddr));
    app->SetAttribute("Local_KMS", AddressValue(kmsAddr));
    app->SetAttribute("Remote", AddressValue(peerSiftAddr));
    app->SetAttribute("Remote_Sifting", AddressValue(peerSiftAddr));
    app->SetAttribute("KeySize", UintegerValue(ppKeySize));
    app->SetAttribute("KeyRate", DataRateValue(DataRate(ppKeyRateBps)));
    app->SetAttribute("PacketSize", UintegerValue(ppPacketSize));
    app->SetAttribute("DataRate", DataRateValue(DataRate(ppDataRateBps)));
    node->AddApplication(app);

    // Identificador local para "Bob" - nunca corre codigo, solo da un GetId()
    Ptr<Node> bobHandle = CreateObject<Node>();
    app->SetSrc(node);
    app->SetDst(bobHandle);

    TypeId tcpTid = TypeId::LookupByName("ns3::TcpSocketFactory");
    TypeId udpTid = TypeId::LookupByName("ns3::UdpSocketFactory");

    Ptr<Socket> sData = Socket::CreateSocket(node, tcpTid);
    Ptr<Socket> sSink = Socket::CreateSocket(node, tcpTid);
    app->SetSocket("send", sData, true);
    app->SetSocket("sink", sSink, true);

    Ptr<Socket> sSiftSend = Socket::CreateSocket(node, udpTid);
    Ptr<Socket> sSiftSink = Socket::CreateSocket(node, udpTid);
    app->SetSiftingSocket("send", sSiftSend);
    app->SetSiftingSocket("sink", sSiftSink);

    app->SetId(ppAliceId);
    app->SetPeerId(ppBobId);

    app->SetStartTime(Seconds(qkdStartTime));
    app->SetStopTime(Seconds(simulationTime));

    Simulator::ScheduleNow(&KeepAlive, MilliSeconds(100), Seconds(simulationTime));

    Ipv4GlobalRoutingHelper::PopulateRoutingTables();

    Config::Connect("/NodeList/*/ApplicationList/*/$ns3::QKDPostprocessingApplication/TxKMS",
                     MakeCallback(+[](std::string ctx, Ptr<const Packet> p) {
                         std::cout << "[HOST1] Clave entregada a KMS Alice, bytes=" << p->GetSize() << std::endl;
                     }));

    Simulator::Stop(Seconds(simulationTime));
    Simulator::Run();
    Simulator::Destroy();
    return 0;
}
