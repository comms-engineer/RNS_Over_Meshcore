"""Shared fixtures and RNS/meshcore mock scaffolding for unit tests.

The production code imports ``RNS`` and ``meshcore`` at module level.  Neither
library is available in the test environment, so we inject lightweight stubs
into ``sys.modules`` *before* any production module is imported.
"""

import sys
import types
from unittest.mock import MagicMock

import pytest


# ---------------------------------------------------------------------------
# RNS stub
# ---------------------------------------------------------------------------

def _build_rns_stub():
    rns = types.ModuleType("RNS")
    rns.LOG_CRITICAL = 50
    rns.LOG_ERROR = 40
    rns.LOG_WARNING = 30
    rns.LOG_INFO = 20
    rns.LOG_DEBUG = 10
    rns.log = MagicMock()
    rns.panic = MagicMock()

    # RNS.Interfaces.Interface.Interface base class
    interfaces_pkg = types.ModuleType("RNS.Interfaces")
    interface_mod = types.ModuleType("RNS.Interfaces.Interface")

    class _StubInterface:
        DEFAULT_IFAC_SIZE = 8
        DEFAULT_IFAC_NAME = ""
        DEFAULT_IFAC_NETKEY = b""
        HW_MTU = 500

        def __init__(self):
            self.online = False
            self.detached = False
            self.txb = 0
            self.rxb = 0
            self.owner = None
            self.name = ""

        @staticmethod
        def get_config_obj(cfg):
            return cfg

        def processIncoming(self, data):
            pass

    interface_mod.Interface = _StubInterface
    interfaces_pkg.Interface = interface_mod

    rns.Interfaces = interfaces_pkg

    sys.modules["RNS"] = rns
    sys.modules["RNS.Interfaces"] = interfaces_pkg
    sys.modules["RNS.Interfaces.Interface"] = interface_mod
    return rns


# ---------------------------------------------------------------------------
# meshcore stub
# ---------------------------------------------------------------------------

def _build_meshcore_stub():
    mc = types.ModuleType("meshcore")

    class _EventType:
        SELF_INFO = "SELF_INFO"
        OK = "OK"
        ERROR = "ERROR"
        CHANNEL_MSG_RECV = "CHANNEL_MSG_RECV"
        CONTACT_MSG_RECV = "CONTACT_MSG_RECV"
        NEW_CONTACT = "NEW_CONTACT"
        RX_LOG_DATA = "RX_LOG_DATA"
        MSG_SENT = "MSG_SENT"
        ACK = "ACK"

    mc.EventType = _EventType
    mc.MeshCore = MagicMock()
    sys.modules["meshcore"] = mc
    return mc


# ---------------------------------------------------------------------------
# Install stubs once at import time so production modules can be imported
# ---------------------------------------------------------------------------

_rns_stub = _build_rns_stub()
_mc_stub = _build_meshcore_stub()

# MeshCore_Interface.py expects bare ``Interface`` and ``RNS`` names injected
# by Reticulum's exec() loader.  We make them importable by patching builtins.
import builtins
builtins.RNS = _rns_stub
builtins.Interface = _rns_stub.Interfaces.Interface.Interface


# ---------------------------------------------------------------------------
# Pytest fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _reset_rns_log():
    """Clear RNS.log call history between tests."""
    _rns_stub.log.reset_mock()
    yield
