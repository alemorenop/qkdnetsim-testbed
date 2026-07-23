/*
 * HOST9 - ETSI 014 BOB (slave), key relay scenario
 *
 *   --devData / --myIpData  link to HOST8 (ETSI014 Alice)
 *   --devKms  / --myIpKms   link to HOST7 (KMS Bob)
 *
 * Contract shared with the other VMs (must match exactly):
 *   etsiBobId   -> also used on HOST7 when registering the app pair
 *   etsiAliceId -> must match the "appId" used by HOST8
 */

#include "ns3/core-module.h"
#include "ns3/network-module.h"
#include "ns3/internet-module.h"
#include "ns3/applications-module.h"
#include "ns3/mobility-module.h"
#include "ns3/fd-net-device-module.h"

#include "ns3/qkd-app-014.h"

using namespace ns3;

NS_LOG_COMPONENT_DEFINE("HOST9_ETSI014_BOB");

// Mitigation (does not fix the root cause) for a known race condition at
// RealtimeSimulatorImpl startup (confirmed via strace: a futex loop between
// 2 threads with the simulated clock not advancing and no network syscalls
// being made). Suspicion: the synchronizer thread is more vulnerable when
// there is NO event scheduled until appStartTime (here the app does nothing
// until then). We keep the event loop busy from t=0 with a trivial
// high-frequency event, in case that reduces the probability of the race.
static void
KeepAlive(Time period, Time stopTime)
{
    if(Simulator::Now() >= stopTime)
        return;
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
    std::string devData  = "eth0";
    std::string devKms   = "eth1";
    std::string myIpData = "192.168.121.9";
    std::string myIpKms  = "192.168.120.9";
    uint16_t    dataPort = 8081;

    // ---- Peers (real IPs of the other VMs) ----
    std::string peerAliceIp = "192.168.121.8"; // HOST8
    std::string kmsBobIp    = "192.168.120.7"; // HOST7 (KMS Bob)

    // ---- Identifier contract shared between VMs ----
    std::string etsiAliceId = "eeeeeeee-0000-0000-0000-000000000001"; // must match HOST8
    std::string etsiBobId   = "eeeeeeee-0000-0000-0000-000000000002"; // must match HOST7

    uint32_t numberOfKeyToFetchFromKMS = 1;
    uint32_t authenticationType = 0;
    uint32_t encryptionType = 1;
    uint32_t aesLifetime = 10000;
    uint32_t useCrypto = 1;

    uint32_t appStartTime = 50;
    uint32_t simulationTime = 5000;

    CommandLine cmd;
    cmd.AddValue("devData", "Real NIC toward HOST8", devData);
    cmd.AddValue("devKms", "Real NIC toward HOST7", devKms);
    cmd.AddValue("myIpData", "Local IP on the link toward HOST8", myIpData);
    cmd.AddValue("myIpKms", "Local IP on the link toward HOST7", myIpKms);
    cmd.AddValue("peerAliceIp", "Real IP of HOST8", peerAliceIp);
    cmd.AddValue("kmsBobIp", "Real IP of HOST7 (KMS Bob)", kmsBobIp);
    cmd.AddValue("etsiAliceId", "UUID of the peer app on HOST8", etsiAliceId);
    cmd.AddValue("etsiBobId", "UUID of this app (must match HOST7)", etsiBobId);
    cmd.AddValue("numberOfKeyToFetchFromKMS", "Keys to request per GET_KEY request", numberOfKeyToFetchFromKMS);
    cmd.AddValue("encryptionType", "0-unencrypted 1-OTP 2-AES", encryptionType);
    cmd.AddValue("authenticationType", "0-none 1-VMAC 2-MD5 3-SHA1", authenticationType);
    cmd.AddValue("useCrypto", "Run real cryptographic functions", useCrypto);
    cmd.AddValue("appStartTime", "Start instant (s)", appStartTime);
    cmd.AddValue("simTime", "Simulation duration (s)", simulationTime);
    cmd.Parse(argc, argv);

    Config::SetDefault("ns3::QKDApp014::NumberOfKeyToFetchFromKMS", UintegerValue(numberOfKeyToFetchFromKMS));
    Config::SetDefault("ns3::QKDApp014::AuthenticationType", UintegerValue(authenticationType));
    Config::SetDefault("ns3::QKDApp014::EncryptionType", UintegerValue(encryptionType));
    Config::SetDefault("ns3::QKDApp014::AESLifetime", UintegerValue(aesLifetime));
    Config::SetDefault("ns3::QKDApp014::UseCrypto", UintegerValue(useCrypto));

    NodeContainer self;
    self.Create(1);
    Ptr<Node> node = self.Get(0);

    InternetStackHelper internet;
    internet.Install(self);

    MobilityHelper mobility;
    mobility.SetMobilityModel("ns3::ConstantPositionMobilityModel");
    mobility.Install(self);

    AddEmuInterface(node, devData, myIpData, "00:00:00:00:19:01");
    AddEmuInterface(node, devKms, myIpKms, "00:00:00:00:19:02");

    Ptr<QKDApp014> app = CreateObject<QKDApp014>();
    app->Setup(
        "tcp",
        etsiBobId,
        etsiAliceId,
        InetSocketAddress(Ipv4Address(myIpData.c_str()), dataPort),
        InetSocketAddress(Ipv4Address(peerAliceIp.c_str()), dataPort),
        InetSocketAddress(Ipv4Address(kmsBobIp.c_str()), 80),
        "bob"
    );
    node->AddApplication(app);
    app->SetStartTime(Seconds(appStartTime));
    app->SetStopTime(Seconds(simulationTime));

    Simulator::Schedule(Seconds(0.0), &KeepAlive, MilliSeconds(100), Seconds(simulationTime));

    Ipv4GlobalRoutingHelper::PopulateRoutingTables();

    Config::Connect("/NodeList/*/ApplicationList/*/$ns3::QKDApp014/TxKMS",
                     MakeCallback(+[](std::string ctx, const std::string& appId, Ptr<const Packet> p) {
                         std::cout << "[HOST9] GET_KEY request to KMS Bob, appId=" << appId << " bytes=" << p->GetSize() << std::endl;
                     }));
    Config::Connect("/NodeList/*/ApplicationList/*/$ns3::QKDApp014/AppListenReady",
                     MakeCallback(+[](std::string ctx, const uint32_t& node) {
                         std::cout << "[HOST9] Listening for application traffic" << std::endl;
                     }));

    Simulator::Stop(Seconds(simulationTime));
    Simulator::Run();
    Simulator::Destroy();
    return 0;
}
