/*
 * HOST2 - QKD Post-processing RELAY-A (slave)
 *
 * "Relay" side of the Alice<->Relay QKD link. Symmetric to HOST1.
 *
 *   --devSift / --myIpSift   link to HOST1 (post-processing Alice)   [sifting]
 *   --devKms  / --myIpKms    link to HOST6 (KMS Relay)               [key delivery]
 *
 * Contract shared with the other VMs (must match exactly):
 *   ppRelayAId -> also used on HOST6 (KMS Relay) when registering the module
 *   ppAliceId  -> must match the "SetId" used by HOST1
 */

#include "ns3/core-module.h"
#include "ns3/network-module.h"
#include "ns3/internet-module.h"
#include "ns3/applications-module.h"
#include "ns3/mobility-module.h"
#include "ns3/fd-net-device-module.h"

#include "ns3/qkd-postprocessing-application.h"

using namespace ns3;

NS_LOG_COMPONENT_DEFINE("HOST2_PP_RELAY_A");

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

    // ---- Real interfaces of this VM (adjust to your lab) ----
    std::string devSift  = "eth0";
    std::string devKms   = "eth1";
    std::string myIpSift = "192.168.111.2";
    std::string myIpKms  = "192.168.113.2";
    uint16_t    siftPort = 7102;

    // ---- Peers (real IPs of the other VMs) ----
    std::string peerAliceIp = "192.168.111.1"; // HOST1, same sifting link
    std::string kmsRelayIp  = "192.168.113.6"; // HOST6 (KMS Relay)

    // ---- Identifier contract shared between VMs ----
    std::string ppAliceId  = "cccccccc-0000-0000-0000-000000000001"; // must match HOST1
    std::string ppRelayAId = "cccccccc-0000-0000-0000-000000000002"; // must match HOST6

    // ---- QKD link parameters (same values as scenario 1) ----
    uint32_t ppKeySize     = 256;
    uint32_t ppKeyRateBps  = 150000;
    uint32_t ppPacketSize  = 300;
    uint32_t ppDataRateBps = 2000;

    uint32_t qkdStartTime  = 0;
    uint32_t simulationTime = 5000;

    CommandLine cmd;
    cmd.AddValue("devSift", "Real NIC toward HOST1", devSift);
    cmd.AddValue("devKms", "Real NIC toward HOST6", devKms);
    cmd.AddValue("myIpSift", "Local IP on the link toward HOST1", myIpSift);
    cmd.AddValue("myIpKms", "Local IP on the link toward HOST6", myIpKms);
    cmd.AddValue("peerAliceIp", "Real IP of HOST1", peerAliceIp);
    cmd.AddValue("kmsRelayIp", "Real IP of HOST6 (KMS Relay)", kmsRelayIp);
    cmd.AddValue("ppAliceId", "UUID of the peer module on HOST1", ppAliceId);
    cmd.AddValue("ppRelayAId", "UUID of this module (must match HOST6)", ppRelayAId);
    cmd.AddValue("simTime", "Simulation duration (s)", simulationTime);
    cmd.Parse(argc, argv);

    NodeContainer self;
    self.Create(1);
    Ptr<Node> node = self.Get(0);

    InternetStackHelper internet;
    internet.Install(self);

    MobilityHelper mobility;
    mobility.SetMobilityModel("ns3::ConstantPositionMobilityModel");
    mobility.Install(self);

    AddEmuInterface(node, devSift, myIpSift, "00:00:00:00:12:01");
    AddEmuInterface(node, devKms, myIpKms, "00:00:00:00:12:02");

    InetSocketAddress selfSiftAddr(Ipv4Address(myIpSift.c_str()), siftPort);
    InetSocketAddress peerSiftAddr(Ipv4Address(peerAliceIp.c_str()), siftPort);
    InetSocketAddress kmsAddr(Ipv4Address(kmsRelayIp.c_str()), 80);

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

    // Local identifier for "Alice" - never runs code, only provides a GetId()
    Ptr<Node> aliceHandle = CreateObject<Node>();
    app->SetSrc(node);
    app->SetDst(aliceHandle);

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

    app->SetId(ppRelayAId);
    app->SetPeerId(ppAliceId);

    app->SetStartTime(Seconds(qkdStartTime));
    app->SetStopTime(Seconds(simulationTime));

    Ipv4GlobalRoutingHelper::PopulateRoutingTables();

    Config::Connect("/NodeList/*/ApplicationList/*/$ns3::QKDPostprocessingApplication/TxKMS",
                     MakeCallback(+[](std::string ctx, Ptr<const Packet> p) {
                         std::cout << "[HOST2] Key delivered to KMS Relay (Alice side), bytes=" << p->GetSize() << std::endl;
                     }));
    Config::Connect("/NodeList/*/ApplicationList/*/$ns3::QKDPostprocessingApplication/ListenReady",
                     MakeCallback(+[](std::string ctx, const uint32_t& node) {
                         std::cout << "[HOST2] Post-processing Relay-A listening" << std::endl;
                     }));

    Simulator::ScheduleNow(&KeepAlive, MilliSeconds(100), Seconds(simulationTime));
    Simulator::Stop(Seconds(simulationTime));
    Simulator::Run();
    Simulator::Destroy();
    return 0;
}
