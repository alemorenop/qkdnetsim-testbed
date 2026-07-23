/*
 * HOST5 - KMS ALICE (key relay scenario)
 *
 * Unlike scenario 1 (direct link), here KMS Alice does NOT have KMS Bob as a
 * direct neighbor - it only reaches KMS Relay directly, and KMS Bob through
 * it (2 hops). It therefore needs:
 *   - A LOCAL Q-Buffer/S-Buffer toward the Relay (fed by HOST1)
 *   - A RELAY-type S-Buffer toward Bob (created explicitly with
 *     BootstrapRelaySBuffer, see comment in qkd-key-manager-system-application.h)
 *   - A direct route entry toward the Relay (1 hop) and another toward Bob
 *     via the Relay (2 hops)
 *   - A periodic check (CheckBufferReplenishment) that keeps the relay
 *     buffer toward Bob replenished, since QKDApp014 never triggers that
 *     replenishment on its own (it doesn't send GET_STATUS)
 *
 *   --devPP   / --myIpPP    link to HOST1 (post-processing Alice)
 *   --devKms  / --myIpKms   link to HOST6 (KMS Relay)          [relay/transform_keys]
 *   --devEtsi / --myIpEtsi  link to HOST8 (ETSI014 Alice)      [GET_KEY]
 *
 * Contract shared with the other VMs (must match exactly):
 *   ppAliceId   -> must match the "SetId" used by HOST1
 *   etsiAliceId -> must match the "appId" used by HOST8
 *   etsiBobId   -> must match the "appId" used by HOST9
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

    // ---- Real interfaces of this VM (adjust to your lab) ----
    std::string devPP    = "eth0";
    std::string devKms   = "eth1";
    std::string devEtsi  = "eth2";
    std::string myIpPP   = "192.168.112.5";
    std::string myIpKms  = "192.168.117.5";
    std::string myIpEtsi = "192.168.119.5";

    // ---- Peers (real IPs of the other VMs) ----
    std::string peerKmsRelayIp = "192.168.117.6"; // HOST6, KMS Alice <-> KMS Relay link

    // ---- Identifier contract shared between VMs ----
    std::string ppAliceId   = "cccccccc-0000-0000-0000-000000000001"; // HOST1's module
    std::string etsiAliceId = "eeeeeeee-0000-0000-0000-000000000001"; // HOST8's app
    std::string etsiBobId   = "eeeeeeee-0000-0000-0000-000000000002"; // HOST9's app

    // ---- Q-Buffer configuration ----
    uint32_t qbMin = 1024;
    uint32_t qbThr = 51200;
    uint32_t qbMax = 500000000;
    // MUST match ppKeySize*8 (256 bytes = 2048 bits) on the post-processing
    // side (see host1_pp_alice.cc): GetDefaultKeyCount() in s-buffer.cc only
    // counts a key as "available" if its size matches GetKeySize() of the
    // local S-Buffer EXACTLY. If a different value is set here (e.g. 512 or
    // 4096), the count is always 0 and the relay never starts up
    // ("insufficient amount of key material"). With 2048 the Relay() ceiling
    // (20*qbDefaultKeyBits bits/tick, 1 tick/s) works out to 40960 bps, with
    // headroom over the ETSI014 side's GET_KEY demand (~6400 bps).
    uint32_t qbDefaultKeyBits = 2048;

    // ---- Replenishment period for the relay buffer toward Bob ----
    // NOTE: lowering this period does NOT help - SBufferClientCheck()/Relay()
    // uses an IsRelayActive() guard that discards overlapping calls, so a
    // period that's too short (tested with 0.02s) leaves the guard
    // permanently locked and the relay never fires again. The actual supply
    // is raised via qbDefaultKeyBits (each tick moves at most
    // 20*qbDefaultKeyBits bits, see Relay() in
    // qkd-key-manager-system-application.cc).
    double relayCheckPeriodSec = 1.0;

    uint32_t simulationTime = 5000;

    CommandLine cmd;
    cmd.AddValue("devPP", "Real NIC toward HOST1", devPP);
    cmd.AddValue("devKms", "Real NIC toward HOST6", devKms);
    cmd.AddValue("devEtsi", "Real NIC toward HOST8", devEtsi);
    cmd.AddValue("myIpPP", "Local IP on the link toward HOST1", myIpPP);
    cmd.AddValue("myIpKms", "Local IP on the link toward HOST6", myIpKms);
    cmd.AddValue("myIpEtsi", "Local IP on the link toward HOST8", myIpEtsi);
    cmd.AddValue("peerKmsRelayIp", "Real IP of HOST6 (KMS Relay)", peerKmsRelayIp);
    cmd.AddValue("ppAliceId", "UUID of HOST1's post-processing module", ppAliceId);
    cmd.AddValue("etsiAliceId", "UUID of HOST8's ETSI014 app", etsiAliceId);
    cmd.AddValue("etsiBobId", "UUID of HOST9's ETSI014 app", etsiBobId);
    cmd.AddValue("simTime", "Simulation duration (s)", simulationTime);
    cmd.Parse(argc, argv);

    // IMPORTANT: qkdnetsim's relay protocol embeds "raw" ns-3 node IDs inside
    // the JSON messages (source_node_id, destination_node_id), assuming all
    // KMSs share the same global numbering (true in a single process, false
    // in ours: each process has its own counter starting at 0). For HOST6 and
    // HOST7 to correctly interpret the IDs we send them, all 3 processes must
    // generate the SAME ID for the SAME conceptual entity, in the same order
    // in all 3 places:
    //   (dummy)=0, Bob=1, Relay=2, Alice(self)=3
    // (host6_kms_relay.cc and host7_kms_bob.cc already follow this same
    // order). The initial "dummy" is mandatory: ProcessRelayRequest() uses
    // NS_ASSERT(srcNodeId && dstNodeId), i.e. ID 0 is reserved as an "empty
    // field" and no real node may have it.
    //
    // While at it, creating the Relay placeholder BEFORE the real node makes
    // Alice end up with a higher GetId(): QKDKeyManagerSystemApplication
    // decides who is "master" of the LOCAL Alice-Relay link by comparing
    // GetNode()->GetId() > dstNodeId. With Alice as master, it is SHE who
    // triggers replenishment of the local S-Buffer when new key arrives from
    // HOST1 (same pattern as in scenario 1).
    CreateObject<Node>();                          // ID 0 = (unused, reserved)
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

    // Library bug (see s-buffer.cc, SBuffer::DoInitialize()): when a new
    // S-Buffer is created, Object::Initialize() overwrites whatever
    // ConfigureRSBuffers() set (via Init()/Configure()) with its OWN ns-3
    // attributes ("SMinimal"=10, "SMaximal"=128000, "SThreshold"=32000,
    // "SDefaultKeySize"=512), which nobody else touches. Without this
    // SetDefault, every RELAY-type S-Buffer is created with Mmax=128000 and
    // KeySize=512 regardless of ConfigureRSBuffers(), which caps the relay
    // supply at 20*512=10240 bits per tick.
    Config::SetDefault("ns3::SBuffer::SMinimal", UintegerValue(qbMin));
    Config::SetDefault("ns3::SBuffer::SThreshold", UintegerValue(qbThr));
    Config::SetDefault("ns3::SBuffer::SMaximal", UintegerValue(qbMax));
    Config::SetDefault("ns3::SBuffer::SDefaultKeySize", UintegerValue(qbDefaultKeyBits));

    Ptr<QKDControl> control = QLinkHelper.InstallQKDNController(node);
    QLinkHelper.ConfigureQBuffers({control}, qbMin, qbThr, qbMax, qbDefaultKeyBits);
    // Mandatory before BootstrapRelaySBuffer(): CreateRSBuffer() internally
    // uses m_rsbuffer_config, which is only initialized here.
    QLinkHelper.ConfigureRSBuffers({control}, qbMin, qbThr, qbMax, 0, qbDefaultKeyBits);

    QAHelper.InstallKeyManager(node, Ipv4Address(myIpKms.c_str()), 80, control);
    Ptr<QKDKeyManagerSystemApplication> kms = control->GetKeyManagerSystemApplication(node);

    uint32_t relayId = relayHandle->GetId();

    // bobHandle was already created at the start of main() (see comment
    // there) - only reachable via relay, but we need its GetId() here.
    uint32_t bobId = bobHandle->GetId();

    // --- Direct LOCAL link toward the Relay (fed by HOST1) ---
    kms->CreateQBuffer(relayId, control->GetQBufferConf(relayId));
    kms->SetPeerKmAddress(relayId, Ipv4Address(peerKmsRelayIp.c_str()));
    kms->RegisterQKDModule(relayId, ppAliceId);

    // --- Routes: Relay at 1 hop (direct), Bob at 2 hops (via Relay) ---
    control->AddRouteEntry(QKDLocationRegisterEntry(
        relayId, Ipv4Address(peerKmsRelayIp.c_str()), 1,
        relayId, Ipv4Address(peerKmsRelayIp.c_str()), "kms-relay"
    ));
    control->AddRouteEntry(QKDLocationRegisterEntry(
        relayId, Ipv4Address(peerKmsRelayIp.c_str()), 2,   // nextHop = Relay, 2 hops
        bobId, Ipv4Address("0.0.0.0"), "kms-bob"            // dst = Bob (not directly reachable)
    ));

    // --- RELAY buffer toward Bob (manual bootstrap, see file header) ---
    kms->BootstrapRelaySBuffer(bobId);
    Simulator::Schedule(Seconds(1.0), &PeriodicRelayCheck, kms, bobId,
                         Seconds(relayCheckPeriodSec), Seconds(simulationTime));

    // --- ETSI014 app pair (HOST8 <-> HOST9); from KMS Alice, HOST9 is reached via the Relay ---
    control->RegisterQKDApplicationPair(etsiAliceId, etsiBobId, bobHandle);

    Ipv4GlobalRoutingHelper::PopulateRoutingTables();

    Config::Connect("/NodeList/*/ApplicationList/*/$ns3::QKDKeyManagerSystemApplication/QKDKeyGenerated",
                     MakeCallback(+[](std::string ctx, const std::string& appId, const std::string& keyId, const uint32_t& bits) {
                         std::cout << "[HOST5] KMS Alice stores key appId=" << appId << " keyId=" << keyId << " bits=" << bits << std::endl;
                     }));
    Config::Connect("/NodeList/*/ApplicationList/*/$ns3::QKDKeyManagerSystemApplication/KeyServed",
                     MakeCallback(+[](std::string ctx, const std::string& appId, const std::string& keyId, const uint32_t& bits) {
                         std::cout << "[HOST5] KMS Alice serves key appId=" << appId << " keyId=" << keyId << " bits=" << bits << std::endl;
                     }));
    Config::Connect("/NodeList/*/ApplicationList/*/$ns3::QKDKeyManagerSystemApplication/RelayConsumption",
                     MakeCallback(+[](std::string ctx, const uint32_t& node, const uint32_t& src, const uint32_t& dst, const uint32_t& amount) {
                         std::cout << "[HOST5] Relay consumed src=" << src << " dst=" << dst << " bits=" << amount << std::endl;
                     }));
    Config::Connect("/NodeList/*/ApplicationList/*/$ns3::QKDKeyManagerSystemApplication/ListenReady",
                     MakeCallback(+[](std::string ctx, const uint32_t& node) {
                         std::cout << "[HOST5] KMS Alice listening" << std::endl;
                     }));

    Simulator::Stop(Seconds(simulationTime));
    Simulator::Run();
    Simulator::Destroy();
    return 0;
}
