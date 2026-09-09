#!/usr/bin/env python3
"""Apply the same latency-tolerant ETSI 014 prefetch policy to an archive."""

from pathlib import Path


def replace_once(text: str, old: str, new: str, label: str) -> str:
    if text.count(old) != 1:
        raise RuntimeError(f"prefetch anchor {label!r} found {text.count(old)} times")
    return text.replace(old, new)


def apply_prefetch_policy(root: Path) -> None:
    """Refill before exhaustion and suppress duplicate in-flight requests."""
    header = root / "model" / "qkd-app-014.h"
    source = root / "model" / "qkd-app-014.cc"
    h = header.read_text(encoding="utf-8")
    c = source.read_text(encoding="utf-8")
    if "KeyBufferLowWatermark" in c:
        return

    h = replace_once(
        h,
        "    uint32_t    m_numberOfKeysKMS;                  //!< number of keys to fetch per request\n",
        "    uint32_t    m_numberOfKeysKMS;                  //!< number of keys to fetch per request\n"
        "    uint32_t    m_keyBufferLowWatermark;             //!< refill threshold\n"
        "    bool        m_encryptionRequestPending {false};\n"
        "    bool        m_authenticationRequestPending {false};\n",
        "header members",
    )
    c = replace_once(
        c,
        "                   MakeUintegerChecker<uint32_t>())\n"
        "    .AddAttribute(\"LengthOfAuthenticationTag\"",
        "                   MakeUintegerChecker<uint32_t>())\n"
        "    .AddAttribute(\"KeyBufferLowWatermark\",\n"
        "                   \"Refill an outbound key store at or below this number of keys\",\n"
        "                   UintegerValue(1),\n"
        "                   MakeUintegerAccessor(&QKDApp014::m_keyBufferLowWatermark),\n"
        "                   MakeUintegerChecker<uint32_t>())\n"
        "    .AddAttribute(\"LengthOfAuthenticationTag\"",
        "attribute",
    )
    c = replace_once(
        c,
        "  m_authStore.clear();\n\n}",
        "  m_authStore.clear();\n"
        "  m_encryptionRequestPending = false;\n"
        "  m_authenticationRequestPending = false;\n"
        "  m_kmsHttpReqQueue.clear();\n\n}",
        "store reset",
    )
    c = replace_once(
        c,
        "      if(GetEncryptionKeySize() != 0 && m_encStore.empty()) //Check the state of encryption key store\n"
        "          GetKeysFromKMS(\"encryption\"); // 0 - Encryption key\n"
        "      if(GetAuthenticationKeySize() != 0 && m_authStore.empty()) //Check the state of authentication key store\n"
        "          GetKeysFromKMS(\"authentication\"); // 1 - Authentication key\n",
        "      if(GetEncryptionKeySize() != 0 &&\n"
        "         m_encStore.size() <= m_keyBufferLowWatermark &&\n"
        "         !m_encryptionRequestPending)\n"
        "          GetKeysFromKMS(\"encryption\");\n"
        "      if(GetAuthenticationKeySize() != 0 &&\n"
        "         m_authStore.size() <= m_keyBufferLowWatermark &&\n"
        "         !m_authenticationRequestPending)\n"
        "          GetKeysFromKMS(\"authentication\");\n",
        "ManageStores",
    )
    c = replace_once(
        c,
        "  if(keyType == \"encryption\")\n"
        "    size = GetEncryptionKeySize();\n"
        "  else if(keyType == \"authentication\")\n"
        "    size = GetAuthenticationKeySize();\n",
        "  if(keyType == \"encryption\")\n"
        "  {\n"
        "    size = GetEncryptionKeySize();\n"
        "    m_encryptionRequestPending = true;\n"
        "  }\n"
        "  else if(keyType == \"authentication\")\n"
        "  {\n"
        "    size = GetAuthenticationKeySize();\n"
        "    m_authenticationRequestPending = true;\n"
        "  }\n",
        "request pending",
    )
    timeout_anchor = (
        "    m_socketToKMS = nullptr; //Forces PrepareSocketToKMS() to create a new socket on the next attempt\n"
        "  }\n\n  if(m_master)\n"
    )
    if timeout_anchor in c:
        c = replace_once(
            c,
            timeout_anchor,
            "    m_socketToKMS = nullptr; //Forces PrepareSocketToKMS() to create a new socket on the next attempt\n"
            "  }\n\n"
            "  m_encryptionRequestPending = false;\n"
            "  m_authenticationRequestPending = false;\n"
            "  m_kmsHttpReqQueue.clear();\n\n"
            "  if(m_master)\n",
            "timeout reset",
        )
    c = replace_once(
        c,
        "  }else if(reqMethod == \"enc_keys\"){\n"
        "    if(header.GetStatus() == HTTPMessage::Ok){\n"
        "      std::string keyType {PopHttpKmsRequest()};\n",
        "  }else if(reqMethod == \"enc_keys\"){\n"
        "    std::string keyType {PopHttpKmsRequest()};\n"
        "    if(keyType == \"encryption\")\n"
        "      m_encryptionRequestPending = false;\n"
        "    else if(keyType == \"authentication\")\n"
        "      m_authenticationRequestPending = false;\n"
        "    if(header.GetStatus() == HTTPMessage::Ok){\n",
        "response reset",
    )
    header.write_text(h, encoding="utf-8")
    source.write_text(c, encoding="utf-8")
