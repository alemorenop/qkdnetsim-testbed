/*
 * HOST6 - KMS RELAY (intermediate node, "trusted node")
 *
 * Has no ETSI014 application of its own: its only job is to act as a bridge
 * between KMS Alice and KMS Bob. It has TWO direct QKD links (one to each
 * side), each with its own LOCAL Q-Buffer/S-Buffer:
 *   - Alice side: fed by HOST2, LOCAL_SBUFFER indexed by KMS Alice's ID
 *   - Bob side:   fed by HOST3, LOCAL_SBUFFER indexed by KMS Bob's ID
 *
 * It doesn't need any RELAY-type S-Buffer of its own (that's only for the
 * endpoints, Alice and Bob) - the actual forwarding is done by the internal
 * Relay()/ProcessRelayRequest function, using these two LOCAL buffers
 * directly, triggered automatically when real relay traffic arrives.
 *
 * "Master" trick in two roles at once (see comment in host5_kms_alice.cc):
 *   - Alice side: the Relay must be "slave" (Alice is already master in its own script)
 *     -> Alice's placeholder is created AFTER the real node (normal order)
 *   - Bob side: the Relay must be "master" (Bob is "slave" in its own script)
 *     -> Bob's placeholder is created BEFORE the real node
 *
 *   --devPPA  / --myIpPPA   link to HOST2 (post-processing Relay-A)
 *   --devPPB  / --myIpPPB   link to HOST3 (post-processing Relay-B)
 *   --devKmsA / --myIpKmsA  link to HOST5 (KMS Alice)
 *   --devKmsB / --myIpKmsB  link to HOST7 (KMS Bob)
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

// Structural bug (not a timing one): QKDKeyManagerSystemApplication::ProcessQKD014()
// only auto-replenishes a link's LOCAL_SBUFFER on receiving STORE_KEY if
// GetNode()->GetId() > dstNodeId ("isMaster"). With our ID scheme
// (dummy=0, Bob=1, Relay=2, Alice=3) the Relay is ALWAYS "slave" toward Alice
// (2 < 3), so its local S-Buffer toward Alice never auto-replenishes and the
// first relay hop (Alice->Relay) always fails with 400 Bad Request
// ("key_ID not found in 'dec' buffer"), no matter how long we wait. This is
// compensated for with this manual periodic check (same pattern as
// PeriodicRelayCheck in host5_kms_alice.cc/host7_kms_bob.cc).
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

    // ---- Real interfaces of this VM (adjust to your lab) ----
    std::string devPPA   = "eth0";
    std::string devPPB   = "eth1";
    std::string devKmsA  = "eth2";
    std::string devKmsB  = "eth3";
    std::string myIpPPA  = "192.168.113.6";
    std::string myIpPPB  = "192.168.115.6";
    std::string myIpKmsA = "192.168.117.6";
    std::string myIpKmsB = "192.168.118.6";

    // ---- Peers (real IPs of the other VMs) ----
    std::string peerKmsAliceIp = "192.168.117.5"; // HOST5
    std::string peerKmsBobIp   = "192.168.118.7"; // HOST7

    // ---- Identifier contract shared between VMs ----
    std::string ppRelayAId = "cccccccc-0000-0000-0000-000000000002"; // HOST2's module
    std::string ppRelayBId = "dddddddd-0000-0000-0000-000000000001"; // HOST3's module

    // ---- Q-Buffer configuration ----
    uint32_t qbMin = 1024;
    uint32_t qbThr = 51200;
    uint32_t qbMax = 500000000;
    // MUST match qbDefaultKeyBits in host5_kms_alice.cc and host7_kms_bob.cc
    // (2048 = ppKeySize*8 on the post-processing side): the relay protocol
    // assumes every KMS uses the same default key size (see the
    // "Keys must be in default size!" comment in Relay(),
    // qkd-key-manager-system-application.cc). A different value here causes
    // NS_FATAL_ERROR "key size != input size" in QKDEncryptor::COTP() when
    // re-encrypting the relayed key with an incompatible size.
    uint32_t qbDefaultKeyBits = 2048;

    uint32_t simulationTime = 5000;

    CommandLine cmd;
    cmd.AddValue("devPPA", "Real NIC toward HOST2", devPPA);
    cmd.AddValue("devPPB", "Real NIC toward HOST3", devPPB);
    cmd.AddValue("devKmsA", "Real NIC toward HOST5", devKmsA);
    cmd.AddValue("devKmsB", "Real NIC toward HOST7", devKmsB);
    cmd.AddValue("myIpPPA", "Local IP on the link toward HOST2", myIpPPA);
    cmd.AddValue("myIpPPB", "Local IP on the link toward HOST3", myIpPPB);
    cmd.AddValue("myIpKmsA", "Local IP on the link toward HOST5", myIpKmsA);
    cmd.AddValue("myIpKmsB", "Local IP on the link toward HOST7", myIpKmsB);
    cmd.AddValue("peerKmsAliceIp", "Real IP of HOST5 (KMS Alice)", peerKmsAliceIp);
    cmd.AddValue("peerKmsBobIp", "Real IP of HOST7 (KMS Bob)", peerKmsBobIp);
    cmd.AddValue("ppRelayAId", "UUID of HOST2's post-processing module", ppRelayAId);
    cmd.AddValue("ppRelayBId", "UUID of HOST3's post-processing module", ppRelayBId);
    cmd.AddValue("simTime", "Simulation duration (s)", simulationTime);
    cmd.Parse(argc, argv);

    // IMPORTANT (see the full note in host5_kms_alice.cc): the relay protocol
    // embeds raw ns-3 node IDs in the JSON messages, so all 3 processes must
    // agree on the same global scheme: (dummy)=0, Bob=1, Relay=2, Alice=3.
    // The initial "dummy" is mandatory (ID 0 is reserved as an "empty field"
    // in ProcessRelayRequest). This creation order also satisfies the local
    // "master" relationship (Relay > Bob = master toward Bob; Relay < Alice =
    // slave toward Alice, since Alice is master in its own script).
    CreateObject<Node>();                          // ID 0 = (unused, reserved)
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

    // Library bug (see the full comment in host5_kms_alice.cc): without this
    // SetDefault, every S-Buffer created (including the LOCAL_SBUFFERs used
    // by this node) ends up with a fixed KeySize=512 via
    // SBuffer::DoInitialize(), regardless of what is passed to
    // ConfigureQBuffers().
    Config::SetDefault("ns3::SBuffer::SMinimal", UintegerValue(qbMin));
    Config::SetDefault("ns3::SBuffer::SThreshold", UintegerValue(qbThr));
    Config::SetDefault("ns3::SBuffer::SMaximal", UintegerValue(qbMax));
    Config::SetDefault("ns3::SBuffer::SDefaultKeySize", UintegerValue(qbDefaultKeyBits));

    Ptr<QKDControl> control = QLinkHelper.InstallQKDNController(node);
    QLinkHelper.ConfigureQBuffers({control}, qbMin, qbThr, qbMax, qbDefaultKeyBits);

    // The KMS Relay listens on port 80; we use the interface toward KMS Bob as
    // the "public" address (same logic as in scenario 1: one is enough).
    QAHelper.InstallKeyManager(node, Ipv4Address(myIpKmsB.c_str()), 80, control);
    Ptr<QKDKeyManagerSystemApplication> kms = control->GetKeyManagerSystemApplication(node);

    uint32_t aliceId = aliceHandle->GetId();
    uint32_t bobId   = bobHandle->GetId();

    // --- Direct LOCAL link toward Alice (fed by HOST2) ---
    kms->CreateQBuffer(aliceId, control->GetQBufferConf(aliceId));
    kms->SetPeerKmAddress(aliceId, Ipv4Address(peerKmsAliceIp.c_str()));
    kms->RegisterQKDModule(aliceId, ppRelayAId);

    // --- Direct LOCAL link toward Bob (fed by HOST3) ---
    kms->CreateQBuffer(bobId, control->GetQBufferConf(bobId));
    kms->SetPeerKmAddress(bobId, Ipv4Address(peerKmsBobIp.c_str()));
    kms->RegisterQKDModule(bobId, ppRelayBId);

    // --- Routes: both neighbors are direct (1 hop) from the Relay's point of view ---
    control->AddRouteEntry(QKDLocationRegisterEntry(
        aliceId, Ipv4Address(peerKmsAliceIp.c_str()), 1,
        aliceId, Ipv4Address(peerKmsAliceIp.c_str()), "kms-alice"
    ));
    control->AddRouteEntry(QKDLocationRegisterEntry(
        bobId, Ipv4Address(peerKmsBobIp.c_str()), 1,
        bobId, Ipv4Address(peerKmsBobIp.c_str()), "kms-bob"
    ));

    // Manual periodic check of both LOCAL S-Buffers (see the full comment
    // next to PeriodicLocalBufferCheck): the Alice side NEVER auto-replenishes
    // (the Relay is "slave" there, 2 < 3) and would stay empty forever
    // without this. Also applied to the Bob side for symmetry/safety, even
    // though that side does auto-replenish via "isMaster" on STORE_KEY.
    Simulator::Schedule(Seconds(1.0), &PeriodicLocalBufferCheck, kms, aliceId, Seconds(1.0), Seconds(simulationTime));
    Simulator::Schedule(Seconds(1.0), &PeriodicLocalBufferCheck, kms, bobId, Seconds(1.0), Seconds(simulationTime));

    Ipv4GlobalRoutingHelper::PopulateRoutingTables();

    Config::Connect("/NodeList/*/ApplicationList/*/$ns3::QKDKeyManagerSystemApplication/QKDKeyGenerated",
                     MakeCallback(+[](std::string ctx, const std::string& appId, const std::string& keyId, const uint32_t& bits) {
                         std::cout << "[HOST6] KMS Relay stores key appId=" << appId << " keyId=" << keyId << " bits=" << bits << std::endl;
                     }));
    Config::Connect("/NodeList/*/ApplicationList/*/$ns3::QKDKeyManagerSystemApplication/RelayConsumption",
                     MakeCallback(+[](std::string ctx, const uint32_t& node, const uint32_t& src, const uint32_t& dst, const uint32_t& amount) {
                         std::cout << "[HOST6] Relay consumed src=" << src << " dst=" << dst << " bits=" << amount << std::endl;
                     }));
    Config::Connect("/NodeList/*/ApplicationList/*/$ns3::QKDKeyManagerSystemApplication/WasteRelay",
                     MakeCallback(+[](std::string ctx, const uint32_t& src, const uint32_t& dst, const uint32_t& amount) {
                         std::cout << "[HOST6] Relay WASTED src=" << src << " dst=" << dst << " bits=" << amount << std::endl;
                     }));
    Config::Connect("/NodeList/*/ApplicationList/*/$ns3::QKDKeyManagerSystemApplication/ListenReady",
                     MakeCallback(+[](std::string ctx, const uint32_t& node) {
                         std::cout << "[HOST6] KMS Relay listening" << std::endl;
                     }));

    Simulator::Stop(Seconds(simulationTime));
    Simulator::Run();
    Simulator::Destroy();
    return 0;
}
