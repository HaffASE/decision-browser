"""Independent tests for the frozen 1280x720 PNG candidate.

ROOT MECHANICAL REBIND ONLY: MODULE_PATH and MODULE_SHA3_256 may be pointed at
a known candidate overlay or future canonical module bytes. Do not change
assertion semantics when rebinding those two names.

Expected pixels come from this file's fixtures. The candidate's private
unfilter and Paeth are not an oracle. A private unfilter trap only counts
calls after a denial.
"""

from __future__ import annotations

import binascii
import hashlib
import importlib.util
import struct
import sys
import traceback
import zlib
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path

import pytest

# ROOT MECHANICAL REBIND ONLY. Not an assertion surface.
MODULE_PATH = Path(__file__).resolve().parents[2] / "src/decision_browser/owned_fixture_frame.py"
MODULE_SHA3_256 = "29536668347d78b8332992a7228da6a2e0f258d844c7dffd3d8b7f7fc92636db"

WIDTH = 1280
HEIGHT = 720
RGBA_OCTETS = WIDTH * HEIGHT * 4
ENCODED_PNG_MAX = 2_097_152
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
SECRET = "SUPERSECRET_MARKER"
ALLOWED_ANCILLARY = (b"gAMA", b"cHRM", b"sRGB", b"pHYs")
FORBIDDEN_TYPES = (
    b"tEXt",
    b"iTXt",
    b"zTXt",
    b"eXIf",
    b"tIME",
    b"iCCP",
    b"PLTE",
    b"tRNS",
    b"bKGD",
    b"hIST",
    b"sBIT",
    b"acTL",
    b"fcTL",
    b"fdAT",
    b"prIV",
    b"GAMA",
)

_RAW: dict[int, bytes] = {}
_PNG: dict[tuple[int, int], bytes] = {}


def _load_candidate():
    blob = MODULE_PATH.read_bytes()
    digest = hashlib.sha3_256(blob).hexdigest()
    if digest != MODULE_SHA3_256:
        raise RuntimeError("candidate sha3-256 does not match the frozen pin")
    name = "candidate_owned_fixture_frame_under_test"
    spec = importlib.util.spec_from_file_location(name, MODULE_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("candidate loader is missing")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


FRAME = _load_candidate()


class SequenceClock:
    """Return scripted positive instants. Zero is not a legal instant."""

    latest_order: list[str] | None = None

    def __init__(self, values: Sequence[float], order: list[str]) -> None:
        self._values = list(values)
        self._order = order
        self.seen: list[float] = []
        SequenceClock.latest_order = order

    def __call__(self) -> float:
        if not self._values:
            raise RuntimeError("clock exhausted")
        value = self._values.pop(0)
        self.seen.append(value)
        self._order.append("clock")
        return value


class BoomClock:
    def __call__(self) -> float:
        raise RuntimeError(SECRET)


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


def _forward_row(raw: bytes, prior: bytes | None, filt: int, bpp: int) -> bytes:
    out = bytearray(len(raw))
    for index, value in enumerate(raw):
        left = raw[index - bpp] if index >= bpp else 0
        up = prior[index] if prior is not None else 0
        up_left = prior[index - bpp] if prior is not None and index >= bpp else 0
        if filt == 0:
            predictor = 0
        elif filt == 1:
            predictor = left
        elif filt == 2:
            predictor = up
        elif filt == 3:
            predictor = (left + up) // 2
        elif filt == 4:
            predictor = _paeth(left, up, up_left)
        else:
            predictor = 0
        out[index] = (value - predictor) & 255
    return bytes(out)


def _chunk(tag: bytes, data: bytes) -> bytes:
    crc = binascii.crc32(tag + data) & 0xFFFFFFFF
    return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", crc)


def _pattern_byte(x: int, y: int, channel: int) -> int:
    return (17 * channel + (x % 7) * 13 + (y % 5) * 29) & 255


def raw_pattern(channels: int) -> bytes:
    cached = _RAW.get(channels)
    if cached is not None:
        return cached
    kinds: list[bytes] = []
    for y_mod in range(5):
        row = bytearray(WIDTH * channels)
        for x in range(WIDTH):
            start = x * channels
            for channel in range(channels):
                row[start + channel] = _pattern_byte(x, y_mod, channel)
        kinds.append(bytes(row))
    blob = b"".join(kinds[y % 5] for y in range(HEIGHT))
    _RAW[channels] = blob
    return blob


def _set_channel(buf: bytearray, x: int, y: int, channel: int, channels: int, value: int) -> None:
    buf[(y * WIDTH + x) * channels + channel] = value


def crafted_raw(channels: int, cases: Sequence[tuple[int, int, int, int, int]]) -> bytearray:
    buf = bytearray(WIDTH * HEIGHT * channels)
    for x, y, channel, left, up in cases:
        _set_channel(buf, x - 1, y, channel, channels, left)
        _set_channel(buf, x, y - 1, channel, channels, up)
        _set_channel(buf, x, y, channel, channels, 200)
    return buf


def paeth_raw() -> bytes:
    cases = (
        (1, 1, 0, 0, 0, 255),
        (1, 1, 1, 10, 20, 30),
        (1, 1, 2, 20, 10, 30),
        (1, 1, 3, 10, 30, 20),
        (1, 3, 0, 0, 30, 10),
    )
    winners = (0, 10, 10, 20, 30)
    buf = bytearray(WIDTH * HEIGHT * 4)
    for (x, y, channel, left, up, up_left), winner in zip(cases, winners, strict=True):
        assert _paeth(left, up, up_left) == winner
        _set_channel(buf, x - 1, y, channel, 4, left)
        _set_channel(buf, x, y - 1, channel, 4, up)
        _set_channel(buf, x - 1, y - 1, channel, 4, up_left)
        _set_channel(buf, x, y, channel, 4, 200)
    return bytes(buf)


def average_raw(channels: int) -> bytes:
    if channels == 4:
        cases = ((1, 1, 0, 1, 2), (1, 1, 1, 255, 1), (1, 1, 2, 5, 6), (1, 1, 3, 7, 8))
    else:
        cases = ((1, 1, 0, 1, 2), (1, 1, 1, 255, 0), (1, 1, 2, 8, 1))
    if channels == 4:
        assert (1 + 2) // 2 == 1
        assert (255 + 1) // 2 == 128
        assert (5 + 6) // 2 == 5
        assert (7 + 8) // 2 == 7
    else:
        assert (1 + 2) // 2 == 1
        assert (255 + 0) // 2 == 127
        assert (8 + 1) // 2 == 4
    return bytes(crafted_raw(channels, cases))


def to_rgba(raw: bytes, channels: int) -> bytes:
    if channels == 4:
        return raw
    out = bytearray(RGBA_OCTETS)
    src = 0
    dst = 0
    for _pixel in range(WIDTH * HEIGHT):
        out[dst] = raw[src]
        out[dst + 1] = raw[src + 1]
        out[dst + 2] = raw[src + 2]
        out[dst + 3] = 255
        src += 3
        dst += 4
    return bytes(out)


def filter_stream(raw: bytes, channels: int, filt: int) -> bytes:
    stride = WIDTH * channels
    rows: list[bytes] = []
    prior: bytes | None = None
    for y in range(HEIGHT):
        scan = raw[y * stride : (y + 1) * stride]
        rows.append(bytes((filt,)) + _forward_row(scan, prior, filt, channels))
        prior = scan
    return b"".join(rows)


def _ihdr(color: int, width: int = WIDTH, height: int = HEIGHT) -> bytes:
    return struct.pack(">IIBBBBB", width, height, 8, color, 0, 0, 0)


def assemble(chunks: Sequence[tuple[bytes, bytes]], tail: bytes = b"") -> bytes:
    parts = [PNG_SIGNATURE]
    for tag, data in chunks:
        parts.append(_chunk(tag, data))
    parts.append(tail)
    blob = b"".join(parts)
    if len(blob) > ENCODED_PNG_MAX:
        raise AssertionError("fixture exceeds the encoded cap")
    return blob


def chunks_of(png: bytes) -> list[tuple[bytes, bytes]]:
    assert png.startswith(PNG_SIGNATURE)
    pos = 8
    found: list[tuple[bytes, bytes]] = []
    while pos < len(png):
        if pos + 12 > len(png):
            raise AssertionError("fixture ended inside a chunk")
        length = struct.unpack_from(">I", png, pos)[0]
        end = pos + 12 + length
        if end > len(png):
            raise AssertionError("fixture chunk length overruns")
        tag = png[pos + 4 : pos + 8]
        data = png[pos + 8 : pos + 8 + length]
        stored = struct.unpack_from(">I", png, pos + 8 + length)[0]
        expect = binascii.crc32(tag + data) & 0xFFFFFFFF
        if stored != expect:
            raise AssertionError("fixture crc mismatch")
        found.append((tag, bytes(data)))
        pos = end
    return found


def pattern_png(color: int, filt: int) -> bytes:
    key = (color, filt)
    cached = _PNG.get(key)
    if cached is not None:
        return cached
    channels = 4 if color == 6 else 3
    filtered = filter_stream(raw_pattern(channels), channels, filt)
    png = assemble(((b"IHDR", _ihdr(color)), (b"IDAT", zlib.compress(filtered, 9)), (b"IEND", b"")))
    _PNG[key] = png
    return png


def _base_chunks() -> list[tuple[bytes, bytes]]:
    return chunks_of(pattern_png(6, 0))


def _insert(chunks: Sequence[tuple[bytes, bytes]], index: int, extra: Sequence[tuple[bytes, bytes]]) -> bytes:
    body = list(chunks)
    body[index:index] = list(extra)
    return assemble(body)


def _split_idat(chunks: Sequence[tuple[bytes, bytes]], parts: int) -> list[tuple[bytes, bytes]]:
    body = list(chunks)
    index = next(pos for pos, (tag, _data) in enumerate(body) if tag == b"IDAT")
    data = body[index][1]
    if len(data) < parts:
        raise AssertionError("IDAT too small to split")
    step = len(data) // parts
    pieces = [data[item * step : (item + 1) * step] for item in range(parts - 1)]
    pieces.append(data[(parts - 1) * step :])
    replacement = [(b"IDAT", piece) for piece in pieces]
    return body[:index] + replacement + body[index + 1 :]


def known_rgba(color: int) -> bytes:
    channels = 4 if color == 6 else 3
    return to_rgba(raw_pattern(channels), channels)


def decode(png: bytes, deadline: float = 4, clock: object | None = None) -> bytes:
    if clock is None:
        clock = SequenceClock((1, 2, 3), [])
    return FRAME.decode_owned_png(png, original_deadline=deadline, clock=clock)


def encode(rgba: bytes, deadline: float = 4, clock: object | None = None) -> bytes:
    if clock is None:
        clock = SequenceClock((1, 2), [])
    return FRAME.encode_canonical_png(rgba, original_deadline=deadline, clock=clock)


def expect_denied(call: Callable[[], object]) -> None:
    with pytest.raises(FRAME.PngDenied) as caught:
        call()
    err = caught.value
    rendered = "".join(traceback.format_exception(err))
    assert type(err) is FRAME.PngDenied
    assert err.args == ("denied",)
    assert str(err) == "denied"
    assert err.__cause__ is None
    assert err.__suppress_context__ is True
    assert SECRET not in rendered
    assert SECRET.encode() not in rendered.encode()


@contextmanager
def observe(mode: str | None = None) -> Iterator[dict[str, object]]:
    """Patch the shared zlib module, delegate to the real objects, then restore."""
    zlib_mod = FRAME.zlib
    real_decompressobj = zlib_mod.decompressobj
    real_decompress = zlib_mod.decompress
    real_compress = zlib_mod.compress
    real_unfilter = FRAME._unfilter_to_rgba
    state: dict[str, object] = {
        "order": [],
        "construct": [],
        "max_lengths": [],
        "flush": 0,
        "bare_decompress": 0,
        "compress_levels": [],
        "unfilter": 0,
    }
    order = state["order"]
    assert isinstance(order, list)

    def _note(label: str) -> None:
        order.append(label)
        shared = SequenceClock.latest_order
        if shared is not None and shared is not order:
            shared.append(label)

    class _Inflater:
        def __init__(self) -> None:
            self._raw = real_decompressobj()
            self._eof = False
            self._unconsumed = b""
            self._unused = b""

        def decompress(self, data: bytes, max_length: int = 0) -> bytes:
            lengths = state["max_lengths"]
            assert isinstance(lengths, list)
            lengths.append(max_length)
            out = self._raw.decompress(data, max_length)
            self._eof = self._raw.eof
            self._unconsumed = self._raw.unconsumed_tail
            self._unused = self._raw.unused_data
            if mode == "eof_false":
                self._eof = False
            elif mode == "tail":
                self._unconsumed = b"\x01"
            elif mode == "unused":
                self._unused = b"\x02"
            elif mode == "unused_none":
                self._unused = None
            elif mode == "tail_none":
                self._unconsumed = None
            elif mode == "short":
                out = out[:-1] if out else b""
            return out

        @property
        def eof(self) -> bool:
            return self._eof

        @property
        def unconsumed_tail(self) -> bytes | None:
            return self._unconsumed

        @property
        def unused_data(self) -> bytes | None:
            if mode == "unused_missing":
                raise AttributeError("unused_data")
            return self._unused

        def flush(self, *args: object, **kwargs: object) -> bytes:
            state["flush"] = int(state["flush"]) + 1
            raise RuntimeError("flush is not a decode path")

    def decompressobj(*args: object, **kwargs: object) -> _Inflater:
        _note("inflate")
        constructed = state["construct"]
        assert isinstance(constructed, list)
        constructed.append((args, kwargs))
        if mode == "ctor_raise":
            raise RuntimeError(SECRET)
        return _Inflater()

    def decompress(*_args: object, **_kwargs: object) -> bytes:
        state["bare_decompress"] = int(state["bare_decompress"]) + 1
        raise RuntimeError("zlib.decompress is not the capped path")

    def compress(data: bytes, level: int = -1) -> bytes:
        _note("compress")
        levels = state["compress_levels"]
        assert isinstance(levels, list)
        levels.append(level)
        if mode == "compress_raise":
            raise RuntimeError(SECRET)
        return real_compress(data, level)

    def unfilter(inflated: bytes, channels: int) -> bytes:
        _note("unfilter")
        state["unfilter"] = int(state["unfilter"]) + 1
        return real_unfilter(inflated, channels)

    try:
        zlib_mod.decompressobj = decompressobj
        zlib_mod.decompress = decompress
        zlib_mod.compress = compress
        FRAME._unfilter_to_rgba = unfilter
        yield state
    finally:
        zlib_mod.decompressobj = real_decompressobj
        zlib_mod.decompress = real_decompress
        zlib_mod.compress = real_compress
        FRAME._unfilter_to_rgba = real_unfilter


def _assert_idle_inflate(state: dict[str, object]) -> None:
    assert state["construct"] == []
    assert state["max_lengths"] == []
    assert state["flush"] == 0
    assert state["bare_decompress"] == 0
    assert state["unfilter"] == 0
    order = state["order"]
    assert isinstance(order, list)
    assert "inflate" not in order
    assert "unfilter" not in order


def _assert_one_cap(state: dict[str, object], channels: int, *, unfilter: int) -> None:
    exact = HEIGHT * (1 + WIDTH * channels)
    assert exact > 0
    assert state["construct"] == [((), {})]
    assert state["max_lengths"] == [exact]
    assert state["flush"] == 0
    assert state["bare_decompress"] == 0
    assert state["unfilter"] == unfilter


def test_public_bounds_match_fixed_viewport() -> None:
    assert FRAME.VIEW_WIDTH == WIDTH
    assert FRAME.VIEW_HEIGHT == HEIGHT
    assert FRAME.RGBA_OCTETS == RGBA_OCTETS
    assert FRAME.ENCODED_PNG_MAX == ENCODED_PNG_MAX
    assert FRAME.zlib is zlib


def test_pattern_literals_are_known_without_the_decoder() -> None:
    rgba = known_rgba(6)
    assert rgba[0:4] == bytes((0, 17, 34, 51))
    assert rgba[4:8] == bytes((13, 30, 47, 64))
    assert rgba[WIDTH * 4 : WIDTH * 4 + 4] == bytes((29, 46, 63, 80))
    last = rgba[-4:]
    assert last == bytes((181, 198, 215, 232))
    opaque = known_rgba(2)
    assert opaque[0:4] == bytes((0, 17, 34, 255))
    assert opaque[-4:] == bytes((181, 198, 215, 255))


@pytest.mark.parametrize("filt", (0, 1, 2, 3, 4))
def test_color_type_6_filter_reconstructs_known_rgba(filt: int) -> None:
    expected = known_rgba(6)
    decoded = decode(pattern_png(6, filt))
    assert decoded == expected


@pytest.mark.parametrize("filt", (0, 1, 2, 3, 4))
def test_color_type_2_filter_reconstructs_known_rgba(filt: int) -> None:
    expected = known_rgba(2)
    decoded = decode(pattern_png(2, filt))
    assert decoded == expected
    assert decoded[3::4] == bytes((255,)) * (WIDTH * HEIGHT)


def test_paeth_ties_and_average_floor_round_trip_known_bytes() -> None:
    paeth = paeth_raw()
    streamed = filter_stream(paeth, 4, 4)
    decoded = decode(assemble(((b"IHDR", _ihdr(6)), (b"IDAT", zlib.compress(streamed, 9)), (b"IEND", b""))))
    assert decoded == paeth
    assert decoded[(WIDTH + 1) * 4 : (WIDTH + 1) * 4 + 4] == bytes((200, 200, 200, 200))

    for channels, color in ((4, 6), (3, 2)):
        raw = average_raw(channels)
        filtered = filter_stream(raw, channels, 3)
        png = assemble(((b"IHDR", _ihdr(color)), (b"IDAT", zlib.compress(filtered, 9)), (b"IEND", b"")))
        assert decode(png) == to_rgba(raw, channels)


def test_allowed_ancillary_is_skipped_and_split_idat_decodes() -> None:
    ancillary = (
        (b"gAMA", b"\x00\x00\xb1\x8f"),
        (b"cHRM", b"\x00" * 32),
        (b"sRGB", b"\x00"),
        (b"pHYs", struct.pack(">IIB", 2835, 2835, 1)),
    )
    png = _insert(_split_idat(_base_chunks(), 2), 1, ancillary)
    expected = known_rgba(6)
    assert decode(png) == expected
    canonical = encode(expected)
    assert [tag for tag, _data in chunks_of(canonical)] == [b"IHDR", b"IDAT", b"IEND"]


def test_canonical_encode_is_color_6_filter_none_level_9_and_round_trips() -> None:
    rgba = known_rgba(2)
    order: list[str] = []
    clock = SequenceClock((1, 2), order)
    raw = bytearray(HEIGHT * (1 + WIDTH * 4))
    stride = 1 + WIDTH * 4
    for y in range(HEIGHT):
        base = y * stride
        src = y * WIDTH * 4
        raw[base + 1 : base + stride] = rgba[src : src + WIDTH * 4]
    expected_idat = zlib.compress(bytes(raw), 9)
    with observe() as state:
        encoded = FRAME.encode_canonical_png(rgba, original_deadline=4, clock=clock)
    assert order == ["clock", "clock", "compress"]
    assert state["compress_levels"] == [9]
    assert state["construct"] == []
    assert state["bare_decompress"] == 0
    assert clock.seen == [1, 2]
    tags = chunks_of(encoded)
    assert [tag for tag, _data in tags] == [b"IHDR", b"IDAT", b"IEND"]
    assert tags[0][1] == struct.pack(">IIBBBBB", WIDTH, HEIGHT, 8, 6, 0, 0, 0)
    assert tags[1][1] == expected_idat
    assert tags[2][1] == b""
    assert encoded.startswith(PNG_SIGNATURE)
    exact = HEIGHT * (1 + WIDTH * 4)
    inflater = zlib.decompressobj()
    inflated = inflater.decompress(tags[1][1], max_length=exact)
    assert inflater.eof is True
    assert inflater.unconsumed_tail == b""
    assert inflater.unused_data == b""
    assert inflated == bytes(raw)
    assert decode(encoded) == rgba
    assert encode(rgba) == encoded


def test_one_positive_cap_and_call_order_for_both_color_types() -> None:
    for color, channels, png in (
        (6, 4, pattern_png(6, 0)),
        (2, 3, pattern_png(2, 4)),
    ):
        order: list[str] = []
        clock = SequenceClock((0.25, 0.5, 0.75), order)
        with observe() as state:
            decoded = FRAME.decode_owned_png(png, original_deadline=1.0, clock=clock)
        assert decoded == known_rgba(color)
        assert order == ["clock", "clock", "inflate", "clock", "unfilter"]
        assert clock.seen == [0.25, 0.5, 0.75]
        _assert_one_cap(state, channels, unfilter=1)


@pytest.mark.parametrize(
    "mode",
    ("eof_false", "short", "tail", "unused", "unused_none", "tail_none", "unused_missing"),
)
def test_injected_incomplete_stream_denies_before_unfilter(mode: str) -> None:
    png = pattern_png(6, 1)
    order: list[str] = []
    clock = SequenceClock((1, 2, 3), order)
    with observe(mode) as state:
        expect_denied(lambda: FRAME.decode_owned_png(png, original_deadline=4, clock=clock))
    _assert_one_cap(state, 4, unfilter=0)
    assert order == ["clock", "clock", "inflate"]
    assert "unfilter" not in order


def test_real_short_and_trailing_deflate_deny_before_unfilter() -> None:
    filtered = filter_stream(raw_pattern(4), 4, 0)
    full = zlib.compress(filtered, 9)
    short = zlib.compress(filtered[:100], 9)
    cases = (
        full[:-1],
        full + b"TQ",
        b"not-a-zlib-stream",
    )
    base = _base_chunks()
    for payload in (short, *cases):
        png = assemble((base[0], (b"IDAT", payload), base[2]))
        order: list[str] = []
        with observe() as state:
            expect_denied(
                lambda png=png, order=order: FRAME.decode_owned_png(
                    png,
                    original_deadline=4,
                    clock=SequenceClock((1, 2, 3), order),
                )
            )
        _assert_one_cap(state, 4, unfilter=0)


def test_deadline_blocks_inflate_unfilter_and_compress() -> None:
    png = pattern_png(6, 2)
    rgba = known_rgba(6)
    blocked = (
        ((4,), 4, png, "decode"),
        ((1, 4), 4, png, "decode"),
        ((1, 2, 4), 4, png, "decode"),
        ((4,), 4, rgba, "encode"),
        ((1, 4), 4, rgba, "encode"),
    )
    for values, deadline, payload, kind in blocked:
        order: list[str] = []
        clock = SequenceClock(values, order)
        with observe() as state:
            if kind == "decode":
                expect_denied(
                    lambda payload=payload, clock=clock, deadline=deadline: FRAME.decode_owned_png(
                        payload,
                        original_deadline=deadline,
                        clock=clock,
                    )
                )
            else:
                expect_denied(
                    lambda payload=payload, clock=clock, deadline=deadline: FRAME.encode_canonical_png(
                        payload,
                        original_deadline=deadline,
                        clock=clock,
                    )
                )
        assert state["compress_levels"] == []
        assert "compress" not in order
        assert "unfilter" not in order
        if values[-1] == deadline and len(values) == 3:
            assert order == ["clock", "clock", "inflate", "clock"]
            _assert_one_cap(state, 4, unfilter=0)
        else:
            _assert_idle_inflate(state)


def test_nonpositive_clock_and_bad_deadline_do_not_start_work() -> None:
    png = pattern_png(6, 0)
    rgba = known_rgba(6)
    for deadline in (0, -1, float("nan"), float("inf"), True, False, None, "4"):
        bomb = SequenceClock((1, 2, 3), [])

        def _decode(deadline: object = deadline, bomb: SequenceClock = bomb) -> bytes:
            return FRAME.decode_owned_png(png, original_deadline=deadline, clock=bomb)

        def _encode(deadline: object = deadline, bomb: SequenceClock = bomb) -> bytes:
            return FRAME.encode_canonical_png(rgba, original_deadline=deadline, clock=bomb)

        with observe() as state:
            expect_denied(_decode)
            expect_denied(_encode)
        _assert_idle_inflate(state)
        assert bomb.seen == []

    for value in (0, -0.1, float("nan"), float("inf"), True, False):
        order: list[str] = []
        clock = SequenceClock((value, value), order)
        with observe() as state:
            expect_denied(lambda clock=clock: FRAME.decode_owned_png(png, original_deadline=4, clock=clock))
            expect_denied(lambda clock=clock: FRAME.encode_canonical_png(rgba, original_deadline=4, clock=clock))
        _assert_idle_inflate(state)
        assert len(clock.seen) == 2
        assert clock.seen[0] is value
        assert clock.seen[1] is value
        assert order == ["clock", "clock"]
        assert "inflate" not in order
        assert "compress" not in order


def test_clock_and_compress_failures_keep_the_generic_denial() -> None:
    png = pattern_png(6, 0)
    rgba = known_rgba(6)
    with observe("ctor_raise") as state:
        expect_denied(lambda: decode(png))
    assert state["construct"] == [((), {})]
    assert state["max_lengths"] == []
    assert state["flush"] == 0
    assert state["bare_decompress"] == 0
    assert state["unfilter"] == 0
    ctor_order = state["order"]
    assert isinstance(ctor_order, list)
    assert ctor_order == ["inflate"]
    assert "unfilter" not in ctor_order
    with observe("compress_raise") as state:
        expect_denied(lambda: encode(rgba))
    assert state["compress_levels"] == [9]
    assert state["unfilter"] == 0
    expect_denied(lambda: decode(png, clock=BoomClock()))
    expect_denied(lambda: FRAME.decode_owned_png(png, original_deadline=4, clock=object()))
    expect_denied(lambda: decode(SECRET.encode()))


def _illegal_ihdr_images() -> list[tuple[str, bytes]]:
    images: list[tuple[str, bytes]] = []
    dims = (
        (0, 0),
        (WIDTH, HEIGHT - 1),
        (WIDTH - 1, HEIGHT),
        (WIDTH, HEIGHT + 1),
        (2**31, HEIGHT),
    )
    for width, height in dims:
        data = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
        images.append((f"dim-{width}x{height}", assemble(((b"IHDR", data), _base_chunks()[1], (b"IEND", b"")))))
    variants = (
        ("depth", struct.pack(">IIBBBBB", WIDTH, HEIGHT, 16, 6, 0, 0, 0)),
        ("color0", struct.pack(">IIBBBBB", WIDTH, HEIGHT, 8, 0, 0, 0, 0)),
        ("color3", struct.pack(">IIBBBBB", WIDTH, HEIGHT, 8, 3, 0, 0, 0)),
        ("color4", struct.pack(">IIBBBBB", WIDTH, HEIGHT, 8, 4, 0, 0, 0)),
        ("compression", struct.pack(">IIBBBBB", WIDTH, HEIGHT, 8, 6, 1, 0, 0)),
        ("filter-method", struct.pack(">IIBBBBB", WIDTH, HEIGHT, 8, 6, 0, 1, 0)),
        ("interlace", struct.pack(">IIBBBBB", WIDTH, HEIGHT, 8, 6, 0, 0, 1)),
    )
    idat = _base_chunks()[1]
    for name, data in variants:
        images.append((name, assemble(((b"IHDR", data), idat, (b"IEND", b"")))))
    images.append(("short-ihdr", assemble(((b"IHDR", b"\x00" * 12), idat, (b"IEND", b"")))))
    return images


def _structure_images() -> list[tuple[str, bytes]]:
    base = _base_chunks()
    ihdr, idat, iend = base
    valid = pattern_png(6, 0)
    flipped = bytearray(valid)
    flipped[16] ^= 0x01
    between = _split_idat(base, 2)
    idat_index = next(pos for pos, (tag, _data) in enumerate(between) if tag == b"IDAT")
    gapped = between[: idat_index + 1] + [(b"gAMA", b"\x00\x00\xb1\x8f")] + between[idat_index + 1 :]
    images = [
        ("empty", b""),
        ("short-signature", PNG_SIGNATURE[:-1]),
        ("bad-signature", b"\x89PNG\r\n\x1a\x00" + valid[8:]),
        ("truncated", valid[:20]),
        ("huge-length", PNG_SIGNATURE + struct.pack(">I", 0xFFFFFFFF) + b"IHDR" + b"\x00" * 8),
        ("bad-crc", bytes(flipped)),
        ("idat-first", assemble((idat, ihdr, iend))),
        ("ancillary-first", _insert(base, 0, ((b"gAMA", b"\x00\x00\xb1\x8f"),))),
        ("no-iend", assemble((ihdr, idat))),
        ("iend-data", assemble((ihdr, idat, (b"IEND", b"\x00")))),
        ("trailing", valid + b"\x00"),
        ("inserted-nul", valid[:8] + b"\x00" + valid[8:]),
        ("empty-idat", assemble((ihdr, (b"IDAT", b""), iend))),
        ("ancillary-after-idat", assemble((ihdr, idat, (b"pHYs", b"\x00" * 9), iend))),
        ("idat-gap", assemble(gapped)),
        ("non-alpha-type", _insert(base, 1, ((b"IHD1", b"\x00"),))),
        ("oversize", PNG_SIGNATURE + b"\x00" * (ENCODED_PNG_MAX - 7)),
    ]
    for tag in FORBIDDEN_TYPES:
        payload = SECRET.encode() if tag == b"tEXt" else b"\x01"
        images.append((tag.decode("ascii", "replace"), _insert(base, 1, ((tag, payload),))))
    return images


def test_invalid_scanline_filter_denies_after_one_capped_inflate() -> None:
    ihdr, _idat, iend = _base_chunks()
    row = 10 * (1 + WIDTH * 4)
    base_filtered = filter_stream(raw_pattern(4), 4, 0)
    for name, filter_byte in (("filter-5", 5), ("filter-255", 255)):
        filtered = bytearray(base_filtered)
        filtered[row] = filter_byte
        png = assemble((ihdr, (b"IDAT", zlib.compress(bytes(filtered), 9)), iend))
        order: list[str] = []
        clock = SequenceClock((1, 2, 3), order)
        with observe() as state:
            expect_denied(
                lambda png=png, clock=clock: FRAME.decode_owned_png(
                    png,
                    original_deadline=4,
                    clock=clock,
                )
            )
        assert order == ["clock", "clock", "inflate", "clock", "unfilter"], name
        assert clock.seen == [1, 2, 3]
        _assert_one_cap(state, 4, unfilter=1)
        assert state["max_lengths"] == [3_687_120]


def test_malformed_inputs_deny_before_inflate() -> None:
    payloads: list[tuple[str, object]] = [(name, blob) for name, blob in _structure_images()]
    payloads.extend(_illegal_ihdr_images())
    payloads.extend(
        (
            ("none", None),
            ("text", SECRET),
            ("bytearray", bytearray(PNG_SIGNATURE)),
            ("memoryview", memoryview(pattern_png(6, 0))),
        )
    )
    oversize = next(blob for name, blob in payloads if name == "oversize")
    assert isinstance(oversize, bytes)
    assert len(oversize) == ENCODED_PNG_MAX + 1
    for name, payload in payloads:
        order: list[str] = []
        with observe() as state:
            expect_denied(
                lambda payload=payload, order=order: FRAME.decode_owned_png(
                    payload,
                    original_deadline=4,
                    clock=SequenceClock((1, 2, 3), order),
                )
            )
        _assert_idle_inflate(state)
        assert "inflate" not in order, name


def test_encode_rejects_bad_buffers_before_compress() -> None:
    good = known_rgba(6)
    rejected = (
        b"",
        good[:-1],
        good + b"\x00",
        bytearray(good),
        SECRET,
        None,
        memoryview(good),
    )
    for payload in rejected:
        order: list[str] = []
        with observe() as state:
            expect_denied(
                lambda payload=payload, order=order: FRAME.encode_canonical_png(
                    payload,
                    original_deadline=4,
                    clock=SequenceClock((1, 2), order),
                )
            )
        assert state["compress_levels"] == []
        assert state["construct"] == []
        assert "compress" not in order


def test_integer_deadline_decodes_and_spy_restores_shared_zlib() -> None:
    original_decompressobj = zlib.decompressobj
    original_compress = zlib.compress
    png = pattern_png(2, 3)
    order: list[str] = []
    with observe() as state:
        assert zlib.decompressobj is not original_decompressobj
        decoded = FRAME.decode_owned_png(png, original_deadline=4, clock=SequenceClock((1, 2, 3), order))
        assert decoded == known_rgba(2)
        _assert_one_cap(state, 3, unfilter=1)
    assert zlib.decompressobj is original_decompressobj
    assert zlib.compress is original_compress
    assert FRAME._unfilter_to_rgba.__name__ == "_unfilter_to_rgba"
