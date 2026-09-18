"""Correctness repairs shared by both sides of comparison builds."""

from pathlib import Path
import re


def _replace_cpp_function(text: str, signature: str, replacement: str) -> str:
    """Replace one out-of-class C++ function through the next ``void``."""
    start = text.find(signature)
    if start < 0:
        raise RuntimeError(f"C++ function not found: {signature}")
    declaration = text.rfind("void\n", 0, start)
    end = text.find("\nvoid\n", start)
    if declaration < 0 or end < 0:
        raise RuntimeError(f"C++ function boundaries not found: {signature}")
    return text[:declaration] + replacement.rstrip() + "\n" + text[end:]


def correlate_skey_create_responses(root: Path) -> None:
    """Match SKEY_CREATE acknowledgements by request ID, not peer FIFO."""
    path = root / "model" / "qkd-key-manager-system-application.cc"
    text = path.read_text(encoding="utf-8")
    signature = "QKDKeyManagerSystemApplication::ProcessSKeyCreateResponse"
    start = text.find(signature)
    end = text.find("\nvoid\n", start)
    if start < 0 or end < 0:
        raise RuntimeError("SKEY_CREATE response handler not found")
    handler = text[start:end]
    if "matchIndex" in handler:
        return

    origin_uri = '    headerUri += "/api/v1/sbuffers/skey_create";'
    if text.count(origin_uri) != 1:
        raise RuntimeError("origin SKEY_CREATE URI anchor not found once")
    text = text.replace(
        origin_uri,
        origin_uri
        + "\n    const std::string requestId = GenerateUUID();"
        + '\n    headerUri += "/?req_id=/" + requestId;',
    )
    origin_query = "    httpRequest.surplus_key_ID = surplusKeyId;"
    if text.count(origin_query) != 1:
        raise RuntimeError("origin SKEY_CREATE query anchor not found once")
    text = text.replace(origin_query, origin_query + "\n    httpRequest.req_id = requestId;")

    forward_uri = '      headerUriFwd += "/api/v1/sbuffers/skey_create";'
    if forward_uri in text:
        text = text.replace(
            forward_uri,
            forward_uri
            + "\n      const std::string forwardRequestId = GenerateUUID();"
            + '\n      headerUriFwd += "/?req_id=/" + forwardRequestId;',
            1,
        )
        forward_query = "      fwdQuery.request_uri = headerIn.GetUri();"
        if text.count(forward_query) != 1:
            raise RuntimeError("forwarded SKEY_CREATE query anchor not found once")
        text = text.replace(
            forward_query,
            forward_query + "\n      fwdQuery.req_id = forwardRequestId;",
        )

    start = text.find(signature)
    declaration = text.rfind("void\n", 0, start)
    end = text.find("\nvoid\n", start)
    handler = text[declaration:end]
    guard_end = handler.find("\n    }", handler.find("auto it = "))
    if guard_end < 0:
        raise RuntimeError("SKEY_CREATE pending-query guard not found")
    guard_end += len("\n    }")
    matcher = """

    if(uriParams.size() < 7 || uriParams[5] != "?req_id="){
      NS_LOG_ERROR(this << "Malformed SKEY_CREATE response URI" << headerIn.GetRequestUri());
      return;
    }
    const std::string requestId = uriParams[6];
    size_t matchIndex = it->second.size();
    for(size_t i = 0; i < it->second.size(); ++i){
      if(it->second[i].req_id == requestId){
        matchIndex = i;
        break;
      }
    }
    if(matchIndex == it->second.size()){
      NS_LOG_ERROR(this << "Unmatched SKEY_CREATE response" << requestId);
      return;
    }
    auto completeMatchedQuery = [&](){
      it->second.erase(it->second.begin() + matchIndex);
      if(it->second.empty())
        m_httpRequestsQueryKMS.erase(it);
    };"""
    handler = handler[:guard_end] + matcher + handler[guard_end:]
    handler = handler.replace("it->second.front()", "it->second[matchIndex]")
    handler = handler.replace("it->second[0]", "it->second[matchIndex]")
    completions = handler.count("HttpKMSCompleteQuery(peerAddress);")
    if completions not in (2, 3):
        raise RuntimeError(f"unexpected SKEY_CREATE completion count: {completions}")
    handler = handler.replace("HttpKMSCompleteQuery(peerAddress);", "completeMatchedQuery();")
    text = text[:declaration] + handler + text[end:]
    path.write_text(text, encoding="utf-8")


def normalize_partial_tcp_sends(root: Path) -> None:
    """Preserve complete HTTP byte streams while TCP applies backpressure.

    The historical KMS code treated ``Socket::Send`` as all-or-nothing and
    even sent queued packets twice after some connections completed. Keep a
    FIFO per socket and resume its unsent head from the send callback after a
    rejected or partially accepted write.
    """
    header = root / "model" / "qkd-key-manager-system-application.h"
    header_text = header.read_text(encoding="utf-8")
    if "#include <deque>" not in header_text:
        include_anchor = "#include <iostream>"
        if header_text.count(include_anchor) != 1:
            raise RuntimeError("could not add deque include to KMS header")
        header_text = header_text.replace(include_anchor, "#include <deque>\n" + include_anchor)

    queue_types = (
        (r"std::map<Ptr<Socket>,\s*Ptr<Packet>\s*>\s+m_packetQueues;[^\n]*",
         "std::map<Ptr<Socket>, std::deque<Ptr<Packet>>> m_packetQueues; //!< Pending APP-KMS HTTP byte streams"),
        (r"std::map<Ptr<Socket>,\s*Ptr<Packet>\s*>\s+m_packetQueuesKMS;[^\n]*",
         "std::map<Ptr<Socket>, std::deque<Ptr<Packet>>> m_packetQueuesKMS; //!< Pending KMS-KMS HTTP byte streams"),
    )
    for pattern, replacement in queue_types:
        header_text, count = re.subn(pattern, replacement, header_text)
        if count != 1:
            raise RuntimeError(f"expected one historical KMS queue declaration, found {count}")
    header.write_text(header_text, encoding="utf-8")

    source = root / "model" / "qkd-key-manager-system-application.cc"
    text = source.read_text(encoding="utf-8")
    has_pqc = "m_pqc_enabled" in text

    text = _replace_cpp_function(
        text,
        "QKDKeyManagerSystemApplication::ConnectionSucceeded(Ptr<Socket> socket)",
        """void
QKDKeyManagerSystemApplication::ConnectionSucceeded(Ptr<Socket> socket)
{
  NS_LOG_FUNCTION(this << socket);
  DataSend(socket, socket->GetTxAvailable());
}""",
    )
    pqc_tail = "\n\n  if(m_pqc_enabled)\n    SendPQCPublicKey(socket);" if has_pqc else ""
    text = _replace_cpp_function(
        text,
        "QKDKeyManagerSystemApplication::ConnectionSucceededKMSs(Ptr<Socket> socket)",
        f"""void
QKDKeyManagerSystemApplication::ConnectionSucceededKMSs(Ptr<Socket> socket)
{{
  NS_LOG_FUNCTION(this << socket);
  DataSendKMSs(socket, socket->GetTxAvailable());{pqc_tail}
}}""",
    )
    text = _replace_cpp_function(
        text,
        "QKDKeyManagerSystemApplication::DataSend(Ptr<Socket> s, uint32_t par)",
        """void
QKDKeyManagerSystemApplication::DataSend(Ptr<Socket> s, uint32_t par)
{
  NS_LOG_FUNCTION(this << s << par);
  auto it = m_packetQueues.find(s);
  if(it == m_packetQueues.end())
    return;
  auto& queue = it->second;
  while(!queue.empty())
  {
    Ptr<Packet> packet = queue.front();
    const int sent = s->Send(packet);
    if(sent <= 0)
      break;
    if(static_cast<uint32_t>(sent) < packet->GetSize())
    {
      packet->RemoveAtStart(static_cast<uint32_t>(sent));
      break;
    }
    queue.pop_front();
  }
  if(queue.empty())
    m_packetQueues.erase(it);
}""",
    )
    text = _replace_cpp_function(
        text,
        "QKDKeyManagerSystemApplication::DataSendKMSs(Ptr<Socket> s",
        """void
QKDKeyManagerSystemApplication::DataSendKMSs(Ptr<Socket> s, uint32_t par)
{
  NS_LOG_FUNCTION(this << s << par);
  auto it = m_packetQueuesKMS.find(s);
  if(it == m_packetQueuesKMS.end())
    return;
  auto& queue = it->second;
  while(!queue.empty())
  {
    Ptr<Packet> packet = queue.front();
    const int sent = s->Send(packet);
    if(sent <= 0)
      break;
    if(static_cast<uint32_t>(sent) < packet->GetSize())
    {
      packet->RemoveAtStart(static_cast<uint32_t>(sent));
      break;
    }
    queue.pop_front();
  }
  if(queue.empty())
    m_packetQueuesKMS.erase(it);
}""",
    )
    text = _replace_cpp_function(
        text,
        "QKDKeyManagerSystemApplication::SendToSocketPair(Ptr<Socket> socket, Ptr<Packet> packet)",
        """void
QKDKeyManagerSystemApplication::SendToSocketPair(Ptr<Socket> socket, Ptr<Packet> packet)
{
  NS_LOG_FUNCTION(this << socket);
  Address connectedAddress;
  m_packetQueues[socket].push_back(packet->Copy());
  m_txTrace(packet);
  if(socket->GetPeerName(connectedAddress) == 0)
    DataSend(socket, socket->GetTxAvailable());
}""",
    )
    text = _replace_cpp_function(
        text,
        "QKDKeyManagerSystemApplication::SendToSocketPairKMS(Ptr<Socket> socket, Ptr<Packet> packet)",
        """void
QKDKeyManagerSystemApplication::SendToSocketPairKMS(Ptr<Socket> socket, Ptr<Packet> packet)
{
  NS_LOG_FUNCTION(this << socket);
  Address connectedAddress;
  m_packetQueuesKMS[socket].push_back(packet->Copy());
  m_txTraceKMSs(packet, GetNode()->GetId());
  if(socket->GetPeerName(connectedAddress) == 0)
    DataSendKMSs(socket, socket->GetTxAvailable());
}""",
    )

    recv_anchor = "s->SetRecvCallback(MakeCallback(&QKDKeyManagerSystemApplication::HandleRead, this));"
    if text.count(recv_anchor) != 1:
        raise RuntimeError("APP-KMS accepted-socket callback anchor not found")
    text = text.replace(
        recv_anchor,
        recv_anchor + "\n  s->SetSendCallback(MakeCallback(&QKDKeyManagerSystemApplication::DataSend, this));",
    )
    recv_kms_anchor = "s->SetRecvCallback(MakeCallback(&QKDKeyManagerSystemApplication::HandleReadKMSs, this));"
    if text.count(recv_kms_anchor) != 1:
        raise RuntimeError("KMS-KMS accepted-socket callback anchor not found")
    text = text.replace(
        recv_kms_anchor,
        recv_kms_anchor + "\n  s->SetSendCallback(MakeCallback(&QKDKeyManagerSystemApplication::DataSendKMSs, this));",
    )
    text = text.replace(
        "socket->SetDataSentCallback( MakeCallback(&QKDKeyManagerSystemApplication::DataSendKMSs, this));",
        "socket->SetSendCallback(MakeCallback(&QKDKeyManagerSystemApplication::DataSendKMSs, this));",
    )
    if "SetDataSentCallback" in text:
        raise RuntimeError("obsolete KMS data-sent callback remains")
    source.write_text(text, encoding="utf-8")


def normalize_etsi014_http_recovery(root: Path) -> None:
    """Use socket-scoped framing and retry malformed KMS responses."""
    header = root / "model" / "qkd-app-014.h"
    header_text = header.read_text(encoding="utf-8")
    if "#include <map>" not in header_text:
        include_anchor = "#include <unordered_map>"
        if header_text.count(include_anchor) != 1:
            raise RuntimeError("could not add map include to ETSI 014 header")
        header_text = header_text.replace(
            include_anchor, "#include <map>\n" + include_anchor
        )
    old_buffer = (
        "std::unordered_map<Address, Ptr<Packet>, AddressHash> m_buffer_kms;"
    )
    new_buffer = "std::map<Ptr<Socket>, Ptr<Packet>> m_buffer_kms;"
    if old_buffer not in header_text:
        raise RuntimeError("historical ETSI 014 HTTP buffer declaration not found")
    header_text = header_text.replace(old_buffer, new_buffer)
    header.write_text(header_text, encoding="utf-8")

    source = root / "model" / "qkd-app-014.cc"
    text = source.read_text(encoding="utf-8")
    old_lookup = "Ptr<Packet> &buffer = m_buffer_kms[from];"
    if text.count(old_lookup) != 1:
        raise RuntimeError("historical ETSI 014 HTTP buffer lookup not found once")
    text = text.replace(old_lookup, "Ptr<Packet> &buffer = m_buffer_kms[socket];")

    extraction = """    if (!parser.TryExtractHttpMessage(bufferStr, httpMsgStr, httpMsgSize)) {
      NS_LOG_DEBUG("[DEBUG] Fragmented or incomplete HTTP message. Awaiting more data.");
      break;
    }
"""
    if text.count(extraction) != 1:
        raise RuntimeError("historical ETSI 014 extraction block not found once")
    recovery = extraction + """
    const size_t bodyStart = httpMsgStr.find("\\r\\n\\r\\n");
    const size_t nestedResponse = bodyStart == std::string::npos
      ? std::string::npos
      : httpMsgStr.find("HTTP/1.1 ", bodyStart + 4);
    if (socket == m_socketToKMS && nestedResponse != std::string::npos) {
      NS_LOG_WARN("Malformed KMS HTTP stream; reconnecting and retrying the request");
      m_buffer_kms.erase(socket);
      if(m_kmsRequestTimeoutEvent.IsPending())
        Simulator::Cancel(m_kmsRequestTimeoutEvent);
      Simulator::ScheduleNow(&QKDApp014::KmsRequestTimeout, this);
      return;
    }
"""
    text = text.replace(extraction, recovery)

    fatal_patterns = (
        'NS_FATAL_ERROR(this << "json parse error");',
        'NS_FATAL_ERROR(this << "json parse error" << header.GetMessageBodyString());',
    )
    replaced = 0
    for fatal in fatal_patterns:
        if fatal in text:
            text = text.replace(
                fatal,
                'NS_LOG_WARN(this << "Malformed JSON response from KMS; reconnecting and retrying");\n'
                '    Simulator::ScheduleNow(&QKDApp014::KmsRequestTimeout, this);\n'
                '    return;',
                1,
            )
            replaced += 1
    if replaced != 1:
        raise RuntimeError(f"expected one historical ETSI 014 fatal parser, found {replaced}")
    source.write_text(text, encoding="utf-8")


def remove_transform_accounting_assertions(root: Path) -> None:
    """Keep reserved S-buffer material out of selector invariants.

    ``m_currentKeyBit`` is global S-buffer accounting and can include keys
    already reserved in stream/supply pools.  ``GetTransformCandidate`` can
    only select READY keys from ``m_keys``.  Equating the two inside the
    selector therefore aborts valid concurrent workloads.
    """
    path = root / "model" / "s-buffer.cc"
    text = path.read_text(encoding="utf-8")
    start = text.find("SBuffer::GetTransformCandidate")
    end = text.find("SBuffer::GetHalfKey", start)
    if start < 0 or end < 0:
        raise RuntimeError("S-buffer transform selector boundaries not found")

    selector = text[start:end]
    selector, removed = re.subn(
        r"\n\s*NS_ASSERT\(totalReadyKeyCount == m_currentKeyBit\);",
        "",
        selector,
    )
    if removed != 2:
        raise RuntimeError(
            f"expected two transform accounting assertions, found {removed}"
        )
    path.write_text(text[:start] + selector + text[end:], encoding="utf-8")
