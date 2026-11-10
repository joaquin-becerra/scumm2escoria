"""SCUMM sound resources. Digital ones (SBL) hold VOC-style 8-bit PCM and become
WAV; the rest (MIDI, AdLib, CD track references) are kept as they are.
"""
from __future__ import annotations

import struct
import wave
from typing import Optional


# Rates the formula only approximates.
VOC_RATES = {0xA5: 11025, 0xA6: 11025, 0xD2: 22050, 0xD3: 22050}


def find_pcm(payload: bytes) -> Optional[tuple[int, bytes]]:
    """(sample rate, unsigned 8-bit samples) of an SOU/SBL sound, if it is one."""
    if payload[:4] != b"SOU ":
        return None
    pos = payload.find(b"AUdt")
    if pos < 0:
        return None
    (size,) = struct.unpack_from(">I", payload, pos + 4)
    data = payload[pos + 8:pos + size]
    samples, rate, i = bytearray(), 0, 0
    while i + 4 <= len(data) and data[i]:
        kind = data[i]
        length = int.from_bytes(data[i + 1:i + 4], "little")
        body = data[i + 4:i + 4 + length]
        if kind == 1 and len(body) >= 2:
            rate = rate or VOC_RATES.get(body[0], 1000000 // (256 - body[0]))
            samples += body[2:]
        elif kind == 2:
            samples += body
        i += 4 + length
    return (rate, bytes(samples)) if rate and samples else None


def write_wav(path: str, rate: int, samples: bytes) -> None:
    with wave.open(path, "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(1)
        f.setframerate(rate)
        f.writeframes(samples)
