# [[MeshCore Interface]]
# type = MeshCore_Interface
# enabled = true
# mode = gateway
# port = /dev/ttyUSB0
# speed = 115200
# endpoint_contact = None

import asyncio
import threading
import time
import struct
import re
import base64

# Minimal MeshCore interface
class MeshCoreInterface(Interface):

    print("[MeshCore] Module imported successfully")

    DEFAULT_IFAC_SIZE = 8

    HW_MTU = 184

    owner = None
    port = None
    mesh = None
    online = False
    
    def __init__(self, owner, configuration):

        import importlib.util
        if importlib.util.find_spec('meshcore') is None:
            RNS.log("Meshcore module not found. Install with: python3 -m pip install meshcore", RNS.LOG_CRITICAL)
            RNS.panic()

        import meshcore
        from meshcore import EventType
        
        self._meshcore_module = meshcore
        self._meshcore_EventType = EventType
        
        RNS.log("MeshCore_Interface: loaded successfully from Reticulum", RNS.LOG_INFO)
        
        # We start out by initialising the super-class
        super().__init__()

        # To make sure the configuration data is in the
        # correct format, we parse it through the following
        # method on the generic Interface class. This step
        # is required to ensure compatibility on all the
        # platforms that Reticulum supports.
        ifconf = Interface.get_config_obj(configuration)

        # Read interface name and configuration
        self.name = ifconf.get("name", "MeshCoreInterface")
        self.port = ifconf.get("port", None)
        if not self.port:
            raise ValueError(f"No port specified for {self}")
        self.endpoint_contact = ifconf.get("endpoint_contact", None)

        self.FREQ = float(ifconf.get("freq", 910.525))
        self.BW = float(ifconf.get("bw", 62.5))
        self.SF = int(ifconf.get("sf", 7))
        self.CR = int(ifconf.get("cr", 5))

        self.owner = owner
        self.online = False
        self.packet_index = 0

        self.outgoing_packet_storage = {}
        self.packet_i_queue = []
        self.assembly_dict = {}
        self.expected_index = {}
        self.requested_index = {}
        
        self.dest_to_node_dict = {}
        self._loop = None
        self._loop_thread = None
        
        self._LOG_WARNING = getattr(RNS, "LOG_WARNING", getattr(RNS, "LOG_WARN", RNS.LOG_INFO))
        
        try:
            self._open_serial_and_init()
        except Exception as e:
            RNS.log(f"Meshcore: failed to open serial or initialize: {e}", RNS.LOG_ERROR)
            raise
    
    def _start_event_loop_thread(self):
        if self._loop is not None:
            return
        
        def _loop_thread_target(loop):
            asyncio.set_event_loop(loop)
            loop.run_forever()
            
        self._loop = asyncio.new_event_loop()
        self._loop_thread = threading.Thread(target=_loop_thread_target, args=(self._loop,), daemon=True)
        self._loop_thread.start()
        time.sleep(0.05)
        RNS.log(f"MeshCore: started event loop thread {self._loop_thread.name}", RNS.LOG_INFO)
    
    def _loop_alive(self):
        RNS.log(f"MeshCore: loop alive check: {self._loop_thread.is_alive() if self._loop_thread else 'no thread'}", RNS.LOG_DEBUG)
        return self._loop_thread and self._loop_thread.is_alive()
    
    def _run_coro(self, coro, timeout=None):
        if not self._loop_alive():
            RNS.log("MeshCore: event loop not running, restarting...", RNS.LOG_WARNING)
            self._start_event_loop_thread()
             
        if self._loop is None:
            raise RuntimeError("Event loop not started")
        
        fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return fut.result(timeout=timeout)
    
    def _run_coro_no_wait(self, coro):
        if self._loop is None:
            raise RuntimeError("Event loop not started")
        return asyncio.run_coroutine_threadsafe(coro, self._loop)
    
    def _resolve_destination(self, candidate):
        """Return a contact dict, public-key hex string, or bytes, or None."""
        if candidate is None:
            return None

        # bytes/bytearray -> bytes
        if isinstance(candidate, (bytes, bytearray)):
            try:
                return bytes(candidate).hex()
            except Exception:
                return None

        # contact dict (from get_contacts) -> pass through
        if isinstance(candidate, dict):
            return candidate

        # string: test for hex public key
        if isinstance(candidate, str):
            s = candidate.strip()
            # quick hex test: hex chars and even length
            if s and all(c in "0123456789abcdefABCDEF" for c in s) and len(s) % 2 == 0:
                return s

            # try to resolve advert name or key prefix using mesh contacts
            try:
                if hasattr(self.mesh, "commands") and hasattr(self.mesh.commands, "get_contacts"):
                    res = self._run_coro(self.mesh.commands.get_contacts(), timeout=5)
                else:
                    res = None
                if res and getattr(res, "type", None) != self._meshcore_EventType.ERROR:
                    contacts = getattr(res, "payload", {}) or {}
                    # exact advert name match (case-insensitive)
                    for key, contact in contacts.items():
                        adv = contact.get("adv_name")
                        if adv and adv.lower() == s.lower():
                            return contact
                    # key prefix match
                    for key, contact in contacts.items():
                        if isinstance(key, str) and key.startswith(s):
                            return contact
            except Exception:
                pass

        return 
    
    def _payload_for_send(self, data: bytes) -> str:
        """Encode binary data as Base64 string for MeshCore transmission."""
        try:
            return base64.b64encode(data).decode("ascii")
        except Exception as e:
            RNS.log(f"MeshCore: base64 encode failed: {e}", RNS.LOG_ERROR)
            return ""

    def _payload_from_received(self, payload_obj):
        """Decode Base64 MeshCore payloads back into raw bytes.
        Accepts dict shapes (payload/data/text), raw bytes, or str.
        If incoming value is raw bytes already, return as-is.
        If it's a string, try Base64 decode first; on failure fall back to latin-1 bytes.
        """
        try:
            # If payload already bytes, assume it's the original raw bytes and return them
            if isinstance(payload_obj, (bytes, bytearray)):
                return bytes(payload_obj)

            # If it's a dict, pick the first present of (payload, data, text)
            if isinstance(payload_obj, dict):
                for k in ("payload", "data", "text"):
                    val = payload_obj.get(k, None)
                    if val is None:
                        continue
                    # If field contains bytes -> return directly
                    if isinstance(val, (bytes, bytearray)):
                        return bytes(val)
                    if isinstance(val, str):
                        s = val
                        break
                else:
                    s = None
            elif isinstance(payload_obj, str):
                s = payload_obj
            else:
                s = None

            if s is None:
                return None

            # try strict base64 decode first (validate=True will raise on non-base64)
            try:
                return base64.b64decode(s, validate=True)
            except Exception:
                # not valid base64 — fall back to latin-1 encode (legacy behaviour)
                try:
                    return s.encode("latin-1")
                except Exception:
                    return s.encode("utf-8", errors="ignore")
        except Exception as e:
            RNS.log(f"MeshCore: _payload_from_received unexpected error: {e}", RNS.LOG_ERROR)
            return None
    
    def _open_serial_and_init(self):
        
        meshcore = self._meshcore_module
        EventType = self._meshcore_EventType

        self._start_event_loop_thread()
        
        try:
            create_coro = meshcore.MeshCore.create_serial(self.port, 115200, debug=True)
            self.mesh = self._run_coro(create_coro, timeout=15)
            if not self.mesh:
                raise RuntimeError("MeshCore: failed to initialize mesh object")
            RNS.log(f"MeshCore: connected to device on {self.port}", RNS.LOG_INFO)
            time.sleep(1.0)
 #           self._run_coro(self.mesh.commands.ping(), timeout=5) << NEED TO SEE IF I CAN USE is_connected FOR THIS
        except Exception as e:
            RNS.log(f"MeshCore: create_serial failed for {self.port}: {e}", RNS.LOG_ERROR)
            raise
            
        def _setup_on_mesh_loop():
            async def _async_setup():
                try:
                    res = await self.mesh.commands.set_radio(self.FREQ, self.BW, self.SF, self.CR)
                    if getattr(res, "type", None) == EventType.ERROR:
                        RNS.log(f"MeshCore: set_radio returned error: {getattr(res, 'payload', None)}", RNS.LOG_WARNING)
                    else:
                        RNS.log(f"MeshCore: radio configured: freq={self.FREQ} MHz bw = {self.BW} kHz sf={self.SF} cr={self.CR}", RNS.LOG_INFO)
                except Exception as e:
                    RNS.log(f"Meshcore: exception while setting radio: {e}", RNS.LOG_WARNING)
                
                try:
                    if hasattr(self.mesh, "start_auto_message_fetching"):
                        await self.mesh.start_auto_message_fetching()
                        RNS.log("Meshcore: auto message fetching started", RNS.LOG_INFO)
                except Exception as e:
                    RNS.log(f"MeshCore: start_auto_message_fetching() failed: {e}", RNS.LOG_WARNING)
                
                try:
                    async def _on_contact_msg(event):
                        payload = getattr(event, "payload", None)
                        from_key = None
                        data_bytes = None
                        contact_obj = None

                        # Extract payload and contact/public key info (many meshcore builds use varying shapes)
                        try:
                            if isinstance(payload, dict):
                                # contact dict may be provided directly
                                contact_obj = payload.get("contact") if isinstance(payload.get("contact"), dict) else None

                                # public key fields
                                if isinstance(payload.get("public_key"), str):
                                    from_key = payload.get("public_key")
                                elif isinstance(payload.get("pubkey_prefix"), str):
                                    from_key = payload.get("pubkey_prefix")

                                # try to normalize the payload to bytes using class helper
                                data_bytes = self._payload_from_received(payload)
                            elif isinstance(payload, (bytes, bytearray, str)):
                                data_bytes = self._payload_from_received(payload)
                        except Exception:
                            data_bytes = None

                        # If contact object present but no from_key, try to read public_key from it
                        try:
                            if not from_key and isinstance(contact_obj, dict):
                                pk = contact_obj.get("public_key") or contact_obj.get("pubkey_prefix")
                                if isinstance(pk, str):
                                    from_key = pk
                        except Exception:
                            pass

                        # If we have data bytes, extract RNS dest slice and store mapping
                        if data_bytes:
                            RNS.log(f"MeshCore: inbound {len(data_bytes)} bytes (hex start: {data_bytes[:16].hex()})", RNS.LOG_DEBUG)
                            self.owner.inbound(data_bytes, self)
                            try:
                                if len(data_bytes) >= 18:
                                    rns_dest_bytes = data_bytes[2:18]
                                    try:
                                        dest_hex = bytes(rns_dest_bytes).hex()
                                    except Exception:
                                        dest_hex = None

                                    # Prefer storing the full contact dict for later send_msg calls if available
                                    stored_value = None
                                    if contact_obj:
                                        stored_value = contact_obj
                                    else:
                                        if isinstance(from_key, (bytes, bytearray)):
                                            try:
                                                stored_value = bytes(from_key).hex()
                                            except Exception:
                                                stored_value = None
                                        elif isinstance(from_key, str):
                                            stored_value = from_key.strip()

                                    if dest_hex and stored_value:
                                        try:
                                            # bound size to avoid unbounded memory growth
                                            if len(self.dest_to_node_dict) > 50:
                                                first_key = next(iter(self.dest_to_node_dict))
                                                self.dest_to_node_dict.pop(first_key, None)
                                        except Exception:
                                            pass
                                        try:
                                            self.dest_to_node_dict[dest_hex] = stored_value
                                        except Exception:
                                            pass
                            except Exception:
                                pass

                        # Deliver bytes to Reticulum owner inbound
                        if data_bytes:
                            RNS.log(f"MeshCore: inbound {len(data_bytes)} bytes (hex start: {data_bytes[:16].hex()})", RNS.LOG_DEBUG)
                            self.owner.inbound(data_bytes, self)
                            try:
                                if hasattr(self.owner, "inbound"):
                                    self.owner.inbound(data_bytes, self)
                                else:
                                    RNS.log("MeshCore: owner has no inbound method", self._LOG_WARNING)
                            except Exception as e:
                                RNS.log(f"MeshCore: owner.inbound() failed: {e}", RNS.LOG_ERROR)
                                
                        async def _on_new_contact(event):
                            try:
                                res = await self.mesh.commands.get_contacts()
                                if getattr(res, "type", None) != EventType.ERROR:
                                    contacts = getattr(res, "payload", {})
                                    if self.endpoint_contact:
                                        for key, contact in contacts.items():
                                            adv = contact.get("adv_name")
                                            if adv and self.endpoint_contact.lower() in adv.lower():
                                                self.endpoint_contact = key
                                                RNS.log(f"MeshCore: using {adv} ({key[:8]}...) as RNS endpoint", RNS.LOG_INFO)
                                                break
                                    else:
                                        RNS.log("MeshCore: no endpoint_contact specified; will broadcast by default", RNS.LOG_WARNING)
                            except Exception as e:
                                RNS.log(f"MeshCore: get_contacts() failed while selecting endpoint: {e}", RNS.LOG_WARNING)
                        
                    try:
                        self.mesh.subscribe(EventType.CONTACT_MSG_RECV, _on_contact_msg)
                        self.mesh.subscribe(EventType.NEW_CONTACT, _on_new_contact)
                        if not self.mesh._event_callbacks.get(EventType.CONTACT_MSG_RECV):
                            RNS.log("MeshCore: no CONTACT_MSG_RECV subscription active!", RNS.LOG_WARNING)
                    except Exception as e:
                        RNS.log(f"Meshcore: subscription to events failed: {e}", RNS.LOG_WARNING)
            
                except Exception as e:
                    RNS.log(f"MeshCore: subscription to events failed outer: {e}", RNS.LOG_WARNING)
            
                try:
                    res = await self.mesh.commands.get_contacts()
                    if getattr(res, "type", None) != EventType.ERROR:
                        contacts = getattr(res, "payload", {})
                        RNS.log(f"MeshCore: discovered {len(contacts)} contacts on device", RNS.LOG_VERBOSE)
                except Exception:
                    pass
            
                self.online = True
                RNS.log("MeshCoreInterface: online", RNS.LOG_INFO)
            
            return _async_setup()
    
        self._run_coro_no_wait(_setup_on_mesh_loop())
    
        self._start_outgoing_worker()
                                            
        if not self.mesh:
            raise RuntimeError("MeshCore: initialization failed, interface offline")
        
    def _start_outgoing_worker(self):
        thread = threading.Thread(target=self._outgoing_worker, daemon=True)
        thread.start()
        
    def _outgoing_worker(self):
        while True:
            # Cleanup old stored packets to prevent memory buildup
            if len(self.outgoing_packet_storage) > 512:
                oldest = list(self.outgoing_packet_storage.keys())[:128]
                for k in oldest:
                    self.outgoing_packet_storage.pop(k, None)

            try:
                if not self.packet_i_queue:
                    time.sleep(0.05)
                    continue

                index, pos = self.packet_i_queue.pop(0)
                handler = self.outgoing_packet_storage.get(index, None)
                if not handler:
                    continue

                data = handler[pos]
                if not data:
                    continue

                # resolve candidate destination (handler.destination_id or endpoint_contact)
                raw_dest = getattr(handler, "destination_id", None) or self.endpoint_contact
                dest_key = self._resolve_destination(raw_dest)

                # fallback: try mapping from RNS destination bytes
                if not dest_key:
                    try:
                        if len(data) >= 18:
                            rns_dest = data[2:18]
                            mapped = self.dest_to_node_dict.get(bytes(rns_dest).hex(), None)
                            dest_key = self._resolve_destination(mapped)
                    except Exception:
                        dest_key = None

                # attempt single send or broadcast
                if dest_key:
                    res = None
                    try:
                        RNS.log(f"MeshCore: about to call send_msg(dest type={type(dest_key).__name__}, payload type={type(data.decode('latin-1')).__name__})", RNS.LOG_DEBUG)
                        payload_for_meshcore = self._payload_for_send(data)   # returns str
                        res = self._run_coro(self.mesh.commands.send_msg(dest_key, payload_for_meshcore), timeout=8)
                        # guarded short id for logs
                        try:
                            if isinstance(dest_key, str):
                                short = dest_key[:8]
                            elif isinstance(dest_key, dict):
                                short = dest_key.get("public_key", "")[:8] or dest_key.get("adv_name", "")[:8]
                            else:
                                short = None
                        except Exception:
                            short = None
                        RNS.log(f"MeshCore: sent {len(data)} bytes to {short}", RNS.LOG_DEBUG)
                    except Exception as e:
                        RNS.log(f"MeshCore: exception while sending: {e}", RNS.LOG_ERROR)
                if res is not None and getattr(res, "type", None) == self._meshcore_EventType.ERROR:
                    RNS.log(f"MeshCore: send_msg returned error: {getattr(res, 'payload', None)}", self._LOG_WARNING)
                
#                else:
                    # broadcast to all contacts
#                    try:
#                        res_contacts = self._run_coro(self.mesh.commands.get_contacts(), timeout=8)
#                    except Exception as e:
#                        RNS.log(f"MeshCore: could not fetch contacts for broadcast: {e}", self._LOG_WARNING)
#                        res_contacts = None

#                    if res_contacts and getattr(res_contacts, "type", None) != self._meshcore_EventType.ERROR:
#                        contacts = getattr(res_contacts, "payload", {}) or {}
#                        for pk, contact in contacts.items():
#                            try:
                                # contact is a contact dict from get_contacts()
#                                RNS.log(f"MeshCore: about to call send_msg(dest type={type(dest_key).__name__}, payload type={type(data.decode('latin-1')).__name__})", RNS.LOG_DEBUG)
#                                payload_for_meshcore = self._payload_for_send(data)
#                                self._run_coro(self.mesh.commands.send_msg(contact, payload_for_meshcore), timeout=8)
#                                short = pk[:8] if isinstance(pk, str) else None
#                                RNS.log(f"MeshCore: broadcasted {len(data)} bytes to {short}", RNS.LOG_DEBUG)
#                            except Exception as e:
#                                RNS.log(f"MeshCore: send_msg to {pk} failed: {e}", self._LOG_WARNING)
#                    else:
#                        RNS.log("MeshCore: no contacts available for broadcast", self._LOG_WARNING)

            except Exception as e:
                RNS.log(f"MeshCore outgoing worker error: {e}", RNS.LOG_ERROR)

            time.sleep(0.05)

    def process_outgoing(self, data: bytes):
        if len(self.packet_i_queue) >= 256:
            RNS.log("MeshCore: outgoing queue full, dropping packet", RNS.LOG_WARNING)
            return

        dest = None
        try:
            if len(data) >= 18:
                rns_dest = data[2:18]
                dest = self.dest_to_node_dict.get(rns_dest, None)
        except Exception:
            dest = None

        # Normalize dest to one of: None, contact dict, or public-key hex string
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
        
    def should_ingress_limit(self, dest=None):
        return False
    
    def __str__(self):
        return f"MeshCoreInterface[{self.name}]"
    
class PacketHandler:
    struct_format = 'Bb'

    def __init__(self, data=None, index=None, max_payload=200, custom_destination_id=None):
        self.max_payload = max_payload
        self.index = index
        self.data_dict = {}
        self.loop_pos = 1
        self.done = False
        self.destination_id = custom_destination_id
        if data:
            self.split_data(data)

    def split_data(self, data: bytes):
        """Split data into even chunks and add metadata to it"""
        data_list = []
        data_len = len(data)
        if data_len == 0:
            return
        num_packets = data_len // self.max_payload + 1
        packet_size = data_len // num_packets + 1
        for i in range(0, data_len, packet_size):
            data_list.append(data[i:i + packet_size])
        for i, packet in enumerate(data_list):
            pos = i + 1
            if pos == len(data_list):
                pos = -pos
            meta_data = struct.pack(self.struct_format, self.index, pos)
            self.data_dict[pos] = meta_data + packet

    def get_keys(self):
        return list(self.data_dict.keys())

    def __getitem__(self, i):
        if i in self.data_dict:
            return self.data_dict[i]
        elif -i in self.data_dict:
            return self.data_dict[-i]
        else:
            return None

interface_class = MeshCoreInterface
