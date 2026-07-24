"""Tests for MeshCore_Channel_Interface tunnel pipeline, deduplication,
fragment reassembly, and outgoing path.
"""

import asyncio
import base64
import hashlib
import queue
import socket
import threading
import time
from collections import OrderedDict
from unittest.mock import MagicMock

import pytest

from Interface.MeshCore_Channel_Interface import (
    MeshCore_Channel_Interface,
    _PacketHandler,
)


def _make_interface(**overrides):
    """Build a MeshCore_Channel_Interface with __init__ bypassed."""
    iface = object.__new__(MeshCore_Channel_Interface)

    # Copy class-level constants
    for attr in ("MAGIC", "HEADER_SIZE", "MSG_PREFIX", "OUTQUEUE_MAXSIZE",
                 "WORKER_POLL_S", "SETUP_TIMEOUT_S"):
        setattr(iface, attr, getattr(MeshCore_Channel_Interface, attr))

    iface.owner = MagicMock()
    iface.name = "test-chan"
    iface.online = True
    iface.detached = False
    iface.txb = 0
    iface.rxb = 0
    iface.debug_level = overrides.get("debug_level", "info")
    iface.channel_secret_hex = overrides.get(
        "channel_secret_hex", "c4d2b6c8254e3b11200f57e95dcb1197"
    )
    iface.fragment_timeout_s = 3600
    iface.rate_limit_bps = 0
    iface.fragment_delay_s = 0.0
    iface.channel_idx = 39

    # Derive src_id same way as production
    raw = f"{iface.channel_secret_hex}:{socket.gethostname()}"
    iface._own_src_id = hashlib.sha256(raw.encode()).digest()[:4]

    iface._pkt_id = 0
    iface._pkt_id_lock = threading.Lock()
    iface._outqueue = queue.Queue(maxsize=512)

    iface._assembly = {}
    iface._assembly_meta = {}
    iface._asm_lock = threading.Lock()

    iface._seen_pkts = OrderedDict()
    iface._seen_lock = threading.Lock()

    return iface


def _make_channel_fragment(data, src_id, pkt_id, frag_idx=0, frag_total=1):
    """Build a channel-format fragment string (9-byte header)."""
    magic = b"RN"
    header = (
        magic
        + bytes([frag_idx & 0xFF])
        + src_id
        + bytes([pkt_id & 0xFF, frag_total & 0xFF])
    )
    encoded = base64.urlsafe_b64encode(header + data).rstrip(b"=").decode()
    return "RNS:" + encoded


# ═══════════════════════════════════════════════════════════════════════════
# _derive_local_src_id
# ═══════════════════════════════════════════════════════════════════════════

class TestDeriveLocalSrcId:

    def test_deterministic(self):
        iface = _make_interface()
        expected = hashlib.sha256(
            f"{iface.channel_secret_hex}:{socket.gethostname()}".encode()
        ).digest()[:4]
        assert iface._own_src_id == expected

    def test_different_secret_different_id(self):
        iface1 = _make_interface(channel_secret_hex="aaaa" * 8)
        iface2 = _make_interface(channel_secret_hex="bbbb" * 8)
        assert iface1._own_src_id != iface2._own_src_id


# ═══════════════════════════════════════════════════════════════════════════
# _process_tunnel_text
# ═══════════════════════════════════════════════════════════════════════════

class TestChannelProcessTunnelText:

    def _run(self, coro):
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    def test_single_fragment_delivery(self):
        iface = _make_interface()
        remote_src = b"\xDE\xAD\xBE\xEF"
        data = b"\x01\x02\x03\x04\x05"
        text = _make_channel_fragment(data, remote_src, pkt_id=1)

        self._run(iface._process_tunnel_text(text))
        iface.owner.inbound.assert_called_once()
        assert iface.owner.inbound.call_args[0][0] == data

    def test_multi_fragment_reassembly(self):
        iface = _make_interface()
        remote_src = b"\x01\x02\x03\x04"
        original = bytes(range(100))
        chunk1 = original[:50]
        chunk2 = original[50:]

        frag1 = _make_channel_fragment(chunk1, remote_src, pkt_id=5, frag_idx=0, frag_total=2)
        frag2 = _make_channel_fragment(chunk2, remote_src, pkt_id=5, frag_idx=1, frag_total=2)

        self._run(iface._process_tunnel_text(frag1))
        iface.owner.inbound.assert_not_called()

        self._run(iface._process_tunnel_text(frag2))
        iface.owner.inbound.assert_called_once()
        assert iface.owner.inbound.call_args[0][0] == original

    def test_own_echo_dropped(self):
        iface = _make_interface()
        data = b"\x01\x02"
        text = _make_channel_fragment(data, iface._own_src_id, pkt_id=1)

        self._run(iface._process_tunnel_text(text))
        iface.owner.inbound.assert_not_called()

    def test_bad_magic_dropped(self):
        iface = _make_interface()
        # Use wrong magic bytes
        header = b"XX" + bytes([0, 0xDE, 0xAD, 0xBE, 0xEF, 1, 1])
        encoded = base64.urlsafe_b64encode(header + b"\x01").rstrip(b"=").decode()
        text = "RNS:" + encoded

        self._run(iface._process_tunnel_text(text))
        iface.owner.inbound.assert_not_called()

    def test_duplicate_packet_deduplicated(self):
        iface = _make_interface()
        remote_src = b"\xAA\xBB\xCC\xDD"
        data = b"\x01"
        text = _make_channel_fragment(data, remote_src, pkt_id=10)

        self._run(iface._process_tunnel_text(text))
        assert iface.owner.inbound.call_count == 1

        self._run(iface._process_tunnel_text(text))
        assert iface.owner.inbound.call_count == 1

    def test_sender_prefix_stripped(self):
        iface = _make_interface()
        remote_src = b"\x11\x22\x33\x44"
        data = b"\x05\x06\x07"
        base_frag = _make_channel_fragment(data, remote_src, pkt_id=2)
        # Simulate firmware-prepended sender name
        text = "NodeName: " + base_frag

        self._run(iface._process_tunnel_text(text))
        iface.owner.inbound.assert_called_once()
        assert iface.owner.inbound.call_args[0][0] == data

    def test_no_rns_prefix_silently_dropped(self):
        iface = _make_interface()
        self._run(iface._process_tunnel_text("Hello, this is not RNS"))
        iface.owner.inbound.assert_not_called()

    def test_invalid_base64_dropped(self):
        iface = _make_interface()
        self._run(iface._process_tunnel_text("RNS:not_valid_base64!!!"))
        iface.owner.inbound.assert_not_called()

    def test_frame_too_short_dropped(self):
        iface = _make_interface()
        short = base64.urlsafe_b64encode(b"\x00\x01").rstrip(b"=").decode()
        self._run(iface._process_tunnel_text("RNS:" + short))
        iface.owner.inbound.assert_not_called()

    def test_zero_frag_total_dropped(self):
        iface = _make_interface()
        header = b"RN" + bytes([0, 0xAA, 0xBB, 0xCC, 0xDD, 1, 0])  # total=0
        encoded = base64.urlsafe_b64encode(header + b"\x01").rstrip(b"=").decode()
        self._run(iface._process_tunnel_text("RNS:" + encoded))
        iface.owner.inbound.assert_not_called()

    def test_frag_idx_exceeding_total_dropped(self):
        iface = _make_interface()
        header = b"RN" + bytes([5, 0xAA, 0xBB, 0xCC, 0xDD, 1, 3])  # idx=5, total=3
        encoded = base64.urlsafe_b64encode(header + b"\x01").rstrip(b"=").decode()
        self._run(iface._process_tunnel_text("RNS:" + encoded))
        iface.owner.inbound.assert_not_called()

    def test_empty_reassembled_packet_dropped(self):
        iface = _make_interface()
        remote_src = b"\xEE\xFF\x00\x11"
        # Single fragment with empty payload
        text = _make_channel_fragment(b"", remote_src, pkt_id=7)
        self._run(iface._process_tunnel_text(text))
        iface.owner.inbound.assert_not_called()

    def test_duplicate_fragment_ignored(self):
        iface = _make_interface()
        remote_src = b"\x12\x34\x56\x78"
        chunk1 = bytes(range(50))
        frag = _make_channel_fragment(chunk1, remote_src, pkt_id=3, frag_idx=0, frag_total=2)

        # Send same fragment twice — assembly should still work
        self._run(iface._process_tunnel_text(frag))
        self._run(iface._process_tunnel_text(frag))

        # Still waiting for frag_idx=1
        iface.owner.inbound.assert_not_called()
        key = (remote_src.hex(), 3)
        assert len(iface._assembly.get(key, {})) == 1


# ═══════════════════════════════════════════════════════════════════════════
# processOutgoing
# ═══════════════════════════════════════════════════════════════════════════

class TestChannelProcessOutgoing:

    def test_outgoing_enqueues_fragments(self):
        iface = _make_interface()
        data = b"\x01\x02\x03\x04\x05"
        iface.processOutgoing(data)

        assert not iface._outqueue.empty()
        assert iface.txb == len(data)
        assert iface._pkt_id == 1

    def test_outgoing_offline_drops_packet(self):
        iface = _make_interface()
        iface.online = False
        iface.processOutgoing(b"\x01\x02\x03")
        assert iface._outqueue.empty()
        assert iface.txb == 0

    def test_outgoing_multiple_fragments(self):
        iface = _make_interface()
        # 200 bytes at 64 byte payload → ceil(200/64) = 4 fragments
        data = bytes(range(200))
        iface.processOutgoing(data)

        count = 0
        while not iface._outqueue.empty():
            iface._outqueue.get_nowait()
            count += 1
        assert count == 4

    def test_outgoing_pkt_id_wraps(self):
        iface = _make_interface()
        iface._pkt_id = 255
        iface.processOutgoing(b"\x00")
        # Channel interface wraps pkt_id at 0xFF (8-bit)
        assert iface._pkt_id == 0


# ═══════════════════════════════════════════════════════════════════════════
# processIncoming
# ═══════════════════════════════════════════════════════════════════════════

class TestChannelProcessIncoming:

    def test_delivers_to_owner(self):
        iface = _make_interface()
        data = b"\x01\x02\x03"
        iface.processIncoming(data)
        iface.owner.inbound.assert_called_once_with(data, iface)
        assert iface.rxb == 3

    def test_offline_does_not_deliver(self):
        iface = _make_interface()
        iface.online = False
        iface.processIncoming(b"\x01")
        iface.owner.inbound.assert_not_called()

    def test_detached_does_not_deliver(self):
        iface = _make_interface()
        iface.detached = True
        iface.processIncoming(b"\x01")
        iface.owner.inbound.assert_not_called()
