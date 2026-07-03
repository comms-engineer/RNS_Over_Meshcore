"""Tests for payload encoding/decoding helpers in MeshCore_Interface."""

import base64

import pytest

from Interface.MeshCore_Interface import MeshCoreInterface


def _make_interface():
    """Build a MeshCoreInterface with __init__ bypassed."""
    iface = object.__new__(MeshCoreInterface)
    iface.debug_level = "off"
    iface._LOG_DEBUG = 10
    iface._LOG_INFO = 20
    iface._LOG_WARNING = 30
    iface._LOG_ERROR = 40
    return iface


class TestPayloadForSend:

    def test_basic_encoding(self):
        iface = _make_interface()
        data = b"\x01\x02\x03\x04"
        result = iface._payload_for_send(data)
        assert result == base64.b64encode(data).decode("ascii")

    def test_round_trip(self):
        iface = _make_interface()
        original = bytes(range(256))
        encoded = iface._payload_for_send(original)
        decoded = base64.b64decode(encoded)
        assert decoded == original

    def test_empty_data(self):
        iface = _make_interface()
        result = iface._payload_for_send(b"")
        assert result == ""  # base64 of empty is empty


class TestPayloadFromReceived:

    def test_bytes_passthrough(self):
        iface = _make_interface()
        data = b"\x01\x02\x03"
        assert iface._payload_from_received(data) == data

    def test_bytearray_passthrough(self):
        iface = _make_interface()
        data = bytearray(b"\x04\x05\x06")
        assert iface._payload_from_received(data) == bytes(data)

    def test_base64_string(self):
        iface = _make_interface()
        original = b"hello world"
        encoded = base64.b64encode(original).decode()
        assert iface._payload_from_received(encoded) == original

    def test_double_base64_string(self):
        iface = _make_interface()
        original = b"test data"
        single = base64.b64encode(original)
        double = base64.b64encode(single).decode()
        assert iface._payload_from_received(double) == original

    def test_dict_with_payload_key(self):
        iface = _make_interface()
        original = b"\x01\x02\x03"
        encoded = base64.b64encode(original).decode()
        payload = {"payload": encoded}
        assert iface._payload_from_received(payload) == original

    def test_dict_with_data_key(self):
        iface = _make_interface()
        original = b"\x04\x05\x06"
        encoded = base64.b64encode(original).decode()
        payload = {"data": encoded}
        assert iface._payload_from_received(payload) == original

    def test_dict_with_text_key(self):
        iface = _make_interface()
        original = b"\x07\x08\x09"
        encoded = base64.b64encode(original).decode()
        payload = {"text": encoded}
        assert iface._payload_from_received(payload) == original

    def test_dict_with_bytes_value(self):
        iface = _make_interface()
        original = b"\xAA\xBB"
        payload = {"payload": original}
        assert iface._payload_from_received(payload) == original

    def test_dict_empty_returns_none(self):
        iface = _make_interface()
        assert iface._payload_from_received({}) is None

    def test_none_returns_none(self):
        iface = _make_interface()
        assert iface._payload_from_received(None) is None

    def test_non_base64_string_fallback(self):
        iface = _make_interface()
        # "hello" is valid base64 but may not decode cleanly to valid base64 again
        result = iface._payload_from_received("not!valid!base64!")
        # Should fall back to latin-1/utf-8 encoding
        assert result is not None
        assert isinstance(result, bytes)


class TestResolveDestination:

    def test_dict_passthrough(self):
        iface = _make_interface()
        iface.dest_to_node_dict = {}
        contact = {"public_key": "abc123", "adv_name": "relay"}
        assert iface._resolve_destination(contact) is contact

    def test_full_pubkey_passthrough(self):
        iface = _make_interface()
        iface.dest_to_node_dict = {}
        key = "a" * 64
        assert iface._resolve_destination(key) == key

    def test_short_prefix_lookup(self):
        iface = _make_interface()
        contact = {"public_key": "deadbeef01234567"}
        iface.dest_to_node_dict = {"dead": contact}
        assert iface._resolve_destination("dead") is contact

    def test_advert_name_lookup(self):
        iface = _make_interface()
        contact = {"public_key": "abcdef0123456789"}
        iface.dest_to_node_dict = {"relay1": contact}
        assert iface._resolve_destination("Relay1") is contact

    def test_unresolvable_returns_none(self):
        iface = _make_interface()
        iface.dest_to_node_dict = {}
        assert iface._resolve_destination("unknown") is None


class TestShouldIngressLimit:

    def test_always_returns_false(self):
        iface = _make_interface()
        assert iface.should_ingress_limit() is False
        assert iface.should_ingress_limit(dest="something") is False


class TestSetDebugLevel:

    def test_valid_levels(self):
        iface = _make_interface()
        for level in ("off", "info", "debug"):
            iface.set_debug_level(level)
            assert iface.debug_level == level

    def test_invalid_level_no_change(self):
        iface = _make_interface()
        iface.debug_level = "info"
        iface.set_debug_level("invalid")
        assert iface.debug_level == "info"

    def test_none_no_change(self):
        iface = _make_interface()
        iface.debug_level = "debug"
        iface.set_debug_level(None)
        assert iface.debug_level == "debug"
