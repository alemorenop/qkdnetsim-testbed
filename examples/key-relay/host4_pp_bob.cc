/*
 * HOST4 - QKD Post-processing BOB (slave)
 *
 * Simetrico a HOST3. Bob no tiene enlace QKD directo con Alice, solo con el Relay.
 *
 *   --devSift / --myIpSift   enlace hacia HOST3 (post-processing Relay-B)   [sifting]
 *   --devKms  / --myIpKms    enlace hacia HOST7 (KMS Bob)                  [entrega de claves]
 *
 * Contrato compartido con las demas VMs (deben coincidir exactamente):
 *   ppBobId    -> tambien usado en HOST7 (KMS Bob) al registrar el modulo
 *   ppRelayBId -> debe coincidir con el "SetId" que use HOST3
 */

#include "ns3/core-module.h"
#include "ns3/network-module.h"
#include "ns3/internet-module.h"
#include "ns3/applications-module.h"
#include "ns3/mobility-module.h"
#include "ns3/fd-net-device-module.h"

#include "ns3/qkd-postprocessing-application.h"

using namespace ns3;

NS_LOG_COMPONENT_DEFINE("HOST4_PP_BOB");

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
    std::string myIpSift = "192.168.114.4";
    std::string myIpKms  = "192.168.116.4";
    uint16_t    siftPort = 7102;

    // ---- Peers (IPs reales de las otras VMs) ----
    std::string peerRelayBIp = "192.168.114.3"; // HOST3, mismo enlace de sifting
    std::string kmsBobIp     = "192.168.116.7"; // HOST7 (KMS Bob)

    // ---- Contrato de identificadores compartido entre VMs ----
    std::string ppRelayBId = "dddddddd-0000-0000-0000-000000000001"; // debe coincidir con HOST3
    std::string ppBobId    = "dddddddd-0000-0000-0000-000000000002"; // debe coincidir con HOST7

    // ---- Parametros del enlace QKD (mismos valores que el escenario 1) ----
    uint32_t ppKeySize     = 256;
    uint32_t ppKeyRateBps  = 150000;
    uint32_t ppPacketSize  = 300;
    uint32_t ppDataRateBps = 2000;

    uint32_t qkdStartTime  = 0;
    uint32_t simulationTime = 5000;

    CommandLine cmd;
    cmd.AddValue("devSift", "NIC real hacia HOST3", devSift);
    cmd.AddValue("devKms", "NIC real hacia HOST7", devKms);
    cmd.AddValue("myIpSift", "IP local en el enlace hacia HOST3", myIpSift);
    cmd.AddValue("myIpKms", "IP local en el enlace hacia HOST7", myIpKms);
    cmd.AddValue("peerRelayBIp", "IP real de HOST3", peerRelayBIp);
    cmd.AddValue("kmsBobIp", "IP real de HOST7 (KMS Bob)", kmsBobIp);
    cmd.AddValue("ppRelayBId", "UUID del modulo par en HOST3", ppRelayBId);
    cmd.AddValue("ppBobId", "UUID de este modulo (debe coincidir con HOST7)", ppBobId);
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

    AddEmuInterface(node, devSift, myIpSift, "00:00:00:00:14:01");
    AddEmuInterface(node, devKms, myIpKms, "00:00:00:00:14:02");

    InetSocketAddress selfSiftAddr(Ipv4Address(myIpSift.c_str()), siftPort);
    InetSocketAddress peerSiftAddr(Ipv4Address(peerRelayBIp.c_str()), siftPort);
    InetSocketAddress kmsAddr(Ipv4Address(kmsBobIp.c_str()), 80);

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

    // Identificador local para "Relay-B" - nunca corre codigo, solo da un GetId()
    Ptr<Node> relayBHandle = CreateObject<Node>();
    app->SetSrc(node);
    app->SetDst(relayBHandle);

    TypeId tcpTid = TypeId::LookupByName("ns3::TcpSocketFactory");
    TypeId udpTid = TypeId::LookupByName("ns3::UdpSocketFactory");

    Ptr<Socket> sData = Socket::CreateSocket(node, tcpTid);
    Ptr<Socket> sSink = Socket::CreateSocket(node, tcpTid);
    app->SetSocket("send", sData, false);
    app->SetSocket("sink", sSink, false);

    Ptr<Socket> sSiftSend = Socket::CreateSocket(node, udpTid);
    Ptr<Socket> sSiftSink = Socket::CreateSocket(node, udpTid);
    app->SetSiftingSocket("send", sSiftSend);
    app->SetSiftingSocket("sink", sSiftSink);

    app->SetId(ppBobId);
    app->SetPeerId(ppRelayBId);

    app->SetStartTime(Seconds(qkdStartTime));
    app->SetStopTime(Seconds(simulationTime));

    Ipv4GlobalRoutingHelper::PopulateRoutingTables();

    Config::Connect("/NodeList/*/ApplicationList/*/$ns3::QKDPostprocessingApplication/TxKMS",
                     MakeCallback(+[](std::string ctx, Ptr<const Packet> p) {
                         std::cout << "[HOST4] Clave entregada a KMS Bob, bytes=" << p->GetSize() << std::endl;
                     }));
    Config::Connect("/NodeList/*/ApplicationList/*/$ns3::QKDPostprocessingApplication/ListenReady",
                     MakeCallback(+[](std::string ctx, const uint32_t& node) {
                         std::cout << "[HOST4] Post-processing Bob escuchando" << std::endl;
                     }));

    Simulator::ScheduleNow(&KeepAlive, MilliSeconds(100), Seconds(simulationTime));
    Simulator::Stop(Seconds(simulationTime));
    Simulator::Run();
    Simulator::Destroy();
    return 0;
}
