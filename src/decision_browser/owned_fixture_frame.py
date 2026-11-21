"""Strict 1280x720 PNG decode and canonical color-type-6 encode.

Stdlib struct, zlib, and binascii only. Decode accepts color type 2 or 6.
Inflate is one zlib.decompressobj() and one decompress call with positive
max_length equal to height * (1 + width * channels). Encode writes the target
RGBA with filter None, one IDAT, zlib.compress level 9, and no ancillary chunks.
Every denial is PngDenied('denied') and carries no input or caught-exception text.
"""

import binascii
import struct
import zlib
from collections.abc import Callable
from typing import NoReturn, TypeGuard

VIEW_WIDTH = 1280
VIEW_HEIGHT = 720
ENCODED_PNG_MAX = 2097152
RGBA_OCTETS = VIEW_WIDTH * VIEW_HEIGHT * 4

_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_ANCILLARY = frozenset((b"gAMA", b"cHRM", b"sRGB", b"pHYs"))

__all__ = (
    "ENCODED_PNG_MAX",
    "RGBA_OCTETS",
    "VIEW_HEIGHT",
    "VIEW_WIDTH",
    "PngDenied",
    "decode_owned_png",
    "encode_canonical_png",
)


class PngDenied(Exception):
    def __init__(self) -> None:
        super().__init__("denied")


def _deny() -> NoReturn:
    raise PngDenied() from None


def _finite_positive(value: object) -> TypeGuard[int | float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return value > 0 and value < float("inf")


def _require_before_stage(clock: Callable[[], object], original_deadline: object) -> None:
    if not _finite_positive(original_deadline) or not callable(clock):
        _deny()
    try:
        now = clock()
    except Exception:  # noqa: BLE001
        _deny()
    if not _finite_positive(now):
        _deny()
    if not now < original_deadline:
        _deny()


def _chunk(tag: bytes, data: bytes) -> bytes:
    crc = binascii.crc32(tag + data) & 0xFFFFFFFF
    return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", crc)


def _paeth(left: int, up: int, up_left: int) -> int:
    estimate = left + up - up_left
    dist_left = abs(estimate - left)
    dist_up = abs(estimate - up)
    dist_up_left = abs(estimate - up_left)
    if dist_left <= dist_up and dist_left <= dist_up_left:
        return left
    if dist_up <= dist_up_left:
        return up
    return up_left


def _unfilter_to_rgba(inflated: bytes, channels: int) -> bytes:
    row_stride = 1 + VIEW_WIDTH * channels
    if len(inflated) != VIEW_HEIGHT * row_stride:
        _deny()
    rgba = bytearray(RGBA_OCTETS)
    prior = bytearray(VIEW_WIDTH * channels)
    pixel_bytes = channels
    for y in range(VIEW_HEIGHT):
        offset = y * row_stride
        filter_byte = inflated[offset]
        if filter_byte > 4:
            _deny()
        scan = inflated[offset + 1 : offset + row_stride]
        recon = bytearray(VIEW_WIDTH * channels)
        for index in range(VIEW_WIDTH * channels):
            raw = scan[index]
            left = recon[index - pixel_bytes] if index >= pixel_bytes else 0
            up = prior[index]
            up_left = prior[index - pixel_bytes] if index >= pixel_bytes else 0
            if filter_byte == 0:
                value = raw
            elif filter_byte == 1:
                value = (raw + left) & 255
            elif filter_byte == 2:
                value = (raw + up) & 255
            elif filter_byte == 3:
                value = (raw + ((left + up) // 2)) & 255
            else:
                value = (raw + _paeth(left, up, up_left)) & 255
            recon[index] = value
        prior = recon
        dest = y * VIEW_WIDTH * 4
        if channels == 4:
            rgba[dest : dest + VIEW_WIDTH * 4] = recon
        else:
            cursor = dest
            for index in range(0, VIEW_WIDTH * 3, 3):
                rgba[cursor] = recon[index]
                rgba[cursor + 1] = recon[index + 1]
                rgba[cursor + 2] = recon[index + 2]
                rgba[cursor + 3] = 255
                cursor += 4
    return bytes(rgba)


def decode_owned_png(
    png: bytes, *, original_deadline: float, clock: Callable[[], object]
) -> bytes:
    _require_before_stage(clock, original_deadline)
    if not isinstance(png, bytes) or len(png) > ENCODED_PNG_MAX or len(png) < 8:
        _deny()
    if png[:8] != _SIGNATURE:
        _deny()
    pos = 8
    stage = 0
    channels = 0
    exact_inflated = 0
    idat_compressed_max = 0
    idat_parts: list[bytes] = []
    idat_len = 0
    while pos < len(png):
        remaining = len(png) - pos
        if remaining < 12:
            _deny()
        length = struct.unpack_from(">I", png, pos)[0]
        if length > remaining or length > ENCODED_PNG_MAX or pos + 12 + length > len(png):
            _deny()
        tag = png[pos + 4 : pos + 8]
        data = png[pos + 8 : pos + 8 + length]
        stored = struct.unpack_from(">I", png, pos + 8 + length)[0]
        if not (tag.isascii() and tag.isalpha()):
            _deny()
        if (binascii.crc32(tag + data) & 0xFFFFFFFF) != stored:
            _deny()
        if stage == 0:
            if tag != b"IHDR" or length != 13:
                _deny()
            width, height, depth, color, compression, png_filter, interlace = struct.unpack(
                ">IIBBBBB", data
            )
            if (
                width != VIEW_WIDTH
                or height != VIEW_HEIGHT
                or depth != 8
                or color not in (2, 6)
                or compression != 0
                or png_filter != 0
                or interlace != 0
            ):
                _deny()
            channels = 3 if color == 2 else 4
            exact_inflated = VIEW_HEIGHT * (1 + VIEW_WIDTH * channels)
            if type(exact_inflated) is not int or exact_inflated <= 0:
                _deny()
            idat_compressed_max = min(len(png), exact_inflated + VIEW_HEIGHT * 2)
            stage = 1
        elif tag in _ANCILLARY:
            if stage != 1:
                _deny()
        elif tag == b"IDAT":
            if stage not in (1, 2) or length < 1:
                _deny()
            if idat_len > idat_compressed_max - length:
                _deny()
            idat_parts.append(data)
            idat_len += length
            stage = 2
        elif tag == b"IEND":
            if stage != 2 or length != 0:
                _deny()
            stage = 3
            pos += 12 + length
            break
        else:
            _deny()
        pos += 12 + length
    if stage != 3 or pos != len(png):
        _deny()
    if idat_len <= 0 or idat_len > idat_compressed_max:
        _deny()
    compressed = b"".join(idat_parts)
    if len(compressed) != idat_len or len(compressed) > idat_compressed_max:
        _deny()
    if type(exact_inflated) is not int or exact_inflated <= 0:
        _deny()
    _require_before_stage(clock, original_deadline)
    try:
        inflater = zlib.decompressobj()
        inflated = inflater.decompress(compressed, max_length=exact_inflated)
    except Exception:  # noqa: BLE001
        _deny()
    try:
        eof = inflater.eof
        unconsumed_tail = inflater.unconsumed_tail
        unused_data = inflater.unused_data
    except Exception:  # noqa: BLE001
        inflated = b""
        _deny()
    if unused_data is None or unconsumed_tail is None:
        inflated = b""
        _deny()
    try:
        complete = (
            eof is True
            and len(inflated) == exact_inflated
            and len(unconsumed_tail) == 0
            and len(unused_data) == 0
        )
    except Exception:  # noqa: BLE001
        inflated = b""
        _deny()
    if not complete:
        inflated = b""
        _deny()
    _require_before_stage(clock, original_deadline)
    return _unfilter_to_rgba(inflated, channels)


def encode_canonical_png(
    rgba: bytes, *, original_deadline: float, clock: Callable[[], object]
) -> bytes:
    _require_before_stage(clock, original_deadline)
    if not isinstance(rgba, bytes) or len(rgba) != RGBA_OCTETS:
        _deny()
    row_stride = 1 + VIEW_WIDTH * 4
    raw = bytearray(VIEW_HEIGHT * row_stride)
    for y in range(VIEW_HEIGHT):
        base = y * row_stride
        raw[base] = 0
        src = y * VIEW_WIDTH * 4
        raw[base + 1 : base + row_stride] = rgba[src : src + VIEW_WIDTH * 4]
    _require_before_stage(clock, original_deadline)
    try:
        compressed = zlib.compress(bytes(raw), 9)
    except Exception:  # noqa: BLE001
        _deny()
    if not isinstance(compressed, bytes) or len(compressed) < 1:
        _deny()
    ihdr = struct.pack(">IIBBBBB", VIEW_WIDTH, VIEW_HEIGHT, 8, 6, 0, 0, 0)
    encoded = _SIGNATURE + _chunk(b"IHDR", ihdr) + _chunk(b"IDAT", compressed) + _chunk(b"IEND", b"")
    # Accepted encoder output must fit the same bounded decoder contract.
    if len(encoded) > ENCODED_PNG_MAX:
        _deny()
    return encoded
