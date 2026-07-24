"""
_meshcore_shared.py — Shared utilities for MeshCore RNS interfaces.

Place alongside MeshCore_Channel_Interface.py and MeshCore_Dynamic_Interface.py
in ~/.reticulum/interfaces/ (or the repo's Interface/ directory).

Extracted from duplicated logic that was present in both
MeshCore_Channel_Interface and MeshCore_Dynamic_Interface.
"""

import RNS
import asyncio
import base64
import threading
import time


# ---------------------------------------------------------------------------
# Library loading
# ---------------------------------------------------------------------------

def load_meshcore(interface_name):
    """
    Import the meshcore library.

    Returns ``(meshcore_module, EventType)`` on success.
    Logs CRITICAL and re-raises ``ImportError`` on failure so the caller
    can invoke its own panic handler.
    """
    try:
        import meshcore
        return meshcore, meshcore.EventType
    except ImportError:
        RNS.log(
            f"{interface_name}: "
            "meshcore library not found. "
            "Install with: pip install meshcore",
            RNS.LOG_CRITICAL,
        )
        raise


# ---------------------------------------------------------------------------
# Asyncio event-loop helpers
# ---------------------------------------------------------------------------

def _run_asyncio_loop(loop, interface_name):
    """Thread target that runs an asyncio event loop forever."""
    asyncio.set_event_loop(loop)
    try:
        loop.run_forever()
    except Exception as exc:
        RNS.log(
            f"{interface_name}: Event loop crashed: {exc}",
            RNS.LOG_ERROR,
        )


def start_asyncio_loop_thread(thread_name, interface_name):
    """
    Create a new asyncio event loop and start it in a daemon thread.

    Returns ``(loop, thread)``.
    """
    loop = asyncio.new_event_loop()
    thread = threading.Thread(
        target=_run_asyncio_loop,
        args=(loop, interface_name),
        daemon=True,
        name=thread_name,
    )
    thread.start()
    return loop, thread


# ---------------------------------------------------------------------------
# MeshCore connection / radio / channel setup
# ---------------------------------------------------------------------------

async def create_meshcore_connection(mc_module, transport, *,
                                      port="/dev/ttyUSB0", baudrate=115200,
                                      host="127.0.0.1", tcp_port=4403,
                                      ble_name="", interface_name=""):
    """
    Create a MeshCore connection for the given transport type.

    Raises ``ValueError`` for unknown transports; propagates driver errors.
    """
    MeshCore = mc_module.MeshCore
    if transport == "serial":
        mc = await MeshCore.create_serial(port, baudrate)
        RNS.log(f"{interface_name}: Connected via serial {port}", RNS.LOG_INFO)
    elif transport == "ble":
        mc = await MeshCore.create_ble(ble_name or None)
        RNS.log(f"{interface_name}: Connected via BLE", RNS.LOG_INFO)
    elif transport == "tcp":
        mc = await MeshCore.create_tcp(host, tcp_port)
        RNS.log(
            f"{interface_name}: Connected via TCP {host}:{tcp_port}",
            RNS.LOG_INFO,
        )
    else:
        raise ValueError(f"Unknown transport '{transport}'")
    return mc


async def configure_radio(mc, freq, bw, sf, cr, interface_name=""):
    """Apply radio parameter overrides if all four values are non-zero."""
    if not (freq and bw and sf and cr):
        return
    try:
        await mc.commands.set_radio(freq, bw, sf, cr)
        RNS.log(
            f"{interface_name}: "
            f"Radio set freq={freq} bw={bw} sf={sf} cr={cr}",
            RNS.LOG_INFO,
        )
    except Exception as exc:
        RNS.log(
            f"{interface_name}: Radio config error: {exc}",
            RNS.LOG_WARNING,
        )


async def configure_channel(mc, channel_idx, channel_name, channel_secret_hex,
                             interface_name=""):
    """Configure a MeshCore channel from a hex-encoded secret."""
    try:
        secret_bytes = bytes.fromhex(channel_secret_hex)
        await mc.commands.set_channel(channel_idx, channel_name, secret_bytes)
        RNS.log(
            f"{interface_name}: "
            f"Channel {channel_idx} ('{channel_name}') configured",
            RNS.LOG_INFO,
        )
    except Exception as exc:
        RNS.log(
            f"{interface_name}: Channel config error: {exc}",
            RNS.LOG_WARNING,
        )


# ---------------------------------------------------------------------------
# RNS interface helpers
# ---------------------------------------------------------------------------

def process_incoming(interface, data):
    """
    Standard RNS inbound delivery used by MeshCore interfaces.

    Updates the byte counter and hands the packet to the RNS owner.
    """
    if interface.online and not interface.detached:
        interface.rxb += len(data)
        interface.owner.inbound(data, interface)


# ---------------------------------------------------------------------------
# Tunnel-frame codec
# ---------------------------------------------------------------------------

def decode_tunnel_frame(text, msg_prefix="RNS:", interface_name=""):
    """
    Locate *msg_prefix* in *text*, strip any sender name prepended by
    MeshCore firmware, and base64url-decode the binary frame.

    Returns ``(raw_bytes, sender_prefix)`` on success, or
    ``(None, None)`` when the prefix is absent, or
    ``(None, sender_prefix)`` when decoding fails.
    """
    rns_idx = text.find(msg_prefix)
    if rns_idx == -1:
        return None, None

    sender_prefix = text[:rns_idx].rstrip(": ") if rns_idx > 0 else ""
    b64 = text[rns_idx + len(msg_prefix):].strip()
    b64 += "=" * (-len(b64) % 4)

    try:
        raw = base64.urlsafe_b64decode(b64)
        return raw, sender_prefix
    except Exception as exc:
        if interface_name:
            RNS.log(
                f"{interface_name}: "
                f"base64 decode error: {exc}  "
                f"len={len(b64)}  text={text[:80]}",
                RNS.LOG_WARNING,
            )
        return None, sender_prefix


# ---------------------------------------------------------------------------
# Fragment reassembly
# ---------------------------------------------------------------------------

def reassemble_fragment(assembly, assembly_meta, asm_lock, key,
                         frag_idx, frag_total, payload, interface_name=""):
    """
    Insert one fragment into the reassembly buffer and return the
    complete packet when all fragments have arrived.

    Returns the reassembled ``bytes`` on completion, or ``None`` if
    the packet is still incomplete (or the fragment is a duplicate).

    Thread-safe: acquires *asm_lock* internally.
    """
    now = time.monotonic()
    with asm_lock:
        if key not in assembly:
            assembly[key] = {}
            assembly_meta[key] = (frag_total, now)

        if frag_idx in assembly[key]:
            return None

        assembly[key][frag_idx] = payload
        expected = assembly_meta[key][0]

        if len(assembly[key]) < expected:
            return None

        try:
            full_packet = b"".join(
                assembly[key][i] for i in range(expected)
            )
            del assembly[key]
            del assembly_meta[key]
            return full_packet
        except KeyError as exc:
            RNS.log(
                f"{interface_name}: "
                f"Reassembly gap — missing fragment {exc}",
                RNS.LOG_WARNING,
            )
            assembly.pop(key, None)
            assembly_meta.pop(key, None)
            return None
        except Exception as exc:
            RNS.log(
                f"{interface_name}: Reassembly failed: {exc}",
                RNS.LOG_ERROR,
            )
            assembly.pop(key, None)
            assembly_meta.pop(key, None)
            return None


def cleanup_stale_fragments(assembly, assembly_meta, asm_lock, timeout_s,
                             interface_name=""):
    """
    Evict fragment assemblies that have exceeded *timeout_s* seconds.

    Returns the number of evicted entries.
    """
    deadline = time.monotonic() - timeout_s
    with asm_lock:
        stale = [k for k, (_, ts) in assembly_meta.items() if ts < deadline]
        for k in stale:
            del assembly[k]
            del assembly_meta[k]
            RNS.log(
                f"{interface_name}: Evicted stale assembly {k}",
                RNS.LOG_WARNING,
            )
    return len(stale)


# ---------------------------------------------------------------------------
# Packet-ID counter
# ---------------------------------------------------------------------------

class PacketIdCounter:
    """Thread-safe rolling packet ID counter with configurable bit width."""

    def __init__(self, bits=8):
        self._value = 0
        self._mask = (1 << bits) - 1
        self._lock = threading.Lock()

    def next_id(self):
        """Return the current value and advance the counter."""
        with self._lock:
            val = self._value
            self._value = (self._value + 1) & self._mask
            return val
