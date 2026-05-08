"""Мелкая утилита: определение mime по magic bytes."""
import hashlib

_PREFIXES = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
)


def detect_mime(data: bytes) -> str | None:
    for prefix, mime in _PREFIXES:
        if data.startswith(prefix):
            return mime
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def sha256(data: bytes) -> bytes:
    return hashlib.sha256(data).digest()
