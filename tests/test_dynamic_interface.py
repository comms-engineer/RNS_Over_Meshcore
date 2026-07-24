"""Tests for peer discovery, rate limiting, routing, and tunnel text processing
in MeshCore_Dynamic_Interface.

These test methods on the interface class without starting real asyncio loops
or hardware connections.
"""

import asyncio
import base64
import hashlib
import struct
import threading
import time
from collections import OrderedDict
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from Interface.MeshCore_Dynamic_Interface import (
    MeshCore_Dynamic_Interface,
    _PacketHandler,
)


def _make_interface(**overrides):
    """Build a MeshCore_Dynamic_Interface with __init__ bypassed."""
    iface = object.__new__(MeshCore_Dynamic_Interface)

    # Copy all class-level constants
    for attr in dir(MeshCore_Dynamic_Interface):
        if not attr.startswith("__") and not callable(getattr(MeshCore_Dynamic_Interface, attr, None)):
            try:
                setattr(iface, attr, getattr(MeshCore_Dynamic_Interface, attr))
            except (AttributeError, TypeError):
                pass

    # Instance state
    iface.owner = MagicMock()
    iface.name = "test"
    iface.online = True
    iface.detached = False
    iface.txb = 0
    iface.rxb = 0
    iface.debug_level = overrides.get("debug_level", "info")
    iface.can_route = overrides.get("can_route", True)
    iface.allow_direct = overrides.get("allow_direct", True)
    iface.payload_size = overrides.get("payload_size", 64)
    iface.fragment_delay_s = 0.0
    iface.direct_frag_delay_s = 0.0
    iface.fragment_timeout_s = 300.0
    iface.rate_limit_bps = 0
    iface._announce_rate_s = overrides.get("announce_rate", 600)
    iface._path_req_rate_s = overrides.get("path_req_rate", 1800)
    iface._path_req_burst_window_s = overrides.get("path_req_burst_window", 60)
    iface.peer_ttl_s = 86400
    iface.channel_idx = 0

    iface._own_node_name = overrides.get("own_node_name", "TestNode")
    iface._own_mc_key = overrides.get("own_mc_key", "abcdef0123456789")

    iface._mc = MagicMock()
    iface._EventType = MagicMock()
    iface._has_direct_api = overrides.get("has_direct_api", True)
    iface._loop = None
    iface._pending_resp_task = None

    iface._pkt_id = 0
    iface._pkt_id_lock = threading.Lock()

    iface._assembly = {}
    iface._assembly_meta = {}
    iface._asm_lock = threading.Lock()

    iface._seen_pkts = {}
    iface._seen_lock = threading.Lock()

    iface._peer_table = {}
    iface._reverse_peers = {}
    iface._peer_last_seen = {}
    iface._peer_caps = {}
    iface._rns_to_mc_map = {}
    iface._peer_lock = threading.Lock()

    iface._announce_sent_times = {}
    iface._announce_sent_lock = threading.Lock()
    iface._path_req_sent_times = {}
    iface._path_req_sent_lock = threading.Lock()

    import queue
    iface._outqueue = queue.Queue(maxsize=512)

    return iface


# ═══════════════════════════════════════════════════════════════════════════
# _handle_bind — peer discovery parsing
# ═══════════════════════════════════════════════════════════════════════════

class TestHandleBind:

    def _run(self, coro):
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    def test_bind_response_registers_peer(self):
        iface = _make_interface()
        text = "RemoteNode: RNSBIND:deadbeef01234567:R"
        self._run(iface._handle_bind(text, bind_idx=12, req_idx=-1))

        assert "RemoteNode" in iface._peer_table
        assert iface._peer_table["RemoteNode"] == "deadbeef01234567"
        assert iface._peer_caps["RemoteNode"] is True  # router

    def test_bind_req_registers_peer_as_edge(self):
        iface = _make_interface()
        text = "EdgeNode: RNSBIND_REQ:aabbccdd11223344:E"
        self._run(iface._handle_bind(text, bind_idx=-1, req_idx=10))

        assert "EdgeNode" in iface._peer_table
        assert iface._peer_caps["EdgeNode"] is False  # edge

    def test_bind_ignores_own_node(self):
        iface = _make_interface(own_node_name="MyNode")
        text = "MyNode: RNSBIND:deadbeef01234567:R"
        self._run(iface._handle_bind(text, bind_idx=8, req_idx=-1))

        assert "MyNode" not in iface._peer_table

    def test_bind_without_capability_defaults_router(self):
        iface = _make_interface()
        text = "OldNode: RNSBIND:aabbccdd11223344"
        self._run(iface._handle_bind(text, bind_idx=9, req_idx=-1))

        assert iface._peer_caps["OldNode"] is True

    def test_bind_updates_reverse_peers(self):
        iface = _make_interface()
        mc_key = "deadbeef01234567890abcdef0000000"
        text = f"Relay1: RNSBIND:{mc_key}:R"
        self._run(iface._handle_bind(text, bind_idx=8, req_idx=-1))

        assert iface._reverse_peers[mc_key] == "Relay1"
        # Also stored by prefix lengths
        assert iface._reverse_peers[mc_key[:8]] == "Relay1"
        assert iface._reverse_peers[mc_key[:12]] == "Relay1"

    def test_bind_empty_pubkey_ignored(self):
        iface = _make_interface()
        text = "Node: RNSBIND::R"
        self._run(iface._handle_bind(text, bind_idx=6, req_idx=-1))
        assert "Node" not in iface._peer_table


# ═══════════════════════════════════════════════════════════════════════════
# _resolve_sender_key
# ═══════════════════════════════════════════════════════════════════════════

class TestResolveSenderKey:

    def test_exact_match(self):
        iface = _make_interface()
        iface._reverse_peers["abcdef01"] = "NodeA"
        assert iface._resolve_sender_key("abcdef01") == "NodeA"

    def test_prefix_match(self):
        iface = _make_interface()
        iface._reverse_peers["abcdef0123456789"] = "NodeB"
        assert iface._resolve_sender_key("abcdef01") == "NodeB"

    def test_no_match_returns_key(self):
        iface = _make_interface()
        assert iface._resolve_sender_key("unknown") == "unknown"

    def test_empty_returns_empty(self):
        iface = _make_interface()
        assert iface._resolve_sender_key("") == ""


# ═══════════════════════════════════════════════════════════════════════════
# Tunnel text processing (_process_tunnel_text)
# ═══════════════════════════════════════════════════════════════════════════

class TestProcessTunnelText:

    def _run(self, coro):
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    def _make_fragment(self, data, pkt_id=1, frag_idx=0, frag_total=1):
        header = struct.pack(">BIB", frag_idx & 0xFF, pkt_id & 0xFFFFFFFF, frag_total & 0xFF)
        encoded = base64.urlsafe_b64encode(header + data).rstrip(b"=").decode()
        return "RNS:" + encoded

    def test_single_fragment_delivered(self):
        iface = _make_interface()
        data = b"\x01" + b"\x00" * 30  # minimal RNS-like packet
        frag = self._make_fragment(data, pkt_id=42)

        self._run(iface._process_tunnel_text(frag, sender="PeerA"))
        iface.owner.inbound.assert_called_once()
        delivered = iface.owner.inbound.call_args[0][0]
        assert delivered == data

    def test_multi_fragment_reassembly(self):
        iface = _make_interface()
        original = bytes(range(100))
        chunk1 = original[:50]
        chunk2 = original[50:]

        frag1 = self._make_fragment(chunk1, pkt_id=7, frag_idx=0, frag_total=2)
        frag2 = self._make_fragment(chunk2, pkt_id=7, frag_idx=1, frag_total=2)

        self._run(iface._process_tunnel_text(frag1, sender="PeerB"))
        iface.owner.inbound.assert_not_called()

        self._run(iface._process_tunnel_text(frag2, sender="PeerB"))
        iface.owner.inbound.assert_called_once()
        delivered = iface.owner.inbound.call_args[0][0]
        assert delivered == original

    def test_own_echo_ignored(self):
        iface = _make_interface(own_node_name="MyNode")
        frag = self._make_fragment(b"\x00" * 10, pkt_id=1)

        self._run(iface._process_tunnel_text(frag, sender="MyNode"))
        iface.owner.inbound.assert_not_called()

    def test_duplicate_packet_rejected(self):
        iface = _make_interface()
        data = b"\x01" + b"\x00" * 10
        frag = self._make_fragment(data, pkt_id=99)

        self._run(iface._process_tunnel_text(frag, sender="PeerC"))
        assert iface.owner.inbound.call_count == 1

        # Send same fragment again — should be deduplicated
        self._run(iface._process_tunnel_text(frag, sender="PeerC"))
        assert iface.owner.inbound.call_count == 1

    def test_invalid_base64_silently_dropped(self):
        iface = _make_interface()
        self._run(iface._process_tunnel_text("RNS:!!!invalid!!!", sender="PeerD"))
        iface.owner.inbound.assert_not_called()

    def test_too_short_payload_dropped(self):
        iface = _make_interface()
        # Encode only 2 bytes — less than HEADER_SIZE (6)
        short = base64.urlsafe_b64encode(b"\x00\x01").rstrip(b"=").decode()
        self._run(iface._process_tunnel_text("RNS:" + short, sender="PeerE"))
        iface.owner.inbound.assert_not_called()

    def test_zero_frag_total_dropped(self):
        iface = _make_interface()
        header = struct.pack(">BIB", 0, 1, 0)  # frag_total=0
        encoded = base64.urlsafe_b64encode(header + b"\x00" * 10).rstrip(b"=").decode()
        self._run(iface._process_tunnel_text("RNS:" + encoded, sender="PeerF"))
        iface.owner.inbound.assert_not_called()

    def test_frag_idx_exceeding_total_dropped(self):
        iface = _make_interface()
        header = struct.pack(">BIB", 5, 1, 3)  # idx=5, total=3
        encoded = base64.urlsafe_b64encode(header + b"\x00" * 10).rstrip(b"=").decode()
        self._run(iface._process_tunnel_text("RNS:" + encoded, sender="PeerG"))
        iface.owner.inbound.assert_not_called()


# ═══════════════════════════════════════════════════════════════════════════
# Rate limiting in processOutgoing
# ═══════════════════════════════════════════════════════════════════════════

class TestRateLimiting:

    def test_announce_rate_suppresses_duplicate(self):
        iface = _make_interface(announce_rate=600)
        # Announce packet: ptype=0x01
        dest = bytes(range(10))
        packet = bytes([0x01, 0x00]) + dest + b"\x00" * 20

        # Pre-seed with old timestamp so the first send goes through
        iface._announce_sent_times[bytes(dest)] = time.monotonic() - 9999

        iface.processOutgoing(packet)
        first_txb = iface.txb
        assert first_txb > 0

        # Second announce to same dest within window → suppressed
        iface.processOutgoing(packet)
        assert iface.txb == first_txb

    def test_announce_rate_allows_different_dest(self):
        iface = _make_interface(announce_rate=600)
        dest1 = bytes([0x01] * 10)
        dest2 = bytes([0x02] * 10)
        pkt1 = bytes([0x01, 0x00]) + dest1 + b"\x00" * 20
        pkt2 = bytes([0x01, 0x00]) + dest2 + b"\x00" * 20

        # Pre-seed both dests with old timestamps so rate limiter allows them
        old_ts = time.monotonic() - 9999
        iface._announce_sent_times[bytes(dest1)] = old_ts
        iface._announce_sent_times[bytes(dest2)] = old_ts

        iface.processOutgoing(pkt1)
        txb_after_first = iface.txb
        iface.processOutgoing(pkt2)
        assert iface.txb > txb_after_first

    def test_path_req_burst_window_allows_initial(self):
        iface = _make_interface(path_req_rate=1800, path_req_burst_window=60)
        # Path request: ptype=DATA(0x00), dest_type=PLAIN(0x02) → flags=0x08
        dest = bytes(range(10))
        packet = bytes([0x08, 0x00]) + dest + b"\x00" * 20

        iface.processOutgoing(packet)
        assert iface.txb > 0

    def test_path_req_within_burst_window_allowed(self):
        iface = _make_interface(path_req_rate=1800, path_req_burst_window=60)
        dest = bytes(range(10))
        packet = bytes([0x08, 0x00]) + dest + b"\x00" * 20

        iface.processOutgoing(packet)
        first_txb = iface.txb
        # Second request within burst window should be allowed
        iface.processOutgoing(packet)
        assert iface.txb > first_txb

    def test_announce_rate_zero_disables_limiting(self):
        iface = _make_interface(announce_rate=0)
        dest = bytes(range(10))
        packet = bytes([0x01, 0x00]) + dest + b"\x00" * 20

        iface.processOutgoing(packet)
        first_txb = iface.txb
        iface.processOutgoing(packet)
        assert iface.txb > first_txb

    def test_offline_interface_drops_packet(self):
        iface = _make_interface()
        iface.online = False
        packet = bytes([0x00]) + b"\x00" * 30
        iface.processOutgoing(packet)
        assert iface.txb == 0


# ═══════════════════════════════════════════════════════════════════════════
# processOutgoing routing decisions
# ═══════════════════════════════════════════════════════════════════════════

class TestProcessOutgoingRouting:

    def test_broadcast_packet_routed_to_channel(self):
        iface = _make_interface(announce_rate=0)
        # Announce packet → broadcast
        packet = bytes([0x01, 0x00]) + b"\x00" * 30
        iface.processOutgoing(packet)

        item = iface._outqueue.get_nowait()
        assert item[0] == "channel"

    def test_unicast_with_known_route_routed_direct(self):
        iface = _make_interface()
        # DATA+SINGLE (flags=0x00), single-header
        dest_token = bytes(range(16))
        mc_key = "deadbeef01234567"
        iface._rns_to_mc_map[dest_token] = mc_key

        packet = bytes([0x00, 0x00]) + dest_token + b"\x00" * 20
        iface.processOutgoing(packet)

        item = iface._outqueue.get_nowait()
        assert item[0] == "direct"
        assert item[1] == mc_key

    def test_unicast_without_route_falls_back_to_channel(self):
        iface = _make_interface()
        packet = bytes([0x00, 0x00]) + b"\xFF" * 16 + b"\x00" * 20
        iface.processOutgoing(packet)

        item = iface._outqueue.get_nowait()
        assert item[0] == "channel"

    def test_no_direct_api_forces_channel(self):
        iface = _make_interface(has_direct_api=False)
        dest_token = bytes(range(16))
        iface._rns_to_mc_map[dest_token] = "somekey"

        packet = bytes([0x00, 0x00]) + dest_token + b"\x00" * 20
        iface.processOutgoing(packet)

        item = iface._outqueue.get_nowait()
        assert item[0] == "channel"

    def test_pkt_id_increments(self):
        iface = _make_interface(announce_rate=0)
        assert iface._pkt_id == 0
        packet = bytes([0x01, 0x00]) + b"\x00" * 30
        iface.processOutgoing(packet)
        assert iface._pkt_id == 1

    def test_pkt_id_wraps_at_32bit(self):
        iface = _make_interface(announce_rate=0)
        iface._pkt_id = 0xFFFFFFFF
        packet = bytes([0x01, 0x00]) + b"\x00" * 30
        iface.processOutgoing(packet)
        assert iface._pkt_id == 0


# ═══════════════════════════════════════════════════════════════════════════
# processIncoming
# ═══════════════════════════════════════════════════════════════════════════

class TestProcessIncoming:

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
