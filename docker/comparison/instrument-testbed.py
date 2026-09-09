#!/usr/bin/env python3
"""Instrument historical distributed consumers for matched measurements."""

from __future__ import annotations

import sys
import shutil
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from prefetch_policy import apply_prefetch_policy
from correctness_fixes import remove_transform_accounting_assertions


root = Path(sys.argv[1])
remove_transform_accounting_assertions(root)

# Add the comparison-only six-site distributed fixture to both immutable
# historical source archives.  This changes no model code: it supplies the
# same deployment adapter to ns-3.46 and ns-3.48.
fixture = Path(__file__).resolve().parents[2] / "examples" / "comparison" / "secoqc_site.cc"
fixture_target = root / "examples" / "comparison" / "secoqc_site.cc"
fixture_target.parent.mkdir(parents=True, exist_ok=True)
shutil.copy2(fixture, fixture_target)

examples_cmake = root / "examples" / "CMakeLists.txt"
cmake_text = examples_cmake.read_text(encoding="utf-8")
if "distributed_secoqc_site" not in cmake_text:
    cmake_text += (
        "\n# Comparison-only six-site distributed SECOQC fixture\n"
        "build_qkd_example(distributed_secoqc_site comparison/secoqc_site.cc)\n"
    )
    examples_cmake.write_text(cmake_text, encoding="utf-8")

dockerfile = root / "docker" / "Dockerfile"
docker_text = dockerfile.read_text(encoding="utf-8")
target_anchor = "        relay_etsi014_bob"
if "distributed_secoqc_site" not in docker_text:
    if docker_text.count(target_anchor) != 1:
        raise RuntimeError("could not add distributed SECOQC build target")
    added_target = target_anchor + " " + chr(92) + "\n        distributed_secoqc_site"
    docker_text = docker_text.replace(target_anchor, added_target)
    dockerfile.write_text(docker_text, encoding="utf-8")
relative_files = (
    "examples/point-to-point/etsi014_alice.cc",
    "examples/point-to-point/etsi014_bob.cc",
    "examples/key-relay/etsi014_alice.cc",
    "examples/key-relay/etsi014_bob.cc",
    "old-examples/examples/point-to-point/etsi014_alice.cc",
    "old-examples/examples/point-to-point/etsi014_bob.cc",
    "old-examples/examples/key-relay/etsi014_alice.cc",
    "old-examples/examples/key-relay/etsi014_bob.cc",
)
changed = 0
for relative in relative_files:
    path = root / relative
    if not path.exists():
        continue
    text = path.read_text(encoding="utf-8")
    is_alice = path.name.endswith("alice.cc")
    if is_alice and '#include "ns3/qkd-app-header.h"' not in text:
        include_anchor = '#include "ns3/qkd-app-014.h"'
        if text.count(include_anchor) != 1:
            raise RuntimeError(f"QKD header include anchor not found once: {path}")
        text = text.replace(
            include_anchor,
            include_anchor + '\n#include "ns3/qkd-app-header.h"',
        )
    if is_alice and 'cmd.AddValue("appRateBps"' not in text:
        anchor = (
            '    cmd.AddValue("numberOfKeyToFetchFromKMS", '
            '"Keys to request per GET_KEY request", numberOfKeyToFetchFromKMS);'
        )
        if text.count(anchor) != 1:
            raise RuntimeError(f"workload anchor not found exactly once: {path}")
        addition = (
            '    cmd.AddValue("appPacketSize", "Application payload size (bytes)", '
            'appPacketSize);\n'
            '    cmd.AddValue("appRateBps", "Offered application traffic rate (bps)", '
            'appRateBps);\n'
        )
        text = text.replace(anchor, addition + anchor)

    # The Padua reference contains three concurrent flows with different stop
    # instants and an explicit AES key lifetime.  Expose those existing model
    # attributes without changing their defaults for the ordinary comparison.
    if 'cmd.AddValue("appStopTime"' not in text:
        declaration = "    uint32_t appStartTime = 2;\n    uint32_t simulationTime = 5000;"
        if text.count(declaration) != 1:
            raise RuntimeError(f"application timing declaration not found once: {path}")
        text = text.replace(
            declaration,
            "    uint32_t appStartTime = 2;\n"
            "    uint32_t appStopTime = 0; // 0 keeps the process-lifetime default\n"
            "    uint32_t simulationTime = 5000;",
        )
        command_anchor = (
            '    cmd.AddValue("appStartTime", "Start instant (s)", appStartTime);\n'
            '    cmd.AddValue("simTime", "Simulation duration (s)", simulationTime);'
        )
        if text.count(command_anchor) != 1:
            raise RuntimeError(f"application timing command anchor not found once: {path}")
        text = text.replace(
            command_anchor,
            '    cmd.AddValue("appStartTime", "Start instant (s)", appStartTime);\n'
            '    cmd.AddValue("appStopTime", "Stop instant (s); 0 uses simTime", appStopTime);\n'
            '    cmd.AddValue("aesLifetime", "AES key lifetime (bytes)", aesLifetime);\n'
            '    cmd.AddValue("simTime", "Simulation duration (s)", simulationTime);',
        )
        parse_anchor = "    cmd.Parse(argc, argv);"
        if text.count(parse_anchor) != 1:
            raise RuntimeError(f"command parse anchor not found once: {path}")
        text = text.replace(
            parse_anchor,
            parse_anchor + "\n    if (appStopTime == 0) appStopTime = simulationTime;",
        )
        stop_call = "    app->SetStopTime(Seconds(simulationTime));"
        if text.count(stop_call) != 1:
            raise RuntimeError(f"application stop call not found once: {path}")
        text = text.replace(
            "    app->SetStopTime(Seconds(simulationTime));",
            "    app->SetStopTime(Seconds(appStopTime));",
        )

    if "[COMPARE_APP]" not in text:
        stop_anchor = "    Simulator::Stop(Seconds(simulationTime));"
        if text.count(stop_anchor) != 1:
            raise RuntimeError(f"trace anchor not found exactly once: {path}")
        direction = "Tx" if is_alice else "Rx"
        key_trace = (
            '                         Ptr<Packet> copy = p->Copy();\n'
            '                         QKDAppHeader header;\n'
            '                         if (copy->PeekHeader(header) > 0) {\n'
            '                             std::cout << "[COMPARE_APP_KEY] appId=" << appId\n'
            '                                       << " encKeyId=" << header.GetEncryptionKeyId()\n'
            '                                       << " authKeyId=" << header.GetAuthenticationKeyId()\n'
            '                                       << " payloadBits="\n'
            '                                       << (header.GetLength() - header.GetSerializedSize()) * 8\n'
            '                                       << std::endl;\n'
            '                         }\n'
            if is_alice else ''
        )
        trace = (
            f'    Config::Connect("/NodeList/*/ApplicationList/*/$ns3::QKDApp014/{direction}",\n'
            '                     MakeCallback(+[](std::string ctx, const std::string& appId, Ptr<const Packet> p) {\n'
            f'                         std::cout << "[COMPARE_APP] {direction} appId=" << appId\n'
            '                                   << " bytes=" << p->GetSize() << std::endl;\n'
            + key_trace
            + '                     }));\n\n'
        )
        text = text.replace(stop_anchor, trace + stop_anchor)

    if "[COMPARE_APP] Mx" not in text:
        stop_anchor = "    Simulator::Stop(Seconds(simulationTime));"
        if text.count(stop_anchor) != 1:
            raise RuntimeError(f"missed-send trace anchor not found exactly once: {path}")
        trace = (
            '    Config::Connect("/NodeList/*/ApplicationList/*/$ns3::QKDApp014/Mx",\n'
            '                     MakeCallback(+[](std::string ctx, const std::string& appId, Ptr<const Packet> p) {\n'
            '                         std::cout << "[COMPARE_APP] Mx appId=" << appId << std::endl;\n'
            '                     }));\n\n'
        )
        text = text.replace(stop_anchor, trace + stop_anchor)

    path.write_text(text, encoding="utf-8")
    changed += 1

if changed != 4:
    raise RuntimeError(f"expected to instrument four endpoint consumers, changed {changed}")
print(f"instrumented {changed} historical toy consumers")

# Preserve enough KeyServedMixed identity to separate application delivery
# (non-empty KSID) from internal KMS-to-KMS material movement.  Older pinned
# revisions do not expose the mixed trace and are intentionally left alone.
kms_files = (
    "examples/point-to-point/kms_alice.cc",
    "examples/point-to-point/kms_bob.cc",
    "examples/key-relay/kms_alice.cc",
    "examples/key-relay/kms_bob.cc",
)
mixed_markers = 0
old_marker = '<< " bits=" << bits << " keyId=" << keyId << std::endl;'
new_marker = (
    '<< " bits=" << bits << " keyId=" << keyId\n'
    '                                   << " ksid=" << ksid << " srcSaeId=" << srcSaeId\n'
    '                                   << " dstSaeId=" << dstSaeId << " srcNodeId=" << srcNodeId\n'
    '                                   << " dstNodeId=" << dstNodeId << std::endl;'
)
for relative in kms_files:
    path = root / relative
    if not path.exists():
        continue
    text = path.read_text(encoding="utf-8")
    if old_marker in text:
        text = text.replace(old_marker, new_marker)
        path.write_text(text, encoding="utf-8")
        mixed_markers += 1
print(f"enriched mixed KMS markers: {mixed_markers}")

# Keep the known OTP fix identical to the monolithic comparison image.  Older
# revisions already contain it; newer revisions are normalized here.
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
apply_prefetch_policy(root)

# Keep the batched skey_create repair identical to the current monolithic
# baseline.  The pinned new distributed revision inherited the same upstream
# error: only one key's material was selected at the sender and reconstructed
# at the receiver for a multi-key request.
kms = root / "model" / "qkd-key-manager-system-application.cc"
kms_text = kms.read_text(encoding="utf-8")
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
print(f"normalized batched skey_create: {normalized_batch_expressions == 4}")
