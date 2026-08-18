/*
 * Point-to-point KMS BOB
 *
 * Symmetric to P2P_KMS_ALICE (kms_alice.cc). A single real node with three interfaces:
 *   --devPP   / --myIpPP    link to P2P_PP_BOB (post-processing Bob)
 *   --devKms  / --myIpKms   link to P2P_KMS_ALICE (KMS Alice)         [relay/transform_keys]
 *   --devEtsi / --myIpEtsi  link to the Bob VPN SAE (ETSI 004/014)      [port 80]
 *
 * Contract shared with the other VMs (must match exactly):
 *   ppBobId     -> must match the "SetId" used by P2P_PP_BOB
 *   etsiBobId   -> must match the SAE ID used by the Bob VPN endpoint
 *   etsiAliceId -> must match the SAE ID used by the Alice VPN endpoint
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

NS_LOG_COMPONENT_DEFINE("P2P_KMS_BOB_KMS_BOB");

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
    std::string devPP    = "eth0";
    std::string devKms   = "eth1";
    std::string devEtsi  = "eth2";
    std::string myIpPP   = "192.168.24.4";
    std::string myIpKms  = "192.168.34.4";
    std::string myIpEtsi = "192.168.46.4";

    // ---- Peers (real IPs of the other VMs) ----
    std::string peerKmsAliceIp = "192.168.34.3"; // P2P_KMS_ALICE, KM-KM link

    // ---- Identifier contract shared between VMs ----
    std::string ppBobId     = "aaaaaaaa-0000-0000-0000-000000000002"; // P2P_PP_BOB's module
    std::string etsiAliceId = "bbbbbbbb-0000-0000-0000-000000000001"; // Alice VPN SAE
    std::string etsiBobId   = "bbbbbbbb-0000-0000-0000-000000000002"; // Bob VPN SAE

    // ---- Q-Buffer configuration ----
    uint32_t qbMin = 1024;
    uint32_t qbThr = 51200;
    uint32_t qbMax = 500000000;
    uint32_t qbDefaultKeyBits = 512;

    uint32_t simulationTime = 5000;

    CommandLine cmd;
    cmd.AddValue("devPP", "Real NIC toward P2P_PP_BOB", devPP);
    cmd.AddValue("devKms", "Real NIC toward P2P_KMS_ALICE", devKms);
    cmd.AddValue("devEtsi", "Real NIC toward the Bob VPN SAE", devEtsi);
    cmd.AddValue("myIpPP", "Local IP on the link toward P2P_PP_BOB", myIpPP);
    cmd.AddValue("myIpKms", "Local IP on the link toward P2P_KMS_ALICE", myIpKms);
    cmd.AddValue("myIpEtsi", "Local IP on the link toward the Bob VPN SAE", myIpEtsi);
    cmd.AddValue("peerKmsAliceIp", "Real IP of P2P_KMS_ALICE (KMS Alice)", peerKmsAliceIp);
    cmd.AddValue("ppBobId", "UUID of P2P_PP_BOB's post-processing module", ppBobId);
    cmd.AddValue("etsiAliceId", "SAE ID of the Alice VPN endpoint", etsiAliceId);
    cmd.AddValue("etsiBobId", "SAE ID of the Bob VPN endpoint", etsiBobId);
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

    AddEmuInterface(node, devPP, myIpPP, "00:00:00:00:04:01");
    AddEmuInterface(node, devKms, myIpKms, "00:00:00:00:04:02");
    AddEmuInterface(node, devEtsi, myIpEtsi, "00:00:00:00:04:03");

    QKDLinkHelper QLinkHelper;
    QKDAppHelper QAHelper;

    Ptr<QKDControl> control = QLinkHelper.InstallQKDNController(node);
    QLinkHelper.ConfigureQBuffers({control}, qbMin, qbThr, qbMax, qbDefaultKeyBits);

    QAHelper.InstallKeyManager(node, Ipv4Address(myIpKms.c_str()), 80, control);
    Ptr<QKDKeyManagerSystemApplication> kms = control->GetKeyManagerSystemApplication(node);

    // Local identifier for "KMS Alice" (P2P_KMS_ALICE)
    Ptr<Node> kmsAliceHandle = CreateObject<Node>();
    uint32_t kmsAliceId = kmsAliceHandle->GetId();

    kms->CreateQBuffer(kmsAliceId, control->GetQBufferConf(kmsAliceId));
    kms->SetPeerKmAddress(kmsAliceId, Ipv4Address(peerKmsAliceIp.c_str()));

    // Direct route entry (1 hop, no relay) toward KMS Alice. Without this,
    // QKDControl::GetRoute() finds nothing in the (empty) routing table and
    // returns a default QKDLocationRegisterEntry with uninitialized fields,
    // which ends up causing a null pointer when processing the first GET_KEY
    // request.
    control->AddRouteEntry(QKDLocationRegisterEntry(
        kmsAliceId,                              // nextHopKmNodeId (direct hop)
        Ipv4Address(peerKmsAliceIp.c_str()),     // nextHopKmNodeAddress
        1,                                        // hops
        kmsAliceId,                               // dstKmNodeId
        Ipv4Address(peerKmsAliceIp.c_str()),      // dstKmAddress
        "kms-alice"                               // dstKmId (descriptive only)
    ));

    // P2P_PP_BOB's post-processing module feeds the buffer used to reach KMS Alice
    kms->RegisterQKDModule(kmsAliceId, ppBobId);

    // From KMS Bob's point of view, the remote SAE is reached "via KMS Alice".
    control->RegisterQKDApplicationPair(etsiBobId, etsiAliceId, kmsAliceHandle);

    Ipv4GlobalRoutingHelper::PopulateRoutingTables();

    Config::Connect("/NodeList/*/ApplicationList/*/$ns3::QKDKeyManagerSystemApplication/QKDKeyGenerated",
                     MakeCallback(+[](std::string ctx, const std::string& appId, const std::string& keyId, const uint32_t& bits) {
                         std::cout << "[P2P_KMS_BOB] KMS Bob stores key appId=" << appId << " keyId=" << keyId << " bits=" << bits << std::endl;
                     }));
    Config::Connect("/NodeList/*/ApplicationList/*/$ns3::QKDKeyManagerSystemApplication/KeyServed",
                     MakeCallback(+[](std::string ctx, const std::string& appId, const std::string& keyId, const uint32_t& bits) {
                         std::cout << "[P2P_KMS_BOB] KMS Bob serves key appId=" << appId << " keyId=" << keyId << " bits=" << bits << std::endl;
                     }));
    Config::Connect("/NodeList/*/ApplicationList/*/$ns3::QKDKeyManagerSystemApplication/ListenReady",
                     MakeCallback(+[](std::string ctx, const uint32_t& node) {
                         std::cout << "[P2P_KMS_BOB] KMS Bob listening" << std::endl;
                     }));

    Simulator::Stop(Seconds(simulationTime));
    Simulator::Run();
    Simulator::Destroy();
    return 0;
}
