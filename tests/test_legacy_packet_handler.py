"""Tests for PacketHandler in MeshCore_Interface (legacy fragmentation)."""

import struct

import pytest

from Interface.MeshCore_Interface import PacketHandler


class TestPacketHandler:

    def test_single_fragment_small_data(self):
        data = b"small"
        handler = PacketHandler(data, index=0, max_payload=200)
        keys = handler.get_keys()
        # Small data → 1 fragment; final fragment has negative pos
        assert len(keys) == 1
        assert keys[0] < 0  # final fragment marker

    def test_fragment_metadata_format(self):
        data = b"hello world"
        handler = PacketHandler(data, index=5, max_payload=200)
        keys = handler.get_keys()
        frag = handler[keys[0]]

        # First 2 bytes are struct.pack("Bb", index, pos)
        index, pos = struct.unpack("Bb", frag[:2])
        assert index == 5
        # Single fragment: pos is -(seq) where seq starts at 1
        assert pos == -1
        assert frag[2:] == data

    def test_multi_fragment_split(self):
        data = bytes(range(100))
        handler = PacketHandler(data, index=1, max_payload=30)
        keys = handler.get_keys()

        assert len(keys) > 1
        # Only the last key should be negative
        for k in keys[:-1]:
            assert k > 0
        assert keys[-1] < 0

    def test_reassembly_from_fragments(self):
        data = bytes(range(250))
        handler = PacketHandler(data, index=3, max_payload=50)
        keys = handler.get_keys()

        reassembled = b""
        for k in sorted(keys, key=abs):
            frag = handler[k]
            # Strip 2-byte metadata header
            reassembled += frag[2:]

        assert reassembled == data

    def test_index_wraps(self):
        handler = PacketHandler(b"x", index=255, max_payload=200)
        frag = handler[handler.get_keys()[0]]
        idx, _ = struct.unpack("Bb", frag[:2])
        assert idx == 255

    def test_getitem_positive_and_negative_lookup(self):
        handler = PacketHandler(b"test", index=0, max_payload=200)
        # Single frag stored under key -1
        assert handler[-1] is not None
        # __getitem__ also checks -i so handler[1] should find handler[-1]
        assert handler[1] is not None
        assert handler[-1] == handler[1]

    def test_getitem_missing_key_returns_none(self):
        handler = PacketHandler(b"test", index=0, max_payload=200)
        assert handler[999] is None

    def test_empty_data_no_fragments(self):
        handler = PacketHandler(data=None, index=0, max_payload=200)
        assert handler.get_keys() == []

    def test_destination_id_stored(self):
        handler = PacketHandler(b"x", index=0, max_payload=200, custom_destination_id="abc123")
        assert handler.destination_id == "abc123"

    def test_fragment_sizes_are_nearly_even(self):
        # Verify the algorithm creates nearly-even fragments
        data = bytes(100)
        handler = PacketHandler(data, index=0, max_payload=40)
        keys = handler.get_keys()
        sizes = [len(handler[k]) - 2 for k in keys]  # subtract metadata
        # Integer division may produce chunks differing by up to 2 bytes
        assert max(sizes) - min(sizes) <= 2

    def test_split_data_called_on_init(self):
        handler = PacketHandler(b"abc", index=7)
        assert len(handler.get_keys()) == 1
        frag = handler[handler.get_keys()[0]]
        idx, pos = struct.unpack("Bb", frag[:2])
        assert idx == 7
