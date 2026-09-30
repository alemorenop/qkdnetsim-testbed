#ifndef QKDNETSIM_TESTBED_DISTRIBUTED_EMU_UTILS_H
#define QKDNETSIM_TESTBED_DISTRIBUTED_EMU_UTILS_H

#include "ns3/abort.h"
#include "ns3/arp-cache.h"
#include "ns3/ipv4-address.h"
#include "ns3/ipv4-interface.h"
#include "ns3/ipv4-l3-protocol.h"
#include "ns3/node.h"
#include "ns3/nstime.h"

#include <string>

namespace qkd_testbed
{

inline void
SetArpRecoveryTimeout(ns3::Ptr<ns3::Node> node,
                      const std::string& localIp,
                      ns3::Time timeout)
{
    ns3::Ptr<ns3::Ipv4L3Protocol> ipv4 = node->GetObject<ns3::Ipv4L3Protocol>();
    const int32_t interface = ipv4->GetInterfaceForAddress(ns3::Ipv4Address(localIp.c_str()));
    NS_ABORT_MSG_IF(interface < 0, "No IPv4 interface for " << localIp);

    ns3::Ptr<ns3::ArpCache> cache = ipv4->GetInterface(interface)->GetArpCache();
    NS_ABORT_MSG_IF(!cache, "No ARP cache for " << localIp);

    cache->SetDeadTimeout(timeout);
}

} // namespace qkd_testbed

#endif
