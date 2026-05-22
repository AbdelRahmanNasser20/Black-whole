"""Byte-identity sanity check on dewatermark.ai API output.

Trust the API. This only catches the one failure mode it can't recover from
on its own — an HTTP 200 response that echoed the input back unchanged.
"""
from pathlib import Path


def watermark_likely_present(original: Path, cleaned: Path) -> bool:
    """Return True only when the cleaned file is unusable.

    Reject if missing, empty, or byte-identical to the original. Anything else
    is trusted as a legitimate API result.
    """
    try:
        if not cleaned.exists() or cleaned.stat().st_size == 0:
            return True
        if cleaned.read_bytes() == original.read_bytes():
            return True
    except Exception:
        return True
    return False
