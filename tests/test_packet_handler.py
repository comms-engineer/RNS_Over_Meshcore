"""Tests for _PacketHandler fragmentation in both Dynamic and Channel interfaces."""

import base64
import struct

import pytest

from Interface.MeshCore_Dynamic_Interface import _PacketHandler as DynPacketHandler
from Interface.MeshCore_Channel_Interface import _PacketHandler as ChanPacketHandler


# ═══════════════════════════════════════════════════════════════════════════
# Dynamic Interface _PacketHandler
# ═══════════════════════════════════════════════════════════════════════════

class TestDynPacketHandler:
    """Tests for _PacketHandler in MeshCore_Dynamic_Interface."""

    def test_single_fragment_small_payload(self):
        data = b"\x01\x02\x03\x04"
        pkt_id = 42
        handler = DynPacketHandler(data, pkt_id)

        assert len(handler) == 1
        frag = handler.fragments[0]
        assert frag.startswith("RNS:")

        b64_part = frag[len("RNS:"):]
        b64_part += "=" * (-len(b64_part) % 4)
        raw = base64.urlsafe_b64decode(b64_part)

        # Header: 1B idx + 4B pkt_id + 1B total = 6 bytes
        assert len(raw) == 6 + len(data)
        frag_idx, decoded_pkt_id, frag_total = struct.unpack(">BIB", raw[:6])
        assert frag_idx == 0
        assert decoded_pkt_id == 42
        assert frag_total == 1
        assert raw[6:] == data

    def test_multiple_fragments(self):
        # Default payload_size is 64; create data that needs 3 fragments
        data = bytes(range(150))
        handler = DynPacketHandler(data, 7)

        assert len(handler) == 3
        for i, frag in enumerate(handler.fragments):
            assert frag.startswith("RNS:")
            b64 = frag[4:]
            b64 += "=" * (-len(b64) % 4)
            raw = base64.urlsafe_b64decode(b64)
            idx, pkt_id, total = struct.unpack(">BIB", raw[:6])
            assert idx == i
            assert pkt_id == 7
            assert total == 3

    def test_reassembly_round_trip(self):
        original = b"Hello MeshCore world! " * 10  # 220 bytes
        pkt_id = 99
        handler = DynPacketHandler(original, pkt_id)

        reassembled = b""
        for frag in handler.fragments:
            b64 = frag[4:]
            b64 += "=" * (-len(b64) % 4)
            raw = base64.urlsafe_b64decode(b64)
            reassembled += raw[6:]  # strip header

        assert reassembled == original

    def test_custom_payload_size(self):
        data = bytes(range(100))
        handler = DynPacketHandler(data, 1, payload_size=25)
        assert len(handler) == 4  # ceil(100 / 25)

    def test_zero_payload_size_uses_default(self):
        data = bytes(range(64))
        handler = DynPacketHandler(data, 1, payload_size=0)
        # payload_size=0 falls back to default of 64
        assert len(handler) == 1

    def test_pkt_id_wraps_at_32bit(self):
        handler = DynPacketHandler(b"\x00", 0xFFFFFFFF)
        b64 = handler.fragments[0][4:]
        b64 += "=" * (-len(b64) % 4)
        raw = base64.urlsafe_b64decode(b64)
        _, pkt_id, _ = struct.unpack(">BIB", raw[:6])
        assert pkt_id == 0xFFFFFFFF

    def test_empty_data(self):
        handler = DynPacketHandler(b"", 0)
        # empty data → no chunks → no fragments
        assert len(handler) == 0

    def test_exact_payload_boundary(self):
        data = bytes(64)  # exactly one chunk at default size
        handler = DynPacketHandler(data, 5)
        assert len(handler) == 1

    def test_one_byte_over_boundary(self):
        data = bytes(65)  # one byte over default payload_size
        handler = DynPacketHandler(data, 5)
        assert len(handler) == 2

    def test_fragment_index_capped_at_255(self):
        # frag_idx is masked to 0xFF; with payload_size=1, 260 bytes → 260 frags
        data = bytes(260)
        handler = DynPacketHandler(data, 0, payload_size=1)
        assert len(handler) == 260

        b64 = handler.fragments[255][4:]
        b64 += "=" * (-len(b64) % 4)
        raw = base64.urlsafe_b64decode(b64)
        idx = raw[0]
        assert idx == 255

        # Fragment 256 wraps to 0
        b64 = handler.fragments[256][4:]
        b64 += "=" * (-len(b64) % 4)
        raw = base64.urlsafe_b64decode(b64)
        idx = raw[0]
        assert idx == 0


# ═══════════════════════════════════════════════════════════════════════════
# Channel Interface _PacketHandler
# ═══════════════════════════════════════════════════════════════════════════

class TestChanPacketHandler:
    """Tests for _PacketHandler in MeshCore_Channel_Interface."""

    def test_single_fragment(self):
        src_id = b"\xAA\xBB\xCC\xDD"
        data = b"test payload"
        handler = ChanPacketHandler(data, src_id, pkt_id=10)

        assert len(handler) == 1
        frag = handler.fragments[0]
        assert frag.startswith("RNS:")

        b64 = frag[4:]
        b64 += "=" * (-len(b64) % 4)
        raw = base64.urlsafe_b64decode(b64)

        # Header: magic(2) + idx(1) + src_id(4) + pkt_id(1) + total(1) = 9 bytes
        assert raw[:2] == b"RN"  # magic
        assert raw[2] == 0       # frag_idx
        assert raw[3:7] == src_id
        assert raw[7] == 10      # pkt_id
        assert raw[8] == 1       # frag_total
        assert raw[9:] == data

    def test_multi_fragment_round_trip(self):
        src_id = b"\x01\x02\x03\x04"
        original = bytes(range(200))
        handler = ChanPacketHandler(original, src_id, pkt_id=55)

        assert len(handler) > 1

        reassembled = b""
        for frag in handler.fragments:
            b64 = frag[4:]
            b64 += "=" * (-len(b64) % 4)
            raw = base64.urlsafe_b64decode(b64)
            # Verify magic
            assert raw[:2] == b"RN"
            # Verify src_id
            assert raw[3:7] == src_id
            reassembled += raw[9:]  # strip 9-byte header

        assert reassembled == original

    def test_pkt_id_wraps_at_byte(self):
        handler = ChanPacketHandler(b"\x00", b"\x00\x00\x00\x00", pkt_id=255)
        b64 = handler.fragments[0][4:]
        b64 += "=" * (-len(b64) % 4)
        raw = base64.urlsafe_b64decode(b64)
        assert raw[7] == 255

    def test_fragment_count_matches_data_size(self):
        # 128 bytes at default 64-byte payload → 2 fragments
        handler = ChanPacketHandler(bytes(128), b"\x00" * 4, pkt_id=0)
        assert len(handler) == 2

    def test_all_fragments_have_consistent_total(self):
        handler = ChanPacketHandler(bytes(200), b"\xAB" * 4, pkt_id=3)
        expected_total = len(handler)
        for frag in handler.fragments:
            b64 = frag[4:]
            b64 += "=" * (-len(b64) % 4)
            raw = base64.urlsafe_b64decode(b64)
            assert raw[8] == expected_total
