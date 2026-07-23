/*
 * HOST3 - KMS ALICE
 *
 * A single real node with three interfaces bridged to the real world:
 *   --devPP   / --myIpPP    link to HOST1 (post-processing Alice)
 *   --devKms  / --myIpKms   link to HOST4 (KMS Bob)          [relay/transform_keys, port 8080]
 *   --devEtsi / --myIpEtsi  link to HOST5 (ETSI014 Alice)    [GET_KEY, port 80]
 *
 * "KMS Bob" (HOST4) is NOT instantiated here: only an empty local Node
 * (kmsBobHandle) is created, which serves as an internal identifier (GetId())
 * for the QKDControl/QKDKeyManagerSystemApplication structures. The real
 * relay traffic goes over --peerKmsBobIp on top of EmuFdNetDevice.
 *
 * Contract shared with the other VMs (must match exactly):
 *   ppAliceId   -> must match the "SetId" used by HOST1
 *   etsiAliceId -> must match the "appId" used by HOST5
 *   etsiBobId   -> must match the "appId" used by HOST6
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

    // ---- Real interfaces of this VM (adjust to your lab) ----
    std::string devPP    = "eth0";
    std::string devKms   = "eth1";
    std::string devEtsi  = "eth2";
    std::string myIpPP   = "192.168.13.3";
    std::string myIpKms  = "192.168.34.3";
    std::string myIpEtsi = "192.168.35.3";

    // ---- Peers (real IPs of the other VMs) ----
    std::string peerKmsBobIp = "192.168.34.4"; // HOST4, KM-KM link

    // ---- Identifier contract shared between VMs ----
    std::string ppAliceId   = "aaaaaaaa-0000-0000-0000-000000000001"; // HOST1's module
    std::string etsiAliceId = "bbbbbbbb-0000-0000-0000-000000000001"; // HOST5's app
    std::string etsiBobId   = "bbbbbbbb-0000-0000-0000-000000000002"; // HOST6's app

    // ---- Q-Buffer configuration (same values as in the examples) ----
    uint32_t qbMin = 1024;
    uint32_t qbThr = 51200;
    uint32_t qbMax = 500000000;
    uint32_t qbDefaultKeyBits = 512;

    uint32_t simulationTime = 5000;

    CommandLine cmd;
    cmd.AddValue("devPP", "Real NIC toward HOST1", devPP);
    cmd.AddValue("devKms", "Real NIC toward HOST4", devKms);
    cmd.AddValue("devEtsi", "Real NIC toward HOST5", devEtsi);
    cmd.AddValue("myIpPP", "Local IP on the link toward HOST1", myIpPP);
    cmd.AddValue("myIpKms", "Local IP on the link toward HOST4", myIpKms);
    cmd.AddValue("myIpEtsi", "Local IP on the link toward HOST5", myIpEtsi);
    cmd.AddValue("peerKmsBobIp", "Real IP of HOST4 (KMS Bob)", peerKmsBobIp);
    cmd.AddValue("ppAliceId", "UUID of HOST1's post-processing module", ppAliceId);
    cmd.AddValue("etsiAliceId", "UUID of HOST5's ETSI014 app", etsiAliceId);
    cmd.AddValue("etsiBobId", "UUID of HOST6's ETSI014 app", etsiBobId);
    cmd.AddValue("simTime", "Simulation duration (s)", simulationTime);
    cmd.Parse(argc, argv);

    // Created BEFORE the real node so that the latter ends up with a higher
    // Node::GetId(): QKDKeyManagerSystemApplication internally decides who is
    // "master" of each KMS-KMS pair by comparing GetNode()->GetId() >
    // dstNodeId (see ProcessRequest/SBufferClientCheck) - that "master" is the
    // one that triggers replenishment of the transformed-key buffer. With
    // HOST3 as "master" and HOST4 (host4_kms_bob.cc, which creates its real
    // node first, without this trick) as "slave", exactly one side ends up in
    // each role.
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

    // The KMS listens on port 80 on the interface facing HOST4/HOST5 (in this
    // point-to-point experiment, a single "public" address is enough for the
    // KMS; we use the interface toward HOST4 for relay requests and the one
    // toward HOST5 for GET_KEY, both served by the same KMS application).
    QAHelper.InstallKeyManager(node, Ipv4Address(myIpKms.c_str()), 80, control);
    Ptr<QKDKeyManagerSystemApplication> kms = control->GetKeyManagerSystemApplication(node);

    // kmsBobHandle was already created at the start of main() (see comment
    // there). It never runs any code, it only provides a stable GetId() to
    // reference "KMS Bob".
    uint32_t kmsBobId = kmsBobHandle->GetId();

    kms->CreateQBuffer(kmsBobId, control->GetQBufferConf(kmsBobId));
    kms->SetPeerKmAddress(kmsBobId, Ipv4Address(peerKmsBobIp.c_str()));

    // Direct route entry (1 hop, no relay) toward KMS Bob. Without this,
    // QKDControl::GetRoute() finds nothing in the (empty) routing table and
    // returns a default QKDLocationRegisterEntry with uninitialized fields,
    // which ends up causing a null pointer when processing the first GET_KEY
    // request.
    control->AddRouteEntry(QKDLocationRegisterEntry(
        kmsBobId,                              // nextHopKmNodeId (direct hop)
        Ipv4Address(peerKmsBobIp.c_str()),     // nextHopKmNodeAddress
        1,                                      // hops
        kmsBobId,                               // dstKmNodeId
        Ipv4Address(peerKmsBobIp.c_str()),      // dstKmAddress
        "kms-bob"                               // dstKmId (descriptive only)
    ));

    // HOST1's post-processing module feeds the buffer used to reach KMS Bob
    kms->RegisterQKDModule(kmsBobId, ppAliceId);

    // Register the ETSI014 app pair (HOST5 <-> HOST6); from KMS Alice's point
    // of view, the remote app (HOST6) is reached "via KMS Bob"
    control->RegisterQKDApplicationPair(etsiAliceId, etsiBobId, kmsBobHandle);

    Ipv4GlobalRoutingHelper::PopulateRoutingTables();

    Config::Connect("/NodeList/*/ApplicationList/*/$ns3::QKDKeyManagerSystemApplication/QKDKeyGenerated",
                     MakeCallback(+[](std::string ctx, const std::string& appId, const std::string& keyId, const uint32_t& bits) {
                         std::cout << "[HOST3] KMS Alice stores key appId=" << appId << " keyId=" << keyId << " bits=" << bits << std::endl;
                     }));
    Config::Connect("/NodeList/*/ApplicationList/*/$ns3::QKDKeyManagerSystemApplication/KeyServed",
                     MakeCallback(+[](std::string ctx, const std::string& appId, const std::string& keyId, const uint32_t& bits) {
                         std::cout << "[HOST3] KMS Alice serves key appId=" << appId << " keyId=" << keyId << " bits=" << bits << std::endl;
                     }));
    Config::Connect("/NodeList/*/ApplicationList/*/$ns3::QKDKeyManagerSystemApplication/ListenReady",
                     MakeCallback(+[](std::string ctx, const uint32_t& node) {
                         std::cout << "[HOST3] KMS Alice listening" << std::endl;
                     }));

    Simulator::Stop(Seconds(simulationTime));
    Simulator::Run();
    Simulator::Destroy();
    return 0;
}
