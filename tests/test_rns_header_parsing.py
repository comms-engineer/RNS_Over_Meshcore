"""Tests for RNS header parsing methods in MeshCore_Dynamic_Interface.

These methods extract packet type, destination type, RNS tokens, and Link IDs
from raw RNS packets — they are critical for correct routing decisions.
"""

import hashlib
import struct
from unittest.mock import MagicMock, patch

import pytest

from Interface.MeshCore_Dynamic_Interface import MeshCore_Dynamic_Interface


def _make_interface(**overrides):
    """Build a MeshCore_Dynamic_Interface with __init__ bypassed."""
    iface = object.__new__(MeshCore_Dynamic_Interface)
    # Set the class-level constants that the methods reference via self
    for attr in dir(MeshCore_Dynamic_Interface):
        if attr.startswith("_RNS_") or attr in (
            "HEADER_SIZE", "MSG_PREFIX", "BIND_PREFIX", "BIND_REQ_PREFIX",
            "CAPABILITY_ROUTER", "CAPABILITY_EDGE", "DEDUPLICATION_TTL_S",
        ):
            setattr(iface, attr, getattr(MeshCore_Dynamic_Interface, attr))
    iface.name = "test"
    iface.debug_level = "info"
    iface.can_route = overrides.get("can_route", True)
    return iface


# ═══════════════════════════════════════════════════════════════════════════
# _is_broadcast_packet
# ═══════════════════════════════════════════════════════════════════════════

class TestIsBroadcastPacket:

    def setup_method(self):
        self.iface = _make_interface()

    def test_empty_packet_is_broadcast(self):
        assert self.iface._is_broadcast_packet(b"") is True

    def test_announce_packet(self):
        # ptype ANNOUNCE = 0x01, any dest_type
        hdr = bytes([0x01])  # flags byte: ptype=ANNOUNCE
        assert self.iface._is_broadcast_packet(hdr + b"\x00" * 20) is True

    def test_data_plain_is_broadcast(self):
        # ptype=DATA(0x00), dest_type=PLAIN(0x02) → flags = 0x08
        hdr = bytes([0x08])
        assert self.iface._is_broadcast_packet(hdr + b"\x00" * 20) is True

    def test_data_single_is_not_broadcast(self):
        # ptype=DATA(0x00), dest_type=SINGLE(0x00) → flags = 0x00
        hdr = bytes([0x00])
        assert self.iface._is_broadcast_packet(hdr + b"\x00" * 20) is False

    def test_linkrequest_is_not_broadcast(self):
        # ptype=LINKREQUEST(0x02)
        hdr = bytes([0x02])
        assert self.iface._is_broadcast_packet(hdr + b"\x00" * 20) is False

    def test_proof_is_not_broadcast(self):
        # ptype=PROOF(0x03)
        hdr = bytes([0x03])
        assert self.iface._is_broadcast_packet(hdr + b"\x00" * 20) is False

    def test_data_group_not_broadcast(self):
        # ptype=DATA(0x00), dest_type=GROUP(0x01) → flags = 0x04
        hdr = bytes([0x04])
        assert self.iface._is_broadcast_packet(hdr + b"\x00" * 20) is False

    def test_data_link_not_broadcast(self):
        # ptype=DATA(0x00), dest_type=LINK(0x03) → flags = 0x0C
        hdr = bytes([0x0C])
        assert self.iface._is_broadcast_packet(hdr + b"\x00" * 20) is False


# ═══════════════════════════════════════════════════════════════════════════
# _extract_rns_token
# ═══════════════════════════════════════════════════════════════════════════

class TestExtractRnsToken:

    DST_LEN = 16

    def setup_method(self):
        self.iface = _make_interface()

    def test_too_short_returns_none(self):
        assert self.iface._extract_rns_token(b"\x00") is None

    def test_single_header_extracts_destination(self):
        # header_type = 0 (bit 6 clear) → single-header packet
        # Token is data[2:2+DST_LEN]
        flags = 0x00  # bit 6 clear
        hops = 0x00
        dest = bytes(range(16))
        packet = bytes([flags, hops]) + dest + b"\x00" * 10

        token = self.iface._extract_rns_token(packet)
        assert token == dest

    def test_two_byte_header_extracts_second_dest(self):
        # header_type = 1 (bit 6 set) → two-header packet
        # Token is data[2+DST_LEN:2+2*DST_LEN]
        flags = 0x40  # bit 6 set
        hops = 0x00
        dest1 = bytes(range(16))
        dest2 = bytes(range(16, 32))
        packet = bytes([flags, hops]) + dest1 + dest2 + b"\x00" * 10

        token = self.iface._extract_rns_token(packet)
        assert token == dest2

    def test_single_header_too_short_for_dest(self):
        flags = 0x00
        # Only 2 header bytes + 10 bytes, need 18
        packet = bytes([flags, 0x00]) + b"\x00" * 10
        assert self.iface._extract_rns_token(packet) is None

    def test_two_byte_header_too_short_for_second_dest(self):
        flags = 0x40
        # Only 2 header + 16 bytes, need 2+32
        packet = bytes([flags, 0x00]) + b"\x00" * 16
        assert self.iface._extract_rns_token(packet) is None


# ═══════════════════════════════════════════════════════════════════════════
# _link_id_from_lr_packet
# ═══════════════════════════════════════════════════════════════════════════

class TestLinkIdFromLrPacket:

    DST_LEN = 16

    def setup_method(self):
        self.iface = _make_interface()

    def test_too_short_returns_none(self):
        assert self.iface._link_id_from_lr_packet(b"\x00") is None

    def test_single_header_link_id(self):
        # header_type=0, ptype=LINKREQUEST(0x02)
        flags = 0x02
        hops = 0x01
        body = bytes(range(40))
        packet = bytes([flags, hops]) + body

        # Expected: hashable = (flags & 0x0F) + body
        hashable = bytes([flags & 0x0F]) + body
        expected = hashlib.sha256(hashable).digest()[:16]

        result = self.iface._link_id_from_lr_packet(packet)
        assert result == expected

    def test_two_header_link_id(self):
        # header_type=1 (bit 6 set)
        flags = 0x42  # bit 6 set, ptype=LINKREQUEST
        hops = 0x01
        dest1 = bytes(range(16))
        rest = bytes(range(30))
        packet = bytes([flags, hops]) + dest1 + rest

        # hashable = (flags & 0x0F) + data[2+DST_LEN:]
        hashable = bytes([flags & 0x0F]) + rest
        expected = hashlib.sha256(hashable).digest()[:16]

        result = self.iface._link_id_from_lr_packet(packet)
        assert result == expected

    def test_two_header_too_short(self):
        flags = 0x40
        packet = bytes([flags, 0x00]) + b"\x00" * 10  # less than 2+16
        assert self.iface._link_id_from_lr_packet(packet) is None


# ═══════════════════════════════════════════════════════════════════════════
# _own_capability
# ═══════════════════════════════════════════════════════════════════════════

class TestOwnCapability:

    def test_router_capability(self):
        iface = _make_interface(can_route=True)
        assert iface._own_capability() == "R"

    def test_edge_capability(self):
        iface = _make_interface(can_route=False)
        assert iface._own_capability() == "E"
