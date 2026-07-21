/*
 * HOST9 - ETSI 014 BOB (slave), escenario key relay
 *
 *   --devData / --myIpData  enlace hacia HOST8 (ETSI014 Alice)
 *   --devKms  / --myIpKms   enlace hacia HOST7 (KMS Bob)
 *
 * Contrato compartido con las demas VMs (deben coincidir exactamente):
 *   etsiBobId   -> tambien usado en HOST7 al registrar el par de apps
 *   etsiAliceId -> debe coincidir con el "appId" que usa HOST8
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

// Mitigacion (no arregla la causa raiz) de una condicion de carrera conocida
// en el arranque de RealtimeSimulatorImpl (confirmada con strace: bucle de
// futex entre 2 hilos sin avanzar el reloj simulado ni hacer ninguna syscall
// de red). Sospecha: el hilo sincronizador es mas vulnerable cuando no hay
// NINGUN evento programado hasta appStartTime (aqui la app no hace nada hasta
// entonces). Mantenemos el bucle de eventos ocupado desde t=0 con un evento
// trivial de alta frecuencia, por si eso reduce la probabilidad de la carrera.
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
    std::string myIpData = "192.168.121.9";
    std::string myIpKms  = "192.168.120.9";
    uint16_t    dataPort = 8081;

    // ---- Peers (IPs reales de las otras VMs) ----
    std::string peerAliceIp = "192.168.121.8"; // HOST8
    std::string kmsBobIp    = "192.168.120.7"; // HOST7 (KMS Bob)

    // ---- Contrato de identificadores compartido entre VMs ----
    std::string etsiAliceId = "eeeeeeee-0000-0000-0000-000000000001"; // debe coincidir con HOST8
    std::string etsiBobId   = "eeeeeeee-0000-0000-0000-000000000002"; // debe coincidir con HOST7

    uint32_t numberOfKeyToFetchFromKMS = 1;
    uint32_t authenticationType = 0;
    uint32_t encryptionType = 1;
    uint32_t aesLifetime = 10000;
    uint32_t useCrypto = 0;

    uint32_t appStartTime = 50;
    uint32_t simulationTime = 5000;

    CommandLine cmd;
    cmd.AddValue("devData", "NIC real hacia HOST8", devData);
    cmd.AddValue("devKms", "NIC real hacia HOST7", devKms);
    cmd.AddValue("myIpData", "IP local en el enlace hacia HOST8", myIpData);
    cmd.AddValue("myIpKms", "IP local en el enlace hacia HOST7", myIpKms);
    cmd.AddValue("peerAliceIp", "IP real de HOST8", peerAliceIp);
    cmd.AddValue("kmsBobIp", "IP real de HOST7 (KMS Bob)", kmsBobIp);
    cmd.AddValue("etsiAliceId", "UUID de la app par en HOST8", etsiAliceId);
    cmd.AddValue("etsiBobId", "UUID de esta app (debe coincidir con HOST7)", etsiBobId);
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
                         std::cout << "[HOST9] Peticion GET_KEY a KMS Bob, appId=" << appId << " bytes=" << p->GetSize() << std::endl;
                     }));
    Config::Connect("/NodeList/*/ApplicationList/*/$ns3::QKDApp014/AppListenReady",
                     MakeCallback(+[](std::string ctx, const uint32_t& node) {
                         std::cout << "[HOST9] Escuchando trafico de aplicacion" << std::endl;
                     }));

    Simulator::Stop(Seconds(simulationTime));
    Simulator::Run();
    Simulator::Destroy();
    return 0;
}
