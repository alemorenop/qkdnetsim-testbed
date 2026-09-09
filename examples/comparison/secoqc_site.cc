/*
 * Distributed replica of the six-site SECOQC example.
 *
 * One instance represents one site (A..F).  Every instance creates the same
 * six logical site Nodes so node identifiers remain stable across processes,
 * but installs the Internet stack, KMS and post-processing applications only
 * on its selected local site.  Docker networks carry the six physical QKD
 * links while a shared classical control network carries KMS-to-KMS traffic.
 */

#include "ns3/applications-module.h"
#include "ns3/core-module.h"
#include "ns3/fd-net-device-module.h"
#include "ns3/internet-module.h"
#include "ns3/mobility-module.h"
#include "ns3/network-module.h"

#include "ns3/q-buffer.h"
#include "ns3/qkd-app-helper.h"
#include "ns3/qkd-control.h"
#include "ns3/qkd-key-manager-system-application.h"
#include "ns3/qkd-link-helper.h"
#include "ns3/qkd-location-register-entry.h"
#include "ns3/qkd-postprocessing-application.h"

#include <array>
#include <sstream>
#include <vector>

using namespace ns3;

NS_LOG_COMPONENT_DEFINE("DISTRIBUTED_SECOQC_SITE");

namespace
{

constexpr uint32_t SITE_COUNT = 6;
const std::array<std::string, SITE_COUNT> SITE_NAMES = {"A", "B", "C", "D", "E", "F"};
const std::array<std::string, SITE_COUNT> CONTROL_IPS = {
    "192.168.230.11", "192.168.230.12", "192.168.230.13",
    "192.168.230.14", "192.168.230.15", "192.168.230.16"};

struct Edge
{
    uint32_t left;
    uint32_t right;
    const char* leftIp;
    const char* rightIp;
    uint16_t port;
    const char* leftModuleId;
    const char* rightModuleId;
    uint32_t keyRateBps;
    uint32_t keySizeBytes;
};

// Same physical graph as examples_qkdnetsim_secoqc.
const std::array<Edge, 6> SECOQC_EDGES = {{
    {0, 1, "192.168.201.11", "192.168.201.12", 7101, "cccccccc-0000-0000-0001-000000000001", "cccccccc-0000-0000-0001-000000000002", 10000, 256},
    {1, 2, "192.168.202.12", "192.168.202.13", 7102, "cccccccc-0000-0000-0002-000000000001", "cccccccc-0000-0000-0002-000000000002", 10000, 256},
    {1, 3, "192.168.203.12", "192.168.203.14", 7103, "cccccccc-0000-0000-0003-000000000001", "cccccccc-0000-0000-0003-000000000002", 10000, 256},
    {2, 4, "192.168.204.13", "192.168.204.15", 7104, "cccccccc-0000-0000-0004-000000000001", "cccccccc-0000-0000-0004-000000000002", 10000, 256},
    {3, 4, "192.168.205.14", "192.168.205.15", 7105, "cccccccc-0000-0000-0005-000000000001", "cccccccc-0000-0000-0005-000000000002", 10000, 256},
    {4, 5, "192.168.206.15", "192.168.206.16", 7106, "cccccccc-0000-0000-0006-000000000001", "cccccccc-0000-0000-0006-000000000002", 10000, 256},
}};

// Padua workload used for Tables 2--4 of the large-scale simulator paper.
// Rates are bits/s and sizes are bytes (the paper reports sizes in bits).
const std::array<Edge, 6> PADUA_EDGES = {{
    {0, 1, "192.168.201.11", "192.168.201.12", 7101, "dddddddd-0000-0000-0001-000000000001", "dddddddd-0000-0000-0001-000000000002", 15000, 1250},
    {1, 2, "192.168.202.12", "192.168.202.13", 7102, "dddddddd-0000-0000-0002-000000000001", "dddddddd-0000-0000-0002-000000000002", 15000, 1250},
    {2, 3, "192.168.203.13", "192.168.203.14", 7103, "dddddddd-0000-0000-0003-000000000001", "dddddddd-0000-0000-0003-000000000002", 100000, 6250},
    {3, 4, "192.168.204.14", "192.168.204.15", 7104, "dddddddd-0000-0000-0004-000000000001", "dddddddd-0000-0000-0004-000000000002", 100000, 6250},
    {4, 5, "192.168.205.15", "192.168.205.16", 7105, "dddddddd-0000-0000-0005-000000000001", "dddddddd-0000-0000-0005-000000000002", 100000, 3750},
    {2, 5, "192.168.206.13", "192.168.206.16", 7106, "dddddddd-0000-0000-0006-000000000001", "dddddddd-0000-0000-0006-000000000002", 100000, 3750},
}};

// Shortest-path next hop and hop count in the graph above.
const uint32_t NEXT_HOP[SITE_COUNT][SITE_COUNT] = {
    {0,1,1,1,1,1}, {0,1,2,3,2,2}, {1,1,2,1,4,4},
    {1,1,1,3,4,4}, {2,2,2,3,4,5}, {4,4,4,4,4,5}};
const uint32_t HOPS[SITE_COUNT][SITE_COUNT] = {
    {0,1,2,2,3,4}, {1,0,1,1,2,3}, {2,1,0,2,1,2},
    {2,1,2,0,1,2}, {3,2,1,1,0,1}, {4,3,2,2,1,0}};

const uint32_t PADUA_NEXT_HOP[SITE_COUNT][SITE_COUNT] = {
    {0,1,1,1,1,1}, {0,1,2,2,2,2}, {1,1,2,3,3,5},
    {2,2,2,3,4,2}, {3,3,3,3,4,3}, {2,2,2,2,2,5}};
const uint32_t PADUA_HOPS[SITE_COUNT][SITE_COUNT] = {
    {0,1,2,3,4,3}, {1,0,1,2,3,2}, {2,1,0,1,2,1},
    {3,2,1,0,1,2}, {4,3,2,1,0,3}, {3,2,1,2,3,0}};

void
AddEmuInterface(Ptr<Node> node,
                const std::string& device,
                const std::string& ip,
                uint8_t network,
                uint8_t host)
{
    EmuFdNetDeviceHelper emu;
    emu.SetDeviceName(device);
    NetDeviceContainer devices = emu.Install(node);
    Ptr<NetDevice> netDevice = devices.Get(0);
    std::ostringstream mac;
    mac << "02:00:00:00:" << std::hex << unsigned(network) << ":" << unsigned(host);
    netDevice->SetAttribute("Address", Mac48AddressValue(Mac48Address(mac.str().c_str())));
    Ptr<Ipv4> ipv4 = node->GetObject<Ipv4>();
    uint32_t index = ipv4->AddInterface(netDevice);
    ipv4->AddAddress(index,
                     Ipv4InterfaceAddress(Ipv4Address(ip.c_str()), Ipv4Mask("255.255.255.0")));
    ipv4->SetUp(index);
}

void
PeriodicCheck(Ptr<QKDKeyManagerSystemApplication> kms,
              uint32_t destination,
              Time period,
              Time stop)
{
    if (Simulator::Now() >= stop)
    {
        return;
    }
    kms->CheckBufferReplenishment(destination);
    Simulator::Schedule(period, &PeriodicCheck, kms, destination, period, stop);
}

void
KeepAlive(Time period, Time stop)
{
    if (Simulator::Now() < stop)
    {
        Simulator::Schedule(period, &KeepAlive, period, stop);
    }
}

} // namespace

int
main(int argc, char* argv[])
{
    GlobalValue::Bind("SimulatorImplementationType", StringValue("ns3::RealtimeSimulatorImpl"));
    GlobalValue::Bind("ChecksumEnabled", BooleanValue(true));

    uint32_t site = 0;
    std::string profile = "secoqc";
    std::string devControl = "eth0";
    std::string devApp;
    std::string appIp;
    std::string dev0;
    std::string dev1;
    std::string dev2;
    uint32_t keyRateBps = 10000;
    uint32_t keySizeBytes = 256;
    uint32_t simulationTime = 5000;
    uint32_t qbMin = 1024;
    uint32_t qbThr = 1800;
    uint32_t qbMax = 500000000;
    // Match the upstream SECOQC/reference scenario.  This is the Q/S-buffer
    // transformation granularity (bits), not the physical PP key size.
    uint32_t qbDefault = 512;
    uint32_t rsThreshold = 16000;
    uint32_t rsMax = 64000;
    uint32_t relayCheckPeriodMs = 1000;
    uint32_t qkdStopTime = 0;

    CommandLine cmd;
    cmd.AddValue("profile", "Topology/workload profile: secoqc or padua-reference", profile);
    cmd.AddValue("site", "SECOQC site index: A=0 ... F=5", site);
    cmd.AddValue("devControl", "Shared classical KMS control interface", devControl);
    cmd.AddValue("devApp", "Optional local application-to-KMS interface", devApp);
    cmd.AddValue("appIp", "Optional address on the application-to-KMS interface", appIp);
    cmd.AddValue("dev0", "First physical QKD-link interface", dev0);
    cmd.AddValue("dev1", "Second physical QKD-link interface", dev1);
    cmd.AddValue("dev2", "Third physical QKD-link interface", dev2);
    cmd.AddValue("keyRateBps", "Generated key rate on every QKD link", keyRateBps);
    cmd.AddValue("keySizeBytes", "Generated key size", keySizeBytes);
    cmd.AddValue("simTime", "Process lifetime", simulationTime);
    cmd.Parse(argc, argv);
    NS_ABORT_MSG_IF(site >= SITE_COUNT, "site must be in [0,5]");
    NS_ABORT_MSG_IF(profile != "secoqc" && profile != "padua-reference",
                    "profile must be secoqc or padua-reference");
    const bool padua = profile == "padua-reference";
    const auto& edges = padua ? PADUA_EDGES : SECOQC_EDGES;
    qkdStopTime = padua ? std::min(simulationTime, 100u) : simulationTime;

    // The KMS relay implementation treats node ID 0 as an absent optional
    // field.  Reserve it explicitly so the six logical KMS IDs are 1..6 in
    // every independent process.
    Ptr<Node> reservedZero = CreateObject<Node>();
    (void)reservedZero;
    NodeContainer sites;
    sites.Create(SITE_COUNT);
    Ptr<Node> local = sites.Get(site);
    InternetStackHelper internet;
    internet.Install(local);
    MobilityHelper mobility;
    mobility.SetMobilityModel("ns3::ConstantPositionMobilityModel");
    mobility.Install(local);

    AddEmuInterface(local, devControl, CONTROL_IPS[site], 0xe0, site + 1);
    if (!devApp.empty() && !appIp.empty())
    {
        AddEmuInterface(local, devApp, appIp, 0xf0, site + 1);
    }
    const std::array<std::string, 3> edgeDevices = {dev0, dev1, dev2};
    std::vector<const Edge*> localEdges;
    for (const auto& edge : edges)
    {
        if (edge.left == site || edge.right == site)
        {
            localEdges.push_back(&edge);
        }
    }
    NS_ABORT_MSG_IF(localEdges.size() > edgeDevices.size(), "site has too many interfaces");
    for (uint32_t i = 0; i < localEdges.size(); ++i)
    {
        NS_ABORT_MSG_IF(edgeDevices[i].empty(), "missing dev interface for QKD edge");
        const Edge& edge = *localEdges[i];
        AddEmuInterface(local,
                        edgeDevices[i],
                        site == edge.left ? edge.leftIp : edge.rightIp,
                        0xc0 + edge.port - 7100,
                        site + 1);
    }

    QKDLinkHelper linkHelper;
    QKDAppHelper appHelper;
    Config::SetDefault("ns3::SBuffer::SMinimal", UintegerValue(0));
    Config::SetDefault("ns3::SBuffer::SThreshold", UintegerValue(rsThreshold));
    Config::SetDefault("ns3::SBuffer::SMaximal", UintegerValue(rsMax));
    Config::SetDefault("ns3::SBuffer::SDefaultKeySize", UintegerValue(qbDefault));
    Ptr<QKDControl> control = linkHelper.InstallQKDNController(local);
    linkHelper.ConfigureQBuffers({control}, qbMin, qbThr, qbMax, qbDefault);
    linkHelper.ConfigureRSBuffers({control}, 0, rsThreshold, rsMax, 0, qbDefault);
    const std::string kmsListenIp = appIp.empty() ? CONTROL_IPS[site] : appIp;
    appHelper.InstallKeyManager(local, Ipv4Address(kmsListenIp.c_str()), 80, control);
    Ptr<QKDKeyManagerSystemApplication> kms =
        control->GetKeyManagerSystemApplication(local);

    // Direct QKD buffers and routes.
    for (const Edge* edgePtr : localEdges)
    {
        const Edge& edge = *edgePtr;
        uint32_t peer = edge.left == site ? edge.right : edge.left;
        uint32_t peerNodeId = sites.Get(peer)->GetId();
        kms->CreateQBuffer(peerNodeId, control->GetQBufferConf(peerNodeId));
        kms->SetPeerKmAddress(peerNodeId, Ipv4Address(CONTROL_IPS[peer].c_str()));
        kms->RegisterQKDModule(peerNodeId,
                               site == edge.left ? edge.leftModuleId : edge.rightModuleId);
    }
    for (uint32_t destination = 0; destination < SITE_COUNT; ++destination)
    {
        if (destination == site)
        {
            continue;
        }
        uint32_t next = padua ? PADUA_NEXT_HOP[site][destination]
                              : NEXT_HOP[site][destination];
        uint32_t hops = padua ? PADUA_HOPS[site][destination]
                              : HOPS[site][destination];
        control->AddRouteEntry(QKDLocationRegisterEntry(
            sites.Get(next)->GetId(),
            Ipv4Address(CONTROL_IPS[next].c_str()),
            hops,
            sites.Get(destination)->GetId(),
            Ipv4Address(CONTROL_IPS[destination].c_str()),
            "kms-" + SITE_NAMES[destination]));
    }

    // Post-processing endpoint for every physical QKD edge incident on this site.
    for (uint32_t i = 0; i < localEdges.size(); ++i)
    {
        const Edge& edge = *localEdges[i];
        uint32_t peer = edge.left == site ? edge.right : edge.left;
        std::string localIp = site == edge.left ? edge.leftIp : edge.rightIp;
        std::string peerIp = site == edge.left ? edge.rightIp : edge.leftIp;
        InetSocketAddress localAddress(Ipv4Address(localIp.c_str()), edge.port);
        InetSocketAddress peerAddress(Ipv4Address(peerIp.c_str()), edge.port);
        Ptr<QKDPostprocessingApplication> pp = CreateObject<QKDPostprocessingApplication>();
        pp->SetAttribute("Local", AddressValue(localAddress));
        pp->SetAttribute("Local_Sifting", AddressValue(localAddress));
        pp->SetAttribute("Local_KMS",
                         AddressValue(InetSocketAddress(Ipv4Address(CONTROL_IPS[site].c_str()), 80)));
        pp->SetAttribute("Remote", AddressValue(peerAddress));
        pp->SetAttribute("Remote_Sifting", AddressValue(peerAddress));
        pp->SetAttribute("KeySize", UintegerValue(padua ? edge.keySizeBytes : keySizeBytes));
        pp->SetAttribute("KeyRate", DataRateValue(DataRate(padua ? edge.keyRateBps : keyRateBps)));
        pp->SetAttribute("PacketSize", UintegerValue(100));
        pp->SetAttribute("DataRate", DataRateValue(DataRate(1000)));
        local->AddApplication(pp);
        pp->SetSrc(sites.Get(site));
        pp->SetDst(sites.Get(peer));
        bool master = site == edge.left;
        TypeId tcp = TypeId::LookupByName("ns3::TcpSocketFactory");
        TypeId udp = TypeId::LookupByName("ns3::UdpSocketFactory");
        pp->SetSocket("send", Socket::CreateSocket(local, tcp), master);
        pp->SetSocket("sink", Socket::CreateSocket(local, tcp), master);
        pp->SetSiftingSocket("send", Socket::CreateSocket(local, udp));
        pp->SetSiftingSocket("sink", Socket::CreateSocket(local, udp));
        pp->SetId(site == edge.left ? edge.leftModuleId : edge.rightModuleId);
        pp->SetPeerId(site == edge.left ? edge.rightModuleId : edge.leftModuleId);
        pp->SetStartTime(Seconds(0));
        pp->SetStopTime(Seconds(qkdStopTime));
        Simulator::Schedule(Seconds(1), &PeriodicCheck, kms, sites.Get(peer)->GetId(),
                            MilliSeconds(relayCheckPeriodMs), Seconds(qkdStopTime));
    }

    struct AppPair
    {
        uint32_t left;
        uint32_t right;
        const char* leftSae;
        const char* rightSae;
    };
    const std::vector<AppPair> appPairs = padua
        ? std::vector<AppPair>{
              {0, 4, "eeeeeeee-0000-0000-0001-000000000001", "eeeeeeee-0000-0000-0001-000000000005"},
              {0, 5, "eeeeeeee-0000-0000-0002-000000000001", "eeeeeeee-0000-0000-0002-000000000006"}}
        : std::vector<AppPair>{
              {0, 5, "eeeeeeee-0000-0000-0000-000000000001", "eeeeeeee-0000-0000-0000-000000000002"}};
    for (const auto& pair : appPairs)
    {
        if (site != pair.left && site != pair.right)
        {
            continue;
        }
        uint32_t remote = site == pair.left ? pair.right : pair.left;
        uint32_t remoteNodeId = sites.Get(remote)->GetId();
        kms->SetPeerKmAddress(remoteNodeId, Ipv4Address(CONTROL_IPS[remote].c_str()));
        kms->BootstrapRelaySBuffer(remoteNodeId);
        control->RegisterQKDApplicationPair(site == pair.left ? pair.leftSae : pair.rightSae,
                                            site == pair.left ? pair.rightSae : pair.leftSae,
                                            sites.Get(remote));
        Simulator::Schedule(Seconds(1), &PeriodicCheck, kms, remoteNodeId,
                            MilliSeconds(relayCheckPeriodMs), Seconds(simulationTime));
    }

    Config::Connect("/NodeList/*/ApplicationList/*/$ns3::QKDKeyManagerSystemApplication/QKDKeyGenerated",
                    MakeCallback(+[](std::string, const std::string& appId,
                                     const std::string& keyId, const uint32_t& bits) {
                        std::cout << "[SECOQC_KMS] stores key appId=" << appId
                                  << " keyId=" << keyId << " bits=" << bits << std::endl;
                    }));
    Config::Connect("/NodeList/*/ApplicationList/*/$ns3::QKDKeyManagerSystemApplication/KeyServed",
                    MakeCallback(+[](std::string, const std::string& appId,
                                     const std::string& keyId, const uint32_t& bits) {
                        std::cout << "[SECOQC_KMS] serves key appId=" << appId
                                  << " keyId=" << keyId << " bits=" << bits << std::endl;
                    }));
    // This trace was introduced with mixed QKD/PQC delivery.  Fail-safe
    // connection keeps the same fixture buildable against the archived
    // pre-PQC revision, where KeyServed above is the available trace.
    Config::ConnectFailSafe("/NodeList/*/ApplicationList/*/$ns3::QKDKeyManagerSystemApplication/KeyServedMixed",
                    MakeCallback(+[](std::string, const std::string& ksid,
                                     const std::string& srcSaeId, const std::string& dstSaeId,
                                     const uint32_t& srcNodeId, const uint32_t& dstNodeId,
                                     const std::string& keyId, const uint32_t& bits,
                                     const std::string& type) {
                        std::cout << "[SECOQC_KMS] Mixed key contribution type=" << type
                                  << " bits=" << bits << " keyId=" << keyId
                                  << " ksid=" << ksid << " srcSaeId=" << srcSaeId
                                  << " dstSaeId=" << dstSaeId << " srcNodeId=" << srcNodeId
                                  << " dstNodeId=" << dstNodeId << std::endl;
                    }));
    Config::Connect("/NodeList/*/ApplicationList/*/$ns3::QKDKeyManagerSystemApplication/RelayConsumption",
                    MakeCallback(+[](std::string, const uint32_t& node, const uint32_t& src,
                                     const uint32_t& dst, const uint32_t& bits) {
                        std::cout << "[SECOQC_KMS] Relay consumed node=" << node
                                  << " src=" << src << " dst=" << dst
                                  << " bits=" << bits << std::endl;
                    }));
    // RelaySuccess is a testbed correctness extension and is absent from the
    // pinned upstream/older distributed revisions.  RelayConsumption remains
    // available there, so the shared comparison fixture must not abort when
    // this optional confirmation trace cannot be resolved.
    Config::ConnectFailSafe("/NodeList/*/ApplicationList/*/$ns3::QKDKeyManagerSystemApplication/RelaySuccess",
                    MakeCallback(+[](std::string, const uint32_t& node, const uint32_t& src,
                                     const uint32_t& dst, const uint32_t& bits) {
                        std::cout << "[SECOQC_KMS] Relay succeeded node=" << node
                                  << " src=" << src << " dst=" << dst
                                  << " bits=" << bits << std::endl;
                    }));
    Config::Connect("/NodeList/*/ApplicationList/*/$ns3::QKDKeyManagerSystemApplication/WasteRelay",
                    MakeCallback(+[](std::string, const uint32_t& src,
                                     const uint32_t& dst, const uint32_t& bits) {
                        std::cout << "[SECOQC_KMS] Relay WASTED src=" << src
                                  << " dst=" << dst << " bits=" << bits << std::endl;
                    }));
    Config::Connect("/NodeList/*/ApplicationList/*/$ns3::QKDKeyManagerSystemApplication/BufferList/*/CurrentChange",
                    MakeCallback(+[](std::string context, uint32_t bits) {
                        std::cout << "[COMPARE_BUFFER] time="
                                  << Simulator::Now().GetSeconds()
                                  << " context=" << context
                                  << " bits=" << bits << std::endl;
                    }));
    Config::Connect("/NodeList/*/ApplicationList/*/$ns3::QKDKeyManagerSystemApplication/ListenReady",
                    MakeCallback(+[](std::string, const uint32_t& node) {
                        std::cout << "[SECOQC_KMS] node=" << node
                                  << " listening" << std::endl;
                    }));

    Ipv4GlobalRoutingHelper::PopulateRoutingTables();
    Simulator::ScheduleNow(&KeepAlive, MilliSeconds(100), Seconds(simulationTime));
    Simulator::Stop(Seconds(simulationTime));
    Simulator::Run();
    Simulator::Destroy();
    return 0;
}
