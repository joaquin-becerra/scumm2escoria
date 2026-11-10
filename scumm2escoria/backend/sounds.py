"""SCUMM sounds for Godot: Creative Voice (.voc) files to WAV.

ScummC builds each `sound x = "name.soun"` from sounds/name.voc, and `voice`
resources point at a .voc directly. Godot can't read VOC, so the samples are
rewritten as WAV (same 8-bit unsigned PCM, same rate).
"""
from __future__ import annotations

import shutil
import struct
import wave


def read_voc(path: str) -> tuple[int, bytes]:
    """(sample rate, 8-bit unsigned mono samples) of a Creative Voice file."""
    data = open(path, "rb").read()
    if not data.startswith(b"Creative Voice File\x1a"):
        raise ValueError(f"{path}: not a VOC file")
    pos = struct.unpack_from("<H", data, 20)[0]
    rate, samples = 11025, bytearray()
    while pos < len(data) and data[pos] != 0:          # 0: terminator
        kind = data[pos]
        size = int.from_bytes(data[pos + 1:pos + 4], "little")
        body = data[pos + 4:pos + 4 + size]
        if kind == 1:                                   # sound data: rate, codec, samples
            if body[1] != 0:
                raise ValueError(f"{path}: compressed VOC (codec {body[1]}) not supported")
            rate = round(1_000_000 / (256 - body[0]))
            samples += body[2:]
        elif kind == 2:                                 # continuation of the samples
            samples += body
        elif kind == 3:                                 # silence: length, rate
            samples += b"\x80" * (struct.unpack_from("<H", body, 0)[0] + 1)
        pos += 4 + size
    return rate, bytes(samples)


def voc_to_wav(src: str, dst: str) -> None:
    if src.endswith(".wav"):            # compiled games: already exported
        shutil.copyfile(src, dst)
        return
    rate, samples = read_voc(src)
    with wave.open(dst, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(1)        # WAV's 8-bit samples are unsigned, like VOC's
        w.setframerate(rate)
        w.writeframes(samples)
