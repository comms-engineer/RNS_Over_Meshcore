# [[MeshCore Interface]]
# type = MeshCore_Interface
# enabled = true
# mode = gateway
# port = /dev/ttyUSB0
# speed = 115200
# endpoint_contact = None

# MeshCore_Interface.py
# Full replacement MeshCore <-> Reticulum interface with configurable debug and endpoint behaviour.

import asyncio
import threading
import time
import struct
import base64
import os
import json
import traceback

# Minimal safe imports from RNS environment - Interface base class and RNS logging are expected to be available.
# This file expects to run inside Reticulum (so "Interface" and "RNS" are globals provided by the environment).
# If these aren't available for static analysis, imports will be resolved at runtime in the Reticulum environment.

def _safe_log(level, msg):
    try:
        RNS.log(msg, level)
    except Exception:
        # Best-effort fallback
        try:
            print(msg)
        except Exception:
            pass
            
class MeshCoreInterface(Interface):
    """
    MeshCoreInterface - rewritten:
      - starts its own asyncio event loop in a background thread
      - initializes meshcore MeshCore.create_serial(...)
      - registers with Reticulum only after MeshCore is ready
      - provides debug_level config (off, info, debug)
      - respects endpoint_contact config for outbound restriction but learns endpoints from inbound traffic
    """

    DEFAULT_IFAC_SIZE = 8
    HW_MTU = 184

    def __init__(self, owner, configuration):
        # Basic attributes
        self.owner = owner
        self.mesh = None
        self._meshcore_module = None
        self._meshcore_EventType = None
        self._loop = None
        self._loop_thread = None
        self._registered_with_rns = False

        self.outgoing_packet_storage = {}
        self.packet_i_queue = []
        self.assembly_dict = {}
        self.dest_to_node_dict = {}   # map node pubkey hex -> contact dict OR pubkey str
        self.packet_index = 0

        # parse configuration object via Interface helper
        ifconf = Interface.get_config_obj(configuration)

        # read name/port and other config values
        self.name = ifconf.get("name", "MeshCore_Interface")
        self.port = ifconf.get("port", None)
        if not self.port:
            raise ValueError("MeshCoreInterface: no 'port' specified in configuration")

        # Allow endpoint_contact (restrict who we will send outbound to), can be public-key hex or contact advert name
        self.endpoint_contact = ifconf.get("endpoint_contact", None)

        # radio params (fall back to defaults)
        self.FREQ = float(ifconf.get("freq", 910.525))
        self.BW = float(ifconf.get("bw", 62.5))
        self.SF = int(ifconf.get("sf", 7))
        self.CR = int(ifconf.get("cr", 5))

        # Debug level: "off", "info", "debug"
        self.debug_level = str(ifconf.get("debug_level", "info")).lower()
        if self.debug_level not in ("off", "info", "debug"):
            self.debug_level = "info"

        # friendly alias to control verbose logging
        self._LOG_DEBUG = getattr(RNS, "LOG_DEBUG", 10)
        self._LOG_INFO = getattr(RNS, "LOG_INFO", 20)
        self._LOG_WARNING = getattr(RNS, "LOG_WARNING", getattr(RNS, "LOG_WARN", 30))
        self._LOG_ERROR = getattr(RNS, "LOG_ERROR", 40)
        self._LOG_CRITICAL = getattr(RNS, "LOG_CRITICAL", 50)

        # Read reticulum config file (best-effort)
        self.reticulum_config = self._read_reticulum_config_file()

        # initialize base class now (calls Interface.__init__)
        super().__init__()

        # start event loop and try to open serial -> initialize meshcore
        self._start_event_loop_thread()
        try:
            self._import_meshcore()
            self._open_serial_and_init()
        except Exception as e:
            _safe_log(self._LOG_ERROR, f"MeshCoreInterface: initialization failed: {e}")
            # Propagate exception to fail interface init in Reticulum
            raise

        # outgoing worker thread
        self._start_outgoing_worker()

        _safe_log(self._LOG_INFO, f"MeshCoreInterface[{self.name}] constructed")

    def _read_reticulum_config_file(self):
        """
        Tries to read the extensionless Reticulum config file.
        Common locations:
            ~/.reticulum/config
            /etc/reticulum/config
        Returns parsed dict or None.
        """
        candidates = [
            os.path.expanduser("~/.reticulum/config"),
            "/etc/reticulum/config",
        ]
        for p in candidates:
            try:
                if os.path.exists(p):
                    with open(p, "r", encoding="utf-8") as fh:
                        txt = fh.read().strip()
                        # Attempt JSON first (some folks store JSON in that file)
                        try:
                            return json.loads(txt)
                        except Exception:
                            # Fallback: simple key=value lines
                            cfg = {}
                            for line in txt.splitlines():
                                line = line.strip()
                                if not line or line.startswith("#"):
                                    continue
                                if "=" in line:
                                    k, v = line.split("=", 1)
                                    cfg[k.strip()] = v.strip()
                            if cfg:
                                return cfg
            except Exception:
                continue
        return None

    def _import_meshcore(self):
        # Import meshcore and stash EventType class
        import importlib.util
        if importlib.util.find_spec("meshcore") is None:
            _safe_log(self._LOG_CRITICAL, "MeshCoreInterface: meshcore module not found. Install with: python3 -m pip install meshcore")
            RNS.panic()
        import meshcore
        from meshcore import EventType
        self._meshcore_module = meshcore
        self._meshcore_EventType = EventType

    def _start_event_loop_thread(self):
        if self._loop is not None and self._loop_thread is not None and self._loop_thread.is_alive():
            return

        def _loop_thread_target(loop):
            try:
                asyncio.set_event_loop(loop)
                loop.run_forever()
            except Exception as e:
                _safe_log(self._LOG_ERROR, f"MeshCoreInterface: event loop thread crashed: {e}\n{traceback.format_exc()}")

        self._loop = asyncio.new_event_loop()
        self._loop_thread = threading.Thread(target=_loop_thread_target, args=(self._loop,), daemon=True, name=f"meshcore-loop-{self.name}")
        self._loop_thread.start()
        # small pause to let the loop start
        time.sleep(0.05)
        _safe_log(self._LOG_DEBUG if self.debug_level == "debug" else self._LOG_INFO, f"MeshCore: event loop thread started ({self._loop_thread.name})")

    def _loop_alive(self):
        return self._loop_thread is not None and self._loop_thread.is_alive()

    def _run_coro(self, coro, timeout=None):
        """
        Submit a coroutine to the background loop and wait for result.
        """
        if not self._loop_alive():
            # try to restart
            _safe_log(self._LOG_WARNING, "MeshCore: event loop not running, restarting...")
            self._start_event_loop_thread()
        fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return fut.result(timeout=timeout)

    def _run_coro_no_wait(self, coro):
        """
        Submit a coroutine to the background loop and return the Future immediately.
        """
        if not self._loop_alive():
            _safe_log(self._LOG_WARNING, "MeshCore: event loop not running, restarting...")
            self._start_event_loop_thread()
        return asyncio.run_coroutine_threadsafe(coro, self._loop)

    def _open_serial_and_init(self):
        """
        Open the MeshCore serial device and perform initial setup asynchronously.
        Register the interface with Reticulum only after meshcore is ready.
        """
        meshcore = self._meshcore_module
        EventType = self._meshcore_EventType

        # Start async task to create MeshCore and set things up
        async def _async_setup():
            try:
                create_coro = meshcore.MeshCore.create_serial(self.port, 115200, debug=True)
                self.mesh = await create_coro
                if not self.mesh:
                    raise RuntimeError("MeshCore: create_serial returned no mesh object")
                _safe_log(self._LOG_INFO, f"MeshCore: connected to device on {self.port}")
            except Exception as e:
                _safe_log(self._LOG_ERROR, f"MeshCore: create_serial failed for {self.port}: {e}")
                raise

            # try to set radio params (best effort)
            try:
                res = await self.mesh.commands.set_radio(self.FREQ, self.BW, self.SF, self.CR)
                if getattr(res, "type", None) == EventType.ERROR:
                    _safe_log(self._LOG_WARNING, f"MeshCore: set_radio returned error: {getattr(res, 'payload', None)}")
                else:
                    _safe_log(self._LOG_INFO, f"MeshCore: radio configured: freq={self.FREQ} MHz bw={self.BW} kHz sf={self.SF} cr={self.CR}")
            except Exception as e:
                _safe_log(self._LOG_WARNING, f"MeshCore: exception while setting radio: {e}")

            # attempt to start auto fetching of messages
            try:
                if hasattr(self.mesh, "start_auto_message_fetching"):
                    await self.mesh.start_auto_message_fetching()
                    _safe_log(self._LOG_INFO, "MeshCore: auto message fetching started")
            except Exception as e:
                _safe_log(self._LOG_WARNING, f"MeshCore: start_auto_message_fetching() failed: {e}")

            # subscribe to events
            try:
                # CONTACT_MSG_RECV: incoming messages
                async def _on_contact_msg(event):
                    # All meshcore callbacks are executed inside the meshcore async context,
                    # but we'll route handling back to a synchronous path by scheduling onto our loop safely.
                    try:
                        # Pull payload and process
                        payload = getattr(event, "payload", None)
                        await self._handle_incoming_payload(event, payload)
                    except Exception as e:
                        _safe_log(self._LOG_WARNING, f"MeshCore: _on_contact_msg handler exception: {e}\n{traceback.format_exc()}")

                # NEW_CONTACT event: update contact list and optionally pick endpoint
                async def _on_new_contact(event):
                    try:
                        await self._handle_new_contact_event(event)
                    except Exception as e:
                        _safe_log(self._LOG_WARNING, f"MeshCore: _on_new_contact exception: {e}\n{traceback.format_exc()}")

                # RX_LOG_DATA: RF raw logs — just log if debug
                async def _on_rx_log(event):
                    if self.debug_level == "debug":
                        try:
                            payload = getattr(event, "payload", None)
                            _safe_log(self._LOG_DEBUG, f"MeshCore: RX_LOG_DATA event payload preview: {str(payload)[:200]}")
                        except Exception:
                            pass

                # subscribe to relevant events
                self.mesh.subscribe(EventType.CONTACT_MSG_RECV, _on_contact_msg)
                self.mesh.subscribe(EventType.NEW_CONTACT, _on_new_contact)
                self.mesh.subscribe(EventType.RX_LOG_DATA, _on_rx_log)
            except Exception as e:
                _safe_log(self._LOG_WARNING, f"MeshCore: subscription to events failed: {e}")

            # On first successful connection, fetch contacts once to populate mapping
            try:
                res = await self.mesh.commands.get_contacts()
                if getattr(res, "type", None) != EventType.ERROR:
                    contacts = getattr(res, "payload", {}) or {}
                    # populate quick mapping of advert->pubkey when available
                    for pk, contact in contacts.items():
                        adv = contact.get("adv_name")
                        if adv:
                            # map advert name lower() -> contact public key for quick resolution
                            self.dest_to_node_dict[adv.lower()] = contact
                    _safe_log(self._LOG_INFO, f"MeshCore: discovered {len(contacts)} contacts on device")
            except Exception:
                pass

            # Map endpoint_contact explicitly if configured
            if self.endpoint_contact:
                if isinstance(self.endpoint_contact, str):
                    # If it looks like a pubkey hex string
                    if len(self.endpoint_contact) >= 32 and all(c in "0123456789abcdef" for c in self.endpoint_contact.lower()):
                        self.dest_to_node_dict[self.endpoint_contact] = self.endpoint_contact
                        self.dest_to_node_dict[self.endpoint_contact[:8]] = self.endpoint_contact
                        _safe_log(self._LOG_INFO, f"MeshCore: endpoint_contact pubkey {self.endpoint_contact[:12]} mapped for outbound use")
                    else:
                        # Treat as advert name
                        adv_key = self.endpoint_contact.lower()
                        if adv_key not in self.dest_to_node_dict:
                            _safe_log(self._LOG_INFO, f"MeshCore: endpoint_contact advert '{adv_key}' will be resolved when contacts are updated")

            # Mark online and register interface with Reticulum (must be done after mesh ready)
            self.online = True
            _safe_log(self._LOG_INFO, "MeshCoreInterface: online (meshcore ready)")

            # Register with RNS transport only once and only after online
            try:
                # RNS.Transport.register_interface(self) - use Interface.register? Use Transport global if available
                if not self._registered_with_rns:
                    try:
                        # Preferred: RNS.Transport.register_interface
                        transport = getattr(RNS, "Transport", None)
                        if transport and hasattr(transport, "register_interface"):
                            transport.register_interface(self)
                        else:
                            # Fallback to Interface.register_interface if present
                            if hasattr(self, "register_interface"):
                                self.register_interface()
                        self._registered_with_rns = True
                        _safe_log(self._LOG_INFO, f"MeshCoreInterface: registered with Reticulum transport (name={self.name})")
                    except Exception as e:
                        _safe_log(self._LOG_WARNING, f"MeshCoreInterface: failed to register with Reticulum transport: {e}")
            except Exception:
                pass

            return True
            
        fut = self._run_coro_no_wait(_async_setup())

        try:
            fut.result(timeout=5)
        except Exception:
            # That's fine; _async_setup will complete asynchronously
            pass
            
    async def _handle_incoming_payload(self, event, payload):
        """
        Decode payload received from meshcore and reassemble RNS fragments.
        If a full RNS packet becomes available, call self.owner.inbound(full_data, self)
        Also learn mapping from remote pubkey -> contact for outbound convenience.
        """
        EventType = self._meshcore_EventType

        # Attempt to decode payload to raw bytes
        data_bytes = None
        try:
            data_bytes = self._payload_from_received(payload)
        except Exception as e:
            _safe_log(self._LOG_WARNING, f"MeshCore: _payload_from_received failed: {e}")
            return

        if not data_bytes:
            if self.debug_level != "off":
                _safe_log(self._LOG_DEBUG, "MeshCore: no data bytes extracted from payload")
            return

        # Log bytes summary if debug
        if self.debug_level == "debug":
            _safe_log(self._LOG_DEBUG, f"MeshCore: Decoded payload ({len(data_bytes)} bytes): {data_bytes[:120].hex()}...")

        from_key = None
        contact_obj = None

        # payload may carry multiple shapes: dict with public_key/pubkey_prefix/contact; or event fields
        if isinstance(payload, dict):
            # meshcore sometimes nests contact dicts
            contact_obj = payload.get("contact") or payload.get("contact_obj") or None
            from_key = payload.get("public_key") or payload.get("pubkey_prefix") or None
            # sometimes meshcore puts the raw payload inside 'payload' with meta keys; try fallback
            if not from_key and isinstance(contact_obj, dict):
                from_key = contact_obj.get("public_key") or contact_obj.get("pubkey")
        # fallback to event attributes
        if not from_key:
            from_key = getattr(event, "pubkey_prefix", None) or getattr(event, "from_id", None) or None

        # Normalize into a hex-string sender_id
        sender_hex = None
        try:
            if isinstance(from_key, bytes) or isinstance(from_key, bytearray):
                sender_hex = bytes(from_key).hex()
            elif isinstance(from_key, str):
                # could already be full hex or prefix or advert; leave as-is
                sender_hex = from_key
            elif isinstance(from_key, dict):
                # weird case: contact dict in from_key
                sender_hex = from_key.get("public_key") or from_key.get("pubkey") or str(from_key)
            else:
                # final fallback: if contact_obj present, try extract public key
                if isinstance(contact_obj, dict):
                    sender_hex = contact_obj.get("public_key") or contact_obj.get("pubkey")
                else:
                    sender_hex = str(from_key)
        except Exception:
            sender_hex = str(from_key)

        # Normalize short prefixes to hex-like strings for dictionary keys
        sender_id = (sender_hex or "unknown_sender")
        if isinstance(sender_id, str) and len(sender_id) > 8:
            sender_id = sender_id.lower()

        # Learn mapping for convenience: store the contact object against the full hex and short prefix
        try:
            if contact_obj and isinstance(contact_obj, dict):
                pk = contact_obj.get("public_key") or None
                if pk:
                    self.dest_to_node_dict[pk] = contact_obj
                    if len(pk) >= 8:
                        self.dest_to_node_dict[pk[:8]] = contact_obj
            if sender_hex and isinstance(sender_hex, str) and len(sender_hex) >= 8:
                # ensure a mapping exists for the sender prefix -> contact (use contact_obj if available)
                self.dest_to_node_dict[sender_hex[:8]] = contact_obj or sender_hex
        except Exception:
            pass

        # Debug: print raw metadata bytes for inspection
        try:
            if len(data_bytes) >= 2 and self.debug_level == "debug":
                _safe_log(self._LOG_DEBUG, f"MeshCore: IN raw meta bytes: {data_bytes[:2].hex()} sender_id={sender_id[:16]} data_len={len(data_bytes)}")
        except Exception:
            pass
            
        try:
            # Expect at least 2 bytes of metadata (index, pos)
            if len(data_bytes) < 2:
                _safe_log(self._LOG_WARNING, f"MeshCore: incoming data too short to contain metadata ({len(data_bytes)} bytes)")
                return

            # Unpack signed/unsigned like previously: 'Bb' was used (index, pos). We'll mirror that behavior.
            try:
                index, pos = struct.unpack("Bb", data_bytes[:2])
            except Exception as e_unpack:
                # try unsigned short? but log and return
                _safe_log(self._LOG_WARNING, f"MeshCore: failed to unpack metadata 'Bb': {e_unpack}. Data hex start: {data_bytes[:40].hex()}")
                return
            if pos == 0:
                if self.debug_level == "debug":
                    _safe_log(self._LOG_DEBUG, "MeshCore: pos==0 detected, treating as pos==1")
                pos = 1

            chunk = data_bytes[2:]

            # --- debug / validation logging ---
            if self.debug_level == "debug":
                try:
                    _safe_log(self._LOG_DEBUG, f"MeshCore: IN index={index} pos={pos} sender={sender_id[:16]} chunk_len={len(chunk)} chunk_head={chunk[:16].hex() if len(chunk)>=16 else chunk.hex()}")
                except Exception:
                    pass

            # per-sender assembly storage (use normalized sender_id)
            if sender_id not in self.assembly_dict:
                self.assembly_dict[sender_id] = {}
            assembly = self.assembly_dict[sender_id]

            # store part
            idx = index
            pos_key = abs(pos)
            if idx not in assembly:
                assembly[idx] = {}
            assembly[idx][pos_key] = chunk

            # log assembly state
            if self.debug_level == "debug":
                try:
                    keys = sorted(assembly[idx].keys())
                    _safe_log(self._LOG_DEBUG, f"MeshCore: ASSEMBLY sender={sender_id[:12]} idx={idx} parts_keys={keys}")
                except Exception:
                    pass

            # if last fragment (pos < 0), reassemble
            if pos < 0:
                try:
                    parts = [assembly[idx][i] for i in sorted(assembly[idx].keys())]
                    full_data = b"".join(parts)
                    # pass to Reticulum (owner)
                    try:
                        # owner.inbound expects (payload_bytes, interface)
                        self.owner.inbound(full_data, self)
                        if self.debug_level in ("info", "debug"):
                            _safe_log(self._LOG_INFO, f"MeshCore: reassembled full RNS packet ({len(full_data)} bytes) from sender {sender_id[:12]}")
                    except Exception as e_owner:
                        _safe_log(self._LOG_WARNING, f"MeshCore: owner.inbound failed: {e_owner}\n{traceback.format_exc()}")
                except Exception as e:
                    _safe_log(self._LOG_WARNING, f"MeshCore: reassembly failed: {e}\n{traceback.format_exc()}")
                finally:
                    try:
                        del assembly[idx]
                    except Exception:
                        pass

        except Exception as e:
            _safe_log(self._LOG_WARNING, f"MeshCore: _handle_incoming_payload unexpected error: {e}\n{traceback.format_exc()}")

    async def _handle_new_contact_event(self, event):
        """
        Called when MeshCore signals NEW_CONTACT or when we fetch contacts.
        Checks for configured endpoint_contact in incoming contacts and maps it.
        """
        EventType = self._meshcore_EventType
        try:
            # If endpoint_contact configured and not yet resolved, try to resolve it
            if self.endpoint_contact:
                try:
                    res = await self.mesh.commands.get_contacts()
                    if getattr(res, "type", None) != EventType.ERROR:
                        contacts = getattr(res, "payload", {}) or {}
                        for pk, contact in contacts.items():
                            adv = contact.get("adv_name")
                            if adv and self.endpoint_contact.lower() in adv.lower():
                                # treat this contact as the outbound endpoint
                                self.endpoint_contact = pk
                                _safe_log(self._LOG_INFO, f"MeshCore: endpoint_contact resolved to {adv} ({pk[:12]}...)")
                                break
                except Exception:
                    pass
        except Exception:
            pass

    def _payload_for_send(self, data: bytes) -> str:
        """
        Prepare the payload string to pass to meshcore.commands.send_msg().
        Uses base64 encoding.
        """
        try:
            return base64.b64encode(data).decode("ascii")
        except Exception as e:
            _safe_log(self._LOG_ERROR, f"MeshCore: base64 encode failed: {e}")
            return ""

    def _payload_from_received(self, payload_obj):
        """
        Decode incoming payloads pulled from meshcore event payload to raw bytes.
        Handles:
          - bytes -> return directly
          - base64 string (single or double-encoded)
          - dict objects that contain 'payload' / 'data' / 'text' fields
        """
        try:
            if isinstance(payload_obj, (bytes, bytearray)):
                return bytes(payload_obj)
            s = None
            if isinstance(payload_obj, dict):
                for k in ("payload", "data", "text"):
                    val = payload_obj.get(k, None)
                    if val is None:
                        continue
                    if isinstance(val, (bytes, bytearray)):
                        return bytes(val)
                    if isinstance(val, str):
                        s = val
                        break
            elif isinstance(payload_obj, str):
                s = payload_obj
            if s is None:
                return None

            # Try strict base64 decode
            try:
                decoded = base64.b64decode(s, validate=True)
                # second layer?
                try:
                    decoded2 = base64.b64decode(decoded, validate=True)
                    if self.debug_level == "debug":
                        _safe_log(self._LOG_DEBUG, "MeshCore: payload double base64 decoded")
                    return decoded2
                except Exception:
                    if self.debug_level == "debug":
                        _safe_log(self._LOG_DEBUG, "MeshCore: payload single base64 decoded")
                    return decoded
            except Exception:
                # fallback: latin-1/raw bytes
                try:
                    return s.encode("latin-1")
                except Exception:
                    return s.encode("utf-8", errors="ignore")
        except Exception as e:
            _safe_log(self._LOG_WARNING, f"MeshCore: _payload_from_received unexpected error: {e}")
            return None

    def _start_outgoing_worker(self):
        thread = threading.Thread(target=self._outgoing_worker, daemon=True, name=f"meshcore-out-{self.name}")
        thread.start()

    def _outgoing_worker(self):
        """
        Worker thread that pulls fragments from packet_i_queue and sends them via meshcore.
        All meshcore coroutines are executed on the background asyncio loop using _run_coro.
        """
        meshcore = self._meshcore_module
        EventType = self._meshcore_EventType

        while True:
            try:
                if not self.packet_i_queue:
                    time.sleep(0.03)
                    continue

                index, pos = self.packet_i_queue.pop(0)
                handler = self.outgoing_packet_storage.get(index, None)
                if not handler:
                    continue
                data = handler[pos]
                if not data:
                    continue

                # Determine destination
                raw_dest = getattr(handler, "destination_id", None) or self.endpoint_contact
                dest_key = None

                # If explicit custom destination provided by handler, resolve it (hex string or contact dict)
                if raw_dest:
                    dest_key = self._resolve_destination(raw_dest)

                # If no explicit dest_key resolved, try to map RNS destination embedded in payload (if present)
                if not dest_key:
                    try:
                        if len(data) >= 18:
                            rns_dest = data[2:18]
                            mapped = self.dest_to_node_dict.get(bytes(rns_dest).hex(), None) or self.dest_to_node_dict.get(bytes(rns_dest).hex()[:8], None)
                            if mapped:
                                dest_key = self._resolve_destination(mapped)
                    except Exception:
                        pass

                # If endpoint_contact configured (string) and still no dest, use it as last resort (restrict outbound)
                if not dest_key and self.endpoint_contact:
                    dest_key = self._resolve_destination(self.endpoint_contact)

                # If still no dest_key then treat as broadcast attempt if allowed (here we will attempt broadcast to all known contacts)
                if not dest_key:
                    # user may wish to avoid broadcasting; we will attempt "broadcast" by iterating contacts
                    try:
                        # fetch contacts from meshcore and send to each contact
                        res_contacts = self._run_coro(self.mesh.commands.get_contacts(), timeout=8)
                        if getattr(res_contacts, "type", None) != EventType.ERROR:
                            contacts = getattr(res_contacts, "payload", {}) or {}
                            for pk, contact in contacts.items():
                                try:
                                    payload_for_meshcore = self._payload_for_send(data)
                                    self._run_coro(self.mesh.commands.send_msg(contact, payload_for_meshcore), timeout=8)
                                    if self.debug_level in ("info", "debug"):
                                        _safe_log(self._LOG_INFO, f"MeshCore: broadcasted fragment ({len(data)} bytes) to {pk[:8]}")
                                except Exception as e:
                                    if self.debug_level == "debug":
                                        _safe_log(self._LOG_WARNING, f"MeshCore: broadcast fragment to {pk[:8]} failed: {e}")
                        else:
                            if self.debug_level in ("info", "debug"):
                                _safe_log(self._LOG_WARNING, "MeshCore: no contacts available for broadcast")
                    except Exception as e:
                        if self.debug_level in ("info", "debug"):
                            _safe_log(self._LOG_WARNING, f"MeshCore: broadcast attempt failed: {e}")
                    continue  # move to next fragment

                # Finally send to resolved dest
                try:
                    payload_for_meshcore = self._payload_for_send(data)
                    res = self._run_coro(self.mesh.commands.send_msg(dest_key, payload_for_meshcore), timeout=8)
                    if getattr(res, "type", None) == EventType.ERROR:
                        _safe_log(self._LOG_WARNING, f"MeshCore: send_msg returned error: {getattr(res, 'payload', None)}")
                    else:
                        if self.debug_level in ("info", "debug"):
                            short = None
                            try:
                                if isinstance(dest_key, str):
                                    short = dest_key[:12]
                                elif isinstance(dest_key, dict):
                                    short = dest_key.get("public_key", "")[:12] or dest_key.get("adv_name", "")[:12]
                            except Exception:
                                pass
                            _safe_log(self._LOG_INFO, f"MeshCore: sent fragment ({len(data)} bytes) to {short}")
                except Exception as e:
                    _safe_log(self._LOG_ERROR, f"MeshCore: exception while sending fragment: {e}\n{traceback.format_exc()}")

            except Exception as e:
                _safe_log(self._LOG_ERROR, f"MeshCore outgoing worker error: {e}\n{traceback.format_exc()}")

            time.sleep(0.02)

    def _resolve_destination(self, raw_dest):
        """
        Resolve a raw destination (pubkey hex, short prefix, or contact dict)
        into a usable MeshCore contact object or pubkey string.
        """
        try:
            _safe_log(self._LOG_INFO, f"Resolving destination: {raw_dest}")

            # If already a contact dict
            if isinstance(raw_dest, dict):
                return raw_dest

            # If full pubkey hex string
            if isinstance(raw_dest, str) and len(raw_dest) >= 32:
                return raw_dest

            # If short prefix string
            if isinstance(raw_dest, str) and raw_dest in self.dest_to_node_dict:
                resolved = self.dest_to_node_dict[raw_dest]
                _safe_log(self._LOG_INFO, f"Resolved short prefix {raw_dest} -> {resolved}")
                return resolved

            # If advert name
            if isinstance(raw_dest, str):
                adv_key = raw_dest.lower()
                if adv_key in self.dest_to_node_dict:
                    resolved = self.dest_to_node_dict[adv_key]
                    _safe_log(self._LOG_INFO, f"Resolved advert '{adv_key}' -> {resolved}")
                    return resolved

            _safe_log(self._LOG_WARNING, f"Could not resolve destination: {raw_dest}")
            return None
        except Exception as e:
            _safe_log(self._LOG_WARNING, f"_resolve_destination error: {e}\n{traceback.format_exc()}")
            return None

    def process_outgoing(self, data: bytes):
        """
        Called by Reticulum when a packet is ready to be sent.
        We create a PacketHandler and enqueue fragments for sending.
        """
        try:
            if len(self.packet_i_queue) >= 512:
                _safe_log(self._LOG_WARNING, "MeshCore: outgoing queue full, dropping packet")
                return

            dest = None
            try:
                if len(data) >= 18:
                    rns_dest = data[2:18]
                    # map by hex or short prefix
                    mapped = self.dest_to_node_dict.get(bytes(rns_dest).hex()) or self.dest_to_node_dict.get(bytes(rns_dest).hex()[:8])
                    if mapped:
                        dest = mapped
            except Exception:
                dest = None

            # normalize dest to hex string if bytes
            if isinstance(dest, (bytes, bytearray)):
                try:
                    dest = bytes(dest).hex()
                except Exception:
                    dest = None

            handler = PacketHandler(data, self.packet_index, max_payload=160, custom_destination_id=dest)
            for key in handler.get_keys():
                self.packet_i_queue.append((handler.index, key))
            self.outgoing_packet_storage[handler.index] = handler
            self.packet_index = (self.packet_index + 1) % 256

            if self.debug_level in ("info", "debug"):
                _safe_log(self._LOG_INFO, f"MeshCore: queued outgoing packet index={handler.index} (fragments={len(handler.get_keys())})")
        except Exception as e:
            _safe_log(self._LOG_ERROR, f"MeshCore: process_outgoing failed: {e}\n{traceback.format_exc()}")

    def should_ingress_limit(self, dest=None):
        """
        Return whether incoming packets should be limited for a certain destination.
        For compatibility, return False (no limit) unless the user wishes otherwise in future.
        """
        return False

    def set_debug_level(self, level):
        l = (level or "").lower()
        if l not in ("off", "info", "debug"):
            return
        self.debug_level = l
        _safe_log(self._LOG_INFO, f"MeshCore: debug level set to {self.debug_level}")

    def __str__(self):
        return f"MeshCoreInterface[{self.name}]"

class PacketHandler:
    struct_format = "Bb"

    def __init__(self, data=None, index=0, max_payload=200, custom_destination_id=None):
        self.max_payload = int(max_payload)
        self.index = int(index) if index is not None else 0
        self.data_dict = {}
        self.destination_id = custom_destination_id
        if data:
            self.split_data(data)

    def split_data(self, data: bytes):
        """
        Split `data` into multiple fragments and prefix metadata struct(pack(index, pos)).
        pos is positive for non-final chunks and negative for the final chunk (abs(pos) = sequence number).
        Algorithm aims to create nearly even-sized fragments.
        """
        if not data:
            return
        data_len = len(data)
        num_packets = (data_len // self.max_payload) + 1
        packet_size = (data_len // num_packets) + 1
        data_list = [data[i:i + packet_size] for i in range(0, data_len, packet_size)]
        for i, packet in enumerate(data_list):
            pos = i + 1
            if pos == len(data_list):
                pos = -pos
            meta = struct.pack(self.struct_format, self.index, pos)
            self.data_dict[pos] = meta + packet

    def get_keys(self):
        return list(self.data_dict.keys())

    def __getitem__(self, i):
        if i in self.data_dict:
            return self.data_dict[i]
        elif -i in self.data_dict:
            return self.data_dict[-i]
        else:
            return None

# Final export variable consumed by Reticulum's interface loader
interface_class = MeshCoreInterface
