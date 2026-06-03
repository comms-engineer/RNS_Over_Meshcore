"""
MeshCore_Channel_Interface.py
Reticulum Network Stack (RNS) interface over a MeshCore channel (room).

Tunnels RNS traffic through a shared MeshCore channel rather than direct
messages.  Any node on the channel that also runs this interface can receive,
reassemble, and forward traffic — giving you the full MeshCore mesh as a
potential relay layer.

WIRE FORMAT
===========
Binary header (9 bytes) + payload, then base64-encoded and prefixed "RNS:":

  [magic:2B][src_id:4B][pkt_id:1B][frag_idx:1B][frag_total:1B][payload:N]

  magic      = b'RN'
  src_id     = stable 4-byte local node ID  (MeshCore channel messages do NOT
               expose sender pubkey — meshcore_py issue #59 — so we embed it)
  pkt_id     = rolling 0-255 counter
  frag_idx   = 0-based fragment index
  frag_total = total fragments for this packet

Channel messages are sent as text:  "RNS:<base64>"

Fragment sizing:
  MeshCore ≈200-char limit → "RNS:" (4) + base64(9+120) = 176 chars ✓
  At SF9/BW125 gives ~0.9 KB/s effective throughput.


TRANSPORT MODES
===============
This interface supports four transport modes:

  transport = serial     — direct serial connection to the MeshCore radio
  transport = ble        — direct BLE connection
  transport = tcp        — direct TCP connection
  transport = remoteterm — use RemoteTerm for MeshCore as the radio backend
                           (shares one radio with RemoteTerm; no meshcore lib needed)

The RemoteTerm mode is useful when you are already running
https://github.com/jkingsman/Remote-Terminal-for-MeshCore (which takes
exclusive ownership of the serial/BLE/TCP connection to the radio).  Instead
of fighting over the serial port, this interface talks to RemoteTerm's
REST + WebSocket API.  RemoteTerm drives the radio; the RNS interface
drives RemoteTerm.

  TX path (remoteterm):  RNS → processOutgoing() → fragment → queue
                         → worker thread → POST /api/messages/channel
  RX path (remoteterm):  WebSocket /api/ws → message event → parse
                         → reassemble → processIncoming() → RNS


INSTALLATION
============
1a. Direct radio (serial/ble/tcp):
      pip install meshcore  (--break-system-packages on RPi if no venv)

1b. RemoteTerm mode:
      pip install websockets  (--break-system-packages on RPi if no venv)
      (meshcore NOT required)

2. Copy this file to  ~/.reticulum/interfaces/

3. Add the appropriate stanza to ~/.reticulum/config  and restart rnsd.


CONFIG STANZA — direct radio (serial example)
=============================================
  [[MeshCore Channel]]
    type = MeshCore_Channel_Interface
    enabled = yes
    transport = serial
    port = /dev/ttyUSB0
    baudrate = 115200
    channel_idx    = 1
    channel_name   = RNS
    channel_secret = c4d2b6c8254e3b11200f57e95dcb1197
    fragment_delay  = 1.5
    fragment_timeout = 3600
    debug_level = debug


CONFIG STANZA — RemoteTerm mode (HTTP)
=======================================
  [[MeshCore Channel]]
    type = MeshCore_Channel_Interface
    enabled = yes
    transport        = remoteterm
    remoteterm_url   = http://localhost:8000
    channel_name     = RNS
    channel_secret   = c4d2b6c8254e3b11200f57e95dcb1197
    fragment_delay   = 1.5
    fragment_timeout = 3600
    debug_level = debug


CONFIG STANZA — RemoteTerm mode (HTTPS / self-signed cert)
===========================================================
  [[MeshCore Channel]]
    type = MeshCore_Channel_Interface
    enabled = yes
    transport             = remoteterm
    remoteterm_url        = https://host.docker.internal:8000
    remoteterm_ssl_verify = false   # set false for self-signed certs
    channel_name          = RNS
    channel_secret        = c4d2b6c8254e3b11200f57e95dcb1197
    fragment_delay        = 1.5
    fragment_timeout      = 3600
    debug_level = debug


IMPORTANT NOTE ON channel_idx vs. channel_key
==============================================
In direct (serial/ble/tcp) mode, the channel is identified by its slot index
on the radio (channel_idx, default 1).

In RemoteTerm mode, RemoteTerm manages slot assignments internally and
loads channels into slot 0 temporarily on every send.  You do NOT need
channel_idx for RemoteTerm mode.  The channel is identified by its key
(channel_secret).  RemoteTerm creates/updates the channel in its database
automatically on first use.
"""

import RNS
from RNS.Interfaces.Interface import Interface  # direct import avoids Python 3.13
                                                # exec() dotted-attr class-inheritance bug
import asyncio
import base64
import hashlib
import json
import queue
import socket
import threading
import time
from collections import OrderedDict
# urllib.error and urllib.request are imported lazily inside the three methods
# that use them (_send_via_remoteterm, _rt_get_sync, _rt_post_sync).
# Top-level submodule imports (dotted names) trigger a Python 3.13 bug in
# the exec() context that RNS uses to load external interface files.
# Lazy imports inside method bodies avoid the exec namespace entirely.


# ---------------------------------------------------------------------------
# PacketHandler — per-packet fragmentation for TX
# ---------------------------------------------------------------------------

class _PacketHandler:
    """Builds the list of ready-to-transmit fragment strings for one RNS packet."""

    MAGIC        = b'RN'
    HEADER_SIZE  = 9       # magic(2) + src_id(4) + pkt_id(1) + idx(1) + total(1)
    PAYLOAD_SIZE = 96     # conservative; need to experiment more with this.
    MSG_PREFIX   = "RNS:"

    def __init__(self, data: bytes, src_id: bytes, pkt_id: int):
        self.fragments = []

        raw_chunks = [data[i:i + self.PAYLOAD_SIZE]
                      for i in range(0, len(data), self.PAYLOAD_SIZE)]
        total = len(raw_chunks)

        for idx, chunk in enumerate(raw_chunks):
            header = (self.MAGIC
                      + src_id
                      + bytes([pkt_id & 0xFF, idx & 0xFF, total & 0xFF]))
            self.fragments.append(
                self.MSG_PREFIX + base64.urlsafe_b64encode(header + chunk).rstrip(b"=").decode()
            )

    def __len__(self):
        return len(self.fragments)


# ---------------------------------------------------------------------------
# Main interface class
# ---------------------------------------------------------------------------

class MeshCore_Channel_Interface(Interface):
    """
    RNS interface that tunnels traffic over a shared MeshCore channel.
    Supports direct radio connections (serial/ble/tcp) and RemoteTerm as a backend.
    """

    # Required by newer RNS Interface base class
    DEFAULT_IFAC_SIZE   = 8
    DEFAULT_IFAC_NAME   = ""
    DEFAULT_IFAC_NETKEY = b""

    MAGIC            = _PacketHandler.MAGIC
    HEADER_SIZE      = _PacketHandler.HEADER_SIZE
    MSG_PREFIX       = _PacketHandler.MSG_PREFIX
    OUTQUEUE_MAXSIZE = 512
    WORKER_POLL_S    = 0.05
    SETUP_TIMEOUT_S  = 30

    # -----------------------------------------------------------------------
    # __init__
    # -----------------------------------------------------------------------

    def __init__(self, owner, configuration):
        super().__init__()

        self.owner = owner
        self.name  = configuration.get("name", "MeshCore Channel")
        cfg        = configuration   # RNS passes a flat dict, not nested ["config"]

        # ---- transport ----
        self.transport   = cfg.get("transport", "serial").lower()

        # ---- direct radio params (serial/ble/tcp) ----
        self.port        = cfg.get("port",     "/dev/ttyUSB0")
        self.baudrate    = int(cfg.get("baudrate", 115200))
        self.host        = cfg.get("host",     "127.0.0.1")
        self.tcp_port    = int(cfg.get("tcp_port", 4403))
        self.ble_name    = cfg.get("ble_name", "")
        self.channel_idx = int(str(cfg.get("channel_idx", 1)).strip())

        # ---- RemoteTerm params ----
        rt_url = cfg.get("remoteterm_url", "http://localhost:8000").rstrip("/")
        self._rt_base_url = rt_url
        self._rt_ws_path  = cfg.get("remoteterm_ws_path", "/api/ws")
        self._rt_ws_url   = (
            rt_url.replace("http://", "ws://").replace("https://", "wss://")
            + self._rt_ws_path
        )
        self._rt_user = cfg.get("remoteterm_user", "")
        self._rt_pass = cfg.get("remoteterm_pass", "")
        self._rt_auth = (
            (self._rt_user, self._rt_pass)
            if self._rt_user else None
        )

        # ---- SSL context for HTTPS/WSS RemoteTerm connections ----
        # remoteterm_ssl_verify = false  →  skip cert validation (self-signed certs)
        # remoteterm_ssl_verify = true   →  full validation (default)
        _ssl_verify = cfg.get("remoteterm_ssl_verify", "true").lower()
        if _ssl_verify in ("false", "no", "0"):
            import ssl as _ssl_mod
            _ctx = _ssl_mod.create_default_context()
            _ctx.check_hostname = False
            _ctx.verify_mode    = _ssl_mod.CERT_NONE
            self._rt_ssl_ctx = _ctx
            RNS.log(
                f"MeshCore_Channel_Interface [{self.name}]: "
                "SSL certificate verification DISABLED (self-signed cert mode)",
                RNS.LOG_WARNING
            )
        else:
            self._rt_ssl_ctx = None

        # ---- channel (both modes) ----
        self.channel_name       = cfg.get("channel_name",   "RNS")
        self.channel_secret_hex = cfg.get("channel_secret",
                                          "c4d2b6c8254e3b11200f57e95dcb1197")

        # conversation_key as RemoteTerm reports it — populated in
        # _rt_ensure_channel() once we learn the actual key format RemoteTerm uses.
        # Fallback is our raw hex key.
        self._rt_conv_key = self.channel_secret_hex.lower()

        # ---- optional radio override (direct modes only) ----
        self.radio_freq = float(cfg.get("freq", 0))
        self.radio_bw   = float(cfg.get("bw",   0))
        self.radio_sf   = int(cfg.get("sf",     0))
        self.radio_cr   = int(cfg.get("cr",     0))

        # ---- reliability / pacing ----
        self.fragment_delay_s   = float(cfg.get("fragment_delay",   1.5))
        self.fragment_timeout_s = float(cfg.get("fragment_timeout", 3600))
        self.rate_limit_bps     = int(cfg.get("rate_limit",         0))

        # ---- debug ----
        self.debug_level = cfg.get("debug_level", "info").lower()

        # ---- internal state (shared by all transport modes) ----
        self._mc            = None
        self._EventType     = None
        self._loop          = None
        self._loop_thread   = None
        self._worker_thread = None

        self._own_src_id  = self._derive_local_src_id()
        self._pkt_id      = 0
        self._pkt_id_lock = threading.Lock()

        self._outqueue = queue.Queue(maxsize=self.OUTQUEUE_MAXSIZE)

        self._assembly      = {}
        self._assembly_meta = {}
        self._asm_lock      = threading.Lock()

        self._seen_pkts = OrderedDict()
        self._seen_lock = threading.Lock()

        self._setup_done = threading.Event()

        # ---- load libraries depending on transport ----
        if self.transport == "remoteterm":
            self._load_websockets_or_panic()
        else:
            self._load_meshcore_or_panic()

        # ---- start asyncio event loop thread ----
        self._loop = asyncio.new_event_loop()
        self._loop_thread = threading.Thread(
            target=self._run_loop, daemon=True,
            name=f"MCChan-loop-{self.name}"
        )
        self._loop_thread.start()

        # ---- start outgoing worker thread ----
        self._worker_thread = threading.Thread(
            target=self._outgoing_worker, daemon=True,
            name=f"MCChan-worker-{self.name}"
        )
        self._worker_thread.start()

        asyncio.run_coroutine_threadsafe(self._async_setup(), self._loop)

        if not self._setup_done.wait(timeout=self.SETUP_TIMEOUT_S):
            RNS.log(
                f"MeshCore_Channel_Interface [{self.name}]: "
                "setup timed out — check transport connection",
                RNS.LOG_ERROR
            )
        else:
            RNS.log(
                f"MeshCore_Channel_Interface [{self.name}]: constructed OK",
                RNS.LOG_DEBUG
            )

    # -----------------------------------------------------------------------
    # Library loading helpers
    # -----------------------------------------------------------------------

    def _load_meshcore_or_panic(self):
        try:
            import meshcore as _mc_module
            self._mc_module = _mc_module
            self._EventType = _mc_module.EventType
        except ImportError:
            RNS.log(
                f"MeshCore_Channel_Interface [{self.name}]: "
                "meshcore library not found. "
                "Install with: pip install meshcore [--break-system-packages]",
                RNS.LOG_CRITICAL
            )
            RNS.panic()

    def _load_websockets_or_panic(self):
        try:
            import websockets as _ws_module  # noqa: F401
            self._ws_module = _ws_module
        except ImportError:
            RNS.log(
                f"MeshCore_Channel_Interface [{self.name}]: "
                "websockets library not found (required for transport=remoteterm). "
                "Install with: pip install websockets [--break-system-packages]",
                RNS.LOG_CRITICAL
            )
            RNS.panic()

    # -----------------------------------------------------------------------
    # Stable local node ID
    # -----------------------------------------------------------------------

    def _derive_local_src_id(self) -> bytes:
        raw = f"{self.channel_secret_hex}:{socket.gethostname()}"
        return hashlib.sha256(raw.encode()).digest()[:4]

    # -----------------------------------------------------------------------
    # Event loop management
    # -----------------------------------------------------------------------

    def _run_loop(self):
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_forever()
        except Exception as exc:
            RNS.log(
                f"MeshCore_Channel_Interface [{self.name}]: "
                f"event loop crashed: {exc}",
                RNS.LOG_ERROR
            )

    def _run_coro(self, coro, timeout: float = 20.0):
        if self._loop is None or not self._loop.is_running():
            return None
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            return future.result(timeout=timeout)
        except asyncio.TimeoutError:
            RNS.log(
                f"MeshCore_Channel_Interface [{self.name}]: coroutine timed out",
                RNS.LOG_WARNING
            )
            return None
        except Exception as exc:
            RNS.log(
                f"MeshCore_Channel_Interface [{self.name}]: coroutine error: {exc}",
                RNS.LOG_WARNING
            )
            return None

    # -----------------------------------------------------------------------
    # Async setup dispatcher
    # -----------------------------------------------------------------------

    async def _async_setup(self):
        if self.transport == "remoteterm":
            await self._async_setup_remoteterm()
        else:
            await self._async_setup_direct()

    # =======================================================================
    # DIRECT TRANSPORT SETUP (serial / ble / tcp)
    # =======================================================================

    async def _async_setup_direct(self):
        MeshCore = self._mc_module.MeshCore
        ET       = self._EventType

        try:
            if self.transport == "serial":
                self._mc = await MeshCore.create_serial(self.port, self.baudrate)
                RNS.log(
                    f"MeshCore_Channel_Interface [{self.name}]: "
                    f"connected via serial {self.port}", RNS.LOG_INFO
                )
            elif self.transport == "ble":
                self._mc = await MeshCore.create_ble(self.ble_name or None)
                RNS.log(
                    f"MeshCore_Channel_Interface [{self.name}]: "
                    "connected via BLE", RNS.LOG_INFO
                )
            elif self.transport == "tcp":
                self._mc = await MeshCore.create_tcp(self.host, self.tcp_port)
                RNS.log(
                    f"MeshCore_Channel_Interface [{self.name}]: "
                    f"connected via TCP {self.host}:{self.tcp_port}", RNS.LOG_INFO
                )
            else:
                RNS.log(
                    f"MeshCore_Channel_Interface [{self.name}]: "
                    f"unknown transport '{self.transport}'",
                    RNS.LOG_CRITICAL
                )
                return
        except Exception as exc:
            RNS.log(
                f"MeshCore_Channel_Interface [{self.name}]: "
                f"connection failed: {exc}", RNS.LOG_ERROR
            )
            return

        try:
            result = await self._mc.commands.send_appstart()
            if result.type == ET.SELF_INFO:
                pk_hex = result.payload.get("public_key", "")
                if len(pk_hex) >= 8:
                    self._own_src_id = bytes.fromhex(pk_hex[:8])
                    RNS.log(
                        f"MeshCore_Channel_Interface [{self.name}]: "
                        f"node ID {pk_hex[:8]}", RNS.LOG_INFO
                    )
        except Exception as exc:
            RNS.log(
                f"MeshCore_Channel_Interface [{self.name}]: "
                f"send_appstart error: {exc}", RNS.LOG_WARNING
            )

        if self.radio_freq and self.radio_bw and self.radio_sf and self.radio_cr:
            try:
                result = await self._mc.commands.set_radio(
                    self.radio_freq, self.radio_bw, self.radio_sf, self.radio_cr)
                if result.type == ET.OK:
                    RNS.log(
                        f"MeshCore_Channel_Interface [{self.name}]: "
                        f"radio set freq={self.radio_freq} bw={self.radio_bw} "
                        f"sf={self.radio_sf} cr={self.radio_cr}", RNS.LOG_INFO
                    )
            except Exception as exc:
                RNS.log(
                    f"MeshCore_Channel_Interface [{self.name}]: "
                    f"radio config error: {exc}", RNS.LOG_WARNING
                )

        try:
            secret_bytes = bytes.fromhex(self.channel_secret_hex)
            if len(secret_bytes) != 16:
                raise ValueError(f"channel_secret must be 16 bytes, got {len(secret_bytes)}")
            result = await self._mc.commands.set_channel(
                self.channel_idx, self.channel_name, secret_bytes)
            if result.type == ET.OK:
                RNS.log(
                    f"MeshCore_Channel_Interface [{self.name}]: "
                    f"channel {self.channel_idx} ('{self.channel_name}') configured",
                    RNS.LOG_INFO
                )
        except Exception as exc:
            RNS.log(
                f"MeshCore_Channel_Interface [{self.name}]: "
                f"channel config error: {exc}", RNS.LOG_WARNING
            )

        # Subscribe WITHOUT attribute_filters — some firmware versions report
        # all received channel messages with channel_idx=0 regardless of the
        # actual slot.  Filtering by the "RNS:" prefix in _process_tunnel_text
        # is sufficient and more reliable across firmware versions.
        self._mc.subscribe(
            self._EventType.CHANNEL_MSG_RECV,
            self._on_channel_msg
        )
        RNS.log(
            f"MeshCore_Channel_Interface [{self.name}]: "
            "subscribed to CHANNEL_MSG_RECV (all channel indices)",
            RNS.LOG_DEBUG
        )

        await self._mc.start_auto_message_fetching()
        asyncio.create_task(self._cleanup_loop())

        self.online = True
        self._setup_done.set()
        RNS.log(
            f"MeshCore_Channel_Interface [{self.name}]: "
            f"online (direct), channel slot {self.channel_idx}",
            RNS.LOG_INFO
        )

    # =======================================================================
    # REMOTETERM TRANSPORT SETUP
    # =======================================================================

    async def _async_setup_remoteterm(self):
        RNS.log(
            f"MeshCore_Channel_Interface [{self.name}]: "
            f"connecting to RemoteTerm at {self._rt_base_url}",
            RNS.LOG_INFO
        )

        try:
            health = await self._rt_get("/api/health")
            if self.debug_level in ("debug", "info"):
                RNS.log(
                    f"MeshCore_Channel_Interface [{self.name}]: "
                    f"RemoteTerm health: {health.get('status', '?')}",
                    RNS.LOG_DEBUG
                )
        except Exception as exc:
            RNS.log(
                f"MeshCore_Channel_Interface [{self.name}]: "
                f"cannot reach RemoteTerm at {self._rt_base_url}: {exc}",
                RNS.LOG_ERROR
            )
            return

        await self._rt_ensure_channel()

        RNS.log(
            f"MeshCore_Channel_Interface [{self.name}]: "
            f"local src_id = {self._own_src_id.hex()}  "
            f"conversation_key = {self._rt_conv_key}",
            RNS.LOG_INFO
        )

        asyncio.create_task(self._remoteterm_ws_listener())
        asyncio.create_task(self._cleanup_loop())

        self.online = True
        self._setup_done.set()
        RNS.log(
            f"MeshCore_Channel_Interface [{self.name}]: "
            f"online (remoteterm), channel '{self.channel_name}'",
            RNS.LOG_INFO
        )

    async def _rt_ensure_channel(self):
        """
        Ensure the tunnel channel exists in RemoteTerm's database.
        Stores the actual conversation_key format RemoteTerm uses so the
        WebSocket filter matches correctly regardless of key normalisation.
        """
        our_key = self.channel_secret_hex.lower()
        try:
            channels = await self._rt_get("/api/channels")
            if isinstance(channels, list):
                for ch in channels:
                    ch_key = ch.get("key", "").lower()
                    if ch_key == our_key:
                        # Store the key exactly as RemoteTerm knows it
                        self._rt_conv_key = ch_key
                        RNS.log(
                            f"MeshCore_Channel_Interface [{self.name}]: "
                            f"tunnel channel '{self.channel_name}' found in RemoteTerm "
                            f"(conversation_key={self._rt_conv_key})",
                            RNS.LOG_INFO
                        )
                        return
        except Exception as exc:
            RNS.log(
                f"MeshCore_Channel_Interface [{self.name}]: "
                f"could not list RemoteTerm channels: {exc}",
                RNS.LOG_WARNING
            )

        # Channel not found — create it
        try:
            resp = await self._rt_post("/api/channels", {
                "name": self.channel_name,
                "key":  self.channel_secret_hex,
            })
            # RemoteTerm may return the created channel; extract its key if present
            if isinstance(resp, dict):
                created_key = resp.get("key", "").lower()
                if created_key:
                    self._rt_conv_key = created_key
            RNS.log(
                f"MeshCore_Channel_Interface [{self.name}]: "
                f"created tunnel channel '{self.channel_name}' "
                f"(conversation_key={self._rt_conv_key})",
                RNS.LOG_INFO
            )
        except Exception as exc:
            RNS.log(
                f"MeshCore_Channel_Interface [{self.name}]: "
                f"failed to create channel: {exc}. "
                "Sends may fail until the channel exists.",
                RNS.LOG_WARNING
            )

    # -----------------------------------------------------------------------
    # RemoteTerm WebSocket listener
    # -----------------------------------------------------------------------

    async def _remoteterm_ws_listener(self):
        """
        Persistent coroutine: maintains WebSocket to RemoteTerm and dispatches
        incoming channel messages.  Reconnects with exponential backoff (2→60 s).
        """
        import websockets

        backoff = 2.0

        while True:
            try:
                extra_headers = {}
                if self._rt_auth:
                    import base64 as _b64
                    cred = _b64.b64encode(
                        f"{self._rt_auth[0]}:{self._rt_auth[1]}".encode()
                    ).decode()
                    extra_headers["Authorization"] = f"Basic {cred}"

                ws_ssl = None
                if self._rt_ws_url.startswith("wss://"):
                    ws_ssl = self._rt_ssl_ctx if self._rt_ssl_ctx is not None else True

                RNS.log(
                    f"MeshCore_Channel_Interface [{self.name}]: "
                    f"connecting WebSocket {self._rt_ws_url}  ssl={ws_ssl is not None}",
                    RNS.LOG_DEBUG
                )

                async with websockets.connect(
                    self._rt_ws_url,
                    additional_headers=extra_headers,
                    ping_interval=20,
                    ping_timeout=10,
                    ssl=ws_ssl,
                ) as ws:
                    backoff = 2.0
                    RNS.log(
                        f"MeshCore_Channel_Interface [{self.name}]: "
                        "RemoteTerm WebSocket connected",
                        RNS.LOG_INFO
                    )

                    async for raw in ws:
                        try:
                            event = json.loads(raw)
                        except json.JSONDecodeError:
                            continue
                        await self._on_remoteterm_ws_event(event)

            except Exception as exc:
                RNS.log(
                    f"MeshCore_Channel_Interface [{self.name}]: "
                    f"WebSocket error: {exc}. Reconnecting in {backoff:.0f}s",
                    RNS.LOG_WARNING
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60.0)

    async def _on_remoteterm_ws_event(self, event: dict):
        """
        Filter and dispatch one WebSocket event from RemoteTerm.

        In debug mode every event is logged so you can see the actual structure
        RemoteTerm sends.  This is intentionally verbose — set debug_level = info
        once the receive path is confirmed working.
        """
        evt_type = event.get("type", "?")

        if self.debug_level == "debug":
            # Log every event so we can see what's arriving
            data_preview = ""
            data = event.get("data", {})
            if isinstance(data, dict):
                data_preview = (
                    f"type={data.get('type')}  "
                    f"conv_key={str(data.get('conversation_key',''))[:32]}  "
                    f"outgoing={data.get('outgoing')}  "
                    f"text={str(data.get('text',''))[:40]}"
                )
            RNS.log(
                f"MeshCore_Channel_Interface [{self.name}]: "
                f"WS evt type={evt_type!r}  {data_preview}",
                RNS.LOG_DEBUG
            )

        if evt_type != "message":
            return

        msg = event.get("data", {})
        if not isinstance(msg, dict):
            return

        msg_type = msg.get("type", "")
        if msg_type != "CHAN":
            if self.debug_level == "debug":
                RNS.log(
                    f"MeshCore_Channel_Interface [{self.name}]: "
                    f"WS drop: msg type={msg_type!r} (not CHAN)",
                    RNS.LOG_DEBUG
                )
            return

        # Compare conversation_key against the key as RemoteTerm reported it
        # at startup (_rt_conv_key).  Both sides lower-cased.
        conv_key = msg.get("conversation_key", "").lower()
        if conv_key != self._rt_conv_key:
            if self.debug_level == "debug":
                RNS.log(
                    f"MeshCore_Channel_Interface [{self.name}]: "
                    f"WS drop: conv_key={conv_key!r} != expected={self._rt_conv_key!r}",
                    RNS.LOG_DEBUG
                )
            return

        if msg.get("outgoing", False):
            if self.debug_level == "debug":
                RNS.log(
                    f"MeshCore_Channel_Interface [{self.name}]: "
                    "WS drop: outgoing=True (our own echo)",
                    RNS.LOG_DEBUG
                )
            return

        text = msg.get("text", "")
        if self.debug_level == "debug":
            RNS.log(
                f"MeshCore_Channel_Interface [{self.name}]: "
                f"WS CHAN message accepted, passing to tunnel pipeline "
                f"text={text[:50]}",
                RNS.LOG_DEBUG
            )
        await self._process_tunnel_text(text)

    # =======================================================================
    # DIRECT TRANSPORT: incoming channel message callback
    # =======================================================================

    async def _on_channel_msg(self, event):
        """
        Called by meshcore_py on any CHANNEL_MSG_RECV event.

        We subscribe without attribute_filters because some firmware versions
        report all received channel messages with channel_idx=0 regardless of
        the actual configured slot, which would silently drop everything.
        Channel identity is verified by the "RNS:" prefix in _process_tunnel_text.
        """
        payload  = event.payload
        recv_idx = payload.get("channel_idx", "?")
        text     = payload.get("text", "")

        if self.debug_level == "debug":
            RNS.log(
                f"MeshCore_Channel_Interface [{self.name}]: "
                f"CHANNEL_MSG_RECV channel_idx={recv_idx}  "
                f"text={text[:60]}",
                RNS.LOG_DEBUG
            )

        await self._process_tunnel_text(text)

    # =======================================================================
    # SHARED INCOMING PIPELINE
    # =======================================================================

    async def _process_tunnel_text(self, text: str):
        """
        Decode, validate header, and reassemble one channel message.
        All silent drops are logged in debug mode.
        """
        # MeshCore prepends the sender's node name to channel message text,
        # e.g. "Janus39: RNS:..." instead of "RNS:...".
        # Find the RNS: marker wherever it appears and strip everything before it.
        rns_idx = text.find(self.MSG_PREFIX)
        if rns_idx == -1:
            if self.debug_level == "debug" and text:
                RNS.log(
                    f"MeshCore_Channel_Interface [{self.name}]: "
                    f"pipeline drop: no RNS: marker found  text={text[:40]}",
                    RNS.LOG_DEBUG
                )
            return
        if rns_idx > 0:
            RNS.log(
                f"MeshCore_Channel_Interface [{self.name}]: "
                f"stripped sender prefix {text[:rns_idx]!r}",
                RNS.LOG_DEBUG
            )
            text = text[rns_idx:]

        b64 = text[len(self.MSG_PREFIX):].strip()

        b64 += "=" * (-len(b64) % 4)

        try:

            raw = base64.urlsafe_b64decode(b64)

        except Exception as exc:
            RNS.log(
                f"MeshCore_Channel_Interface [{self.name}]: "
                f"base64 decode error: {exc} "
                f"len={len(b64)} "
                f"mod4={len(b64)%4} "
                f"text={text[:80]}",
                RNS.LOG_WARNING
            )
            return

        if len(raw) < self.HEADER_SIZE:
            RNS.log(
                f"MeshCore_Channel_Interface [{self.name}]: "
                f"pipeline drop: frame too short ({len(raw)} < {self.HEADER_SIZE})",
                RNS.LOG_WARNING
            )
            return

        magic      = raw[0:2]
        src_id     = raw[2:6]
        pkt_id     = raw[6]
        frag_idx   = raw[7]
        frag_total = raw[8]
        payload    = raw[self.HEADER_SIZE:]

        if frag_total == 0:
            RNS.log(
                f"MeshCore_Channel_Interface [{self.name}]: "
                "invalid fragment count 0",
                RNS.LOG_WARNING
            )
            return

        if frag_idx >= frag_total:
            RNS.log(
                f"MeshCore_Channel_Interface [{self.name}]: "
                f"invalid fragment index "
                f"{frag_idx}/{frag_total}",
                RNS.LOG_WARNING
            )
            return

        if magic != self.MAGIC:
            if self.debug_level == "debug":
                RNS.log(
                    f"MeshCore_Channel_Interface [{self.name}]: "
                    f"pipeline drop: bad magic {magic!r}",
                    RNS.LOG_DEBUG
                )
            return

        if src_id == self._own_src_id:
            if self.debug_level == "debug":
                RNS.log(
                    f"MeshCore_Channel_Interface [{self.name}]: "
                    f"pipeline drop: own echo src={src_id.hex()}",
                    RNS.LOG_DEBUG
                )
            return

        src_hex = src_id.hex()
        key     = (src_hex, pkt_id)

        cache_hit = False

        with self._seen_lock:
            cache_hit = key in self._seen_pkts
            if cache_hit:
                self._seen_pkts.move_to_end(key)

        if self.debug_level == "debug":
            RNS.log(
                f"MeshCore_Channel_Interface [{self.name}]: "
                f"dedupe check "
                f"src={src_hex} "
                f"pkt={pkt_id} "
                f"frag={frag_idx+1}/{frag_total} "
                f"cache_hit={cache_hit}",
                RNS.LOG_DEBUG
            )

        if cache_hit:
            if self.debug_level == "debug":
                RNS.log(
                    f"MeshCore_Channel_Interface [{self.name}]: "
                    f"pipeline drop: already delivered "
                    f"(src={src_hex} pkt={pkt_id})",
                    RNS.LOG_DEBUG
                )
            return

        RNS.log(
            f"MeshCore_Channel_Interface [{self.name}]: "
            f"RX frag src={src_hex}  pkt={pkt_id}  "
            f"{frag_idx+1}/{frag_total}  payload={len(payload)}B",
            RNS.LOG_DEBUG
        )

        with self._asm_lock:
            if key not in self._assembly:
                self._assembly[key]      = {}
                self._assembly_meta[key] = (frag_total, time.monotonic())

            if frag_idx in self._assembly[key]:
                if self.debug_level == "debug":
                    RNS.log(
                        f"MeshCore_Channel_Interface [{self.name}]: "
                        f"assembly state src={src_hex} "
                        f"pkt={pkt_id} "
                        f"stored={len(self._assembly[key])}/{frag_total}",
                        RNS.LOG_DEBUG
                    )
                return

            self._assembly[key][frag_idx] = payload

            expected_total = self._assembly_meta[key][0]
            if len(self._assembly[key]) < expected_total:
                return

            missing = [
                i for i in range(expected_total)
                if i not in self._assembly[key]
            ]

            if missing:
                RNS.log(
                    f"MeshCore_Channel_Interface [{self.name}]: "
                    f"reassembly incomplete despite count match "
                    f"missing={missing}",
                    RNS.LOG_WARNING
                )
                return

            try:
                full_packet = b"".join(
                    self._assembly[key][i] for i in range(expected_total)
                )

                del self._assembly[key]
                del self._assembly_meta[key]

            except KeyError as exc:
                RNS.log(
                    f"MeshCore_Channel_Interface [{self.name}]: "
                    f"reassembly gap — missing fragment {exc}",
                    RNS.LOG_WARNING
                )
                del self._assembly[key]
                del self._assembly_meta[key]
                return
            except Exception as exc:
                RNS.log(
                    f"MeshCore_Channel_Interface [{self.name}]: "
                    f"reassembly failed: {exc}",
                    RNS.LOG_ERROR
                )
                del self._assembly[key]
                del self._assembly_meta[key]
                return

            RNS.log(
                f"MeshCore_Channel_Interface [{self.name}]: "
                f"reassembly complete "
                f"src={src_hex} "
                f"pkt={pkt_id} "
                f"len={len(full_packet)}",
                RNS.LOG_DEBUG
            )

        with self._seen_lock:
            self._seen_pkts[key] = time.monotonic()

            if len(self._seen_pkts) > 512:
                while len(self._seen_pkts) > 256:
                    self._seen_pkts.popitem(last=False)

        RNS.log(
            f"MeshCore_Channel_Interface [{self.name}]: "
            f"RX reassembled {len(full_packet)}B from src={src_hex}",
            RNS.LOG_INFO
        )

        try:
            if len(full_packet) == 0:
                RNS.log(
                    f"MeshCore_Channel_Interface [{self.name}]: "
                    "empty reassembled packet",
                    RNS.LOG_WARNING
                )
                return

            RNS.log(
                f"MeshCore_Channel_Interface [{self.name}]: "
                f"reassembled len={len(full_packet)} "
                f"from {expected_total} fragments",
                RNS.LOG_DEBUG
            )

            self.processIncoming(full_packet)

            RNS.log(
                f"MeshCore_Channel_Interface [{self.name}]: "
                f"delivered packet "
                f"src={src_hex} "
                f"pkt={pkt_id} "
                f"len={len(full_packet)}",
                RNS.LOG_INFO
            )

        except Exception as exc:
            RNS.log(
                f"MeshCore_Channel_Interface [{self.name}]: "
                f"processIncoming failed: {exc}",
                RNS.LOG_ERROR
            )

    async def _cleanup_loop(self):
        while True:
            await asyncio.sleep(60)
            deadline = time.monotonic() - self.fragment_timeout_s
            with self._asm_lock:
                stale = [k for k, (_, ts) in self._assembly_meta.items()
                         if ts < deadline]
                for k in stale:
                    del self._assembly[k]
                    del self._assembly_meta[k]
                    RNS.log(
                        f"MeshCore_Channel_Interface [{self.name}]: "
                        f"evicted stale assembly {k}",
                        RNS.LOG_WARNING
                    )

    # =======================================================================
    # OUTGOING PATH
    # =======================================================================

    def process_outgoing(self, data):
        return self.processOutgoing(data)

    def processOutgoing(self, data):
        if not self.online:
            return

        with self._pkt_id_lock:
            pkt_id       = self._pkt_id
            self._pkt_id = (self._pkt_id + 1) & 0xFF

        handler = _PacketHandler(data, self._own_src_id, pkt_id)

        RNS.log(
            f"MeshCore_Channel_Interface [{self.name}]: "
            f"TX {len(data)}B → {len(handler)} frag(s)  pkt_id={pkt_id}",
            RNS.LOG_DEBUG
        )

        for frag_str in handler.fragments:
            try:
                self._outqueue.put_nowait(frag_str)
            except queue.Full:
                RNS.log(
                    f"MeshCore_Channel_Interface [{self.name}]: "
                    "outgoing queue full — packet dropped",
                    RNS.LOG_WARNING
                )
                return

        self.txb += len(data)

    def _outgoing_worker(self):
        ET = None

        while True:
            try:
                frag_str = self._outqueue.get(timeout=self.WORKER_POLL_S)
            except queue.Empty:
                continue

            if not self.online:
                try:
                    self._outqueue.put_nowait(frag_str)
                except queue.Full:
                    pass
                time.sleep(0.5)
                continue

            if self.transport == "remoteterm":
                self._send_via_remoteterm(frag_str)
            else:
                if self._mc is None:
                    try:
                        self._outqueue.put_nowait(frag_str)
                    except queue.Full:
                        pass
                    time.sleep(0.5)
                    continue

                if ET is None:
                    ET = self._EventType

                result = self._run_coro(
                    self._mc.commands.send_chan_msg(self.channel_idx, frag_str)
                )

                if result is None:
                    RNS.log(
                        f"MeshCore_Channel_Interface [{self.name}]: "
                        "send_chan_msg timed out",
                        RNS.LOG_WARNING
                    )
                elif result.type == ET.ERROR:
                    RNS.log(
                        f"MeshCore_Channel_Interface [{self.name}]: "
                        f"send_chan_msg error: {result.payload}",
                        RNS.LOG_WARNING
                    )

            delay = self.fragment_delay_s
            if self.rate_limit_bps > 0:
                delay = max(delay, (len(frag_str) * 3 // 4) / self.rate_limit_bps)
            time.sleep(delay)

    def _send_via_remoteterm(self, frag_str: str):
        import urllib.error
        import urllib.request

        url  = f"{self._rt_base_url}/api/messages/channel"
        body = json.dumps({
            "channel_key": self.channel_secret_hex,
            "text":        frag_str,
        }).encode()

        req = urllib.request.Request(
            url, data=body,
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        if self._rt_auth:
            import base64 as _b64
            cred = _b64.b64encode(
                f"{self._rt_auth[0]}:{self._rt_auth[1]}".encode()
            ).decode()
            req.add_header("Authorization", f"Basic {cred}")

        try:
            with urllib.request.urlopen(
                req, context=self._rt_ssl_ctx, timeout=15
            ) as resp:
                if self.debug_level == "debug":
                    RNS.log(
                        f"MeshCore_Channel_Interface [{self.name}]: "
                        f"TX frag → RemoteTerm HTTP {resp.status}",
                        RNS.LOG_DEBUG
                    )
        except urllib.error.HTTPError as exc:
            snippet = ""
            try:
                snippet = exc.read(200).decode(errors="replace")
            except Exception:
                pass
            RNS.log(
                f"MeshCore_Channel_Interface [{self.name}]: "
                f"RemoteTerm HTTP {exc.code}: {snippet}",
                RNS.LOG_WARNING
            )
        except Exception as exc:
            RNS.log(
                f"MeshCore_Channel_Interface [{self.name}]: "
                f"RemoteTerm send error: {exc}",
                RNS.LOG_WARNING
            )

    # =======================================================================
    # RemoteTerm HTTP helpers
    # =======================================================================

    async def _rt_get(self, path: str) -> dict:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._rt_get_sync, path)

    async def _rt_post(self, path: str, data: dict) -> dict:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._rt_post_sync, path, data)

    def _rt_get_sync(self, path: str) -> dict:
        import urllib.request
        url = self._rt_base_url + path
        req = urllib.request.Request(url)
        if self._rt_auth:
            import base64 as _b64
            cred = _b64.b64encode(
                f"{self._rt_auth[0]}:{self._rt_auth[1]}".encode()
            ).decode()
            req.add_header("Authorization", f"Basic {cred}")
        with urllib.request.urlopen(req, context=self._rt_ssl_ctx, timeout=10) as resp:
            return json.loads(resp.read())

    def _rt_post_sync(self, path: str, data: dict) -> dict:
        import urllib.request
        url  = self._rt_base_url + path
        body = json.dumps(data).encode()
        req  = urllib.request.Request(
            url, data=body,
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        if self._rt_auth:
            import base64 as _b64
            cred = _b64.b64encode(
                f"{self._rt_auth[0]}:{self._rt_auth[1]}".encode()
            ).decode()
            req.add_header("Authorization", f"Basic {cred}")
        with urllib.request.urlopen(req, context=self._rt_ssl_ctx, timeout=10) as resp:
            return json.loads(resp.read())

    # =======================================================================
    # RNS interface bookkeeping
    # =======================================================================

    def processIncoming(self, data: bytes):
        # RNS 1.x Interface base class has no processIncoming method.
        # The correct pattern (matching TCPClientInterface and all other RNS
        # interfaces) is: update rxb, then call owner.inbound() directly.
        # super() also does not work in exec()'d files under Python 3.13.
        if self.online and not self.detached:
            self.rxb += len(data)
            self.owner.inbound(data, self)

    def __str__(self):
        return f"MeshCore_Channel_Interface[{self.name}]"


# RNS's _synthesize_interface looks for this name in the exec'd globals
interface_class = MeshCore_Channel_Interface
