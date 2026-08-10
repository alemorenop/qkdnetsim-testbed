/*
 * Key-relay KMS BOB
 *
 * Symmetric to RELAY_KMS_ALICE (KMS Alice) - see the comments there for details on
 * why the manual relay S-Buffer bootstrap and periodic check are needed.
 *
 *   --devPP   / --myIpPP    link to RELAY_PP_BOB (post-processing Bob)
 *   --devKms  / --myIpKms   link to RELAY_KMS_TRUSTED (KMS Relay)          [relay/transform_keys]
 *   --devEtsi / --myIpEtsi  link to RELAY_ETSI014_BOB (ETSI014 Bob)        [GET_KEY]
 *
 * Contract shared with the other VMs (must match exactly):
 *   ppBobId     -> must match the "SetId" used by RELAY_PP_BOB
 *   etsiBobId   -> must match the "appId" used by RELAY_ETSI014_BOB
 *   etsiAliceId -> must match the "appId" used by RELAY_ETSI014_ALICE
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

NS_LOG_COMPONENT_DEFINE("RELAY_KMS_BOB_KMS_BOB");

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
    std::string myIpPP   = "192.168.116.7";
    std::string myIpKms  = "192.168.118.7";
    std::string myIpEtsi = "192.168.120.7";

    // ---- Peers (real IPs of the other VMs) ----
    std::string peerKmsRelayIp = "192.168.118.6"; // RELAY_KMS_TRUSTED, KMS Bob <-> KMS Relay link

    // ---- Identifier contract shared between VMs ----
    std::string ppBobId     = "dddddddd-0000-0000-0000-000000000002"; // RELAY_PP_BOB's module
    std::string etsiAliceId = "eeeeeeee-0000-0000-0000-000000000001"; // RELAY_ETSI014_ALICE's app
    std::string etsiBobId   = "eeeeeeee-0000-0000-0000-000000000002"; // RELAY_ETSI014_BOB's app

    // ---- Q-Buffer configuration ----
    uint32_t qbMin = 1024;
    uint32_t qbThr = 51200;
    uint32_t qbMax = 500000000;
    // See the full note in kms_alice.cc: MUST match ppKeySize*8 on the
    // post-processing side (pp_bob.cc), or GetDefaultKeyCount() will
    // never count any key as available and the relay will permanently fail
    // to start.
    uint32_t qbDefaultKeyBits = 2048;

    // ---- Replenishment period for the relay buffer toward Alice ----
    // See the full note in kms_alice.cc: do NOT lower this period (it
    // breaks Relay()'s IsRelayActive() guard). Supply is tuned via qbDefaultKeyBits.
    double relayCheckPeriodSec = 1.0;

    uint32_t simulationTime = 5000;

    CommandLine cmd;
    cmd.AddValue("devPP", "Real NIC toward RELAY_PP_BOB", devPP);
    cmd.AddValue("devKms", "Real NIC toward RELAY_KMS_TRUSTED", devKms);
    cmd.AddValue("devEtsi", "Real NIC toward RELAY_ETSI014_BOB", devEtsi);
    cmd.AddValue("myIpPP", "Local IP on the link toward RELAY_PP_BOB", myIpPP);
    cmd.AddValue("myIpKms", "Local IP on the link toward RELAY_KMS_TRUSTED", myIpKms);
    cmd.AddValue("myIpEtsi", "Local IP on the link toward RELAY_ETSI014_BOB", myIpEtsi);
    cmd.AddValue("peerKmsRelayIp", "Real IP of RELAY_KMS_TRUSTED (KMS Relay)", peerKmsRelayIp);
    cmd.AddValue("ppBobId", "UUID of RELAY_PP_BOB's post-processing module", ppBobId);
    cmd.AddValue("etsiAliceId", "UUID of RELAY_ETSI014_ALICE's ETSI014 app", etsiAliceId);
    cmd.AddValue("etsiBobId", "UUID of RELAY_ETSI014_BOB's ETSI014 app", etsiBobId);
    cmd.AddValue("simTime", "Simulation duration (s)", simulationTime);
    cmd.Parse(argc, argv);

    // IMPORTANT (see the full note in kms_alice.cc): the relay protocol
    // embeds raw ns-3 node IDs in the JSON messages, so all 3 processes must
    // agree on the same global scheme: (dummy)=0, Bob=1, Relay=2, Alice=3.
    // The initial "dummy" is mandatory (ID 0 is reserved as an "empty field"
    // in ProcessRelayRequest). NORMAL order (self first after the dummy) also
    // satisfies the local "master" relationship (Bob < Relay = Bob is "slave"
    // on that link, since the Relay is "master" toward Bob, see
    // kms_trusted.cc).
    CreateObject<Node>();                          // ID 0 = (unused, reserved)
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

    // Library bug (see the full comment in kms_alice.cc): without this
    // SetDefault, SBuffer::DoInitialize() overwrites any RELAY-type S-Buffer
    // with a fixed Mmax=128000 and KeySize=512, ignoring ConfigureRSBuffers().
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

    // Local identifier for "KMS Relay" (RELAY_KMS_TRUSTED) - direct neighbor
    Ptr<Node> relayHandle = CreateObject<Node>();  // ID 2 = Relay
    uint32_t relayId = relayHandle->GetId();

    // Local identifier for "KMS Alice" (RELAY_KMS_ALICE) - only reachable via relay
    Ptr<Node> aliceHandle = CreateObject<Node>();  // ID 3 = Alice
    uint32_t aliceId = aliceHandle->GetId();

    // --- Direct LOCAL link toward the Relay (fed by RELAY_PP_BOB) ---
    kms->CreateQBuffer(relayId, control->GetQBufferConf(relayId));
    kms->SetPeerKmAddress(relayId, Ipv4Address(peerKmsRelayIp.c_str()));
    kms->RegisterQKDModule(relayId, ppBobId);

    // --- Routes: Relay at 1 hop (direct), Alice at 2 hops (via Relay) ---
    control->AddRouteEntry(QKDLocationRegisterEntry(
        relayId, Ipv4Address(peerKmsRelayIp.c_str()), 1,
        relayId, Ipv4Address(peerKmsRelayIp.c_str()), "kms-relay"
    ));
    control->AddRouteEntry(QKDLocationRegisterEntry(
        relayId, Ipv4Address(peerKmsRelayIp.c_str()), 2,   // nextHop = Relay, 2 hops
        aliceId, Ipv4Address("0.0.0.0"), "kms-alice"        // dst = Alice (not directly reachable)
    ));

    // --- RELAY buffer toward Alice (manual bootstrap) ---
    kms->BootstrapRelaySBuffer(aliceId);
    Simulator::Schedule(Seconds(1.0), &PeriodicRelayCheck, kms, aliceId,
                         Seconds(relayCheckPeriodSec), Seconds(simulationTime));

    // Structural bug (see the full comment in kms_trusted.cc,
    // PeriodicLocalBufferCheck): the LOCAL_SBUFFER toward the Relay (relayId=2)
    // only auto-replenishes on receiving STORE_KEY if GetNode()->GetId() >
    // relayId. Since Bob(self)=1 < Relay=2, Bob is ALWAYS "slave" on that
    // link and never auto-replenishes. We reuse PeriodicRelayCheck (generic:
    // it just calls CheckBufferReplenishment, which dispatches by buffer
    // type) to cover this.
    Simulator::Schedule(Seconds(1.0), &PeriodicRelayCheck, kms, relayId,
                         Seconds(relayCheckPeriodSec), Seconds(simulationTime));

    // --- ETSI014 app pair (RELAY_ETSI014_BOB <-> RELAY_ETSI014_ALICE); from KMS Bob, RELAY_ETSI014_ALICE is reached via the Relay ---
    control->RegisterQKDApplicationPair(etsiBobId, etsiAliceId, aliceHandle);

    Ipv4GlobalRoutingHelper::PopulateRoutingTables();

    Config::Connect("/NodeList/*/ApplicationList/*/$ns3::QKDKeyManagerSystemApplication/QKDKeyGenerated",
                     MakeCallback(+[](std::string ctx, const std::string& appId, const std::string& keyId, const uint32_t& bits) {
                         std::cout << "[RELAY_KMS_BOB] KMS Bob stores key appId=" << appId << " keyId=" << keyId << " bits=" << bits << std::endl;
                     }));
    Config::Connect("/NodeList/*/ApplicationList/*/$ns3::QKDKeyManagerSystemApplication/KeyServed",
                     MakeCallback(+[](std::string ctx, const std::string& appId, const std::string& keyId, const uint32_t& bits) {
                         std::cout << "[RELAY_KMS_BOB] KMS Bob serves key appId=" << appId << " keyId=" << keyId << " bits=" << bits << std::endl;
                     }));
    Config::Connect("/NodeList/*/ApplicationList/*/$ns3::QKDKeyManagerSystemApplication/RelayConsumption",
                     MakeCallback(+[](std::string ctx, const uint32_t& node, const uint32_t& src, const uint32_t& dst, const uint32_t& amount) {
                         std::cout << "[RELAY_KMS_BOB] Relay consumed src=" << src << " dst=" << dst << " bits=" << amount << std::endl;
                     }));
    Config::Connect("/NodeList/*/ApplicationList/*/$ns3::QKDKeyManagerSystemApplication/ListenReady",
                     MakeCallback(+[](std::string ctx, const uint32_t& node) {
                         std::cout << "[RELAY_KMS_BOB] KMS Bob listening" << std::endl;
                     }));

    Simulator::Stop(Seconds(simulationTime));
    Simulator::Run();
    Simulator::Destroy();
    return 0;
}
