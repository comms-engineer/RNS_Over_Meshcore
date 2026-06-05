"""
MeshCore_Dynamic_Interface.py
RNS interface over a MeshCore LoRa mesh network.

Uses a hybrid channel/direct-message routing strategy with automatic peer
discovery — no static remote-node configuration required.

    Announces              → MeshCore channel  (broadcast needed)
    All other RNS traffic  → MeshCore direct messages to known peers
                             (channel fallback while peer table is empty)
    RX (channel + direct)  → same reassembly pipeline

Peers are discovered via a lightweight BIND broadcast each node sends on
startup and periodically.  As soon as both ends have exchanged BINDs, all
non-announce traffic switches from channel to direct messages, which are
more reliable (firmware-level retry + ACK) and keep the shared channel
clear for discovery traffic.


WIRE FORMAT
===========
Binary 3-byte header + payload, base64url-encoded and prefixed "RNS:":

  [frag_idx:1B][pkt_id:1B][frag_total:1B][payload:N]

  frag_idx   = 0-based index of this fragment
  pkt_id     = rolling 0-255 transmit counter (per node)
  frag_total = total fragment count for this packet

Channel text:  "RNS:<base64url>"

Sender identity is NOT embedded in the header.  For channel messages the
MeshCore firmware prepends the node name to every channel message text
("node_name: RNS:...").  For direct messages the meshcore_py library
supplies the sender public key as event metadata.  Both are normalised
to a stable sender_id before entering the reassembly pipeline.

Wire-format compatibility note: this format is INCOMPATIBLE with the
9-byte header used by MeshCore_Channel_Interface.py.  Both ends must
run MeshCore_Dynamic_Interface.py.


BIND PROTOCOL
=============
Each node periodically broadcasts its MeshCore public key on the tunnel
channel so peers can build their local peer table.

  Raw channel text (firmware prepends name automatically):
      "node_name: RNSBIND:<mc_pubkey_hex>"

Recipients store:  node_name → mc_pubkey_hex
Once a peer's key is known, outgoing data packets switch from channel
broadcast to targeted direct messages using that key.


DELIVERY ACKS
=============
In direct (serial/BLE/TCP) mode meshcore_py fires an async ACK event when
the remote radio confirms receipt of each message.  This interface subscribes
to that event and logs each confirmed delivery so you can see per-fragment
acknowledgment in the debug output.  An unacknowledged fragment (no ACK
within the send timeout) is logged as a warning; RNS-level retransmission
handles reliability above that layer.


TRANSPORT MODES
===============
  transport = serial   — direct serial connection
  transport = ble      — direct BLE connection  (pass ble_name for target device)
  transport = tcp      — direct TCP connection


INSTALLATION
============
1.  pip install meshcore  (append --break-system-packages on RPi without venv)
2.  Copy this file to  ~/.reticulum/interfaces/
3.  Add a config stanza and restart rnsd.


CONFIG STANZA — serial (typical for radionode / phantom-bridge)
===============================================================
  [[MeshCore Dynamic]]
    type             = MeshCore_Dynamic_Interface
    enabled          = yes
    transport        = serial
    port             = /dev/ttyUSB0
    baudrate         = 115200
    channel_idx      = 0
    channel_name     = RNSTunnel
    channel_secret   = c4d2b6c8254e3b11200f57e95dcb1197
    payload_size     = 130
    fragment_delay   = 2.0
    fragment_timeout = 3600
    bind_interval    = 120
    allow_direct     = yes
    debug_level      = info


CONFIG STANZA — BLE
===================
  [[MeshCore Dynamic BLE]]
    type             = MeshCore_Dynamic_Interface
    enabled          = yes
    transport        = ble
    ble_name         = my-meshcore-device
    channel_idx      = 0
    channel_name     = RNSTunnel
    channel_secret   = c4d2b6c8254e3b11200f57e95dcb1197
    payload_size     = 130
    fragment_delay   = 2.0
    bind_interval    = 120
    allow_direct     = yes
    debug_level      = info


CONFIG STANZA — TCP
===================
  [[MeshCore Dynamic TCP]]
    type             = MeshCore_Dynamic_Interface
    enabled          = yes
    transport        = tcp
    host             = 127.0.0.1
    tcp_port         = 4403
    channel_idx      = 0
    channel_name     = RNSTunnel
    channel_secret   = c4d2b6c8254e3b11200f57e95dcb1197
    payload_size     = 130
    fragment_delay   = 2.0
    bind_interval    = 120
    allow_direct     = yes
    debug_level      = info


CONFIG REFERENCE
================
  payload_size       Max binary payload bytes per fragment.  With a 16-char
                     node name and "RNS:" prefix:
                       payload_size=130 → ~198-char channel message  (safe)
                       payload_size=120 → ~184-char channel message  (headroom)
                     Default: 130.

  fragment_delay     Seconds to sleep between successive fragment sends.
                     Prevents radio collisions on rapid multi-fragment packets.
                     Default: 2.0.

  direct_frag_delay  Seconds between fragment sends when routing via direct
                     messages.  Can be shorter than fragment_delay because
                     direct messages are firmware-ACKed and don't compete with
                     broadcast traffic on the channel.  Defaults to fragment_delay
                     if not set.

  fragment_timeout   Seconds before an incomplete assembly is evicted.
                     Default: 3600.

  bind_interval      Seconds between BIND broadcasts.  A new peer will be
                     discovered within one interval.  Default: 120.

  allow_direct       Set to 'no' to disable direct-message routing and always
                     use channel broadcast.  Useful for debugging or if the
                     meshcore_py version doesn't support send_msg.  Default: yes.

  rate_limit         Optional transmit rate cap in bytes/sec.  When non-zero,
                     fragment_delay is extended if needed to stay within budget.
                     Default: 0 (disabled).

  freq / bw / sf / cr
                     Optional radio parameter overrides (all four must be set
                     together).  Leave unset to use radio's current configuration.

  debug_level        'debug' logs every fragment, every BIND event, every ACK,
                     and every routing decision.  'info' logs reassembled packets
                     and peer discoveries only.  Default: info.
"""

import RNS
from RNS.Interfaces.Interface import Interface
import asyncio
import base64
import queue
import threading
import time
from collections import OrderedDict


# ---------------------------------------------------------------------------
# _PacketHandler — fragment one RNS packet into channel-sized pieces
# ---------------------------------------------------------------------------

class _PacketHandler:
    """
    Encodes one RNS binary packet into one or more channel message strings.

    Header layout (3 bytes, big-endian, prepended before base64url encoding):
        frag_idx   [0]  0-based index of this fragment
        pkt_id     [1]  rolling 0-255 transmit counter for this node
        frag_total [2]  total number of fragments for this packet

    The sender is identified externally (node-name firmware prefix on channel
    messages; pubkey metadata from meshcore_py on direct messages) so no
    sender identity bytes are embedded in the header.
    """

    HEADER_SIZE  = 3
    PAYLOAD_SIZE = 130   # default; overridden by config 'payload_size'
    MSG_PREFIX   = "RNS:"

    def __init__(self, data: bytes, pkt_id: int, payload_size: int = 0):
        ps = payload_size if payload_size > 0 else self.PAYLOAD_SIZE
        raw_chunks = [data[i:i + ps] for i in range(0, len(data), ps)]
        total = len(raw_chunks)
        self.fragments = [
            self.MSG_PREFIX + base64.urlsafe_b64encode(
                bytes([idx & 0xFF, pkt_id & 0xFF, total & 0xFF]) + chunk
            ).rstrip(b"=").decode()
            for idx, chunk in enumerate(raw_chunks)
        ]

    def __len__(self):
        return len(self.fragments)


# ---------------------------------------------------------------------------
# MeshCore_Dynamic_Interface
# ---------------------------------------------------------------------------

class MeshCore_Dynamic_Interface(Interface):

    DEFAULT_IFAC_SIZE   = 8
    DEFAULT_IFAC_NAME   = ""
    DEFAULT_IFAC_NETKEY = b""

    MSG_PREFIX       = _PacketHandler.MSG_PREFIX
    BIND_PREFIX      = "RNSBIND:"
    HEADER_SIZE      = _PacketHandler.HEADER_SIZE

    OUTQUEUE_MAXSIZE = 512
    WORKER_POLL_S    = 0.05
    SETUP_TIMEOUT_S  = 30

    # Bits 1-0 of the first RNS packet header byte encode the packet type.
    # (Bit 7 = IFAC flag; bit 6 = header type; bits 5-4 = context;
    #  bits 3-2 = destination type; bits 1-0 = packet type.)
    _RNS_PTYPE_DATA     = 0x00
    _RNS_PTYPE_ANNOUNCE = 0x01
    _RNS_PTYPE_LINK_REQ = 0x02
    _RNS_PTYPE_PROOF    = 0x03

    # -----------------------------------------------------------------------
    # Construction
    # -----------------------------------------------------------------------

    def __init__(self, owner, configuration):
        super().__init__()

        self.owner = owner
        self.name  = configuration.get("name", "MeshCore Dynamic")
        cfg        = configuration

        # Transport
        self.transport = cfg.get("transport", "serial").lower()

        # Connection params
        self.port     = cfg.get("port",     "/dev/ttyUSB0")
        self.baudrate = int(cfg.get("baudrate", 115200))
        self.host     = cfg.get("host",     "127.0.0.1")
        self.tcp_port = int(cfg.get("tcp_port", 4403))
        self.ble_name = cfg.get("ble_name", "")

        # Channel identity
        self.channel_idx        = int(str(cfg.get("channel_idx", 0)).strip())
        self.channel_name       = cfg.get("channel_name",   "RNSTunnel")
        self.channel_secret_hex = cfg.get("channel_secret", "c4d2b6c8254e3b11200f57e95dcb1197")

        # Optional radio overrides (all four must be non-zero to apply)
        self.radio_freq = float(cfg.get("freq", 0))
        self.radio_bw   = float(cfg.get("bw",   0))
        self.radio_sf   = int(cfg.get("sf",     0))
        self.radio_cr   = int(cfg.get("cr",     0))

        # Protocol tuning
        self.payload_size       = int(cfg.get("payload_size",     130))
        self.fragment_delay_s   = float(cfg.get("fragment_delay",   2.0))
        self.fragment_timeout_s = float(cfg.get("fragment_timeout", 3600))
        self.rate_limit_bps     = int(cfg.get("rate_limit",         0))

        # Direct-message fragment delay (can be shorter than channel delay)
        raw_dfd = cfg.get("direct_frag_delay", None)
        self.direct_frag_delay_s = (
            float(raw_dfd) if raw_dfd is not None else self.fragment_delay_s
        )

        # Peer discovery
        self.bind_interval_s = float(cfg.get("bind_interval", 120))
        self.allow_direct    = (
            cfg.get("allow_direct", "yes").lower() not in ("no", "false", "0")
        )

        # Debug verbosity
        self.debug_level = cfg.get("debug_level", "info").lower()

        # Internal asyncio / threading state
        self._mc            = None
        self._EventType     = None
        self._loop          = None
        self._loop_thread   = None
        self._worker_thread = None

        # Our own MeshCore identity (populated from send_appstart SELF_INFO)
        self._own_node_name = ""
        self._own_mc_key    = ""

        # Rolling per-node packet counter
        self._pkt_id      = 0
        self._pkt_id_lock = threading.Lock()

        # Outgoing queue items: (mode, target_or_None, frag_str)
        #   mode="channel"  target=None       → send_chan_msg
        #   mode="direct"   target=pubkey_hex → send_msg
        self._outqueue = queue.Queue(maxsize=self.OUTQUEUE_MAXSIZE)

        # Fragment re-assembly buffers, keyed by (sender_id, pkt_id)
        self._assembly      = {}   # key → {frag_idx: payload_bytes}
        self._assembly_meta = {}   # key → (frag_total, monotonic_ts)
        self._asm_lock      = threading.Lock()

        # Delivered-packet dedup cache
        self._seen_pkts = OrderedDict()
        self._seen_lock = threading.Lock()

        # Peer discovery tables  (all guarded by _peer_lock)
        self._peer_table    = {}   # node_name   → mc_pubkey_hex (full)
        self._reverse_peers = {}   # mc_pubkey_hex → node_name
        self._peer_lock     = threading.Lock()

        # Set True once send_msg is confirmed available
        self._has_direct_api = False

        self._setup_done = threading.Event()

        # Validate and load meshcore library before spawning threads
        self._load_meshcore_or_panic()

        # Dedicated asyncio event loop in its own thread
        self._loop = asyncio.new_event_loop()
        self._loop_thread = threading.Thread(
            target=self._run_loop, daemon=True,
            name=f"MCDyn-loop-{self.name}"
        )
        self._loop_thread.start()

        # Outgoing worker (blocking sends with inter-fragment delay)
        self._worker_thread = threading.Thread(
            target=self._outgoing_worker, daemon=True,
            name=f"MCDyn-worker-{self.name}"
        )
        self._worker_thread.start()

        # Kick off async setup on the event loop.
        # Save the future so we can retrieve any exception that killed the
        # coroutine before it reached _setup_done.set().
        _setup_future = asyncio.run_coroutine_threadsafe(
            self._async_setup(), self._loop
        )

        # If _async_setup raises an unhandled exception the coroutine exits
        # immediately without setting _setup_done, which would cause a 30-second
        # silent hang.  The done-callback fires as soon as the coroutine exits
        # (success or failure) and unblocks the wait below right away.
        def _on_setup_future_done(fut):
            if fut.done() and not fut.cancelled():
                exc = fut.exception()
                if exc is not None:
                    import traceback
                    RNS.log(
                        f"MeshCore_Dynamic_Interface [{self.name}]: "
                        f"setup raised an exception: {exc}",
                        RNS.LOG_ERROR
                    )
                    RNS.log(
                        "".join(traceback.format_exception(
                            type(exc), exc, exc.__traceback__
                        )),
                        RNS.LOG_ERROR
                    )
                    # Unblock the wait so __init__ doesn't hang for 30 s
                    self._setup_done.set()

        _setup_future.add_done_callback(_on_setup_future_done)

        if not self._setup_done.wait(timeout=self.SETUP_TIMEOUT_S):
            RNS.log(
                f"MeshCore_Dynamic_Interface [{self.name}]: "
                "setup timed out — check transport / radio connection",
                RNS.LOG_ERROR
            )
        elif not self.online:
            # _setup_done was set by the error callback, not by successful setup
            RNS.log(
                f"MeshCore_Dynamic_Interface [{self.name}]: "
                "interface did not come online (see errors above)",
                RNS.LOG_ERROR
            )
        else:
            RNS.log(
                f"MeshCore_Dynamic_Interface [{self.name}]: constructed OK",
                RNS.LOG_DEBUG
            )

    # -----------------------------------------------------------------------
    # Library loader
    # -----------------------------------------------------------------------

    def _load_meshcore_or_panic(self):
        try:
            import meshcore as _mc_mod
            self._mc_module = _mc_mod
            self._EventType = _mc_mod.EventType
        except ImportError:
            RNS.log(
                f"MeshCore_Dynamic_Interface [{self.name}]: "
                "meshcore library not found. "
                "Install: pip install meshcore [--break-system-packages]",
                RNS.LOG_CRITICAL
            )
            RNS.panic()

    # -----------------------------------------------------------------------
    # Event loop management
    # -----------------------------------------------------------------------

    def _run_loop(self):
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_forever()
        except Exception as exc:
            RNS.log(
                f"MeshCore_Dynamic_Interface [{self.name}]: "
                f"event loop crashed: {exc}",
                RNS.LOG_ERROR
            )

    def _run_coro(self, coro, timeout: float = 20.0):
        """Schedule a coroutine on the event loop from a synchronous thread."""
        if self._loop is None or not self._loop.is_running():
            return None
        fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            return fut.result(timeout=timeout)
        except asyncio.TimeoutError:
            RNS.log(
                f"MeshCore_Dynamic_Interface [{self.name}]: "
                "coroutine timed out",
                RNS.LOG_WARNING
            )
            return None
        except Exception as exc:
            RNS.log(
                f"MeshCore_Dynamic_Interface [{self.name}]: "
                f"coroutine error: {exc}",
                RNS.LOG_WARNING
            )
            return None

    # -----------------------------------------------------------------------
    # Async setup
    # -----------------------------------------------------------------------

    async def _async_setup(self):
        MeshCore = self._mc_module.MeshCore
        ET       = self._EventType

        # --- Connect to radio ---
        try:
            if self.transport == "serial":
                self._mc = await MeshCore.create_serial(self.port, self.baudrate)
                RNS.log(
                    f"MeshCore_Dynamic_Interface [{self.name}]: "
                    f"connected via serial {self.port}",
                    RNS.LOG_INFO
                )
            elif self.transport == "ble":
                self._mc = await MeshCore.create_ble(self.ble_name or None)
                RNS.log(
                    f"MeshCore_Dynamic_Interface [{self.name}]: "
                    "connected via BLE",
                    RNS.LOG_INFO
                )
            elif self.transport == "tcp":
                self._mc = await MeshCore.create_tcp(self.host, self.tcp_port)
                RNS.log(
                    f"MeshCore_Dynamic_Interface [{self.name}]: "
                    f"connected via TCP {self.host}:{self.tcp_port}",
                    RNS.LOG_INFO
                )
            else:
                RNS.log(
                    f"MeshCore_Dynamic_Interface [{self.name}]: "
                    f"unsupported transport '{self.transport}' "
                    "(valid: serial, ble, tcp)",
                    RNS.LOG_CRITICAL
                )
                return
        except Exception as exc:
            RNS.log(
                f"MeshCore_Dynamic_Interface [{self.name}]: "
                f"connection failed: {exc}",
                RNS.LOG_ERROR
            )
            return

        # --- Get our own identity ---
        try:
            result = await self._mc.commands.send_appstart()
            if result.type == ET.SELF_INFO:
                self._own_node_name = result.payload.get("name", "")
                self._own_mc_key    = result.payload.get("public_key", "")
                RNS.log(
                    f"MeshCore_Dynamic_Interface [{self.name}]: "
                    f"node name='{self._own_node_name}'  "
                    f"key={self._own_mc_key[:16]}...",
                    RNS.LOG_INFO
                )
        except Exception as exc:
            RNS.log(
                f"MeshCore_Dynamic_Interface [{self.name}]: "
                f"send_appstart error: {exc}",
                RNS.LOG_WARNING
            )

        # --- Optional radio parameter override ---
        if self.radio_freq and self.radio_bw and self.radio_sf and self.radio_cr:
            try:
                result = await self._mc.commands.set_radio(
                    self.radio_freq, self.radio_bw,
                    self.radio_sf, self.radio_cr
                )
                if result.type == ET.OK:
                    RNS.log(
                        f"MeshCore_Dynamic_Interface [{self.name}]: "
                        f"radio → freq={self.radio_freq}  bw={self.radio_bw}  "
                        f"sf={self.radio_sf}  cr={self.radio_cr}",
                        RNS.LOG_INFO
                    )
            except Exception as exc:
                RNS.log(
                    f"MeshCore_Dynamic_Interface [{self.name}]: "
                    f"radio config error: {exc}",
                    RNS.LOG_WARNING
                )

        # --- Configure tunnel channel ---
        try:
            secret_bytes = bytes.fromhex(self.channel_secret_hex)
            if len(secret_bytes) != 16:
                raise ValueError(
                    f"channel_secret must be 16 bytes, got {len(secret_bytes)}"
                )
            result = await self._mc.commands.set_channel(
                self.channel_idx, self.channel_name, secret_bytes
            )
            if result.type == ET.OK:
                RNS.log(
                    f"MeshCore_Dynamic_Interface [{self.name}]: "
                    f"channel slot {self.channel_idx} "
                    f"('{self.channel_name}') configured",
                    RNS.LOG_INFO
                )
        except Exception as exc:
            RNS.log(
                f"MeshCore_Dynamic_Interface [{self.name}]: "
                f"channel config error: {exc}",
                RNS.LOG_WARNING
            )

        # --- Probe direct-message API ---
        # meshcore_py exposes send_msg(pubkey, text) for direct (non-channel)
        # messages.  The method name may vary across library versions; we check
        # at setup time and fall back to channel-only if it's absent.
        if self.allow_direct:
            self._has_direct_api = hasattr(self._mc.commands, "send_msg")
            if self._has_direct_api:
                RNS.log(
                    f"MeshCore_Dynamic_Interface [{self.name}]: "
                    "direct message API (send_msg) found — hybrid routing enabled",
                    RNS.LOG_INFO
                )
            else:
                RNS.log(
                    f"MeshCore_Dynamic_Interface [{self.name}]: "
                    "direct message API (send_msg) not found — "
                    "channel-only mode active  "
                    "(set allow_direct = no to suppress this warning)",
                    RNS.LOG_WARNING
                )

        # --- Subscribe to incoming events ---
        # All subscriptions use getattr rather than direct attribute access.
        # EventType names vary across meshcore_py versions; a missing attribute
        # raises AttributeError which would silently kill the setup coroutine.

        # Channel messages — always required.
        self._mc.subscribe(ET.CHANNEL_MSG_RECV, self._on_channel_msg)
        RNS.log(
            f"MeshCore_Dynamic_Interface [{self.name}]: "
            "subscribed to CHANNEL_MSG_RECV",
            RNS.LOG_DEBUG
        )

        # Direct (private) messages — name varies by meshcore_py version.
        _direct_recv_et = None
        for _dm_candidate in ("DIRECT_MSG_RECV", "PRIVATE_MSG_RECV",
                              "MSG_RECV", "PRIV_MSG_RECV"):
            _direct_recv_et = getattr(ET, _dm_candidate, None)
            if _direct_recv_et is not None:
                self._mc.subscribe(_direct_recv_et, self._on_direct_msg)
                RNS.log(
                    f"MeshCore_Dynamic_Interface [{self.name}]: "
                    f"subscribed to direct messages ({_dm_candidate})",
                    RNS.LOG_DEBUG
                )
                break

        if _direct_recv_et is None:
            RNS.log(
                f"MeshCore_Dynamic_Interface [{self.name}]: "
                "no direct-message receive event found in this meshcore_py "
                "version — direct RX disabled, channel RX still active",
                RNS.LOG_WARNING
            )
            # Can't receive direct messages, so sending them would be one-way;
            # fall back to channel-only for both directions.
            self._has_direct_api = False

        # Delivery ACKs — name also varies by version.
        self._ack_event_name = None
        for _ack_candidate in ("ACK", "MSG_ACKED", "MESSAGE_ACKED", "CHAN_ACK"):
            _ack_et = getattr(ET, _ack_candidate, None)
            if _ack_et is not None:
                self._mc.subscribe(_ack_et, self._on_msg_ack)
                self._ack_event_name = _ack_candidate
                RNS.log(
                    f"MeshCore_Dynamic_Interface [{self.name}]: "
                    f"subscribed to delivery ACK events ({_ack_candidate})",
                    RNS.LOG_DEBUG
                )
                break

        if self._ack_event_name is None:
            RNS.log(
                f"MeshCore_Dynamic_Interface [{self.name}]: "
                "no ACK event type found in this meshcore_py version — "
                "delivery confirmations will not be logged",
                RNS.LOG_DEBUG
            )

        await self._mc.start_auto_message_fetching()
        asyncio.create_task(self._cleanup_loop())
        asyncio.create_task(self._bind_broadcast_loop())

        self.online = True
        self._setup_done.set()
        RNS.log(
            f"MeshCore_Dynamic_Interface [{self.name}]: "
            f"online — channel slot {self.channel_idx}  "
            f"node '{self._own_node_name}'  "
            f"payload_size={self.payload_size}  "
            f"direct={'enabled' if self._has_direct_api else 'disabled'}",
            RNS.LOG_INFO
        )

    # -----------------------------------------------------------------------
    # Periodic tasks
    # -----------------------------------------------------------------------

    async def _bind_broadcast_loop(self):
        """
        Broadcast our MeshCore identity on the tunnel channel so peers can
        discover us and route direct messages our way.

        The firmware automatically prepends our node name, so what peers
        receive is:  "our_node_name: RNSBIND:<mc_pubkey_hex>"
        """
        # Let setup settle before first broadcast
        await asyncio.sleep(5)
        while True:
            if self.online and self._own_mc_key:
                bind_msg = f"{self.BIND_PREFIX}{self._own_mc_key}"
                try:
                    await self._mc.commands.send_chan_msg(
                        self.channel_idx, bind_msg
                    )
                    if self.debug_level == "debug":
                        RNS.log(
                            f"MeshCore_Dynamic_Interface [{self.name}]: "
                            f"BIND broadcast sent  key={self._own_mc_key[:16]}...",
                            RNS.LOG_DEBUG
                        )
                except Exception as exc:
                    RNS.log(
                        f"MeshCore_Dynamic_Interface [{self.name}]: "
                        f"BIND broadcast error: {exc}",
                        RNS.LOG_WARNING
                    )
            await asyncio.sleep(self.bind_interval_s)

    async def _cleanup_loop(self):
        """Evict incomplete assembly buffers that have been waiting too long."""
        while True:
            await asyncio.sleep(60)
            deadline = time.monotonic() - self.fragment_timeout_s
            with self._asm_lock:
                stale = [
                    k for k, (_, ts) in self._assembly_meta.items()
                    if ts < deadline
                ]
                for k in stale:
                    del self._assembly[k]
                    del self._assembly_meta[k]
                    RNS.log(
                        f"MeshCore_Dynamic_Interface [{self.name}]: "
                        f"evicted stale assembly key={k}",
                        RNS.LOG_WARNING
                    )

    # -----------------------------------------------------------------------
    # Incoming: channel messages
    # -----------------------------------------------------------------------

    async def _on_channel_msg(self, event):
        payload  = event.payload
        recv_idx = payload.get("channel_idx", "?")
        text     = payload.get("text", "")

        if self.debug_level == "debug":
            RNS.log(
                f"MeshCore_Dynamic_Interface [{self.name}]: "
                f"CHANNEL_MSG_RECV idx={recv_idx}  text={text[:60]}",
                RNS.LOG_DEBUG
            )

        # Route BIND messages and RNS tunnel traffic separately
        rns_idx  = text.find(self.MSG_PREFIX)
        bind_idx = text.find(self.BIND_PREFIX)

        if bind_idx != -1 and (rns_idx == -1 or bind_idx < rns_idx):
            await self._handle_bind(text, bind_idx)
            return

        if rns_idx != -1:
            # Extract the sender name the firmware prepended (if any)
            sender = text[:rns_idx].rstrip(": ") if rns_idx > 0 else ""
            await self._process_tunnel_text(text[rns_idx:], sender)

    # -----------------------------------------------------------------------
    # Incoming: direct messages
    # -----------------------------------------------------------------------

    async def _on_direct_msg(self, event):
        """
        Handle a direct (non-channel) message from a peer.

        meshcore_py exposes the sender's public key in the event payload;
        exact field name varies by library version.  We normalise it to a
        node name (if the peer has already sent a BIND) for consistent
        dedup-key generation with channel traffic.
        """
        payload = event.payload
        # Try several field name variants across meshcore_py versions
        sender_key = (
            payload.get("pubkey_prefix")
            or payload.get("sender_pubkey")
            or payload.get("pubkey")
            or payload.get("from_pubkey")
            or ""
        )
        text = payload.get("text", "")

        if self.debug_level == "debug":
            RNS.log(
                f"MeshCore_Dynamic_Interface [{self.name}]: "
                f"DIRECT_MSG_RECV  src_key={sender_key[:16]}  "
                f"text={text[:60]}",
                RNS.LOG_DEBUG
            )

        if not text.startswith(self.MSG_PREFIX):
            # Not our tunnel traffic (could be a non-RNS direct message)
            return

        # Resolve the pubkey to the stable node name for consistent dedup
        sender_id = self._resolve_sender_key(sender_key)
        await self._process_tunnel_text(text, sender_id)

    # -----------------------------------------------------------------------
    # Delivery ACK handler
    # -----------------------------------------------------------------------

    async def _on_msg_ack(self, event):
        """
        Called by meshcore_py when the remote radio confirms receipt of a
        channel or direct message we sent.

        Payload fields vary by firmware/library version.  We log what we can
        extract and don't rely on any specific field being present.
        """
        if self.debug_level != "debug":
            return

        payload = event.payload if hasattr(event, "payload") else {}
        msg_id  = payload.get("msg_id") or payload.get("id") or "?"
        success = payload.get("success", payload.get("acked", True))
        to_key  = payload.get("pubkey") or payload.get("to_pubkey") or "?"

        with self._peer_lock:
            to_name = self._reverse_peers.get(to_key[:16], to_key[:16])

        if success:
            RNS.log(
                f"MeshCore_Dynamic_Interface [{self.name}]: "
                f"ACK  msg_id={msg_id}  to={to_name}  ✓",
                RNS.LOG_DEBUG
            )
        else:
            RNS.log(
                f"MeshCore_Dynamic_Interface [{self.name}]: "
                f"ACK  msg_id={msg_id}  to={to_name}  ✗ (no delivery confirmation)",
                RNS.LOG_WARNING
            )

    # -----------------------------------------------------------------------
    # BIND handler
    # -----------------------------------------------------------------------

    async def _handle_bind(self, text: str, bind_idx: int):
        """
        Parse a peer BIND advertisement and update the peer table.

        Expected channel text (firmware prepends node name):
            "node_name: RNSBIND:<mc_pubkey_hex>"
        """
        sender_name = text[:bind_idx].rstrip(": ") if bind_idx > 0 else ""
        mc_pubkey   = text[bind_idx + len(self.BIND_PREFIX):].strip()

        if not sender_name or not mc_pubkey:
            if self.debug_level == "debug":
                RNS.log(
                    f"MeshCore_Dynamic_Interface [{self.name}]: "
                    f"BIND drop: missing name or key  text={text[:60]}",
                    RNS.LOG_DEBUG
                )
            return

        # Ignore our own echoed BIND
        if sender_name == self._own_node_name:
            return

        with self._peer_lock:
            existing = self._peer_table.get(sender_name)
            if existing == mc_pubkey:
                if self.debug_level == "debug":
                    RNS.log(
                        f"MeshCore_Dynamic_Interface [{self.name}]: "
                        f"BIND refresh: '{sender_name}' (no change)",
                        RNS.LOG_DEBUG
                    )
                return

            self._peer_table[sender_name]    = mc_pubkey
            self._reverse_peers[mc_pubkey]   = sender_name
            # Index common prefix lengths for partial-key matching from
            # direct-message events that supply only a key prefix
            for pfx_len in (8, 12, 16, 24):
                pfx = mc_pubkey[:pfx_len]
                if pfx:
                    self._reverse_peers[pfx] = sender_name

        RNS.log(
            f"MeshCore_Dynamic_Interface [{self.name}]: "
            f"peer {'updated' if existing else 'discovered'}: "
            f"'{sender_name}'  key={mc_pubkey[:16]}...",
            RNS.LOG_INFO
        )

    def _resolve_sender_key(self, key_str: str) -> str:
        """
        Map a full or partial MeshCore public key to the known node name.
        Returns key_str unchanged if not yet in the peer table.
        """
        if not key_str:
            return key_str
        with self._peer_lock:
            name = self._reverse_peers.get(key_str)
            if name:
                return name
            # Try prefix / suffix matching for partial keys
            for stored_key, stored_name in self._reverse_peers.items():
                if stored_key.startswith(key_str) or key_str.startswith(stored_key):
                    return stored_name
        return key_str

    # -----------------------------------------------------------------------
    # Shared incoming reassembly pipeline
    # -----------------------------------------------------------------------

    async def _process_tunnel_text(self, text: str, sender: str = ""):
        """
        Decode one tunnel fragment, run it through the reassembly buffer,
        and deliver completed packets to RNS via processIncoming().

        text   — starts with MSG_PREFIX ("RNS:")
        sender — stable sender identifier (node name or pubkey string)
        """
        # Echo suppression: drop anything we transmitted ourselves
        if sender and sender == self._own_node_name:
            if self.debug_level == "debug":
                RNS.log(
                    f"MeshCore_Dynamic_Interface [{self.name}]: "
                    f"drop: own echo  sender={sender!r}",
                    RNS.LOG_DEBUG
                )
            return

        # --- Base64 decode ---
        b64 = text[len(self.MSG_PREFIX):].strip()
        b64 += "=" * (-len(b64) % 4)

        try:
            raw = base64.urlsafe_b64decode(b64)
        except Exception as exc:
            RNS.log(
                f"MeshCore_Dynamic_Interface [{self.name}]: "
                f"base64 decode error: {exc}  "
                f"len={len(b64)} mod4={len(b64) % 4}  text={text[:80]}",
                RNS.LOG_WARNING
            )
            return

        if len(raw) < self.HEADER_SIZE:
            RNS.log(
                f"MeshCore_Dynamic_Interface [{self.name}]: "
                f"drop: frame too short ({len(raw)} < {self.HEADER_SIZE})",
                RNS.LOG_WARNING
            )
            return

        # --- Parse header ---
        frag_idx   = raw[0]
        pkt_id     = raw[1]
        frag_total = raw[2]
        payload    = raw[self.HEADER_SIZE:]

        if frag_total == 0:
            RNS.log(
                f"MeshCore_Dynamic_Interface [{self.name}]: "
                "drop: frag_total=0",
                RNS.LOG_WARNING
            )
            return

        if frag_idx >= frag_total:
            RNS.log(
                f"MeshCore_Dynamic_Interface [{self.name}]: "
                f"drop: bad frag_idx {frag_idx}/{frag_total}",
                RNS.LOG_WARNING
            )
            return

        key = (sender, pkt_id)

        # --- Dedup: have we already delivered this (sender, pkt_id) pair? ---
        with self._seen_lock:
            cache_hit = key in self._seen_pkts
            if cache_hit:
                self._seen_pkts.move_to_end(key)

        if self.debug_level == "debug":
            RNS.log(
                f"MeshCore_Dynamic_Interface [{self.name}]: "
                f"dedupe  src={sender!r}  pkt={pkt_id}  "
                f"frag={frag_idx+1}/{frag_total}  cache_hit={cache_hit}",
                RNS.LOG_DEBUG
            )

        if cache_hit:
            return

        RNS.log(
            f"MeshCore_Dynamic_Interface [{self.name}]: "
            f"RX frag  src={sender!r}  pkt={pkt_id}  "
            f"{frag_idx+1}/{frag_total}  payload={len(payload)}B",
            RNS.LOG_DEBUG
        )

        # --- Reassembly ---
        with self._asm_lock:
            if key not in self._assembly:
                self._assembly[key]      = {}
                self._assembly_meta[key] = (frag_total, time.monotonic())

            if frag_idx in self._assembly[key]:
                return  # duplicate fragment within this packet

            self._assembly[key][frag_idx] = payload

            expected = self._assembly_meta[key][0]
            if len(self._assembly[key]) < expected:
                return  # still waiting for more fragments

            missing = [i for i in range(expected) if i not in self._assembly[key]]
            if missing:
                RNS.log(
                    f"MeshCore_Dynamic_Interface [{self.name}]: "
                    f"reassembly count mismatch  missing={missing}",
                    RNS.LOG_WARNING
                )
                return

            try:
                full_packet = b"".join(
                    self._assembly[key][i] for i in range(expected)
                )
                del self._assembly[key]
                del self._assembly_meta[key]
            except Exception as exc:
                RNS.log(
                    f"MeshCore_Dynamic_Interface [{self.name}]: "
                    f"reassembly join error: {exc}",
                    RNS.LOG_ERROR
                )
                self._assembly.pop(key, None)
                self._assembly_meta.pop(key, None)
                return

            RNS.log(
                f"MeshCore_Dynamic_Interface [{self.name}]: "
                f"reassembly complete  src={sender!r}  pkt={pkt_id}  "
                f"len={len(full_packet)}",
                RNS.LOG_DEBUG
            )

        # Mark delivered BEFORE calling processIncoming to prevent a race
        # if the resulting outbound traffic loops back quickly
        with self._seen_lock:
            self._seen_pkts[key] = time.monotonic()
            if len(self._seen_pkts) > 512:
                while len(self._seen_pkts) > 256:
                    self._seen_pkts.popitem(last=False)

        if len(full_packet) == 0:
            RNS.log(
                f"MeshCore_Dynamic_Interface [{self.name}]: "
                "drop: empty reassembled packet",
                RNS.LOG_WARNING
            )
            return

        RNS.log(
            f"MeshCore_Dynamic_Interface [{self.name}]: "
            f"RX reassembled {len(full_packet)}B from src={sender!r}",
            RNS.LOG_INFO
        )
        RNS.log(
            f"MeshCore_Dynamic_Interface [{self.name}]: "
            f"reassembled len={len(full_packet)} from {expected} fragments",
            RNS.LOG_DEBUG
        )

        try:
            self.processIncoming(full_packet)
            RNS.log(
                f"MeshCore_Dynamic_Interface [{self.name}]: "
                f"delivered  src={sender!r}  pkt={pkt_id}  len={len(full_packet)}",
                RNS.LOG_INFO
            )
        except Exception as exc:
            RNS.log(
                f"MeshCore_Dynamic_Interface [{self.name}]: "
                f"processIncoming error: {exc}",
                RNS.LOG_ERROR
            )

    # -----------------------------------------------------------------------
    # Outgoing path
    # -----------------------------------------------------------------------

    def _is_broadcast_packet(self, data: bytes) -> bool:
        """
        Return True if this RNS packet must be sent via channel broadcast.

        RNS Announce packets must reach all peers (every node needs to learn
        the route), so they always go to channel regardless of peer table state.
        Everything else (data, proofs, link requests) is directed.

        Conservative fallback: packets with IFAC flag set (bit 7) are treated
        as broadcast since we don't attempt to parse the extended header.
        """
        if len(data) < 1:
            return True
        flags = data[0]
        if flags & 0x80:                          # IFAC flag — don't guess
            return True
        return (flags & 0x03) == self._RNS_PTYPE_ANNOUNCE

    def process_outgoing(self, data):
        return self.processOutgoing(data)

    def processOutgoing(self, data):
        if not self.online:
            return

        with self._pkt_id_lock:
            pkt_id       = self._pkt_id
            self._pkt_id = (self._pkt_id + 1) & 0xFF

        handler = _PacketHandler(data, pkt_id, self.payload_size)

        broadcast = self._is_broadcast_packet(data)

        # Decide routing targets
        if broadcast or not self._has_direct_api:
            targets   = [("channel", None)]
            route_log = "channel (announce)" if broadcast else "channel (no direct API)"
        else:
            with self._peer_lock:
                peers = list(self._peer_table.items())   # snapshot
            if peers:
                targets   = [("direct", pubkey) for _, pubkey in peers]
                names     = [n for n, _ in peers]
                route_log = f"direct → {names}"
            else:
                targets   = [("channel", None)]
                route_log = "channel (no peers yet)"

        RNS.log(
            f"MeshCore_Dynamic_Interface [{self.name}]: "
            f"TX {len(data)}B → {len(handler)} frag(s)  "
            f"pkt_id={pkt_id}  {route_log}",
            RNS.LOG_DEBUG
        )

        for frag_str in handler.fragments:
            for mode, target in targets:
                try:
                    self._outqueue.put_nowait((mode, target, frag_str))
                except queue.Full:
                    RNS.log(
                        f"MeshCore_Dynamic_Interface [{self.name}]: "
                        "outgoing queue full — packet dropped",
                        RNS.LOG_WARNING
                    )
                    return

        self.txb += len(data)

    # -----------------------------------------------------------------------
    # Outgoing worker
    # -----------------------------------------------------------------------

    def _outgoing_worker(self):
        """
        Blocking thread that drains the outgoing queue one fragment at a time,
        sleeping fragment_delay_s (or direct_frag_delay_s) between sends to
        prevent radio collisions on multi-fragment packets.
        """
        ET = None

        while True:
            try:
                item = self._outqueue.get(timeout=self.WORKER_POLL_S)
            except queue.Empty:
                continue

            if not self.online or self._mc is None:
                # Not ready yet — put back and wait
                try:
                    self._outqueue.put_nowait(item)
                except queue.Full:
                    pass
                time.sleep(0.5)
                continue

            if ET is None:
                ET = self._EventType

            mode, target, frag_str = item

            # --- Send ---
            if mode == "direct":
                result = self._run_coro(
                    self._mc.commands.send_msg(target, frag_str)
                )
            else:
                result = self._run_coro(
                    self._mc.commands.send_chan_msg(self.channel_idx, frag_str)
                )

            # --- Log result ---
            if result is None:
                RNS.log(
                    f"MeshCore_Dynamic_Interface [{self.name}]: "
                    f"send timed out  mode={mode}  "
                    f"target={str(target)[:16] if target else 'channel'}",
                    RNS.LOG_WARNING
                )
            else:
                # result.type may be ET.OK (queued), ET.ACK (delivered+acked),
                # or ET.ERROR.  Log all three distinctly.
                rt = getattr(result, "type", None)
                ok_type  = getattr(ET, "OK",    None)
                ack_type = getattr(ET, "ACK",   None)
                err_type = getattr(ET, "ERROR", None)

                if rt == err_type:
                    RNS.log(
                        f"MeshCore_Dynamic_Interface [{self.name}]: "
                        f"send error  mode={mode}  payload={result.payload}",
                        RNS.LOG_WARNING
                    )
                elif ack_type is not None and rt == ack_type:
                    # Radio returned an inline ACK — remote confirmed receipt
                    if self.debug_level == "debug":
                        RNS.log(
                            f"MeshCore_Dynamic_Interface [{self.name}]: "
                            f"TX frag → {mode} ACK'd ✓",
                            RNS.LOG_DEBUG
                        )
                elif rt == ok_type or rt is not None:
                    if self.debug_level == "debug":
                        RNS.log(
                            f"MeshCore_Dynamic_Interface [{self.name}]: "
                            f"TX frag → {mode} queued OK",
                            RNS.LOG_DEBUG
                        )

            # --- Inter-fragment delay ---
            delay = (
                self.direct_frag_delay_s
                if mode == "direct"
                else self.fragment_delay_s
            )
            if self.rate_limit_bps > 0:
                bits = (len(frag_str) * 3 // 4) * 8
                delay = max(delay, bits / self.rate_limit_bps)
            time.sleep(delay)

    # -----------------------------------------------------------------------
    # RNS interface bookkeeping
    # -----------------------------------------------------------------------

    def processIncoming(self, data: bytes):
        # RNS 1.x Interface base class has no processIncoming method.
        # Correct pattern (per TCPClientInterface and all first-party interfaces):
        # update rxb, then call owner.inbound() directly.
        # super() does not work in exec()'d files under Python 3.13.
        if self.online and not self.detached:
            self.rxb += len(data)
            self.owner.inbound(data, self)

    def __str__(self):
        return f"MeshCore_Dynamic_Interface[{self.name}]"


# RNS's _synthesize_interface loader looks for this name in exec()'d globals
interface_class = MeshCore_Dynamic_Interface
