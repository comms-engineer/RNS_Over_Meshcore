"""
MeshCore_Dynamic_Interface.py
Reticulum (RNS) interface over a MeshCore LoRa mesh network.

Implements a hybrid channel-broadcast / unicast-direct routing strategy with
demand-driven peer discovery and edge-node capability advertisement.  No static
remote-node configuration is required.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CHANGELOG (this revision)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Audit fixes applied — see inline comments tagged [FIX n] at each site:

  [FIX 1] Startup race between RNSBIND completion and inbound RNS announces.
          Previously, _rns_to_mc_map was only populated when the sending
          peer's MeshCore key was ALREADY known in _peer_table at the moment
          a packet was reassembled. Since RNSBIND discovery runs on its own
          schedule (5s settle + up to BIND_RESP_WINDOW_S per retry) and RNS
          announces arrive independently (often within the first few seconds
          of stack startup), any packet — including the propagation node's
          initial announce — that arrived before RNSBIND completed was
          silently dropped on the floor for routing purposes, and the
          "if rns_token not in _rns_to_mc_map" guard prevented the entry from
          ever being corrected later. A new map, _rns_to_sender_map, now
          parks token -> node_name associations the instant a packet is
          reassembled, regardless of whether the MeshCore key is known yet.
          processOutgoing() resolves it retroactively once RNSBIND has
          caught up, and promotes it into the fast-path map at that point.

  [FIX 2] Outbound token extraction used two conditional shifts that did NOT
          match the fixed, unconditional bytes[1:11] window used on the
          inbound (learning) side. Any packet that tripped either shift
          looked up a token that was never stored under that key, guaranteeing
          a routing miss (silent fallback to channel). Both shifts are removed;
          inbound and outbound now use the identical bytes[1:11] window.

  [FIX 3] Map entries were never updated/refreshed once first written ("if
          rns_token not in map" guard). A peer that changed its MeshCore key
          (firmware reflash, factory reset) would leave stale routes pointing
          at a key that no longer exists, with no recovery until peer_ttl
          expired. The guard is removed — every successful reassembly now
          refreshes the route.

  [FIX 4] If no supported direct-message receive EventType name is found,
          unicast routing is disabled silently (_has_direct_api = False) with
          no diagnostic indicating *why*. A LOG_WARNING now fires, listing the
          EventType names that were probed and the names actually available in
          the installed meshcore library, so a library version mismatch is
          immediately diagnosable instead of presenting as "direct never
          works" with no further explanation.

  [FIX 5] _async_outgoing_worker's offline-requeue path skipped
          self._outqueue.task_done() before "continue", permanently inflating
          asyncio.Queue's unfinished-task counter. Not currently load-bearing
          (queue.join() isn't called anywhere), but it's a latent correctness
          bug that will bite if queue draining/shutdown logic is added later.
          task_done() is now called before every requeue.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
WIRE FORMAT
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Each RNS binary packet is split into payload-sized chunks.  Each chunk is
encoded as a MeshCore channel (or direct) message:

    "RNS:" + base64url( [frag_idx:1][pkt_id:1][frag_total:1] + payload )

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

  RNS TOKEN WINDOW (bytes 1:11 of a single-header packet)
    For ANY single-header packet — ANNOUNCE or DATA — bytes 1 through 16 are
    the destination hash. The first 10 bytes of that hash, data[1:11], are
    used throughout this interface as a compact "RNS token" correlating an
    announce with subsequent data traffic to the same destination. This
    window is fixed and unconditional: it must be identical on both the
    inbound learning path (_process_tunnel_text) and the outbound lookup path
    (processOutgoing). See [FIX 2] above — a previous revision applied
    conditional byte-shifts on the outbound side that broke this invariant.

PAYLOAD SIZE
  MeshCore firmware silently truncates channel messages that exceed a hardware-
  dependent character limit (observed ~128 chars on common firmware builds).
  The firmware also prepends the sender's node name when relaying channel
  messages, so the effective character budget for the encoded portion is:

      budget = firmware_limit - len(node_name) - 2       (": " separator)

  Encoded message length:
      msg_len = ceil((payload_size + HEADER_SIZE) * 4/3) + len("RNS:")

  With the default payload_size = 64:
      msg_len = ceil(67 * 4/3) + 4 = 90 + 4 = 94 chars
      Safe for node names up to ~30 characters at a 128-char firmware limit.

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

  IMPORTANT TIMING NOTE (see [FIX 1]): RNSBIND discovery and RNS announce
  reception are two independent, asynchronously-scheduled processes. There is
  NO guarantee that this node's peer table is populated by the time the first
  RNS announce from a given peer (e.g. a propagation node) arrives. The
  _rns_to_sender_map mechanism exists specifically to bridge this gap without
  dropping the routing opportunity.

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
  │     fragment_timeout = 3600  # seconds before stale frags discarded    │
  │                                                                         │
  │     # Outgoing rate limiting (set to 0 to disable)                     │
  │     outgoing_announce_rate = 600    # min s between announces per dest │
  │     outgoing_path_req_rate = 1800   # min s between path reqs per dest │
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

MOBILE / CLIENT NODE  (portable device, no guaranteed upstream connectivity)
──────────────────────────────────────────────────────────────────────────────
  [[MeshCore Dynamic Interface]]   mode = roaming    can_route = no
  [[Backbone/TCP Interface]]       mode = boundary   (when present)

  roaming
    The interface is physically mobile from the perspective of infrastructure
    nodes.  Paths via roaming interfaces expire faster, preventing stale
    routing entries from accumulating at the transport node.

  can_route = no
    Advertises to all peers at discovery time that this node cannot carry
    traffic to the wider mesh.  Peers will not attempt to use it as an upstream
    gateway.  The node remains fully functional for direct P2P messaging and
    for serving client devices (phones, laptops) connected behind it.

  peer_ttl
    Set a short peer_ttl on mobile nodes so that infrastructure nodes stop
    forwarding path requests for them onto the LoRa channel sooner after they
    go offline.  A value matching the expected maximum offline duration of the
    device is appropriate (e.g. 7200 s for a device used within a single day).

  If the mobile node hosts a hotspot for downstream client devices (phones,
  laptops), add a server interface on the hotspot subnet.  Use TCPServerInterface
  for explicit client configuration, or AutoInterface for zero-config mDNS
  discovery (supported by Sideband and NomadNet).

  Full example — mobile / edge node
  ┌─────────────────────────────────────────────────────────────────────────┐
  │ [reticulum]                                                             │
  │   enable_transport = yes                                                │
  │   share_instance = yes                                                  │
  │                                                                         │
  │ [logging]                                                               │
  │   loglevel = 4                                                          │
  │                                                                         │
  │ [interfaces]                                                            │
  │                                                                         │
  │   [[MeshCore Dynamic Interface]]                                        │
  │     type = MeshCore_Dynamic_Interface                                   │
  │     interface_enabled = yes                                             │
  │                                                                         │
  │     # Role                                                              │
  │     mode = roaming                                                      │
  │     can_route = no                                                      │
  │                                                                         │
  │     # Transport — uncomment exactly one block                          │
  │     # Serial:                                                           │
  │     transport = serial                                                  │
  │     port = /dev/ttyACM0                                                 │
  │     baudrate = 115200                                                   │
  │     #                                                                   │
  │     # TCP:                                                              │
  │     # transport = tcp                                                   │
  │     # host = 127.0.0.1                                                 │
  │     # tcp_port = 4403                                                   │
  │     #                                                                   │
  │     # BLE:                                                              │
  │     # transport = ble                                                   │
  │     # ble_name =           # blank = connect to first found device     │
  │                                                                         │
  │     # Channel — must match all other nodes on the tunnel               │
  │     channel_idx = 0                                                     │
  │     channel_name = RNSTunnel                                            │
  │     channel_secret = <same key as infrastructure node>                 │
  │                                                                         │
  │     # Radio overrides (leave commented to use stored node values)      │
  │     # freq = 915.0                                                      │
  │     # bw   = 250.0                                                      │
  │     # sf   = 10                                                         │
  │     # cr   = 5                                                          │
  │                                                                         │
  │     # Fragmentation                                                     │
  │     payload_size = 64                                                   │
  │     fragment_delay = 2.5                                                │
  │     direct_frag_delay = 0.5                                             │
  │     fragment_timeout = 3600                                             │
  │                                                                         │
  │     # Outgoing rate limiting                                            │
  │     outgoing_announce_rate = 600                                        │
  │     outgoing_path_req_rate = 1800                                       │
  │                                                                         │
  │     # Peer discovery                                                    │
  │     allow_direct = yes                                                  │
  │     peer_ttl = 7200        # short TTL for intermittently-connected    │
  │                            # devices; adjust to max expected offline   │
  │                            # window (seconds)                          │
  │                                                                         │
  │     debug_level = info                                                  │
  │                                                                         │
  │   # Backbone — disable when operating away from the home network.      │
  │   # Enable when connected to a fixed LAN with upstream transport.      │
  │   [[Backbone Interface]]                                                │
  │     type = BackboneInterface                                            │
  │     interface_enabled = no   # toggle yes when on home LAN             │
  │     mode = boundary                                                     │
  │     target_host = <backbone-server-hostname-or-ip>                     │
  │     target_port = 4242                                                  │
  │                                                                         │
  │   # Hotspot server — for phones or laptops connected to this node's    │
  │   # WiFi AP.  Use the hotspot gateway IP as listen_ip.                 │
  │   # Option A: explicit TCP (client must configure server address)      │
  │   [[Hotspot TCP Server]]                                                │
  │     type = TCPServerInterface                                           │
  │     interface_enabled = yes                                             │
  │     mode = access_point                                                 │
  │     listen_ip = 192.168.4.1                                             │
  │     listen_port = 4242                                                  │
  │                                                                         │
  │   # Option B: AutoInterface (zero-config mDNS discovery)               │
  │   # [[Hotspot AutoInterface]]                                           │
  │   #   type = AutoInterface                                              │
  │   #   interface_enabled = yes                                           │
  │   #   mode = access_point                                               │
  │   #   devices = wlan0      # hotspot interface name                    │
  │   #   outgoing = yes                                                    │
  └─────────────────────────────────────────────────────────────────────────┘
"""

import RNS
from RNS.Interfaces.Interface import Interface
import asyncio
import base64
import random
import threading
import time
from collections import OrderedDict


# ─────────────────────────────────────────────────────────────────────────────
# Fragmentation helper
# ─────────────────────────────────────────────────────────────────────────────

class _PacketHandler:
    """
    Encodes one RNS binary packet into one or more channel/direct message
    strings.  Each fragment carries a 3-byte binary header:

        [ frag_idx : 1 byte ] [ pkt_id : 1 byte ] [ frag_total : 1 byte ]

    followed by the raw payload chunk.  The combined bytes are base64url-
    encoded (no padding) and prefixed with MSG_PREFIX ("RNS:").
    """

    HEADER_SIZE  = 3
    # Default payload bytes per fragment.  Gives ~94-char encoded messages,
    # safely under the observed ~128-char MeshCore firmware truncation limit
    # even with typical node name prefixes.  See module docstring for the
    # sizing formula if your node name is unusually long.
    PAYLOAD_SIZE = 64
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

    # Bind protocol prefixes.
    # NOTE: RNSBIND: is a literal substring of RNSBIND_REQ:.  Both text.find()
    # calls may return the same index on a REQ message.  The disambiguation
    # logic in _handle_bind and _on_channel_msg relies on this property and
    # handles it correctly — do not change these strings independently.
    BIND_PREFIX     = "RNSBIND:"
    BIND_REQ_PREFIX = "RNSBIND_REQ:"

    # Routing capability tokens embedded in RNSBIND / RNSBIND_REQ messages.
    CAPABILITY_ROUTER = "R"   # Node has upstream connectivity; safe to route via
    CAPABILITY_EDGE   = "E"   # No upstream; do not use as a transit gateway

    HEADER_SIZE      = _PacketHandler.HEADER_SIZE
    OUTQUEUE_MAXSIZE = 512
    SETUP_TIMEOUT_S  = 30

    # Bind discovery timing
    BIND_BACKOFF_MIN   =  3.0    # Min random response delay after a REQ (s)
    BIND_BACKOFF_MAX   = 15.0    # Max random response delay after a REQ (s)
    BIND_HEARTBEAT_S   = 3600.0  # Quiet heartbeat interval once peers known (s)
    BIND_RESP_WINDOW_S = 60.0    # Time to collect responses per REQ attempt (s)
    BIND_MAX_RETRIES   = 3       # REQ attempts before falling back to heartbeat

    # RNS single-header byte bit layout:
    #   bits 7-6 : header type     (0b10 = two-byte header; always broadcast)
    #   bits 5-4 : transport flags
    #   bits 3-2 : destination type  <- (flags >> 2) & 0x03
    #   bits 1-0 : packet type       <- flags & 0x03
    #
    # Packet type constants (bits 1-0):
    _RNS_PTYPE_DATA     = 0x00
    _RNS_PTYPE_ANNOUNCE = 0x01
    _RNS_PTYPE_LINK_REQ = 0x02
    _RNS_PTYPE_PROOF    = 0x03
    #
    # Destination type constants (bits 3-2):
    _RNS_DTYPE_SINGLE = 0x00
    _RNS_DTYPE_GROUP  = 0x01
    _RNS_DTYPE_PLAIN  = 0x02   # DATA + PLAIN = path request for an unknown dest
    _RNS_DTYPE_LINK   = 0x03

    # Maximum entries in _rns_to_mc_map before the oldest half is pruned
    _RNS_MAP_MAX = 512

    # Maximum entries in _rns_to_sender_map before the oldest half is pruned.
    # [FIX 1] This map is intentionally separate from _RNS_MAP_MAX / the
    # resolved-route map: it only ever holds tokens that arrived before the
    # owning peer's MeshCore key was known, so it should normally stay small
    # and short-lived. The cap exists purely as a safety backstop against
    # pathological cases (e.g. a misbehaving peer that never completes
    # RNSBIND but announces frequently).
    _RNS_PENDING_MAP_MAX = 256

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
        # channel_secret must be a 32-char hex string (128-bit key).
        # Change this value — do not use the placeholder default in production.
        self.channel_secret_hex = cfg.get("channel_secret",
                                          "00000000000000000000000000000000")

        # --- Optional radio parameter overrides ----------------------------
        # If all four are non-zero, the radio parameters are pushed to the
        # MeshCore node at startup.  Leave at 0 to use the node's stored config.
        self.radio_freq = float(cfg.get("freq", 0))
        self.radio_bw   = float(cfg.get("bw",   0))
        self.radio_sf   = int(cfg.get("sf",     0))
        self.radio_cr   = int(cfg.get("cr",     0))

        # --- Protocol tuning -----------------------------------------------
        # payload_size: raw bytes per fragment before base64 encoding.
        # Default 64 → ~94-char encoded strings.  See module docstring for the
        # sizing formula.  Setting this too high causes silent firmware
        # truncation and broken base64 on the receiving end.
        self.payload_size = int(cfg.get("payload_size", 64))

        # fragment_delay: inter-fragment pause for channel (broadcast) mode.
        # LoRa is half-duplex shared media; 2.5 s gives the air time to clear
        # between fragments and allows other nodes to interleave transmissions.
        self.fragment_delay_s = float(cfg.get("fragment_delay", 2.5))

        # direct_frag_delay: inter-fragment pause for unicast direct messages.
        # MeshCore firmware provides ACK and retry for direct messages at the
        # link layer, so a shorter delay is safe and improves perceived latency.
        raw_dfd = cfg.get("direct_frag_delay", None)
        self.direct_frag_delay_s = float(raw_dfd) if raw_dfd is not None else 0.5

        self.fragment_timeout_s = float(cfg.get("fragment_timeout", 3600))
        self.rate_limit_bps     = int(cfg.get("rate_limit", 0))

        # outgoing_announce_rate: minimum seconds between forwarding announces
        # for any single RNS destination out through this interface.  Acts as a
        # per-destination throttle independent of RNS's own announce_cap and
        # announce_rate_target mechanisms.  Set to 0 to disable.
        self._announce_rate_s = float(cfg.get("outgoing_announce_rate", 600))

        # outgoing_path_req_rate: minimum seconds between forwarding path
        # requests (DATA + PLAIN destination) for any single destination.
        # AP mode does not suppress path requests, only announce re-broadcasting.
        # When a node goes offline, the wider mesh will continue sending path
        # requests for it at the full RNS retry rate; without this limiter those
        # requests pass straight through AP mode onto the LoRa channel.
        # Set to 0 to disable.  Default 1800 s (30 min).
        self._path_req_rate_s = float(cfg.get("outgoing_path_req_rate", 1800))

        # --- Routing capability --------------------------------------------
        # can_route: set to 'no' on edge/leaf nodes that have no upstream
        # connectivity beyond the local MeshCore channel.  Advertised in every
        # RNSBIND / RNSBIND_REQ so peers know not to use this node as a transit
        # gateway.  Does not affect per-packet delivery decisions; see the
        # CAPABILITY FIELD section in the module docstring.
        self.can_route = (
            cfg.get("can_route", "yes").lower() not in ("no", "false", "0")
        )

        # allow_direct: enable unicast direct message delivery to known peers
        # when a route has been learned via _rns_to_mc_map.
        self.allow_direct = (
            cfg.get("allow_direct", "yes").lower() not in ("no", "false", "0")
        )

        # peer_ttl: seconds of silence before a peer entry is removed.
        # Use a value that matches the expected maximum offline window for the
        # devices on the channel (e.g. 7200 s for intermittently-connected
        # mobile devices; 86400 s for fixed infrastructure nodes).
        self.peer_ttl_s = float(cfg.get("peer_ttl", 86400))

        # --- Debug verbosity -----------------------------------------------
        self.debug_level = cfg.get("debug_level", "info").lower()

        # --- Internal async state ------------------------------------------
        self._mc          = None
        self._EventType   = None
        self._loop        = None
        self._loop_thread = None
        self._outqueue    = None  # Created inside the async loop thread

        self._own_node_name = ""
        self._own_mc_key    = ""

        self._pkt_id      = 0
        self._pkt_id_lock = threading.Lock()

        # Fragment reassembly buffers keyed by (sender_name, pkt_id)
        self._assembly      = {}   # key -> {frag_idx: bytes}
        self._assembly_meta = {}   # key -> (frag_total, arrival_timestamp)
        self._asm_lock      = threading.Lock()

        # Packet deduplication cache (LRU, capped at 512 entries)
        self._seen_pkts = OrderedDict()  # (sender, pkt_id) -> seen_timestamp
        self._seen_lock = threading.Lock()

        # Peer resolution tables — all guarded by _peer_lock:
        #
        #   _peer_table       : node_name        -> mc_pubkey_hex
        #   _reverse_peers     : mc_pubkey_hex    -> node_name
        #                        (also stores prefix-length variants for fuzzy lookup)
        #   _peer_last_seen    : node_name        -> last-heard monotonic timestamp
        #   _peer_caps         : node_name        -> bool (True = can route upstream)
        #                        Populated from the RNSBIND capability field.
        #                        Defaults to True for peers using the old wire format.
        #                        Informational only — not consulted for packet routing.
        #   _rns_to_mc_map     : rns_token(bytes) -> mc_pubkey_hex
        #                        rns_token = bytes 1:11 of a received RNS packet;
        #                        correlates the RNS source identifier with the
        #                        MeshCore hardware key for unicast direct delivery.
        #                        This is the FAST PATH consulted directly by
        #                        processOutgoing().
        #   _rns_to_sender_map : rns_token(bytes) -> node_name
        #                        [FIX 1] PENDING PATH. Populated whenever a
        #                        packet is reassembled from a sender whose
        #                        MeshCore key is not yet known in _peer_table
        #                        (i.e. RNSBIND for that peer hasn't completed
        #                        yet). processOutgoing() falls back to this map
        #                        and resolves it against _peer_table at send
        #                        time, promoting the result into
        #                        _rns_to_mc_map once resolved. Entries are
        #                        popped once successfully promoted, and pruned
        #                        on peer expiry / size cap like the other maps.
        self._peer_table        = {}
        self._reverse_peers     = {}
        self._peer_last_seen    = {}
        self._peer_caps         = {}
        self._rns_to_mc_map     = {}
        self._rns_to_sender_map = {}   # [FIX 1]
        self._peer_lock         = threading.Lock()

        # Per-destination outgoing rate limit timestamps
        self._announce_sent_times = {}   # dest_id(bytes) -> monotonic timestamp
        self._announce_sent_lock  = threading.Lock()
        self._path_req_sent_times = {}   # dest_id(bytes) -> monotonic timestamp
        self._path_req_sent_lock  = threading.Lock()

        self._has_direct_api    = False  # Does this MC build support send_msg?
        self._pending_resp_task = None   # At most one RNSBIND response in-flight

        self._setup_done = threading.Event()
        self._load_meshcore_or_panic()

        # Spawn an isolated asyncio event loop in a daemon thread so the
        # MeshCore async API does not interfere with any event loop the host
        # application may be running.
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
            RNS.panic()

    def _run_loop(self):
        """Entry point for the dedicated asyncio worker thread."""
        asyncio.set_event_loop(self._loop)
        # asyncio.Queue must be created on the thread that owns the loop.
        self._outqueue = asyncio.Queue(maxsize=self.OUTQUEUE_MAXSIZE)
        try:
            self._loop.run_forever()
        except Exception as exc:
            RNS.log(
                f"MeshCore_Dynamic_Interface [{self.name}]: Loop crashed: {exc}",
                RNS.LOG_ERROR
            )

    async def _async_setup(self):
        """Initialise the MeshCore driver connection and start background tasks."""
        MeshCore = self._mc_module.MeshCore
        ET       = self._EventType

        # --- Driver init ---------------------------------------------------
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

        # --- Identity fetch ------------------------------------------------
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

        # --- Optional radio parameter override -----------------------------
        if self.radio_freq and self.radio_bw and self.radio_sf and self.radio_cr:
            try:
                await self._mc.commands.set_radio(
                    self.radio_freq, self.radio_bw, self.radio_sf, self.radio_cr
                )
            except Exception:
                pass

        # --- Channel init --------------------------------------------------
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

        # --- Direct message API detection ----------------------------------
        if self.allow_direct:
            self._has_direct_api = hasattr(self._mc.commands, "send_msg")

        # --- Event subscriptions -------------------------------------------
        # Callbacks are synchronous shims that schedule coroutines on the
        # dedicated event loop without blocking the MeshCore event thread.
        self._mc.subscribe(
            ET.CHANNEL_MSG_RECV,
            lambda e: asyncio.run_coroutine_threadsafe(
                self._on_channel_msg(e), self._loop
            )
        )

        # The direct-message receive event name varies across library versions.
        _direct_recv_et = None
        _probed_names = ("CONTACT_MSG_RECV", "DIRECT_MSG_RECV", "PRIVATE_MSG_RECV",
                          "MSG_RECV", "PRIV_MSG_RECV")
        for _name in _probed_names:
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
            # [FIX 4] No supported direct-message event found; disable unicast
            # routing. Previously this failed silently — every outgoing packet
            # would then fall back to channel broadcast with the routing log
            # reading "Direct routing API disabled or undetected by interface",
            # with no indication of *why*. Surface it loudly at setup time so
            # a meshcore library version mismatch is immediately diagnosable
            # instead of presenting as an opaque "direct never works" symptom.
            self._has_direct_api = False
            _available = sorted(
                n for n in dir(ET) if not n.startswith("_")
            )
            RNS.log(
                f"MeshCore_Dynamic_Interface [{self.name}]: "
                f"No direct-message receive EventType found among probed names "
                f"{_probed_names} — unicast/direct routing is DISABLED for this "
                f"interface; all traffic will use channel broadcast. "
                f"EventType members available in installed meshcore library: "
                f"{_available}",
                RNS.LOG_WARNING
            )

        # ACK events are informational; reserved for future reliability work.
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

        # --- Background coroutines -----------------------------------------
        asyncio.create_task(self._cleanup_loop())
        asyncio.create_task(self._bind_discovery_loop())
        asyncio.create_task(self._async_outgoing_worker())

        self.online = True
        self._setup_done.set()

    # -------------------------------------------------------------------------
    # Peer discovery
    # -------------------------------------------------------------------------

    def _own_capability(self) -> str:
        """Return the capability token for this node."""
        return self.CAPABILITY_ROUTER if self.can_route else self.CAPABILITY_EDGE

    async def _bind_discovery_loop(self):
        """
        Demand-driven peer discovery via RNSBIND_REQ / RNSBIND exchange.

        Behaviour:
          - No peers known → send RNSBIND_REQ:<pubkey>:<cap> and wait
            BIND_RESP_WINDOW_S for responses.  Retry up to BIND_MAX_RETRIES.
          - Peers known (or retries exhausted) → send a quiet RNSBIND:<pubkey>:<cap>
            heartbeat every BIND_HEARTBEAT_S so that newly-arriving nodes can
            learn this node's key and capability without soliciting a response
            storm from existing peers.

        The capability field is included in both REQ and heartbeat messages so
        that peers learn routing capability as early as possible — including from
        the initial solicitation before any response has arrived.

        NOTE: This loop's timing is intentionally decoupled from RNS's own
        announce schedule (see [FIX 1]). It is normal and expected for an RNS
        announce from a peer to arrive before this loop's first RNSBIND_REQ has
        even been sent (the 5s settle delay below, plus channel airtime). The
        outbound routing path in processOutgoing() is built to tolerate this
        ordering rather than assume RNSBIND always completes first.
        """
        await asyncio.sleep(5)  # Let the MeshCore connection settle
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
                # Either we have peers, or retries are exhausted.
                # Reset counter and shift to the long quiet heartbeat interval.
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
        """
        Respond to a RNSBIND_REQ after a random backoff delay.

        The random delay (BIND_BACKOFF_MIN to BIND_BACKOFF_MAX seconds)
        distributes responses in time when multiple nodes hear the same request,
        preventing a simultaneous response burst on the shared LoRa channel.
        This mirrors RFC 2236 (IGMP) report suppression: if another node
        responds first, all overhearing nodes passively learn from that response.

        The capability field is included so the requester immediately knows
        whether this node can carry upstream traffic.
        """
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
        """
        Periodic housekeeping to prevent unbounded memory growth.

        Cleans up:
          - Stale fragment reassembly buffers (older than fragment_timeout_s)
          - Expired peers (silent for longer than peer_ttl_s), including their
            capability flags and associated RNS→MC route map entries, AND any
            pending (unresolved) RNS→sender entries for that peer [FIX 1]
          - Old outgoing announce rate timestamps (older than 2× rate window)
          - Old outgoing path request rate timestamps (older than 2× rate window)
        """
        while True:
            await asyncio.sleep(60)
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

            # --- Expired peers ---------------------------------------------
            peer_deadline = now - self.peer_ttl_s
            with self._peer_lock:
                expired = [
                    name for name, ts in self._peer_last_seen.items()
                    if ts < peer_deadline
                ]
                expired_set = set(expired)
                for name in expired:
                    mc_key = self._peer_table.pop(name, None)
                    self._peer_last_seen.pop(name, None)
                    self._peer_caps.pop(name, None)
                    if mc_key:
                        self._reverse_peers.pop(mc_key, None)
                        for pfx_len in (8, 12, 16, 24):
                            self._reverse_peers.pop(mc_key[:pfx_len], None)
                        # Also remove any RNS→MC route entries for this peer
                        stale_tokens = [
                            t for t, k in self._rns_to_mc_map.items()
                            if k == mc_key
                        ]
                        for t in stale_tokens:
                            del self._rns_to_mc_map[t]
                if expired_set:
                    # [FIX 1] Also prune any pending (unresolved) tokens that
                    # belonged to a now-expired peer — there is no point
                    # holding onto a token waiting to resolve against a peer
                    # that has just been forgotten.
                    stale_pending = [
                        t for t, n in self._rns_to_sender_map.items()
                        if n in expired_set
                    ]
                    for t in stale_pending:
                        del self._rns_to_sender_map[t]
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
                        k for k, ts in self._path_req_sent_times.items()
                        if ts < pr_deadline
                    ]
                    for k in stale_pr:
                        del self._path_req_sent_times[k]

    # -------------------------------------------------------------------------
    # Inbound event handlers
    # -------------------------------------------------------------------------

    async def _on_channel_msg(self, event):
        """
        Dispatched for every message received on the shared MeshCore channel.

        Checks for bind protocol messages first (RNSBIND_REQ or RNSBIND),
        then falls through to RNS tunnel data processing.

        Prefix detection note: RNSBIND: is a literal substring of RNSBIND_REQ:,
        so both text.find() calls may land on the same index for a REQ message.
        The 'REQ takes precedence on tie' logic in the eff_bind block ensures
        correct dispatch, and _handle_bind re-disambiguates using the same
        comparison to extract the pubkey at the correct prefix length.
        """
        text = event.payload.get("text", "")

        rns_idx  = text.find(self.MSG_PREFIX)       # "RNS:"
        bind_idx = text.find(self.BIND_PREFIX)      # "RNSBIND:"
        req_idx  = text.find(self.BIND_REQ_PREFIX)  # "RNSBIND_REQ:"

        # Identify the earliest bind-protocol token; REQ takes precedence on tie.
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
        """Handles incoming unicast direct messages from known MeshCore peers."""
        payload    = event.payload
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
        """ACK receipt hook — reserved for future retry/reliability tracking."""
        pass

    async def _handle_bind(self, text: str, bind_idx: int, req_idx: int = -1):
        """
        Process a RNSBIND_REQ (discovery request) or RNSBIND (response/heartbeat).

        Wire format:  <SenderName>: RNSBIND[_REQ]:<pubkey>[:<cap>]

          <pubkey>  MeshCore hex public key of the sender.
          <cap>     Optional capability token: R (router) or E (edge).
                    Absent in messages from nodes using older firmware versions;
                    treated as R (can route) for backward compatibility.

        Both message types trigger identical peer table updates — every
        overhearing node learns the sender's identity and capability (passive
        L2 learning).  A RNSBIND_REQ additionally schedules a delayed response.

        [FIX 1] Whenever a peer's MeshCore key becomes known (or changes) here,
        any tokens parked in _rns_to_sender_map for that peer name are eagerly
        promoted into the fast-path _rns_to_mc_map immediately, rather than
        waiting for processOutgoing() to resolve them lazily on the next send
        attempt. This minimises the window during which a learned route sits
        unresolved.
        """
        # Disambiguate REQ vs plain response.
        # (req_idx <= bind_idx handles the equal-index case caused by RNSBIND:
        # being a substring of RNSBIND_REQ:)
        is_req  = (req_idx != -1 and (bind_idx == -1 or req_idx <= bind_idx))
        prefix  = self.BIND_REQ_PREFIX if is_req else self.BIND_PREFIX
        pfx_idx = req_idx              if is_req else bind_idx

        sender_name = text[:pfx_idx].rstrip(": ") if pfx_idx > 0 else ""
        raw_value   = text[pfx_idx + len(prefix):].strip()

        if not sender_name or not raw_value or sender_name == self._own_node_name:
            return

        # Parse the optional capability suffix: "PUBKEYHEX:R" or "PUBKEYHEX:E".
        # rsplit with maxsplit=1 is robust against any future pubkey format that
        # might contain a colon (current hex pubkeys do not).
        if ":" in raw_value:
            mc_pubkey, cap_str = raw_value.rsplit(":", 1)
            peer_can_route = (cap_str.strip().upper() != self.CAPABILITY_EDGE)
        else:
            mc_pubkey      = raw_value
            peer_can_route = True   # No capability field — backward-compatible default

        mc_pubkey = mc_pubkey.strip()
        if not mc_pubkey:
            return

        promoted_tokens = 0
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

            # [FIX 1] Eagerly promote any pending tokens parked for this
            # sender now that its MeshCore key is known.
            if self._rns_to_sender_map:
                pending_tokens = [
                    t for t, n in self._rns_to_sender_map.items()
                    if n == sender_name
                ]
                for t in pending_tokens:
                    self._rns_to_mc_map[t] = mc_pubkey
                    del self._rns_to_sender_map[t]
                promoted_tokens = len(pending_tokens)

        if existing != mc_pubkey or cap_changed:
            RNS.log(
                f"MeshCore_Dynamic_Interface [{self.name}]: "
                f"{'REQ from' if is_req else 'Peer'} '{sender_name}' "
                f"-> {mc_pubkey[:16]}... "
                f"[{'router' if peer_can_route else 'edge — no upstream routing'}]",
                RNS.LOG_INFO
            )
        if promoted_tokens:
            RNS.log(
                f"MeshCore_Dynamic_Interface [{self.name}]: "
                f"Promoted {promoted_tokens} pending direct route(s) for "
                f"'{sender_name}' now that its MeshCore key is known.",
                RNS.LOG_INFO
            )

        # Schedule a delayed response to a REQ — at most one pending at a time.
        if is_req and self._own_mc_key:
            if self._pending_resp_task is None or self._pending_resp_task.done():
                self._pending_resp_task = asyncio.create_task(
                    self._delayed_bind_response()
                )

    def _resolve_sender_key(self, key_str: str) -> str:
        """Map a MeshCore pubkey or prefix back to a human-readable node name."""
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
        """
        Decode a base64-encoded fragment and reassemble into a full RNS packet.

        Dynamic L2/L3 link learning
        ───────────────────────────
        Once a packet is fully reassembled, bytes 1:11 are extracted as an RNS
        origin token.  This window is consistent across the announce → path-reply
        → data packet sequence for a given RNS destination, AND is the same
        fixed window used on the outbound lookup side in processOutgoing()
        (see [FIX 2] and the WIRE FORMAT / RNS TOKEN WINDOW note in the module
        docstring — these two windows must never diverge).

        [FIX 1] If the sender's MeshCore key is already known (RNSBIND has
        completed for this peer), the token is written straight into the
        fast-path _rns_to_mc_map, exactly as before. If the MeshCore key is
        NOT yet known — e.g. this is the peer's first announce, arriving
        before RNSBIND discovery has completed — the token is instead parked
        in _rns_to_sender_map keyed by sender NAME, so it can be resolved
        retroactively (by processOutgoing(), or eagerly by a later
        _handle_bind() call) once the key becomes known. Previously this case
        silently dropped the routing opportunity and could leave a peer
        permanently un-resolved if the "already in map" guard had nothing to
        latch onto.

        [FIX 3] The previous "only write if not already present" guard has
        been removed. The route is now refreshed on every successful
        reassembly, so a peer that changes its MeshCore key (firmware
        reflash, factory reset) self-heals instead of leaving a stale,
        dead key in the map until peer_ttl expires.

        Entries in _rns_to_mc_map represent paths that have demonstrably carried
        traffic, including paths that transit through an edge node to reach a
        downstream client (e.g. a phone connected to a hotspot on the edge node).
        These entries are trusted unconditionally for delivery; the peer capability
        flag is not consulted here.
        """
        # Echo suppression: MeshCore delivers our own channel messages back to us.
        if sender and sender == self._own_node_name:
            return

        # Restore base64 padding stripped during encode, then decode.
        b64 = text[len(self.MSG_PREFIX):].strip()
        b64 += "=" * (-len(b64) % 4)
        try:
            raw = base64.urlsafe_b64decode(b64)
        except Exception:
            return

        if len(raw) < self.HEADER_SIZE:
            return

        frag_idx   = raw[0]
        pkt_id     = raw[1]
        frag_total = raw[2]
        payload    = raw[self.HEADER_SIZE:]

        if frag_total == 0 or frag_idx >= frag_total:
            return

        key = (sender, pkt_id)

        # Deduplication: return early if this (sender, pkt_id) was already
        # fully delivered.  Move to end on hit for LRU ordering.
        with self._seen_lock:
            if key in self._seen_pkts:
                self._seen_pkts.move_to_end(key)
                return

        # Fragment reassembly
        with self._asm_lock:
            if key not in self._assembly:
                self._assembly[key]      = {}
                self._assembly_meta[key] = (frag_total, time.monotonic())

            if frag_idx in self._assembly[key]:
                return

            self._assembly[key][frag_idx] = payload

            if len(self._assembly[key]) < self._assembly_meta[key][0]:
                return  # Still waiting for remaining fragments

            # All fragments received — reassemble in index order
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

        # Mark as seen; prune cache if over the 512-entry LRU cap.
        with self._seen_lock:
            self._seen_pkts[key] = time.monotonic()
            if len(self._seen_pkts) > 512:
                while len(self._seen_pkts) > 256:
                    self._seen_pkts.popitem(last=False)

        if not full_packet:
            return

        # Dynamic L2/L3 link learning: index bytes 1:11 of the reassembled
        # packet (RNS origin token) against the MeshCore sender key so that
        # future outbound packets for this destination can be sent unicast.
        rns_token = self._extract_rns_token(full_packet)
        if rns_token and sender:
            with self._peer_lock:
                mc_key = self._peer_table.get(sender)
                if mc_key:
                    # [FIX 3] Always refresh — do not gate on "not already
                    # present" so a peer's key change self-heals.
                    self._rns_to_mc_map[rns_token] = mc_key
                    # A token that's now resolved should not also linger in
                    # the pending map under a stale association.
                    self._rns_to_sender_map.pop(rns_token, None)
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
                else:
                    # [FIX 1] Peer not yet resolved via RNSBIND — park the
                    # token by sender name for later/retroactive resolution
                    # instead of dropping it.
                    self._rns_to_sender_map[rns_token] = sender
                    if len(self._rns_to_sender_map) > self._RNS_PENDING_MAP_MAX:
                        trim = list(self._rns_to_sender_map.keys())[
                            : self._RNS_PENDING_MAP_MAX // 2
                        ]
                        for t in trim:
                            del self._rns_to_sender_map[t]
                    if self.debug_level == "debug":
                        RNS.log(
                            f"MeshCore_Dynamic_Interface [{self.name}]: "
                            f"Parked RNS token {rns_token.hex()[:8]} -> "
                            f"'{sender}' (MeshCore key not yet known; "
                            f"pending RNSBIND)",
                            RNS.LOG_DEBUG
                        )

        if full_packet:
            # Extract RNS packet type (bits 1-0 of the header byte)
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

    def _extract_rns_token(self, data: bytes) -> bytes:
        """
        Dynamically extracts an invariant 10-byte RNS address token.
        Excludes the volatile network hops byte (data[1]) to ensure tracking
        remains stable across multi-hop transport boundaries.
        """
        if len(data) < 3:
            return b""

        header_type = (data[0] >> 6) & 0x01
        dest_type = (data[0] >> 2) & 0x03

        # LINK Packets: Extract 10-byte Link ID
        if dest_type == 0x03: 
            return data[2:12] if len(data) >= 12 else b""
        
        # Header Type 2: Inbound multi-hop frame. Isolate 10 bytes of Peer Source Hash
        if header_type == 1:
            return data[18:28] if len(data) >= 28 else b""
        
        # Header Type 1: Single address frame. Isolate 10 bytes of Destination Hash
        return data[2:12] if len(data) >= 12 else b""
  
    # -------------------------------------------------------------------------
    # Outbound
    # -------------------------------------------------------------------------

    def _is_broadcast_packet(self, data: bytes) -> bool:
        """
        Return True if this RNS packet must be sent via channel broadcast rather
        than unicast direct message.

        Announces are always broadcast — all nodes need them for path discovery.
        Two-byte-header packets (bit 7 set) are also always broadcast.

        The packet type is in bits 1-0 of the header byte.  Bits 3-2 are the
        destination type — a different field.  Extracting bits 3-2 instead would
        cause unicast data packets to be misidentified as broadcast, adding
        unnecessary load to the shared channel.
        """
        if len(data) < 1:
            return True
        flags = data[0]
        if flags & 0x80:
            # Two-byte header format — always broadcast
            return True
        ptype = flags & 0x03   # bits 1-0 = packet type
        return ptype == self._RNS_PTYPE_ANNOUNCE

    def process_outgoing(self, data):
        """Compatibility shim — RNS may call either spelling."""
        return self.processOutgoing(data)
    
    def processOutgoing(self, data):
        """
        Fragment and enqueue an outbound RNS packet.

        Announce rate limiting  (outgoing_announce_rate, default 600 s)
        ─────────────────────────────────────────────────────────────────
        Suppresses re-forwarding of ANNOUNCE packets for any single destination
        seen within the rate window.  Independent of RNS's own announce_cap and
        announce_rate_target mechanisms, providing an additional layer of
        protection on constrained radio links.

        Path request rate limiting  (outgoing_path_req_rate, default 1800 s)
        ─────────────────────────────────────────────────────────────────────
        AP mode only suppresses ANNOUNCE re-broadcasting.  DATA+PLAIN packets
        (path requests, header byte 0x08) pass through AP mode unchecked.
        When a node goes offline after having been reachable, remote nodes on
        the wider mesh generate path requests for it continuously at the full
        RNS retry rate.  This limiter throttles how often any single destination
        is searched for via the LoRa channel.

        Routing decision
        ────────────────
        Broadcast packets (announces, link requests) always go to the shared
        channel.  For all other traffic:

          1. The RNS token is looked up directly in _rns_to_mc_map. 
             If found, the packet goes direct immediately.

          2. [FIX 1] If not found in the fast-path map, the token is checked
             against _rns_to_sender_map (tokens learned before the owning
             peer's MeshCore key was known). If a sender name is found there
             AND that peer has since completed RNSBIND (now present in
             _peer_table), the route is resolved on the spot, promoted into
             _rns_to_mc_map for future fast-path hits, and the packet goes
             direct on this same call — no need to wait for another announce
             cycle from the peer.

          3. Otherwise, the packet falls back to channel broadcast.

        The cached route is trusted unconditionally once resolved — it exists
        only because traffic has demonstrably flowed over it, including traffic
        that transits through an edge node to reach a downstream client.
        """
        if not self.online:
            return

        # Extract packet and destination type from the header byte for all
        # rate-limiting checks that follow.
        hdr_byte  = data[0] if data else 0
        ptype     = hdr_byte & 0x03          # bits 1-0 = packet type
        dest_type = (hdr_byte >> 2) & 0x03   # bits 3-2 = destination type

        # Per-destination outgoing announce rate limiter
        if self._announce_rate_s > 0 and len(data) >= 12:
            if ptype == self._RNS_PTYPE_ANNOUNCE:
                # Bytes 2:12 approximate the destination hash location.
                # False collisions between destinations are harmless — they
                # share a rate-limit bucket, nothing more.
                dest_id = bytes(data[2:12])
                now     = time.monotonic()
                with self._announce_sent_lock:
                    if now - self._announce_sent_times.get(dest_id, 0) < self._announce_rate_s:
                        return
                    self._announce_sent_times[dest_id] = now

        # Per-destination outgoing path request rate limiter
        if self._path_req_rate_s > 0 and len(data) >= 12:
            if ptype == self._RNS_PTYPE_DATA and dest_type == self._RNS_DTYPE_PLAIN:
                dest_id = bytes(data[2:12])
                now     = time.monotonic()
                with self._path_req_sent_lock:
                    if now - self._path_req_sent_times.get(dest_id, 0) < self._path_req_rate_s:
                        return
                    self._path_req_sent_times[dest_id] = now

        with self._pkt_id_lock:
            pkt_id       = self._pkt_id
            self._pkt_id = (self._pkt_id + 1) & 0xFF

        handler   = _PacketHandler(data, pkt_id, self.payload_size)
        broadcast = self._is_broadcast_packet(data)
        
        # Identify explicit network path discovery frames
        is_path_req = (ptype == self._RNS_PTYPE_DATA and dest_type == self._RNS_DTYPE_PLAIN)

        is_link_req = (ptype == self._RNS_PTYPE_LINK)
      
        # Resolve a unicast next-hop from the cached RNS→MC route map.
        target_key = None
        channel_reason = ""

        if broadcast:
            channel_reason = "Mandatory broadcast packet (e.g., Announce)"
        elif is_path_req:
            # Discovery signals must remain omnidirectional to traverse mesh topology
            channel_reason = "Network discovery frame (Path Request) - bypassing unicast map"
        elif is_link_req and self._peer_table:
            target_key = list(self._peer_table.values())[0]
        elif not self._has_direct_api:
            channel_reason = "Direct routing API disabled or undetected by interface"
        elif len(data) < 11:
            channel_reason = f"Packet too short to extract origin token (len: {len(data)})"
        else:
            # -------------------------------------------------------------
            # OUTBOUND TOKEN EXTRACTION
            # For outbound packets, the destination next-hop token is ALWAYS
            # derived from Address 1 (the Destination Hash), regardless of whether
            # it is a Header Type 1 or Header Type 2 packet.
            # -------------------------------------------------------------
            if dest_type == self._RNS_DTYPE_LINK:
                # Reticulum Wire Protocol Spec: LINK ID occupies bytes 2:12
                next_hop_token = data[2:12] if len(data) >= 12 else b""
            else:
                # Standard packet destination: Hops byte (data[1]) + first 9 bytes of Destination Address
                next_hop_token = data[2:12] if len(data) >= 12 else b""

            if not next_hop_token:
                channel_reason = "Packet too short for RNS token extraction"
            else:
                # -------------------------------------------------------------
                # DYNAMIC LINK TARGET RESOLUTION
                # If this payload is traveling over an established RNS link, the 
                # next_hop_token is a 10-byte truncated Link ID. We must query
                # Reticulum's global Transport table to find the actual target
                # identity hash associated with this specific connection session.
                # -------------------------------------------------------------
                lookup_token = next_hop_token
                if dest_type == self._RNS_DTYPE_LINK:
                    for link in getattr(RNS.Transport, "links", []):
                        if getattr(link, "link_id", b"")[:10] == next_hop_token:
                            if getattr(link, "type", None) == RNS.Link.OUT:
                                lookup_token = link.destination.hash
                            elif getattr(link, "type", None) == RNS.Link.IN and getattr(link, "remote_identity", None):
                                lookup_token = link.remote_identity.hash
                            break

                with self._peer_lock:
                    target_key = self._rns_to_mc_map.get(lookup_token)
                    resolved_pending_name = None
                    if not target_key:
                        # [FIX 1] Retroactive resolution: check the pending map
                        # for users whose identity was confirmed post-announcement.
                        pending_name = self._rns_to_sender_map.get(lookup_token)
                        if pending_name:
                            candidate_key = self._peer_table.get(pending_name)
                            if candidate_key:
                                target_key = candidate_key
                                self._rns_to_mc_map[lookup_token] = candidate_key
                                del self._rns_to_sender_map[lookup_token]
                                resolved_pending_name = pending_name

            if resolved_pending_name:
                RNS.log(
                    f"MeshCore_Dynamic_Interface [{self.name}]: "
                    f"Retroactively resolved direct route for token "
                    f"{lookup_token.hex()[:8]} -> '{resolved_pending_name}' "
                    f"(RNSBIND has since completed for this peer).",
                    RNS.LOG_INFO
                )

            if not target_key:
                channel_reason = f"No direct route bound for RNS token {lookup_token.hex()[:8]}"

        # Apply routing decision and log the result
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
                f"Routing -> DIRECT via peer key {target_key[:12].hex() if isinstance(target_key, bytes) else target_key}...",
                RNS.LOG_INFO
            )
            route = [("direct", target_key)]
            
        for frag_str in handler.fragments:
            for mode, target in route:
                self._loop.call_soon_threadsafe(
                    self._inject_frag, (mode, target, frag_str)
                )

        self.txb += len(data)

    def _inject_frag(self, item):
        """Thread-safe enqueue of a fragment tuple onto the async outqueue."""
        try:
            self._outqueue.put_nowait(item)
        except asyncio.QueueFull:
            pass

    async def _async_outgoing_worker(self):
        """
        Dequeue and transmit fragment tuples, enforcing per-fragment delays.

        Delay strategy:
          channel mode   (fragment_delay_s, default 2.5 s)
            Longer inter-fragment pause for broadcast on half-duplex shared
            media.  Gives other nodes time to receive each fragment and allows
            interleaving of traffic from different sources.

          direct mode    (direct_frag_delay_s, default 0.5 s)
            Shorter pause because MeshCore firmware handles ACK and retry for
            direct messages at the link layer, improving session latency without
            sacrificing delivery reliability.

        If rate_limit_bps is configured, the actual delay is the greater of the
        configured delay and the theoretical on-air transmission time.
        """
        while True:
            item = await self._outqueue.get()

            if not self.online or self._mc is None:
                await asyncio.sleep(0.5)
                try:
                    self._outqueue.put_nowait(item)
                except asyncio.QueueFull:
                    pass
                # [FIX 5] task_done() must be called for every get() exactly
                # once, regardless of which path the item takes. The previous
                # code "continue"d here without it, permanently inflating
                # asyncio.Queue's internal unfinished-tasks counter. Not
                # currently load-bearing (nothing calls queue.join() today),
                # but left uncorrected this would silently break any future
                # graceful-shutdown / drain logic added on top of this queue.
                self._outqueue.task_done()
                continue

            mode, target, frag_str = item

            try:
                if mode == "direct":
                    await self._mc.commands.send_msg(target, frag_str)
                else:
                    await self._mc.commands.send_chan_msg(self.channel_idx, frag_str)
            except Exception:
                if mode == "direct":
                    try:
                        self._outqueue.put_nowait(("channel", None, frag_str))
                    except asyncio.QueueFull:
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
        """Deliver a fully-reassembled RNS packet to the Reticulum stack."""
        if self.online and not self.detached:
            self.rxb += len(data)
            self.owner.inbound(data, self)

    def __str__(self):
        return f"MeshCore_Dynamic_Interface[{self.name}]"


# Required by the RNS custom interface loader
interface_class = MeshCore_Dynamic_Interface
