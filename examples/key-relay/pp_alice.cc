/*
 * Key-relay QKD post-processing ALICE (master)
 *
 * "Key relay" scenario: Alice has no direct QKD link with Bob, only with
 * an intermediate Relay node.
 *
 *   --devSift / --myIpSift   link to RELAY_PP_A (post-processing Relay-A)   [sifting]
 *   --devKms  / --myIpKms    link to RELAY_KMS_ALICE (KMS Alice)                 [key delivery]
 *
 * Contract shared with the other VMs (must match exactly):
 *   ppAliceId -> also used on RELAY_KMS_ALICE (KMS Alice) when registering the module
 *   ppRelayAId -> must match the "SetId" used by RELAY_PP_A
 */

#include "ns3/core-module.h"
#include "ns3/network-module.h"
#include "ns3/internet-module.h"
#include "ns3/applications-module.h"
#include "ns3/mobility-module.h"
#include "ns3/fd-net-device-module.h"

#include "ns3/qkd-postprocessing-application.h"

using namespace ns3;

NS_LOG_COMPONENT_DEFINE("RELAY_PP_ALICE_PP_ALICE");

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
    std::string myIpSift = "192.168.111.1";
    std::string myIpKms  = "192.168.112.1";
    uint16_t    siftPort = 7102;

    // ---- Peers (real IPs of the other VMs) ----
    std::string peerRelayAIp = "192.168.111.2"; // RELAY_PP_A, same sifting link
    std::string kmsAliceIp   = "192.168.112.5"; // RELAY_KMS_ALICE (KMS Alice)

    // ---- Identifier contract shared between VMs ----
    std::string ppAliceId  = "cccccccc-0000-0000-0000-000000000001"; // must match RELAY_KMS_ALICE
    std::string ppRelayAId = "cccccccc-0000-0000-0000-000000000002"; // must match RELAY_PP_A

    // ---- QKD link parameters (same values as scenario 1) ----
    uint32_t ppKeySize     = 256;    // bytes (2048 bits)
    uint32_t ppKeyRateBps  = 150000; // 150 kbps
    uint32_t ppPacketSize  = 300;    // bytes
    uint32_t ppDataRateBps = 2000;   // 2 kbps

    uint32_t qkdStartTime  = 0;
    uint32_t simulationTime = 5000;

    CommandLine cmd;
    cmd.AddValue("devSift", "Real NIC toward RELAY_PP_A", devSift);
    cmd.AddValue("devKms", "Real NIC toward RELAY_KMS_ALICE", devKms);
    cmd.AddValue("myIpSift", "Local IP on the link toward RELAY_PP_A", myIpSift);
    cmd.AddValue("myIpKms", "Local IP on the link toward RELAY_KMS_ALICE", myIpKms);
    cmd.AddValue("peerRelayAIp", "Real IP of RELAY_PP_A", peerRelayAIp);
    cmd.AddValue("kmsAliceIp", "Real IP of RELAY_KMS_ALICE (KMS Alice)", kmsAliceIp);
    cmd.AddValue("ppAliceId", "UUID of this module (must match RELAY_KMS_ALICE)", ppAliceId);
    cmd.AddValue("ppRelayAId", "UUID of the peer module on RELAY_PP_A", ppRelayAId);
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

    AddEmuInterface(node, devSift, myIpSift, "00:00:00:00:11:01");
    AddEmuInterface(node, devKms, myIpKms, "00:00:00:00:11:02");

    InetSocketAddress selfSiftAddr(Ipv4Address(myIpSift.c_str()), siftPort);
    InetSocketAddress peerSiftAddr(Ipv4Address(peerRelayAIp.c_str()), siftPort);
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

    // Local identifier for "Relay-A" - never runs code, only provides a GetId()
    Ptr<Node> relayAHandle = CreateObject<Node>();
    app->SetSrc(node);
    app->SetDst(relayAHandle);

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
    app->SetPeerId(ppRelayAId);

    app->SetStartTime(Seconds(qkdStartTime));
    app->SetStopTime(Seconds(simulationTime));

    Ipv4GlobalRoutingHelper::PopulateRoutingTables();

    Config::Connect("/NodeList/*/ApplicationList/*/$ns3::QKDPostprocessingApplication/TxKMS",
                     MakeCallback(+[](std::string ctx, Ptr<const Packet> p) {
                         std::cout << "[RELAY_PP_ALICE] Key delivered to KMS Alice, bytes=" << p->GetSize() << std::endl;
                     }));

    Simulator::ScheduleNow(&KeepAlive, MilliSeconds(100), Seconds(simulationTime));
    Simulator::Stop(Seconds(simulationTime));
    Simulator::Run();
    Simulator::Destroy();
    return 0;
}
