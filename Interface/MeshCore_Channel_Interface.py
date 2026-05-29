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

  transport = serial    — direct serial connection to the MeshCore radio
  transport = ble       — direct BLE connection
  transport = tcp       — direct TCP connection
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
    channel_idx    = 39
    channel_name   = RNS
    channel_secret = c4d2b6c8254e3b11200f57e95dcb1197
    fragment_delay  = 1.5
    fragment_timeout = 3600
    debug_level = info


CONFIG STANZA — RemoteTerm mode
================================
  [[MeshCore Channel]]
    type = MeshCore_Channel_Interface
    enabled = yes

    # Point at RemoteTerm (must be running and connected to the radio)
    transport        = remoteterm
    remoteterm_url   = http://localhost:8000
    # remoteterm_ws_path = /api/ws          # default; change only if RemoteTerm moves it
    # remoteterm_user = youruser            # only needed if you set MESHCORE_BASIC_AUTH_*
    # remoteterm_pass = yourpass

    # Channel — RemoteTerm will create it if it does not already exist.
    # All nodes MUST use identical channel_name and channel_secret values.
    channel_name   = RNS
    channel_secret = c4d2b6c8254e3b11200f57e95dcb1197   # 16 bytes hex

    fragment_delay   = 1.5
    fragment_timeout = 3600
    debug_level = info


IMPORTANT NOTE ON channel_idx vs. channel_key
==============================================
In direct (serial/ble/tcp) mode, the channel is identified by its slot index
on the radio (channel_idx, default 39).

In RemoteTerm mode, RemoteTerm manages slot assignments internally and
load channels into slot 0 temporarily on every send.  You do NOT need
channel_idx for RemoteTerm mode.  The channel is identified by its key
(channel_secret).  RemoteTerm creates/updates the channel in its database
automatically on first use.
"""

import RNS
import asyncio
import base64
import hashlib
import json
import queue
import socket
import threading
import time
import urllib.error
import urllib.request


# ---------------------------------------------------------------------------
# PacketHandler — per-packet fragmentation for TX
# ---------------------------------------------------------------------------

class _PacketHandler:
    """Builds the list of ready-to-transmit fragment strings for one RNS packet."""

    MAGIC        = b'RN'
    HEADER_SIZE  = 9       # magic(2) + src_id(4) + pkt_id(1) + idx(1) + total(1)
    PAYLOAD_SIZE = 120     # conservative fragment payload; see sizing note above
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
                self.MSG_PREFIX + base64.b64encode(header + chunk).decode("ascii")
            )

    def __len__(self):
        return len(self.fragments)


# ---------------------------------------------------------------------------
# Main interface class
# ---------------------------------------------------------------------------

class MeshCore_Channel_Interface(RNS.Interfaces.Interface):
    """
    RNS interface that tunnels traffic over a shared MeshCore channel.
    Supports direct radio connections (serial/ble/tcp) and RemoteTerm as a backend.
    """

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
        self.name  = configuration["name"]
        cfg        = configuration["config"]

        # ---- transport ----
        self.transport   = cfg.get("transport", "serial").lower()

        # ---- direct radio params (serial/ble/tcp) ----
        self.port        = cfg.get("port",     "/dev/ttyUSB0")
        self.baudrate    = int(cfg.get("baudrate", 115200))
        self.host        = cfg.get("host",     "127.0.0.1")
        self.tcp_port    = int(cfg.get("tcp_port", 4403))
        self.ble_name    = cfg.get("ble_name", "")
        self.channel_idx = int(cfg.get("channel_idx", 39))

        # ---- RemoteTerm params ----
        rt_url = cfg.get("remoteterm_url", "http://localhost:8000").rstrip("/")
        self._rt_base_url  = rt_url
        self._rt_ws_path   = cfg.get("remoteterm_ws_path", "/api/ws")
        self._rt_ws_url    = (
            rt_url.replace("http://", "ws://").replace("https://", "wss://")
            + self._rt_ws_path
        )
        self._rt_user      = cfg.get("remoteterm_user", "")
        self._rt_pass      = cfg.get("remoteterm_pass", "")
        self._rt_auth      = (
            (self._rt_user, self._rt_pass)
            if self._rt_user else None
        )

        # ---- channel (both modes) ----
        self.channel_name       = cfg.get("channel_name",   "RNSTunnel")
        self.channel_secret_hex = cfg.get("channel_secret",
                                          "c4d2b6c8254e3b11200f57e95dcb1197")

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
        self._mc          = None   # meshcore.MeshCore  (direct modes only)
        self._EventType   = None   # meshcore.EventType (direct modes only)
        self._loop        = None
        self._loop_thread = None
        self._worker_thread = None

        # src_id:
        #   direct  — first 4 bytes of the radio's own public key (from SELF_INFO)
        #   remoteterm — SHA256(channel_secret + hostname)[:4], stable per host
        self._own_src_id   = self._derive_local_src_id()
        self._pkt_id       = 0
        self._pkt_id_lock  = threading.Lock()

        self._outqueue     = queue.Queue(maxsize=self.OUTQUEUE_MAXSIZE)

        # Reassembly state
        self._assembly      = {}   # (src_hex, pkt_id) → {frag_idx: bytes}
        self._assembly_meta = {}   # (src_hex, pkt_id) → (frag_total, monotonic_ts)
        self._asm_lock      = threading.Lock()

        # Delivered-packet dedup (echo / late-duplicate suppression)
        self._seen_pkts  = set()
        self._seen_lock  = threading.Lock()

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

        # ---- kick off async setup ----
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
        """
        Derive a stable 4-byte local node ID.

        For direct transports this is overwritten with the real radio pubkey
        once SELF_INFO arrives.  For RemoteTerm transport (where we can't
        directly query the pubkey), this hash stays in place permanently.
        It is stable across restarts on the same host using the same channel.
        """
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
        """Submit *coro* to our asyncio loop from any thread; block until done."""
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

        # -- connect --
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
                    f"unknown transport '{self.transport}' — "
                    "expected serial | ble | tcp | remoteterm",
                    RNS.LOG_CRITICAL
                )
                return
        except Exception as exc:
            RNS.log(
                f"MeshCore_Channel_Interface [{self.name}]: "
                f"connection failed: {exc}", RNS.LOG_ERROR
            )
            return

        # -- learn our own node ID --
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

        # -- optional radio parameter override --
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
                else:
                    RNS.log(
                        f"MeshCore_Channel_Interface [{self.name}]: "
                        "radio config returned non-OK", RNS.LOG_WARNING
                    )
            except Exception as exc:
                RNS.log(
                    f"MeshCore_Channel_Interface [{self.name}]: "
                    f"radio config error: {exc}", RNS.LOG_WARNING
                )

        # -- configure tunnel channel --
        try:
            secret_bytes = bytes.fromhex(self.channel_secret_hex)
            if len(secret_bytes) != 16:
                raise ValueError(
                    f"channel_secret must be 16 bytes (32 hex chars); "
                    f"got {len(secret_bytes)}"
                )
            result = await self._mc.commands.set_channel(
                self.channel_idx, self.channel_name, secret_bytes)
            if result.type == ET.OK:
                RNS.log(
                    f"MeshCore_Channel_Interface [{self.name}]: "
                    f"channel {self.channel_idx} ('{self.channel_name}') configured",
                    RNS.LOG_INFO
                )
            else:
                RNS.log(
                    f"MeshCore_Channel_Interface [{self.name}]: "
                    "set_channel returned non-OK (channel may already be correct)",
                    RNS.LOG_WARNING
                )
        except Exception as exc:
            RNS.log(
                f"MeshCore_Channel_Interface [{self.name}]: "
                f"channel config error: {exc}", RNS.LOG_WARNING
            )

        # -- subscribe to incoming channel messages --
        self._mc.subscribe(
            self._EventType.CHANNEL_MSG_RECV,
            self._on_channel_msg,
            attribute_filters={"channel_idx": self.channel_idx}
        )

        await self._mc.start_auto_message_fetching()
        asyncio.create_task(self._cleanup_loop())

        self.online = True
        self._setup_done.set()
        RNS.log(
            f"MeshCore_Channel_Interface [{self.name}]: "
            f"online (direct), channel {self.channel_idx}",
            RNS.LOG_INFO
        )

    # =======================================================================
    # REMOTETERM TRANSPORT SETUP
    # =======================================================================

    async def _async_setup_remoteterm(self):
        """
        Set up the RemoteTerm-backed transport:
          1. Verify RemoteTerm is reachable.
          2. Ensure our tunnel channel exists in RemoteTerm's database.
          3. Log the stable src_id we will embed in all outgoing frames.
          4. Launch the persistent WebSocket listener coroutine.
        """

        RNS.log(
            f"MeshCore_Channel_Interface [{self.name}]: "
            f"connecting to RemoteTerm at {self._rt_base_url}",
            RNS.LOG_INFO
        )

        # -- verify RemoteTerm is up --
        try:
            health = await self._rt_get("/health")
            if self.debug_level in ("debug", "info"):
                RNS.log(
                    f"MeshCore_Channel_Interface [{self.name}]: "
                    f"RemoteTerm health: {health.get('status', '?')}",
                    RNS.LOG_DEBUG
                )
        except Exception as exc:
            RNS.log(
                f"MeshCore_Channel_Interface [{self.name}]: "
                f"cannot reach RemoteTerm at {self._rt_base_url}: {exc}. "
                "Is RemoteTerm running?",
                RNS.LOG_ERROR
            )
            return

        # -- ensure tunnel channel exists in RemoteTerm --
        await self._rt_ensure_channel()

        RNS.log(
            f"MeshCore_Channel_Interface [{self.name}]: "
            f"local src_id = {self._own_src_id.hex()} "
            f"(SHA256({self.channel_secret_hex[:8]}...:{socket.gethostname()})[:4])",
            RNS.LOG_INFO
        )

        # -- start WebSocket listener --
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
        Check whether our tunnel channel already exists in RemoteTerm.
        If not, create it.  Does not push the channel to the radio — RemoteTerm
        loads it temporarily to slot 0 on every send (its normal behaviour).
        """
        key = self.channel_secret_hex.lower()
        try:
            channels = await self._rt_get("/api/channels")
            existing_keys = {
                ch.get("key", "").lower()
                for ch in (channels if isinstance(channels, list) else [])
            }
            if key in existing_keys:
                RNS.log(
                    f"MeshCore_Channel_Interface [{self.name}]: "
                    f"tunnel channel '{self.channel_name}' already in RemoteTerm",
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
            await self._rt_post("/api/channels", {
                "name": self.channel_name,
                "key":  self.channel_secret_hex,
            })
            RNS.log(
                f"MeshCore_Channel_Interface [{self.name}]: "
                f"created tunnel channel '{self.channel_name}' in RemoteTerm",
                RNS.LOG_INFO
            )
        except Exception as exc:
            RNS.log(
                f"MeshCore_Channel_Interface [{self.name}]: "
                f"failed to create channel in RemoteTerm: {exc}. "
                "Sends may fail until the channel exists.",
                RNS.LOG_WARNING
            )

    # -----------------------------------------------------------------------
    # RemoteTerm WebSocket listener
    # -----------------------------------------------------------------------

    async def _remoteterm_ws_listener(self):
        """
        Persistent asyncio coroutine that maintains the WebSocket connection
        to RemoteTerm and dispatches incoming channel messages to the
        reassembly pipeline.

        Reconnects automatically with exponential backoff up to 60 s.
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

                RNS.log(
                    f"MeshCore_Channel_Interface [{self.name}]: "
                    f"connecting WebSocket {self._rt_ws_url}",
                    RNS.LOG_DEBUG
                )

                async with websockets.connect(
                    self._rt_ws_url,
                    additional_headers=extra_headers,
                    ping_interval=20,
                    ping_timeout=10,
                ) as ws:
                    backoff = 2.0  # reset on successful connect
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
        Process one WebSocket event from RemoteTerm.

        We care only about:
          event["type"] == "message"
          event["data"]["type"] == "CHAN"
          event["data"]["conversation_key"] == our channel secret
          event["data"]["outgoing"] == False   (suppress our own echoes)
          event["data"]["text"].startswith("RNS:")
        """
        if event.get("type") != "message":
            return

        msg = event.get("data", {})

        if msg.get("type") != "CHAN":
            return

        # conversation_key in RemoteTerm is the 32-hex-char channel key
        if msg.get("conversation_key", "").lower() != self.channel_secret_hex.lower():
            return

        # RemoteTerm marks messages we sent with outgoing=True — skip them
        if msg.get("outgoing", False):
            return

        text = msg.get("text", "")
        await self._process_tunnel_text(text)

    # =======================================================================
    # DIRECT TRANSPORT: incoming channel message callback
    # =======================================================================

    async def _on_channel_msg(self, event):
        """Called by meshcore_py on CHANNEL_MSG_RECV (direct transports only)."""
        text = event.payload.get("text", "")
        await self._process_tunnel_text(text)

    # =======================================================================
    # SHARED INCOMING PIPELINE
    # =======================================================================

    async def _process_tunnel_text(self, text: str):
        """
        Common entry point for both transport modes.
        Decodes a channel message text, validates the RNS tunnel header,
        and feeds the payload into the reassembly state machine.
        """
        if not text.startswith(self.MSG_PREFIX):
            return

        try:
            raw = base64.b64decode(text[len(self.MSG_PREFIX):])
        except Exception:
            return

        if len(raw) < self.HEADER_SIZE:
            return

        magic      = raw[0:2]
        src_id     = raw[2:6]
        pkt_id     = raw[6]
        frag_idx   = raw[7]
        frag_total = raw[8]
        payload    = raw[self.HEADER_SIZE:]

        if magic != self.MAGIC:
            return

        # Echo suppression: drop fragments whose src_id matches our own
        if src_id == self._own_src_id:
            return

        src_hex = src_id.hex()
        key     = (src_hex, pkt_id)

        with self._seen_lock:
            if key in self._seen_pkts:
                return  # Already delivered — late/duplicate fragment

        if self.debug_level == "debug":
            RNS.log(
                f"MeshCore_Channel_Interface [{self.name}]: "
                f"RX  src={src_hex}  pkt={pkt_id}  "
                f"frag {frag_idx+1}/{frag_total}  payload={len(payload)}B",
                RNS.LOG_DEBUG
            )

        with self._asm_lock:
            if key not in self._assembly:
                self._assembly[key]      = {}
                self._assembly_meta[key] = (frag_total, time.monotonic())

            self._assembly[key][frag_idx] = payload

            expected_total = self._assembly_meta[key][0]
            if len(self._assembly[key]) < expected_total:
                return  # Still incomplete

            # Reassemble
            try:
                full_packet = b"".join(
                    self._assembly[key][i] for i in range(expected_total)
                )
            except KeyError as exc:
                RNS.log(
                    f"MeshCore_Channel_Interface [{self.name}]: "
                    f"reassembly gap — missing fragment {exc}",
                    RNS.LOG_WARNING
                )
                del self._assembly[key]
                del self._assembly_meta[key]
                return

            del self._assembly[key]
            del self._assembly_meta[key]

        with self._seen_lock:
            self._seen_pkts.add(key)
            if len(self._seen_pkts) > 512:
                while len(self._seen_pkts) > 256:
                    self._seen_pkts.pop()

        if self.debug_level in ("debug", "info"):
            RNS.log(
                f"MeshCore_Channel_Interface [{self.name}]: "
                f"RX  reassembled {len(full_packet)}B  from src={src_hex}",
                RNS.LOG_DEBUG
            )

        self.processIncoming(full_packet)

    async def _cleanup_loop(self):
        """Periodic stale-assembly eviction."""
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
                        f"evicted stale assembly {k} "
                        f"(>{self.fragment_timeout_s:.0f}s old)",
                        RNS.LOG_WARNING
                    )

    # =======================================================================
    # OUTGOING PATH
    # =======================================================================

    def processOutgoing(self, data):
        """Called by RNS to transmit a packet. Fragments and enqueues."""
        if not self.online:
            return

        with self._pkt_id_lock:
            pkt_id       = self._pkt_id
            self._pkt_id = (self._pkt_id + 1) & 0xFF

        handler = _PacketHandler(data, self._own_src_id, pkt_id)

        if self.debug_level in ("debug", "info"):
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
        """
        Worker thread: dequeues fragment strings and transmits them.
        Routes to the appropriate send method based on transport.
        """
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

            # -- send --
            if self.transport == "remoteterm":
                self._send_via_remoteterm(frag_str)
            else:
                # Direct transport via meshcore library
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
                elif self.debug_level == "debug":
                    RNS.log(
                        f"MeshCore_Channel_Interface [{self.name}]: "
                        f"TX fragment sent ({len(frag_str)} chars)",
                        RNS.LOG_DEBUG
                    )

            # -- pacing --
            delay = self.fragment_delay_s
            if self.rate_limit_bps > 0:
                raw_bytes = len(frag_str) * 3 // 4
                delay = max(delay, raw_bytes / self.rate_limit_bps)
            time.sleep(delay)

    def _send_via_remoteterm(self, frag_str: str):
        """
        POST one fragment string to RemoteTerm's channel message endpoint.
        Runs in the worker thread (synchronous HTTP via urllib).
        """
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
            with urllib.request.urlopen(req, timeout=15) as resp:
                status = resp.status
            if self.debug_level == "debug":
                RNS.log(
                    f"MeshCore_Channel_Interface [{self.name}]: "
                    f"TX fragment → RemoteTerm HTTP {status} ({len(frag_str)} chars)",
                    RNS.LOG_DEBUG
                )
        except urllib.error.HTTPError as exc:
            body_snippet = ""
            try:
                body_snippet = exc.read(200).decode(errors="replace")
            except Exception:
                pass
            RNS.log(
                f"MeshCore_Channel_Interface [{self.name}]: "
                f"RemoteTerm HTTP {exc.code} on channel send: {body_snippet}",
                RNS.LOG_WARNING
            )
        except Exception as exc:
            RNS.log(
                f"MeshCore_Channel_Interface [{self.name}]: "
                f"RemoteTerm send error: {exc}",
                RNS.LOG_WARNING
            )

    # =======================================================================
    # RemoteTerm HTTP helpers (async, for use from the event loop)
    # =======================================================================

    async def _rt_get(self, path: str) -> dict:
        """Async GET against RemoteTerm, returns parsed JSON."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._rt_get_sync, path)

    async def _rt_post(self, path: str, data: dict) -> dict:
        """Async POST against RemoteTerm, returns parsed JSON."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._rt_post_sync, path, data)

    def _rt_get_sync(self, path: str) -> dict:
        url = self._rt_base_url + path
        req = urllib.request.Request(url)
        if self._rt_auth:
            import base64 as _b64
            cred = _b64.b64encode(
                f"{self._rt_auth[0]}:{self._rt_auth[1]}".encode()
            ).decode()
            req.add_header("Authorization", f"Basic {cred}")
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())

    def _rt_post_sync(self, path: str, data: dict) -> dict:
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
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())

    # =======================================================================
    # RNS interface bookkeeping
    # =======================================================================

    def processIncoming(self, data: bytes):
        self.rxb += len(data)
        super().processIncoming(data)

    def __str__(self):
        return f"MeshCore_Channel_Interface[{self.name}]"
