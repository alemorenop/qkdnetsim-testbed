"""Correctness repairs shared by both sides of comparison builds."""

from pathlib import Path
import re


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

