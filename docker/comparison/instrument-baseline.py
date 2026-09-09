#!/usr/bin/env python3
"""Normalize an archived upstream tree for a fair architecture comparison.

The benchmark workload is normalized without changing protocol semantics.  The
direct ETSI 014 example is reduced from two application flows to the single
Alice-to-Bob flow used by the distributed fixture, QKD-link inputs are aligned
with it, and workload knobs are exposed on the command line.  Known upstream
OTP-lifetime and batched ``skey_create`` correctness regressions are repaired
identically in both comparison deployments.
"""

from __future__ import annotations

import sys
import re
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from prefetch_policy import apply_prefetch_policy
from correctness_fixes import remove_transform_accounting_assertions


PADUA_INPUT = {
    "qkd_links": [
        {"startTime": 0, "stopTime": 100, "keyRate": rate,
         "keySize": size // 8, "ppPacketSize": 100, "ppRate": 1000,
         "srcDstDistance": 0, "srcNodeId": src, "dstNodeId": dst}
        for src, dst, rate, size in (
            (1, 2, 15000, 10000), (2, 3, 15000, 10000),
            (3, 4, 100000, 50000), (4, 5, 100000, 50000),
            (5, 6, 100000, 30000), (3, 6, 100000, 30000),
        )
    ],
    "etsi_004": [],
    "etsi_014": [
        {"startTime": start, "stopTime": stop, "encryptionType": enc,
         "authenticationType": 0, "numberOfKeyToFetchFromKMSOptions": 3,
         "appHoldTimeValue": 1, "appRate": rate, "appPacketSize": packet,
         "aesLifetime": lifetime, "srcDstDistance": 0,
         "srcNodeId": src, "dstNodeId": dst}
        for src, dst, rate, packet, enc, lifetime, start, stop in (
            (1, 5, 7000, 300, 1, 300000, 15, 75),
            (5, 1, 14000, 300, 1, 300000, 25, 85),
            (1, 6, 10000000, 800, 2, 300000, 10, 130),
        )
    ],
}


def add_accounting_traces(source: str, mixed: bool) -> str:
    """Expose stable KMS markers without changing model behaviour."""
    namespace_anchor = "using namespace ns3;"
    if source.count(namespace_anchor) != 1:
        raise RuntimeError("namespace anchor not found exactly once")
    if mixed:
        callback = r'''

void CompareKeyServed(std::string context, const std::string& ksid,
                      const std::string& srcSae, const std::string& dstSae,
                      const uint32_t& srcNode, const uint32_t& dstNode,
                      const std::string& keyId, const uint32_t& bits,
                      const std::string& materialType)
{
    if (Simulator::Now().GetSeconds() < comparisonMeasurementStart) return;
    std::cout << "[COMPARE_KMS] served context=" << context << " ksid=" << ksid
              << " srcSae=" << srcSae << " dstSae=" << dstSae
              << " srcNode=" << srcNode << " dstNode=" << dstNode
              << " keyId=" << keyId << " bits=" << bits
              << " type=" << materialType << std::endl;
}
'''
        served_trace = "KeyServedMixed"
    else:
        callback = r'''

void CompareKeyServed(std::string context, const std::string& appId,
                      const std::string& keyId, const uint32_t& bits)
{
    if (Simulator::Now().GetSeconds() < comparisonMeasurementStart) return;
    std::cout << "[COMPARE_KMS] served context=" << context << " appId=" << appId
              << " keyId=" << keyId << " bits=" << bits
              << " type=qkd" << std::endl;
}
'''
        served_trace = "KeyServed"
    callback += r'''

void CompareRelay(std::string, const uint32_t& node, const uint32_t& src,
                  const uint32_t& dst, const uint32_t& bits)
{
    if (Simulator::Now().GetSeconds() < comparisonMeasurementStart) return;
    std::cout << "[COMPARE_KMS] relay node=" << node << " src=" << src
              << " dst=" << dst << " bits=" << bits << std::endl;
}

void CompareWaste(std::string, const uint32_t& src, const uint32_t& dst,
                  const uint32_t& bits)
{
    if (Simulator::Now().GetSeconds() < comparisonMeasurementStart) return;
    std::cout << "[COMPARE_KMS] waste src=" << src << " dst=" << dst
              << " bits=" << bits << std::endl;
}

void CompareBuffer(std::string context, uint32_t bits)
{
    if (Simulator::Now().GetSeconds() < comparisonMeasurementStart) return;
    std::cout << "[COMPARE_BUFFER] time=" << Simulator::Now().GetSeconds()
              << " context=" << context << " bits=" << bits << std::endl;
}
'''
    source = source.replace(
        namespace_anchor,
        namespace_anchor + "\nextern double comparisonMeasurementStart;" + callback,
    )
    stop = source.find("Simulator::Stop")
    if stop < 0:
        raise RuntimeError("Simulator::Stop anchor not found")
    line_start = source.rfind("\n", 0, stop) + 1
    indent = source[line_start:stop]
    connections = (
        f'{indent}Config::Connect("/NodeList/*/ApplicationList/*/'
        f'$ns3::QKDKeyManagerSystemApplication/{served_trace}", '
        'MakeCallback(&CompareKeyServed));\n'
        f'{indent}Config::Connect("/NodeList/*/ApplicationList/*/'
        '$ns3::QKDKeyManagerSystemApplication/RelayConsumption", '
        'MakeCallback(&CompareRelay));\n'
        f'{indent}Config::Connect("/NodeList/*/ApplicationList/*/'
        '$ns3::QKDKeyManagerSystemApplication/WasteRelay", '
        'MakeCallback(&CompareWaste));\n\n'
        f'{indent}Config::Connect("/NodeList/*/ApplicationList/*/'
        '$ns3::QKDKeyManagerSystemApplication/BufferList/*/CurrentChange", '
        'MakeCallback(&CompareBuffer));\n\n'
    )
    return source[:line_start] + connections + source[line_start:]


def remove_second_application(source: str) -> str:
    marker = "cryptographicApplications.Add("
    first = source.find(marker)
    second = source.find(marker, first + len(marker)) if first >= 0 else -1
    if second < 0:
        raise RuntimeError("second ETSI 014 application flow not found")

    depth = 0
    seen_parenthesis = False
    end = second
    for index in range(second, len(source)):
        character = source[index]
        if character == "(":
            depth += 1
            seen_parenthesis = True
        elif character == ")":
            depth -= 1
        elif character == ";" and seen_parenthesis and depth == 0:
            end = index + 1
            break
    else:
        raise RuntimeError("unterminated second ETSI 014 application flow")

    while end < len(source) and source[end] in "\r\n":
        end += 1
    return source[:second] + source[end:]


def add_measurement_window(source: str) -> str:
    """Exclude application warm-up from the monolithic counters."""
    declaration = "uint32_t showKeyServed = 0;"
    if source.count(declaration) != 1:
        raise RuntimeError("measurement-window declaration anchor not found once")
    source = source.replace(
        declaration,
        declaration + "\ndouble comparisonMeasurementStart = 0.0;",
    )
    for function in (
        "KeyGenerated", "KeyServed", "SentPacket", "ReceivedPacket",
        "MissedSendPacketCall",
    ):
        pattern = rf"(void\s+{function}\s*\([^)]*\)\s*\{{\s*)"
        source, count = re.subn(
            pattern,
            rf"\1\n    if (Simulator::Now().GetSeconds() < comparisonMeasurementStart) return;\n",
            source,
            count=1,
        )
        if count != 1:
            raise RuntimeError(f"could not gate {function} to the measurement window")
    if '#include "ns3/qkd-app-header.h"' not in source:
        source = source.replace(
            '#include "ns3/qkd-app-004.h"',
            '#include "ns3/qkd-app-004.h"\n#include "ns3/qkd-app-header.h"',
        )
    sent_match = re.search(r"void\s+SentPacket\s*\([^)]*\)\s*\{", source)
    if not sent_match:
        raise RuntimeError("could not locate SentPacket for key-use instrumentation")
    guard_end = source.find("return;", sent_match.end())
    if guard_end < 0:
        raise RuntimeError("could not locate SentPacket measurement guard")
    guard_end = source.find("\n", guard_end) + 1
    key_use = r'''    Ptr<Packet> comparisonCopy = p->Copy();
    QKDAppHeader comparisonHeader;
    if (comparisonCopy->PeekHeader(comparisonHeader) > 0)
    {
        std::cout << "[COMPARE_APP_KEY] appId=" << appId
                  << " encKeyId=" << comparisonHeader.GetEncryptionKeyId()
                  << " authKeyId=" << comparisonHeader.GetAuthenticationKeyId()
                  << " payloadBits="
                  << (comparisonHeader.GetLength() - comparisonHeader.GetSerializedSize()) * 8
                  << std::endl;
    }
'''
    source = source[:guard_end] + key_use + source[guard_end:]
    trace_arg = 'cmd.AddValue ("trace", "Enable datapath stats and pcap traces", trace);'
    if source.count(trace_arg) != 1:
        raise RuntimeError("measurement-window command anchor not found once")
    return source.replace(
        trace_arg,
        'cmd.AddValue ("comparisonMeasurementStart", "Start of the measured application window", comparisonMeasurementStart);\n'
        '    ' + trace_arg,
    )


def prepare_padua_reference(root: Path, mixed: bool,
                            simulator: str = "realtime") -> None:
    """Make the JSON-driven upstream example usable as a single-process control."""
    path = root / "examples" / "examples_qkdnetsim_etsi_combined_input.cc"
    source = path.read_text(encoding="utf-8")
    source = source.replace('#include "ns3/mpi-module.h"\n', "")
    source = re.sub(r"\s*MpiInterface::Enable \(&argc, &argv\);\s*", "\n", source)
    source = re.sub(
        r"\s*systemId = MpiInterface::GetSystemId \(\);\s*"
        r"systemCount = MpiInterface::GetSize \(\);\s*",
        "\n", source,
    )
    source = re.sub(r"\s*MpiInterface::Disable \(\);\s*", "\n", source)
    implementation = {
        "realtime": "ns3::RealtimeSimulatorImpl",
        "default": "ns3::DefaultSimulatorImpl",
    }[simulator]
    source = source.replace("ns3::DistributedSimulatorImpl", implementation)
    source = source.replace(
        "std::vector<uint32_t> appRates = {20000, 30000, 50000, 100000, 150000};",
        "std::vector<uint32_t> appRates = {7000, 14000, 20000, 30000, 50000, "
        "100000, 150000, 10000000};",
    )
    # The reference config specifies a uniform 2 ms classical channel.  The
    # generic input example otherwise derives application delay from a distance
    # heuristic, which would introduce an unrelated workload difference.
    source = re.sub(
        r"std::string\s+CalculateAverageDelayBasedOnDistance\([^)]*\)\s*\{.*?\n\}",
        'std::string CalculateAverageDelayBasedOnDistance(double)\n{\n    return "2ms";\n}',
        source,
        count=1,
        flags=re.DOTALL,
    )
    if '#include "ns3/qkd-app-header.h"' not in source:
        source = source.replace(
            '#include "ns3/qkd-app-004.h"',
            '#include "ns3/qkd-app-004.h"\n#include "ns3/qkd-app-header.h"',
        )
    if simulator == "realtime":
        sent = re.search(r"void\s+SentPacket\s*\([^)]*\)\s*\{", source)
        if not sent:
            raise RuntimeError("Padua reference SentPacket callback not found")
        insert = source.find("\n", sent.end()) + 1
        key_trace = r'''    Ptr<Packet> comparisonCopy = p->Copy();
    QKDAppHeader comparisonHeader;
    if (comparisonCopy->PeekHeader(comparisonHeader) > 0)
    {
        std::cout << "[COMPARE_APP_KEY] appId=" << appId
                  << " encKeyId=" << comparisonHeader.GetEncryptionKeyId()
                  << " authKeyId=" << comparisonHeader.GetAuthenticationKeyId()
                  << " payloadBits="
                  << (comparisonHeader.GetLength() - comparisonHeader.GetSerializedSize()) * 8
                  << std::endl;
}
'''
        source = source[:insert] + key_trace + source[insert:]
    source = source.replace(
        "uint32_t showKeyAdded = 1;",
        "double comparisonMeasurementStart = 0.0;\nuint32_t showKeyAdded = 1;",
    )
    source = add_accounting_traces(source, mixed)
    path.write_text(source, encoding="utf-8")
    (root / "examples" / "padua-reference-input.json").write_text(
        json.dumps(PADUA_INPUT, indent=2), encoding="utf-8"
    )


def main() -> int:
    if len(sys.argv) not in (2, 3) or (len(sys.argv) == 3 and
                                     sys.argv[2] not in ("realtime", "default")):
        raise SystemExit(
            "usage: instrument-baseline.py ARCHIVE_ROOT [realtime|default]")
    root = Path(sys.argv[1])
    simulator = sys.argv[2] if len(sys.argv) == 3 else "realtime"
    examples = root / "examples"
    direct = examples / "examples_qkdnetsim_etsi_014.cc"
    relay = examples / "examples_qkdnetsim_secoqc.cc"
    if not direct.is_file() or not relay.is_file():
        raise RuntimeError("upstream comparison examples not found")

    has_mixed_trace = 'AddTraceSource("KeyServedMixed"' in (
        root / "model" / "qkd-key-manager-system-application.cc"
    ).read_text(encoding="utf-8")
    prepare_padua_reference(root, has_mixed_trace, simulator)
    direct_text = remove_second_application(direct.read_text(encoding="utf-8"))
    direct_text = direct_text.replace(
        "uint32_t ppKeySize = 8192;", "uint32_t ppKeySize = 256;"
    )
    direct_text = direct_text.replace(
        'cmd.AddValue ("trace", "Enable datapath stats and pcap traces", trace);',
        'cmd.AddValue ("appRate", "Offered application traffic rate (bps)", appRate);\n'
        '    cmd.AddValue ("appPacketSize", "Application payload size (bytes)", appPacketSize);\n'
        '    cmd.AddValue ("trace", "Enable datapath stats and pcap traces", trace);',
    )
    direct.write_text(
        add_measurement_window(add_accounting_traces(direct_text, has_mixed_trace)),
        encoding="utf-8",
    )

    relay_text = relay.read_text(encoding="utf-8")
    relay_text = relay_text.replace(
        "uint32_t ppKeySize = 8192;", "uint32_t ppKeySize = 256;"
    ).replace(
        "DataRate (7000), //@testing relay errors, use different ppKeyRate: e.g., 7000",
        "DataRate (ppKeyRate), //comparison workload: equal rate on every QKD link",
    )
    relay_text = relay_text.replace(
        'cmd.AddValue ("trace", "Enable datapath stats and pcap traces", trace);',
        'cmd.AddValue ("numberOfKeyToFetchFromKMS", "Keys per request", '
        'numberOfKeyToFetchFromKMS);\n'
        'cmd.AddValue ("appRate", "Offered application traffic rate (bps)", appRate);\n'
        '    cmd.AddValue ("appPacketSize", "Application payload size (bytes)", appPacketSize);\n'
        '    cmd.AddValue ("trace", "Enable datapath stats and pcap traces", trace);',
    )
    relay.write_text(
        add_measurement_window(add_accounting_traces(relay_text, has_mixed_trace)),
        encoding="utf-8",
    )

    # 1cda34c already contains this AES-only guard.  525e9bf accidentally
    # dropped it, causing OTP keys to be evicted on first use.  Apply the same
    # correctness condition to the archived upstream and distributed trees so
    # the deployment boundary, rather than a known regression, is compared.
    app014 = root / "model" / "qkd-app-014.cc"
    app014_text = app014.read_text(encoding="utf-8")
    unguarded = "if( localKey->GetLifetime() < 2*m_size ){"
    guarded = (
        "if(m_encryptionType == QKDEncryptor::QKDCRYPTO_AES && "
        "localKey->GetLifetime() < 2*m_size ){"
    )
    app014_text = app014_text.replace(unguarded, guarded)
    if app014_text.count(guarded) != 2:
        raise RuntimeError("expected two AES-only key-lifetime guards")
    app014.write_text(app014_text, encoding="utf-8")
    remove_transform_accounting_assertions(root)
    apply_prefetch_policy(root)

    # Keep keySize_qkd/keySize_pqc in bits *per returned key*.  Upstream's
    # first batched implementation mixed per-request and per-key units, so its
    # availability test could pass and GetTransformCandidate() still fail.
    kms = root / "model" / "qkd-key-manager-system-application.cc"
    kms_text = kms.read_text(encoding="utf-8")
    unit_fixes = (
        ("uint32_t keySize_pqc = requestedBits - keySize_qkd;",
         "uint32_t keySize_pqc = keySize - keySize_qkd;"),
        ("NS_ASSERT( keySize_qkd + keySize_pqc == requestedBits );",
         "NS_ASSERT((keySize_qkd + keySize_pqc) * keyNumber == requestedBits);"),
        ("sBufferPQC->GetBitCount() < keySize_pqc){",
         "sBufferPQC->GetBitCount() < keySize_pqc * keyNumber){"),
        ("keySize_qkd = requestedBits;",
         "keySize_qkd = size;"),
        ("ComputePqcMixing(requestedBits, availableKeyBits)",
         "ComputePqcMixing(size, availableKeyBits)"),
        ("if(requestedBits > availableKeyBits || !keySize_qkd)",
         "const uint32_t requiredQkdBits = keySize_qkd * number;\n"
         "    if(requiredQkdBits > availableKeyBits || !keySize_qkd)"),
    )
    applied_unit_fixes = 0
    for broken, fixed in unit_fixes:
        occurrences = kms_text.count(broken)
        if occurrences > 1:
            raise RuntimeError(f"ambiguous ETSI 014 unit expression: {broken}")
        if occurrences == 1:
            kms_text = kms_text.replace(broken, fixed)
            applied_unit_fixes += 1
    if applied_unit_fixes not in (0, len(unit_fixes)):
        raise RuntimeError("incomplete ETSI 014 per-key unit normalization")

    # Current upstream selects only one key's component at the sender and also
    # reconstructs only one at the receiver of a batched skey_create request.
    # Normalize both ends for QKD and PQC; the old source predates this code.
    batch_fixes = (
        ("uint32_t targetSize = keySize_qkd;",
         "uint32_t targetSize = keySize_qkd * keyNumber;"),
        ("uint32_t targetSizePQC = keySize_pqc;",
         "uint32_t targetSizePQC = keySize_pqc * keyNumber;"),
        ("uint32_t targetSize = keySizeQKD;",
         "uint32_t targetSize = keySizeQKD * keyNumber;"),
        ("uint32_t targetSizePQC = keySizePQC;",
         "uint32_t targetSizePQC = keySizePQC * keyNumber;"),
    )
    applied_batch_fixes = 0
    for broken, fixed in batch_fixes:
        occurrences = kms_text.count(broken)
        if occurrences > 1:
            raise RuntimeError(f"ambiguous skey_create target-size expression: {broken}")
        if occurrences == 1:
            kms_text = kms_text.replace(broken, fixed)
            applied_batch_fixes += 1
    normalized_batch_expressions = sum(
        kms_text.count(fixed) for _, fixed in batch_fixes
    )
    if normalized_batch_expressions not in (0, 4):
        raise RuntimeError("incomplete batched skey_create normalization")
    kms.write_text(kms_text, encoding="utf-8")

    etsi004 = examples / "examples_qkdnetsim_etsi_004.cc"
    if etsi004.is_file():
        etsi004.write_text(
            etsi004.read_text(encoding="utf-8").replace(
                "uint32_t ppKeySize = 8192;", "uint32_t ppKeySize = 256;"
            ),
            encoding="utf-8",
        )
    print(
        "instrumented upstream workload and applied shared correctness fixes "
        f"(per-key-units={applied_unit_fixes == len(unit_fixes)}, "
        f"batched-skey-create={normalized_batch_expressions == 4})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
