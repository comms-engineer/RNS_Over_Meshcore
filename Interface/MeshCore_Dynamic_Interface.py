"""
MeshCore_Dynamic_Interface.py
Reticulum (RNS) interface over a MeshCore LoRa mesh network.

Implements a hybrid channel-broadcast / unicast-direct routing strategy with
demand-driven peer discovery and edge-node capability advertisement.  No static
remote-node configuration is required.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
WIRE FORMAT
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Each RNS binary packet is split into payload-sized chunks.  Each chunk is
encoded as a MeshCore channel (or direct) message:

    "RNS:" + base64url( [frag_idx:1][pkt_id:4][frag_total:1] + payload )

No base64 padding is transmitted; the receiver restores it before decode.

RNS HEADER BYTE BIT LAYOUT (single-header packet, bit 7 = 0)
  bits 7-6 : header type     (0b10 = two-byte header; always broadcast)
  bits 5-4 : transport flags
  bits 3-2 : destination type  <- extracted with (flags >> 2) & 0x03
  bits 1-0 : packet type       <- extracted with  flags       & 0x03

  Packet type values:  DATA=0x00  ANNOUNCE=0x01  LINKREQUEST=0x02  PROOF=0x03
  Dest type values:    SINGLE=0x00  GROUP=0x01  PLAIN=0x02  LINK=0x03

  A DATA packet with PLAIN destination (header byte 0x08) is a PATH REQUEST —
  a node searching for a destination it has lost the path to.  AP mode does
  NOT suppress path requests; it only blocks ANNOUNCE re-broadcasting.  If a
  node that was recently reachable goes offline, remote nodes will generate a
  continuous stream of path requests that will pass straight through AP mode
  and onto the LoRa channel.  The outgoing_path_req_rate limiter handles this.

PAYLOAD SIZE
  MeshCore firmware silently truncates channel messages that exceed a hardware-
  dependent character limit (observed ~128 chars on common firmware builds).
  The firmware also prepends the sender's node name when relaying channel
  messages, so the effective character budget for the encoded portion is:

      budget = firmware_limit - len(node_name) - 2       (": " separator)

  Encoded message length:
      msg_len = ceil((payload_size + HEADER_SIZE) * 4/3) + len("RNS:")

  With a 4-byte pkt_id, HEADER_SIZE is 6 bytes. With default payload_size = 64:
      msg_len = ceil(70 * 4/3) + 4 = 94 + 4 = 98 chars
      Safe for node names up to ~28 characters at a 128-char firmware limit.

  To calculate the maximum safe payload size for your node name length:
      budget      = firmware_limit - len(node_name) - 2
      max_payload = floor((budget - 4) * 3/4) - HEADER_SIZE

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
PEER DISCOVERY PROTOCOL
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Peer discovery uses a demand-driven RNSBIND_REQ / RNSBIND exchange rather than
periodic push-based broadcasting, minimising channel airtime consumption.

  1. A node with no known peers sends "RNSBIND_REQ:<pubkey>:<cap>" on the
     channel, advertising its own routing capability alongside its identity.
  2. Each overhearing node immediately records the requester's info and
     capability (passive L2 learning), waits a random delay (BIND_BACKOFF_MIN
     to BIND_BACKOFF_MAX seconds), then responds with "RNSBIND:<pubkey>:<cap>".
  3. The random backoff follows the RFC 2236 (IGMP) report suppression
     principle: responses are spread in time to prevent a simultaneous burst
     on the shared half-duplex LoRa channel.
  4. Every node overhearing any RNSBIND response also records the responder,
     so a single discovery round passively populates all peer tables.
  5. Once peers are known, a quiet RNSBIND heartbeat is sent every
     BIND_HEARTBEAT_S (default 1 hour) — no response is solicited.

CAPABILITY FIELD
  The capability suffix ("R" = router, "E" = edge) is appended to every RNSBIND
  and RNSBIND_REQ message so that peers learn at discovery time whether a node
  can carry transit traffic to the wider Reticulum mesh.

      RNSBIND:<pubkey>:R    — routing node (has upstream connectivity)
      RNSBIND:<pubkey>:E    — edge node (no upstream; do not route through me)
      RNSBIND:<pubkey>      — legacy format (no capability field); treated as :R

  The capability field operates at the discovery layer only.  It is recorded in
  the peer table and logged, but it does NOT affect per-packet routing decisions.
  The _rns_to_mc_map is populated by observed packet flow, so any entry in it
  represents a path that has demonstrably worked — including paths that transit
  through an edge node to reach a client device behind it (e.g. a phone
  connected to a hotspot hosted by the edge node).  Filtering those map entries
  by capability would incorrectly block delivery to legitimate downstream clients.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
RNS INTERFACE MODE CONFIGURATION
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Interface modes have a significant impact on announce traffic, path expiry,
and channel load.  The modes below are sourced from the official Reticulum
manual (https://reticulum.network/manual/interfaces.html).

INFRASTRUCTURE / TRANSPORT NODE  (fixed gateway with backbone connectivity)
──────────────────────────────────────────────────────────────────────────────
  [[MeshCore Dynamic Interface]]   mode = access_point    can_route = yes
  [[Backbone Interface]]           mode = boundary

  access_point
    Announces are NOT automatically re-broadcast on this interface.  Paths to
    destinations on the interface expire faster, matching the transient nature
    of battery-powered or intermittently-connected field devices.  Path requests
    from clients are still forwarded and resolved on their behalf, as with
    gateway mode.

    NOTE: AP mode only suppresses ANNOUNCE re-broadcasting.  DATA+PLAIN path
    requests from the wider mesh for recently-offline nodes will still pass
    through AP mode onto the LoRa channel.  Use outgoing_path_req_rate to
    throttle these independently.

    !! NEVER use gateway mode on a LoRa interface on a node that is also
    !! connected to a high-connectivity backbone.  gateway mode proactively
    !! pushes ALL known announces to clients on that interface.  With thousands
    !! of routes from the public Reticulum mesh, this will flood a shared LoRa
    !! channel indefinitely and render it unusable.

  boundary
    Applied to the backbone/TCP interface connecting the slow radio segment to
    the fast LAN or Internet.  Marks the network edge and prevents the transport
    node from treating the backbone as a client-facing interface for proactive
    path distribution.

  Add announce rate control to the backbone interface to throttle how quickly
  announces from the wider network are re-propagated to other interfaces:

      announce_rate_target  = 3600   # min seconds between re-announces per dest
      announce_rate_grace   = 2      # violations tolerated before enforcement
      announce_rate_penalty = 7200   # extended quiet period after a violation

  Full example — infrastructure / transport node
  ┌─────────────────────────────────────────────────────────────────────────┐
  │ [reticulum]                                                             │
  │   enable_transport = yes                                                │
  │   share_instance = yes                                                  │
  │                                                                         │
  │ [logging]                                                               │
  │   loglevel = 4    # increase to 7 for debug                            │
  │                                                                         │
  │ [interfaces]                                                            │
  │                                                                         │
  │   [[MeshCore Dynamic Interface]]                                        │
  │     type = MeshCore_Dynamic_Interface                                   │
  │     interface_enabled = yes                                             │
  │                                                                         │
  │     # Role                                                              │
  │     mode = access_point                                                 │
  │     can_route = yes                                                     │
  │                                                                         │
  │     # Transport — uncomment exactly one block                          │
  │     # Serial (most common):                                             │
  │     transport = serial                                                  │
  │     port = /dev/ttyUSB0    # adjust to your serial device              │
  │     baudrate = 115200                                                   │
  │     #                                                                   │
  │     # TCP (MeshCore node reachable over IP):                           │
  │     # transport = tcp                                                   │
  │     # host = 127.0.0.1                                                 │
  │     # tcp_port = 4403                                                   │
  │     #                                                                   │
  │     # BLE:                                                              │
  │     # transport = ble                                                   │
  │     # ble_name =           # blank = connect to first found device     │
  │                                                                         │
  │     # Channel — all nodes on the same tunnel must share these values   │
  │     channel_idx = 0                                                     │
  │     channel_name = RNSTunnel                                            │
  │     channel_secret = <32 hex chars>  # openssl rand -hex 16           │
  │                                                                         │
  │     # Radio overrides — all four must be non-zero to take effect.      │
  │     # Leave commented to use the values stored on the MeshCore node.   │
  │     # freq = 915.0         # MHz centre frequency                      │
  │     # bw   = 250.0         # kHz bandwidth  (125 / 250 / 500)         │
  │     # sf   = 10            # spreading factor (7–12)                   │
  │     # cr   = 5             # coding rate denominator (5=4/5 … 8=4/8)  │
  │                                                                         │
  │     # Fragmentation                                                     │
  │     payload_size = 64      # bytes/fragment (see PAYLOAD SIZE note)    │
  │     fragment_delay = 2.5   # seconds between channel-mode fragments    │
  │     direct_frag_delay = 0.5  # seconds between direct-message frags   │
  │     fragment_timeout = 300   # 5-minute window for high-latency meshes │
  │                                                                         │
  │     # Outgoing rate limiting (set to 0 to disable)                     │
  │     outgoing_announce_rate = 600    # min s between announces per dest │
  │     outgoing_path_req_rate = 1800   # min s between path reqs per dest │
  │     path_req_burst_window = 60      # s to let RNS's own retry burst   │
  │                                      # through before the cooldown     │
  │                                      # above applies (see code comment)│
  │                                                                         │
  │     # Optional hard bandwidth cap in bits per second (0 = disabled)    │
  │     # rate_limit = 1200                                                 │
  │                                                                         │
  │     # Peer discovery                                                    │
  │     allow_direct = yes      # use unicast direct msgs when route known │
  │     peer_ttl = 86400        # seconds before a silent peer expires     │
  │                                                                         │
  │     debug_level = info      # info | debug                             │
  │                                                                         │
  │   [[Backbone Interface]]                                                │
  │     type = BackboneInterface                                            │
  │     interface_enabled = yes                                             │
  │     mode = boundary                                                     │
  │     target_host = <backbone-server-hostname-or-ip>                     │
  │     target_port = 4242                                                  │
  │     # Rate-limit announce re-propagation from the fast network         │
  │     announce_rate_target  = 3600                                        │
  │     announce_rate_grace   = 2                                           │
  │     announce_rate_penalty = 7200                                        │
  └─────────────────────────────────────────────────────────────────────────┘
"""

import RNS
from RNS.Interfaces.Interface import Interface
import asyncio
import base64
import hashlib
import struct
import queue
import random
import threading
import time


# ─────────────────────────────────────────────────────────────────────────────
# Fragmentation helper
# ─────────────────────────────────────────────────────────────────────────────

class _PacketHandler:
    """
    Encodes one RNS binary packet into one or more channel/direct message
    strings. Each fragment carries a 6-byte binary header:

        [ frag_idx : 1 byte ] [ pkt_id : 4 bytes ] [ frag_total : 1 byte ]

    followed by the raw payload chunk.  The combined bytes are base64url-
    encoded (no padding) and prefixed with MSG_PREFIX ("RNS:").
    """

    HEADER_SIZE  = 6  # 1 byte idx + 4 bytes pkt_id + 1 byte total
    PAYLOAD_SIZE = 64
    MSG_PREFIX   = "RNS:"

    def __init__(self, data: bytes, pkt_id: int, payload_size: int = 0):
        ps = payload_size if payload_size > 0 else self.PAYLOAD_SIZE
        raw_chunks = [data[i:i + ps] for i in range(0, len(data), ps)]
        total = len(raw_chunks)
        
        self.fragments = []
        for idx, chunk in enumerate(raw_chunks):
            # Header layout packed big-endian: B (1B index), I (4B packet ID), B (1B total fragments)
            header = struct.pack(">BIB", idx & 0xFF, pkt_id & 0xFFFFFFFF, total & 0xFF)
            encoded = base64.urlsafe_b64encode(header + chunk).rstrip(b"=").decode()
            self.fragments.append(self.MSG_PREFIX + encoded)

    def __len__(self):
        return len(self.fragments)


# ─────────────────────────────────────────────────────────────────────────────
# Interface
# ─────────────────────────────────────────────────────────────────────────────

class MeshCore_Dynamic_Interface(Interface):

    # -------------------------------------------------------------------------
    # Class-level constants
    # -------------------------------------------------------------------------

    DEFAULT_IFAC_SIZE   = 8
    DEFAULT_IFAC_NAME   = ""
    DEFAULT_IFAC_NETKEY = b""

    MSG_PREFIX      = _PacketHandler.MSG_PREFIX

    BIND_PREFIX     = "RNSBIND:"
    BIND_REQ_PREFIX = "RNSBIND_REQ:"

    CAPABILITY_ROUTER = "R"
    CAPABILITY_EDGE   = "E"

    HEADER_SIZE      = _PacketHandler.HEADER_SIZE
    OUTQUEUE_MAXSIZE = 512
    SETUP_TIMEOUT_S  = 30

    BIND_BACKOFF_MIN   =  3.0
    BIND_BACKOFF_MAX   = 15.0
    BIND_HEARTBEAT_S   = 3600.0
    BIND_RESP_WINDOW_S = 60.0
    BIND_MAX_RETRIES   = 3

    _RNS_DST_LEN = 16
    _RNS_PTYPE_DATA     = 0x00
    _RNS_PTYPE_ANNOUNCE = 0x01
    _RNS_PTYPE_LINK_REQ = 0x02
    _RNS_PTYPE_PROOF    = 0x03
    
    _RNS_DTYPE_SINGLE = 0x00
    _RNS_DTYPE_GROUP  = 0x01
    _RNS_DTYPE_PLAIN  = 0x02
    _RNS_DTYPE_LINK   = 0x03

    _RNS_MAP_MAX = 512
    
    # Time window (seconds) to block processing duplicate packets that were fully reassembled.
    DEDUPLICATION_TTL_S = 30.0

    # -------------------------------------------------------------------------
    # Constructor
    # -------------------------------------------------------------------------

    def __init__(self, owner, configuration):
        super().__init__()

        self.owner = owner
        self.name  = configuration.get("name", "MeshCore Dynamic")
        cfg        = configuration

        # --- Transport selection -------------------------------------------
        self.transport = cfg.get("transport", "serial").lower()

        # --- Connection parameters -----------------------------------------
        self.port     = cfg.get("port",     "/dev/ttyUSB0")
        self.baudrate = int(cfg.get("baudrate", 115200))
        self.host     = cfg.get("host",     "127.0.0.1")
        self.tcp_port = int(cfg.get("tcp_port", 4403))
        self.ble_name = cfg.get("ble_name", "")

        # --- Channel identity ----------------------------------------------
        self.channel_idx        = int(str(cfg.get("channel_idx", 0)).strip())
        self.channel_name       = cfg.get("channel_name", "RNSTunnel")
        self.channel_secret_hex = cfg.get("channel_secret", "")

        # Validate channel_secret: refuse to start with a missing or insecure
        # default.  A known/all-zeros key means traffic is effectively
        # unencrypted — any eavesdropper can decode and inject packets.
        _INSECURE_DEFAULTS = (
            "",
            "00000000000000000000000000000000",
        )
        if self.channel_secret_hex.lower().strip() in _INSECURE_DEFAULTS:
            RNS.log(
                f"MeshCore_Dynamic_Interface [{self.name}]: CRITICAL — "
                f"'channel_secret' is missing or set to an insecure default. "
                f"All nodes sharing this interface MUST use a unique secret. "
                f"Generate one with: openssl rand -hex 16",
                RNS.LOG_CRITICAL
            )
            raise ValueError(
                f"MeshCore_Dynamic_Interface [{self.name}]: "
                "channel_secret is missing or insecure — refusing to start. "
                "Set a unique 32-character hex secret (openssl rand -hex 16)."
            )

        # Validate format: must be valid hex and exactly 16 bytes (32 hex chars)
        try:
            _secret_bytes = bytes.fromhex(self.channel_secret_hex)
        except ValueError:
            raise ValueError(
                f"MeshCore_Dynamic_Interface [{self.name}]: "
                f"channel_secret is not valid hexadecimal: "
                f"'{self.channel_secret_hex[:8]}...'"
            )
        if len(_secret_bytes) != 16:
            raise ValueError(
                f"MeshCore_Dynamic_Interface [{self.name}]: "
                f"channel_secret must be exactly 16 bytes (32 hex chars), "
                f"got {len(_secret_bytes)} bytes."
            )

        # --- Optional radio parameter overrides ----------------------------
        self.radio_freq = float(cfg.get("freq", 0))
        self.radio_bw   = float(cfg.get("bw",   0))
        self.radio_sf   = int(cfg.get("sf",     0))
        self.radio_cr   = int(cfg.get("cr",     0))

        # --- Protocol tuning -----------------------------------------------
        self.payload_size = int(cfg.get("payload_size", 64))
        self.fragment_delay_s = float(cfg.get("fragment_delay", 2.5))

        raw_dfd = cfg.get("direct_frag_delay", None)
        self.direct_frag_delay_s = float(raw_dfd) if raw_dfd is not None else 0.5

        # Minimum time to wait for a delivery ACK on a DIRECT send before
        # treating it as failed and falling back to CHANNEL. The radio also
        # hands back its own per-send "suggested_timeout" (based on path
        # length/airtime); we wait whichever of the two is longer.
        self.direct_ack_timeout_s = float(cfg.get("direct_ack_timeout", 4.0))

        # Hard ceiling on that wait, regardless of what the firmware suggests.
        # A contact with no known path (flood mode) can report a suggested
        # timeout of many seconds to minutes; since the outgoing worker is a
        # single shared queue, waiting that long would stall every other
        # queued fragment behind it. Our own CHANNEL fallback is cheap, so we
        # cap the wait and let the fallback handle it instead.
        self.direct_ack_timeout_max_s = float(cfg.get("direct_ack_timeout_max", 8.0))

        # Default adjusted to 300s (5 minutes) for high-latency meshes
        self.fragment_timeout_s = float(cfg.get("fragment_timeout", 300.0))
        self.rate_limit_bps     = int(cfg.get("rate_limit", 0))

        self._announce_rate_s = float(cfg.get("outgoing_announce_rate", 600))
        self._path_req_rate_s = float(cfg.get("outgoing_path_req_rate", 1800))

        # RNS's own outbound retry logic fires a burst of path requests to the
        # same destination roughly 4-10s apart (typically ~4 attempts over
        # ~50s) before giving up. If outgoing_path_req_rate suppresses all but
        # the first of those, a single lost broadcast (common on lossy LoRa)
        # means the whole burst fails with no recovery for the full cooldown
        # period. path_req_burst_window lets RNS's own retry burst through
        # unthrottled; outgoing_path_req_rate only takes effect once the
        # burst window has elapsed, to stop genuine long-run spam.
        self._path_req_burst_window_s = float(cfg.get("path_req_burst_window", 60))

        # --- Routing capability --------------------------------------------
        self.can_route = (
            cfg.get("can_route", "yes").lower() not in ("no", "false", "0")
        )

        self.allow_direct = (
            cfg.get("allow_direct", "yes").lower() not in ("no", "false", "0")
        )

        self.peer_ttl_s = float(cfg.get("peer_ttl", 86400))
        self.bitrate = int(cfg.get("bitrate", 300))
        self.debug_level = cfg.get("debug_level", "info").lower()

        # --- Internal async / threading state ------------------------------
        self._mc          = None
        self._EventType   = None
        self._loop        = None
        self._loop_thread = None
        
        # Thread-safe queue used to decouple synchronous execution from the worker loop
        self._outqueue    = queue.Queue(maxsize=self.OUTQUEUE_MAXSIZE)

        self._own_node_name = ""
        self._own_mc_key    = ""

        self._pkt_id      = 0
        self._pkt_id_lock = threading.Lock()

        self._assembly      = {}   
        self._assembly_meta = {}   
        self._asm_lock      = threading.Lock()

        # Sliding time-window cache: (sender, pkt_id) -> expiration_monotonic_timestamp
        self._seen_pkts = {}  
        self._seen_lock = threading.Lock()

        self._peer_table     = {}
        self._reverse_peers  = {}
        self._peer_last_seen = {}
        self._peer_caps      = {}
        self._rns_to_mc_map  = {}
        self._peer_lock      = threading.Lock()

        self._announce_sent_times = {}   
        self._announce_sent_lock  = threading.Lock()
        self._path_req_sent_times = {}   
        self._path_req_sent_lock  = threading.Lock()

        self._has_direct_api    = False  
        self._pending_resp_task = None  

        self._setup_done = threading.Event()
        self._load_meshcore_or_panic()

        self._loop = asyncio.new_event_loop()
        self._loop_thread = threading.Thread(
            target=self._run_loop, daemon=True,
            name=f"MCDyn-loop-{self.name}"
        )
        self._loop_thread.start()

        _setup_future = asyncio.run_coroutine_threadsafe(
            self._async_setup(), self._loop
        )

        def _on_setup_done(fut):
            if fut.done() and not fut.cancelled():
                exc = fut.exception()
                if exc is not None:
                    import traceback
                    RNS.log(
                        f"MeshCore_Dynamic_Interface [{self.name}]: "
                        f"Setup exception: {exc}", RNS.LOG_ERROR
                    )
                    RNS.log(
                        "".join(traceback.format_exception(
                            type(exc), exc, exc.__traceback__
                        )),
                        RNS.LOG_ERROR
                    )
                    self._setup_done.set()

        _setup_future.add_done_callback(_on_setup_done)

        if not self._setup_done.wait(timeout=self.SETUP_TIMEOUT_S):
            RNS.log(
                f"MeshCore_Dynamic_Interface [{self.name}]: Setup timed out.",
                RNS.LOG_ERROR
            )
        elif not self.online:
            RNS.log(
                f"MeshCore_Dynamic_Interface [{self.name}]: Initialization failed.",
                RNS.LOG_ERROR
            )

    # -------------------------------------------------------------------------
    # Startup helpers
    # -------------------------------------------------------------------------

    def _load_meshcore_or_panic(self):
        try:
            import meshcore as _mc_mod
            self._mc_module = _mc_mod
            self._EventType = _mc_mod.EventType
        except ImportError:
            RNS.log(
                f"MeshCore_Dynamic_Interface [{self.name}]: "
                f"meshcore library not found — cannot continue.",
                RNS.LOG_CRITICAL
            )
            self.owner.panic()

    def _run_loop(self):
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_forever()
        except Exception as exc:
            RNS.log(
                f"MeshCore_Dynamic_Interface [{self.name}]: Loop crashed: {exc}",
                RNS.LOG_ERROR
            )

    async def _async_setup(self):
        MeshCore = self._mc_module.MeshCore
        ET       = self._EventType

        try:
            if self.transport == "serial":
                self._mc = await MeshCore.create_serial(self.port, self.baudrate)
            elif self.transport == "ble":
                self._mc = await MeshCore.create_ble(self.ble_name or None)
            elif self.transport == "tcp":
                self._mc = await MeshCore.create_tcp(self.host, self.tcp_port)
            else:
                RNS.log(
                    f"MeshCore_Dynamic_Interface [{self.name}]: "
                    f"Unknown transport '{self.transport}'.", RNS.LOG_ERROR
                )
                return
        except Exception as exc:
            RNS.log(
                f"MeshCore_Dynamic_Interface [{self.name}]: "
                f"Driver init error: {exc}", RNS.LOG_ERROR
            )
            return

        try:
            result = await self._mc.commands.send_appstart()
            if result.type == ET.SELF_INFO:
                self._own_node_name = result.payload.get("name", "")
                self._own_mc_key    = result.payload.get("public_key", "")
                cap_label = (
                    "router" if self.can_route else "edge (no upstream routing)"
                )
                RNS.log(
                    f"MeshCore_Dynamic_Interface [{self.name}]: "
                    f"Node identity: '{self._own_node_name}' "
                    f"key={self._own_mc_key[:16]}... [{cap_label}]",
                    RNS.LOG_INFO
                )
        except Exception as exc:
            RNS.log(
                f"MeshCore_Dynamic_Interface [{self.name}]: "
                f"Identity fetch failed: {exc}", RNS.LOG_WARNING
            )

        if self.radio_freq and self.radio_bw and self.radio_sf and self.radio_cr:
            try:
                await self._mc.commands.set_radio(
                    self.radio_freq, self.radio_bw, self.radio_sf, self.radio_cr
                )
            except Exception:
                pass

        try:
            secret_bytes = bytes.fromhex(self.channel_secret_hex)
            await self._mc.commands.set_channel(
                self.channel_idx, self.channel_name, secret_bytes
            )
        except Exception as exc:
            RNS.log(
                f"MeshCore_Dynamic_Interface [{self.name}]: "
                f"Channel init error: {exc}", RNS.LOG_WARNING
            )

        if self.allow_direct:
            self._has_direct_api = hasattr(self._mc.commands, "send_msg")
            if self._has_direct_api:
                # Keep the local contact cache (self._mc.contacts) populated and
                # current -- this rides the same connection rnsd already owns,
                # it's not a second client. auto_update_contacts re-fetches
                # automatically whenever the firmware reports a path change,
                # so out_path_len is always fresh when we need to log it.
                try:
                    self._mc.auto_update_contacts = True
                    await self._mc.ensure_contacts()
                except Exception as exc:
                    RNS.log(
                        f"MeshCore_Dynamic_Interface [{self.name}]: "
                        f"Initial contact fetch failed: {exc}", RNS.LOG_DEBUG
                    )

        self._mc.subscribe(
            ET.CHANNEL_MSG_RECV,
            lambda e: asyncio.run_coroutine_threadsafe(
                self._on_channel_msg(e), self._loop
            )
        )

        _direct_recv_et = None
        for _name in ("CONTACT_MSG_RECV", "DIRECT_MSG_RECV", "PRIVATE_MSG_RECV",
                      "MSG_RECV", "PRIV_MSG_RECV"):
            _direct_recv_et = getattr(ET, _name, None)
            if _direct_recv_et is not None:
                self._mc.subscribe(
                    _direct_recv_et,
                    lambda e: asyncio.run_coroutine_threadsafe(
                        self._on_direct_msg(e), self._loop
                    )
                )
                break

        if _direct_recv_et is None:
            self._has_direct_api = False

        for _name in ("ACK", "MSG_ACKED", "MESSAGE_ACKED", "CHAN_ACK"):
            _ack_et = getattr(ET, _name, None)
            if _ack_et is not None:
                self._mc.subscribe(
                    _ack_et,
                    lambda e: asyncio.run_coroutine_threadsafe(
                        self._on_msg_ack(e), self._loop
                    )
                )
                break

        await self._mc.start_auto_message_fetching()

        asyncio.create_task(self._cleanup_loop())
        asyncio.create_task(self._bind_discovery_loop())
        asyncio.create_task(self._async_outgoing_worker())

        self.online = True
        self._setup_done.set()

    # -------------------------------------------------------------------------
    # Peer discovery
    # -------------------------------------------------------------------------

    def _own_capability(self) -> str:
        return self.CAPABILITY_ROUTER if self.can_route else self.CAPABILITY_EDGE

    async def _bind_discovery_loop(self):
        await asyncio.sleep(5)  # Let connection settle
        retries = 0

        while True:
            with self._peer_lock:
                have_peers = bool(self._peer_table)

            if not have_peers and retries < self.BIND_MAX_RETRIES:
                if self.online and self._own_mc_key:
                    RNS.log(
                        f"MeshCore_Dynamic_Interface [{self.name}]: "
                        f"No peers — sending RNSBIND_REQ "
                        f"(attempt {retries + 1}/{self.BIND_MAX_RETRIES}, "
                        f"cap={self._own_capability()})",
                        RNS.LOG_INFO
                    )
                    try:
                        await self._mc.commands.send_chan_msg(
                            self.channel_idx,
                            f"{self.BIND_REQ_PREFIX}"
                            f"{self._own_mc_key}:{self._own_capability()}"
                        )
                    except Exception:
                        pass
                retries += 1
                await asyncio.sleep(self.BIND_RESP_WINDOW_S)

            else:
                retries = 0
                if self.online and self._own_mc_key:
                    try:
                        await self._mc.commands.send_chan_msg(
                            self.channel_idx,
                            f"{self.BIND_PREFIX}"
                            f"{self._own_mc_key}:{self._own_capability()}"
                        )
                    except Exception:
                        pass
                await asyncio.sleep(self.BIND_HEARTBEAT_S)

    async def _delayed_bind_response(self):
        delay = random.uniform(self.BIND_BACKOFF_MIN, self.BIND_BACKOFF_MAX)
        await asyncio.sleep(delay)
        if not self.online or not self._own_mc_key:
            return
        try:
            await self._mc.commands.send_chan_msg(
                self.channel_idx,
                f"{self.BIND_PREFIX}{self._own_mc_key}:{self._own_capability()}"
            )
            RNS.log(
                f"MeshCore_Dynamic_Interface [{self.name}]: "
                f"Sent RNSBIND response [cap={self._own_capability()}] "
                f"after {delay:.1f}s backoff.",
                RNS.LOG_DEBUG
            )
        except Exception:
            pass

    # -------------------------------------------------------------------------
    # Maintenance
    # -------------------------------------------------------------------------

    async def _cleanup_loop(self):
        while True:
            await asyncio.sleep(30)  
            now = time.monotonic()

            # --- Stale fragment buffers ------------------------------------
            frag_deadline = now - self.fragment_timeout_s
            with self._asm_lock:
                stale = [
                    k for k, (_, ts) in self._assembly_meta.items()
                    if ts < frag_deadline
                ]
                for k in stale:
                    del self._assembly[k]
                    del self._assembly_meta[k]

            # --- Expired sliding window deduplication records --------------
            with self._seen_lock:
                expired_seen = [k for k, exp in self._seen_pkts.items() if now >= exp]
                for k in expired_seen:
                    del self._seen_pkts[k]

            # --- Expired peers ---------------------------------------------
            peer_deadline = now - self.peer_ttl_s
            with self._peer_lock:
                expired = [
                    name for name, ts in self._peer_last_seen.items()
                    if ts < peer_deadline
                ]
                for name in expired:
                    mc_key = self._peer_table.pop(name, None)
                    self._peer_last_seen.pop(name, None)
                    self._peer_caps.pop(name, None)
                    if mc_key:
                        self._reverse_peers.pop(mc_key, None)
                        for pfx_len in (8, 12, 16, 24):
                            self._reverse_peers.pop(mc_key[:pfx_len], None)
                        stale_tokens = [
                            t for t, k in self._rns_to_mc_map.items()
                            if k == mc_key
                        ]
                        for t in stale_tokens:
                            del self._rns_to_mc_map[t]
                if expired:
                    RNS.log(
                        f"MeshCore_Dynamic_Interface [{self.name}]: "
                        f"Expired {len(expired)} stale peer(s).",
                        RNS.LOG_DEBUG
                    )

            # --- Old announce rate entries ---------------------------------
            if self._announce_rate_s > 0:
                ar_deadline = now - (self._announce_rate_s * 2)
                with self._announce_sent_lock:
                    stale_ar = [
                        k for k, ts in self._announce_sent_times.items()
                        if ts < ar_deadline
                    ]
                    for k in stale_ar:
                        del self._announce_sent_times[k]

            # --- Old path request rate entries ----------------------------
            if self._path_req_rate_s > 0:
                pr_deadline = now - (self._path_req_rate_s * 2)
                with self._path_req_sent_lock:
                    stale_pr = [
                        k for k, (_, last_ts) in self._path_req_sent_times.items()
                        if last_ts < pr_deadline
                    ]
                    for k in stale_pr:
                        del self._path_req_sent_times[k]

    # -------------------------------------------------------------------------
    # Inbound event handlers
    # -------------------------------------------------------------------------

    async def _on_channel_msg(self, event):
        text = event.payload.get("text", "")

        rns_idx  = text.find(self.MSG_PREFIX)      
        bind_idx = text.find(self.BIND_PREFIX)     
        req_idx  = text.find(self.BIND_REQ_PREFIX) 

        eff_bind = -1
        if req_idx != -1 and (bind_idx == -1 or req_idx <= bind_idx):
            eff_bind = req_idx
        elif bind_idx != -1:
            eff_bind = bind_idx

        if eff_bind != -1 and (rns_idx == -1 or eff_bind < rns_idx):
            await self._handle_bind(text, bind_idx, req_idx)
            return

        if rns_idx != -1:
            sender = text[:rns_idx].rstrip(": ") if rns_idx > 0 else ""
            await self._process_tunnel_text(text[rns_idx:], sender, rx_mode="CHANNEL")

    async def _on_direct_msg(self, event):
        payload = event.payload
        sender_key = (
            payload.get("pubkey_prefix") or payload.get("sender_pubkey") or
            payload.get("pubkey")        or payload.get("from_pubkey") or ""
        )
        text = payload.get("text", "")
        if not text.startswith(self.MSG_PREFIX):
            return
        sender_id = self._resolve_sender_key(sender_key)
        await self._process_tunnel_text(text, sender_id, rx_mode="DIRECT")

    async def _on_msg_ack(self, event):
        pass

    async def _handle_bind(self, text: str, bind_idx: int, req_idx: int = -1):
        is_req  = (req_idx != -1 and (bind_idx == -1 or req_idx <= bind_idx))
        prefix  = self.BIND_REQ_PREFIX if is_req else self.BIND_PREFIX
        pfx_idx = req_idx              if is_req else bind_idx

        sender_name = text[:pfx_idx].rstrip(": ") if pfx_idx > 0 else ""
        raw_value   = text[pfx_idx + len(prefix):].strip()

        if not sender_name or not raw_value or sender_name == self._own_node_name:
            return

        if ":" in raw_value:
            mc_pubkey, cap_str = raw_value.rsplit(":", 1)
            peer_can_route = (cap_str.strip().upper() != self.CAPABILITY_EDGE)
        else:
            mc_pubkey      = raw_value
            peer_can_route = True  

        mc_pubkey = mc_pubkey.strip()
        if not mc_pubkey:
            return

        with self._peer_lock:
            existing    = self._peer_table.get(sender_name)
            cap_changed = self._peer_caps.get(sender_name) != peer_can_route

            if existing != mc_pubkey:
                self._peer_table[sender_name]  = mc_pubkey
                self._reverse_peers[mc_pubkey] = sender_name
                for pfx_len in (8, 12, 16, 24):
                    pfx = mc_pubkey[:pfx_len]
                    if pfx:
                        self._reverse_peers[pfx] = sender_name

            self._peer_caps[sender_name]      = peer_can_route
            self._peer_last_seen[sender_name] = time.monotonic()

        if existing != mc_pubkey or cap_changed:
            RNS.log(
                f"MeshCore_Dynamic_Interface [{self.name}]: "
                f"{'REQ from' if is_req else 'Peer'} '{sender_name}' "
                f"-> {mc_pubkey[:16]}... "
                f"[{'router' if peer_can_route else 'edge — no upstream routing'}]",
                RNS.LOG_INFO
            )

        if is_req and self._own_mc_key:
            if self._pending_resp_task is None or self._pending_resp_task.done():
                self._pending_resp_task = asyncio.create_task(
                    self._delayed_bind_response()
                )

    def _resolve_sender_key(self, key_str: str) -> str:
        if not key_str:
            return key_str
        with self._peer_lock:
            name = self._reverse_peers.get(key_str)
            if name:
                return name
            for stored_key, stored_name in self._reverse_peers.items():
                if stored_key.startswith(key_str) or key_str.startswith(stored_key):
                    return stored_name
        return key_str

    async def _process_tunnel_text(self, text: str, sender: str = "", rx_mode: str = "UNKNOWN"):
        if sender and sender == self._own_node_name:
            return

        b64 = text[len(self.MSG_PREFIX):].strip()
        b64 += "=" * (-len(b64) % 4)
        try:
            raw = base64.urlsafe_b64decode(b64)
        except Exception:
            return

        if len(raw) < self.HEADER_SIZE:
            return

        # Header unpacked big-endian matching structural change (1B index, 4B packet ID, 1B total fragments)
        frag_idx, pkt_id, frag_total = struct.unpack(">BIB", raw[:6])
        payload    = raw[self.HEADER_SIZE:]

        if frag_total == 0 or frag_idx >= frag_total:
            return

        key = (sender, pkt_id)
        now = time.monotonic()

        # Sliding time-window deduplication check
        with self._seen_lock:
            if key in self._seen_pkts:
                if now < self._seen_pkts[key]:
                    return  
                else:
                    del self._seen_pkts[key]

        # Fragment reassembly
        with self._asm_lock:
            if key not in self._assembly:
                self._assembly[key]      = {}
                self._assembly_meta[key] = (frag_total, now)

            if frag_idx in self._assembly[key]:
                return

            self._assembly[key][frag_idx] = payload

            if len(self._assembly[key]) < self._assembly_meta[key][0]:
                return  

            try:
                expected    = self._assembly_meta[key][0]
                full_packet = b"".join(
                    self._assembly[key][i] for i in range(expected)
                )
                del self._assembly[key]
                del self._assembly_meta[key]
            except Exception:
                self._assembly.pop(key, None)
                self._assembly_meta.pop(key, None)
                return

        # Mark as completely reassembled inside sliding time window
        with self._seen_lock:
            self._seen_pkts[key] = now + self.DEDUPLICATION_TTL_S

        if not full_packet:
            return

        rns_token = self._extract_rns_token(full_packet)
        if rns_token is not None and sender:
            with self._peer_lock:
                mc_key = self._peer_table.get(sender)
                if mc_key:
                    if rns_token not in self._rns_to_mc_map:
                        self._rns_to_mc_map[rns_token] = mc_key
                        if len(self._rns_to_mc_map) > self._RNS_MAP_MAX:
                            trim = list(self._rns_to_mc_map.keys())[
                                : self._RNS_MAP_MAX // 2
                            ]
                            for t in trim:
                                del self._rns_to_mc_map[t]
                        if self.debug_level == "debug":
                            RNS.log(
                                f"MeshCore_Dynamic_Interface [{self.name}]: "
                                f"Linked RNS token {rns_token.hex()[:8]} "
                                f"-> '{sender}'",
                                RNS.LOG_DEBUG
                            )

                    if full_packet[0] & 0x03 == self._RNS_PTYPE_LINK_REQ:
                        link_id = self._link_id_from_lr_packet(full_packet)
                        if link_id is not None and link_id not in self._rns_to_mc_map:
                            self._rns_to_mc_map[link_id] = mc_key
                            if self.debug_level == "debug":
                                RNS.log(
                                    f"MeshCore_Dynamic_Interface [{self.name}]: "
                                    f"Pre-bound link_id {link_id.hex()[:8]} "
                                    f"-> '{sender}' from LINK_REQUEST",
                                    RNS.LOG_DEBUG
                                )
        if full_packet:
            ptype = full_packet[0] & 0x03
            ptype_str = {
                0x00: "DATA",
                0x01: "ANNOUNCE",
                0x02: "LINK_REQ",
                0x03: "PROOF"
            }.get(ptype, "UNKNOWN")
            
            RNS.log(
                f"MeshCore_Dynamic_Interface [{self.name}]: "
                f"RX -> {rx_mode} from '{sender}'. Reassembled {len(full_packet)}b {ptype_str} packet.",
                RNS.LOG_INFO
            )
        try:
            self.processIncoming(full_packet)
        except Exception as exc:
            RNS.log(
                f"MeshCore_Dynamic_Interface [{self.name}]: "
                f"Delivery error: {exc}", RNS.LOG_ERROR
            )

    # -------------------------------------------------------------------------
    # Outbound
    # -------------------------------------------------------------------------

    def _is_broadcast_packet(self, data: bytes) -> bool:
        if len(data) < 1:
            return True
        flags     = data[0]
        ptype     = flags & 0x03         
        dest_type = (flags >> 2) & 0x03  
        if ptype == self._RNS_PTYPE_ANNOUNCE:
            return True
        if ptype == self._RNS_PTYPE_DATA and dest_type == self._RNS_DTYPE_PLAIN:
            return True
        return False

    def _extract_rns_token(self, data: bytes):
        if len(data) < 2:
            return None
        header_type = (data[0] & 0x40) >> 6
        DST_LEN = self._RNS_DST_LEN
        if header_type == 1: 
            end = 2 + 2 * DST_LEN
            if len(data) < end:
                return None
            return bytes(data[2 + DST_LEN:end])
        else: 
            end = 2 + DST_LEN
            if len(data) < end:
                return None
            return bytes(data[2:end])

    def _link_id_from_lr_packet(self, raw: bytes):
        if len(raw) < 2:
            return None
        DST_LEN = self._RNS_DST_LEN
        header_type = (raw[0] & 0x40) >> 6
        hashable = bytes([raw[0] & 0b00001111])
        if header_type == 1:
            if len(raw) < 2 + DST_LEN:
                return None
            hashable += raw[2 + DST_LEN:]
        else:
            hashable += raw[2:]
        return hashlib.sha256(hashable).digest()[:DST_LEN]

    def process_outgoing(self, data):
        return self.processOutgoing(data)

    def processOutgoing(self, data):
        if not self.online:
            return

        hdr_byte  = data[0] if data else 0
        ptype     = hdr_byte & 0x03         
        dest_type = (hdr_byte >> 2) & 0x03  

        # Per-destination outgoing announce rate limiter
        if self._announce_rate_s > 0 and len(data) >= 12:
            if ptype == self._RNS_PTYPE_ANNOUNCE:
                dest_id = bytes(data[2:12])
                now     = time.monotonic()
                with self._announce_sent_lock:
                    if now - self._announce_sent_times.get(dest_id, 0) < self._announce_rate_s:
                        return
                    self._announce_sent_times[dest_id] = now

        # Per-destination outgoing path request rate limiter, with a burst
        # window that lets RNS's own natural retry cluster through before the
        # long-run anti-spam cooldown kicks in. See path_req_burst_window
        # comment in __init__ for rationale.
        if self._path_req_rate_s > 0 and len(data) >= 12:
            if ptype == self._RNS_PTYPE_DATA and dest_type == self._RNS_DTYPE_PLAIN:
                dest_id = bytes(data[2:12])
                now     = time.monotonic()
                with self._path_req_sent_lock:
                    entry = self._path_req_sent_times.get(dest_id)
                    if entry is None:
                        # First request for this destination: starts a new burst.
                        self._path_req_sent_times[dest_id] = (now, now)
                    else:
                        first_ts, last_ts = entry
                        if now - first_ts < self._path_req_burst_window_s:
                            # Still inside the burst window -- let it through,
                            # just refresh last_ts for cleanup purposes.
                            self._path_req_sent_times[dest_id] = (first_ts, now)
                        elif now - last_ts < self._path_req_rate_s:
                            # Burst window elapsed and still within the
                            # long-run cooldown -- suppress.
                            return
                        else:
                            # Cooldown expired -- this starts a fresh burst.
                            self._path_req_sent_times[dest_id] = (now, now)

        with self._pkt_id_lock:
            pkt_id       = self._pkt_id
            self._pkt_id = (self._pkt_id + 1) & 0xFFFFFFFF  # 32-bit bound integer tracking

        handler   = _PacketHandler(data, pkt_id, self.payload_size)
        broadcast = self._is_broadcast_packet(data)

        target_key = None
        channel_reason = ""

        if broadcast:
            channel_reason = "Mandatory broadcast packet (e.g., Announce)"
        elif not self._has_direct_api:
            channel_reason = "Direct routing API disabled or undetected by interface"
        else:
            next_hop_token = self._extract_rns_token(data)
            if next_hop_token is None:
                channel_reason = f"Packet too short to extract destination token (len: {len(data)})"
            else:
                with self._peer_lock:
                    target_key = self._rns_to_mc_map.get(next_hop_token)
                if not target_key:
                    channel_reason = f"No direct route bound for RNS token {next_hop_token.hex()[:8]}"

        if channel_reason:
            RNS.log(
                f"MeshCore_Dynamic_Interface [{self.name}]: "
                f"Routing -> CHANNEL. Reason: {channel_reason}",
                RNS.LOG_INFO
            )
            route = [("channel", None)]
        else:
            RNS.log(
                f"MeshCore_Dynamic_Interface [{self.name}]: "
                f"Routing -> DIRECT via peer key {target_key[:12]}...",
                RNS.LOG_INFO
            )
            route = [("direct", target_key)]
            
        for frag_str in handler.fragments:
            for mode, target in route:
                try:
                    # Thread-safe blocking put handles backpressure cleanly
                    self._outqueue.put((mode, target, frag_str), block=True, timeout=None)
                except Exception:
                    pass

        self.txb += len(data)

    async def _async_outgoing_worker(self):
        """
        Worker task pulling payload chunks from the thread-safe synchronized queue
        using the event loop executor pool to preserve pure async interface execution.
        """
        while True:
            if not self.online or self._mc is None:
                await asyncio.sleep(0.5)
                continue

            # Safe non-blocking cross-thread extraction via run_in_executor
            item = await self._loop.run_in_executor(None, self._outqueue.get)
            mode, target, frag_str = item

            try:
                if mode == "direct":
                    result = await self._mc.commands.send_msg(target, frag_str)
                    if result is None or result.type != self._EventType.MSG_SENT:
                        reason = (
                            result.payload.get("reason", "no path/unknown")
                            if result is not None else "no response"
                        )
                        raise RuntimeError(f"direct send rejected: {reason}")

                    # CORRECTED UNDERSTANDING: MSG_SENT only confirms the local
                    # radio queued the frame for transmission -- it is NOT
                    # end-to-end delivery confirmation. The firmware hands back
                    # an "expected_ack" tag in the MSG_SENT payload; actual
                    # over-air delivery is confirmed later (if at all) by a
                    # separate ACK event carrying that same tag. Without
                    # waiting on it, a frame that never reaches the peer
                    # (out of range, collision, stale/broken path) is
                    # indistinguishable from one that was delivered.
                    exp_ack = result.payload.get("expected_ack")
                    if exp_ack is not None:
                        exp_ack_hex = (
                            exp_ack.hex() if isinstance(exp_ack, (bytes, bytearray))
                            else str(exp_ack)
                        )
                        suggested_ms = result.payload.get("suggested_timeout", 0) or 0
                        # NOTE: for a contact with out_path_len == -1 (no known
                        # route -- flood mode), the firmware's suggested_timeout
                        # can be very large, since it has to budget for a full
                        # flood-and-wait cycle. _async_outgoing_worker is a
                        # single task pulling from one shared queue -- an
                        # uncapped wait here stalls EVERY other queued fragment
                        # (channel broadcasts, other peers) for however long the
                        # firmware suggests, which can be minutes. We deliberately
                        # cap it: our own CHANNEL fallback is cheap, so there's no
                        # reason to let one flood-mode contact block the whole
                        # queue for as long as the radio itself would wait.
                        raw_ack_timeout = max(
                            self.direct_ack_timeout_s, (suggested_ms / 1000.0) * 1.2
                        )
                        ack_timeout = min(raw_ack_timeout, self.direct_ack_timeout_max_s)
                        if raw_ack_timeout > ack_timeout:
                            RNS.log(
                                f"MeshCore_Dynamic_Interface [{self.name}]: "
                                f"Firmware suggested {suggested_ms}ms ACK timeout for "
                                f"peer key {target[:12] if target else '?'}... "
                                f"(likely flood/no-path) -- capping wait at "
                                f"{ack_timeout:.1f}s instead of {raw_ack_timeout:.1f}s "
                                f"to avoid blocking the outgoing queue.",
                                RNS.LOG_INFO
                            )
                        ack = await self._mc.dispatcher.wait_for_event(
                            self._EventType.ACK,
                            attribute_filters={"code": exp_ack_hex},
                            timeout=ack_timeout,
                        )
                        if ack is None:
                            raise RuntimeError(
                                f"no delivery ACK within {ack_timeout:.1f}s "
                                f"(expected_ack={exp_ack_hex}, "
                                f"firmware suggested {suggested_ms}ms)"
                            )
                else:
                    await self._mc.commands.send_chan_msg(self.channel_idx, frag_str)
            except Exception as exc:
                if mode == "direct":
                    # Diagnostic: pull the target's out_path status from the
                    # meshcore library's local contact cache (self._mc.contacts).
                    # This reads in-memory state populated by earlier
                    # CONTACTS/PATH_UPDATE/ADVERTISEMENT events -- it does NOT
                    # touch the serial port, so it's safe to call from here
                    # without contending with rnsd's own use of the connection.
                    path_info = "unknown (no cached contact)"
                    try:
                        contact = self._mc.get_contact_by_key_prefix(target) if target else None
                        if contact:
                            opl = contact.get("out_path_len", -1)
                            path_info = (
                                f"out_path_len={opl}"
                                if opl != -1 else "out_path_len=-1 (no known route)"
                            )
                    except Exception:
                        pass
                    RNS.log(
                        f"MeshCore_Dynamic_Interface [{self.name}]: "
                        f"DIRECT send to peer key {target[:12] if target else '?'}... "
                        f"failed ({exc}) [{path_info}] -- falling back to CHANNEL.",
                        RNS.LOG_INFO
                    )
                    try:
                        # Fallback to channel if targeted routing exceptions happen mid-transit
                        self._outqueue.put_nowait(("channel", None, frag_str))
                    except queue.Full:
                        pass
                self._outqueue.task_done()
                continue

            delay = (
                self.direct_frag_delay_s
                if mode == "direct"
                else self.fragment_delay_s
            )
            if self.rate_limit_bps > 0:
                bits  = (len(frag_str) * 3 // 4) * 8
                delay = max(delay, bits / self.rate_limit_bps)

            await asyncio.sleep(delay)
            self._outqueue.task_done()

    # -------------------------------------------------------------------------
    # Inbound delivery
    # -------------------------------------------------------------------------

    def processIncoming(self, data: bytes):
        if self.online and not self.detached:
            self.rxb += len(data)
            self.owner.inbound(data, self)

    def __str__(self):
        return f"MeshCore_Dynamic_Interface[{self.name}]"


interface_class = MeshCore_Dynamic_Interface
