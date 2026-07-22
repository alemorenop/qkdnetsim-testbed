/*
 * HOST5 - ETSI 014 ALICE (master) + hey
 *
 * Un unico nodo real con dos interfaces:
 *   --devData / --myIpData  enlace hacia HOST6 (ETSI014 Bob)     [trafico cifrado]
 *   --devKms  / --myIpKms   enlace hacia HOST3 (KMS Alice)       [GET_KEY]
 *
 * Ademas de esta simulacion ns-3, en esta misma VM se lanza por fuera el
 * generador de carga HTTP "hey" (https://github.com/rakyll/hey) apuntando
 * al puerto 80 de HOST3 para reproducir el benchmark del paper, por ejemplo:
 *
 *   hey -n 2200 -c 1 -m GET \
 *       http://192.168.13.3/api/v1/keys/<etsiAliceId>/enc_keys/number/100/size/1024
 *
 * Contrato compartido con las demas VMs (deben coincidir exactamente):
 *   etsiAliceId -> tambien usado en HOST3 al registrar el par de apps
 *   etsiBobId   -> debe coincidir con el "appId" que usa HOST6
 */

#include "ns3/core-module.h"
#include "ns3/network-module.h"
#include "ns3/internet-module.h"
#include "ns3/applications-module.h"
#include "ns3/mobility-module.h"
#include "ns3/fd-net-device-module.h"

#include "ns3/qkd-app-014.h"

using namespace ns3;

NS_LOG_COMPONENT_DEFINE("HOST5_ETSI014_ALICE");

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

    // ---- Interfaces reales de esta VM (ajusta a tu laboratorio) ----
    std::string devData  = "eth0";
    std::string devKms   = "eth1";
    std::string myIpData = "192.168.56.5";
    std::string myIpKms  = "192.168.35.5";
    uint16_t    dataPort = 8081;

    // ---- Peers (IPs reales de las otras VMs) ----
    std::string peerBobIp   = "192.168.56.6"; // HOST6
    std::string kmsAliceIp  = "192.168.35.3"; // HOST3 (KMS Alice)

    // ---- Contrato de identificadores compartido entre VMs ----
    std::string etsiAliceId = "bbbbbbbb-0000-0000-0000-000000000001"; // debe coincidir con HOST3
    std::string etsiBobId   = "bbbbbbbb-0000-0000-0000-000000000002"; // debe coincidir con HOST6/HOST4

    // ---- Parametros de la app criptografica ----
    uint32_t appPacketSize = 800;  // bytes
    uint32_t appRateBps    = 6400; // bps (=800 bytes/s, ~1 paquete/s; el valor original del
                                    // ejemplo de qkdnetsim era 10 bps, ~640s por paquete)
    uint32_t numberOfKeyToFetchFromKMS = 1;
    uint32_t authenticationType = 0; // 0-sin autenticacion, 1-VMAC, 2-MD5, 3-SHA1
    uint32_t encryptionType = 1;     // 0-sin cifrar, 1-OTP, 2-AES
    uint32_t aesLifetime = 10000;
    uint32_t useCrypto = 1;

    uint32_t appStartTime = 50;
    uint32_t simulationTime = 5000;

    CommandLine cmd;
    cmd.AddValue("devData", "NIC real hacia HOST6", devData);
    cmd.AddValue("devKms", "NIC real hacia HOST3", devKms);
    cmd.AddValue("myIpData", "IP local en el enlace hacia HOST6", myIpData);
    cmd.AddValue("myIpKms", "IP local en el enlace hacia HOST3", myIpKms);
    cmd.AddValue("peerBobIp", "IP real de HOST6", peerBobIp);
    cmd.AddValue("kmsAliceIp", "IP real de HOST3 (KMS Alice)", kmsAliceIp);
    cmd.AddValue("etsiAliceId", "UUID de esta app (debe coincidir con HOST3)", etsiAliceId);
    cmd.AddValue("etsiBobId", "UUID de la app par en HOST6", etsiBobId);
    cmd.AddValue("numberOfKeyToFetchFromKMS", "Claves a pedir por peticion GET_KEY", numberOfKeyToFetchFromKMS);
    cmd.AddValue("encryptionType", "0-sin cifrar 1-OTP 2-AES", encryptionType);
    cmd.AddValue("authenticationType", "0-ninguna 1-VMAC 2-MD5 3-SHA1", authenticationType);
    cmd.AddValue("useCrypto", "Ejecutar funciones criptograficas reales", useCrypto);
    cmd.AddValue("appStartTime", "Instante de inicio (s)", appStartTime);
    cmd.AddValue("simTime", "Duracion de la simulacion (s)", simulationTime);
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

    AddEmuInterface(node, devData, myIpData, "00:00:00:00:05:01");
    AddEmuInterface(node, devKms, myIpKms, "00:00:00:00:05:02");

    Ptr<QKDApp014> app = CreateObject<QKDApp014>();
    app->Setup(
        "tcp",
        etsiAliceId,
        etsiBobId,
        InetSocketAddress(Ipv4Address(myIpData.c_str()), dataPort),
        InetSocketAddress(Ipv4Address(peerBobIp.c_str()), dataPort),
        InetSocketAddress(Ipv4Address(kmsAliceIp.c_str()), 80),
        appPacketSize,
        DataRate(appRateBps),
        "alice"
    );
    node->AddApplication(app);
    app->SetStartTime(Seconds(appStartTime));
    app->SetStopTime(Seconds(simulationTime));

    Simulator::ScheduleNow(&KeepAlive, MilliSeconds(100), Seconds(simulationTime));

    Ipv4GlobalRoutingHelper::PopulateRoutingTables();

    Config::Connect("/NodeList/*/ApplicationList/*/$ns3::QKDApp014/TxKMS",
                     MakeCallback(+[](std::string ctx, const std::string& appId, Ptr<const Packet> p) {
                         std::cout << "[HOST5] Peticion GET_KEY a KMS Alice, appId=" << appId << " bytes=" << p->GetSize() << std::endl;
                     }));

    Simulator::Stop(Seconds(simulationTime));
    Simulator::Run();
    Simulator::Destroy();
    return 0;
}
