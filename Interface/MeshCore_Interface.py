# [[MeshCore Interface]]
# type = MeshCore_Interface
# enabled = true
# mode = boundary
# port = /dev/ttyUSB0
# speed = 115200

import asyncio
import threading
import time
import struct
import re

# Minimal MeshCore interface
class MeshCoreInterface(Interface):
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
    
    def _loop_alive(self):
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
    
    def _open_serial_and_init(self):
        
        meshcore = self._meshcore_module
        EventType = self._meshcore_EventType

        self._start_event_loop_thread()
        
        try:
            create_coro = meshcore.MeshCore.create_serial(self.port, 115200, debug=True)
            self.mesh = self._run_coro(create_coro, timeout=15)
            RNS.log(f"MeshCore: connected to device on {self.port}", RNS.LOG_INFO)
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
                except Exception as e:
                    RNS.log(f"MeshCore: start_auto_message_fetching() failed: {e}", RNS.LOG_WARNING)
                
                try:
                    async def _on_contact_msg(event):
                        payload = event.payload
                        from_key = None
                        data_bytes = None
                    
                        if isinstance(payload, dict):
                            if 'payload' in payload and isinstance(payload['payload'], (bytes, bytearray)):
                                data_bytes = bytes(payload['payload'])
                            elif 'text' in payload and isinstance(payload['text'], str):
                                data_bytes = payload['text'].encode('utf-8')
                            elif 'data' in payload and isinstance(payload['data'], (bytes, bytearray)):
                                data_bytes = bytes(payload['data'])
                                
                            if 'public_key' in payload:
                                from_key = payload['public_key']
                            elif 'pubkey_prefix' in payload:
                                from_key = payload['pubkey_prefix']
                    
                        if data_bytes:
                            try:
                                if len(data_bytes) >= 18:
                                    bit_str = "{:08b}".format(data_bytes[0])
                                    if re.match(r'00..11..', bit_str):
                                        dest = data_bytes[2:18]
                                        try:
                                            if len(self.dest_to_node_dict) > 20:
                                                first_key = next(iter(self.dest_to_node_dict))
                                                self.dest_to_node_dict.pop(first_key, None)
                                        except Exception:
                                            pass
                                        if from_key:
                                            self.dest_to_node_dict[dest] = from_key
                            except Exception:
                                pass
                            
                            try:
                                if hasattr(self.owner, "inbound"):
                                    self.owner.inbound(data_bytes, self)
                                else:
                                    RNS.log(f"MeshCore: owner has no inbound method", RNS.LOG_WARNING)
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
                    except Exception:
                        try:
                            await self.mesh.subscribe(EventType.CONTACT_MSG_RECV, _on_contact_msg)
                            await self.mesh.subscribe(EventType.NEW_CONTACT, _on_new_contact)
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
                    
                dest_key = handler.destination_id or self.endpoint_contact
                if not dest_key:
                    try:
                        if len(data) >= 18:
                            rns_dest = data[2:18]
                            dest_key = self.dest_to_node_dict.get(rns_dest, None)
                    except Exception:
                        dest_key = None
                        
                try:
                    if dest_key:
                        try:
                            fut = self._run_coro(self.mesh.commands.send_msg(dest_key, data), timeout=8)
                            res = fut
                        except Exception as e:
                            RNS.log(f"MeshCore: exception while sending: {e}", RNS.LOG_ERROR)
                            res = None
                    else:
                        res_contacts = None
                        try:
                            res_contacts = self._run_coro(self.mesh.commands.get_contacts(), timeout=8)
                        except Exception as e:
                            RNS.log(f"MeshCore: could not fetch contacts for broadcast: {e}", RNS.LOG_WARNING)
                            res_contacts = None
                        
                        if not res_contacts:
                            continue
                        
                        if getattr(res_contacts, "type", None) == self._meshcore_EventType.ERROR:
                            RNS.log("MeshCore: could not fetch contacts for broadcast", RNS.LOG_WARNING)
                        
                        contacts = getattr(res_contacts, "payload", {})
                        for pk, contact in contacts.items():
                            try:
                                try:
                                    self._run_coro(self.mesh.commands.send_msg(contact, data), timeout=8)
                                except Exception as e:
                                    RNS.log(f"MeshCore: send_msg to {pk} failed: {e}", RNS.LOG_WARNING)
                                    continue
                            except Exception as e:
                                RNS.log(f"MeshCore: exception while sending to contact {pk}: {e}", RNS.LOG_WARNING)
                                continue
                        res = None
                            
                    if res is not None and getattr(res, "type", None) == self._meshcore_EventType.ERROR:
                        RNS.log(f"MeshCore: send_msg returned error: {getattr(res, 'payload', None)}", RNS.LOG_WARNING)
                except Exception as e:
                    RNS.log(f"MeshCore: outgoing worker error: {e}", RNS.LOG_ERROR)
            
            except Exception as e:
                RNS.log(f"MeshCore outgoing worker error: {e}", RNS.LOG_ERROR)
            
            if self.mesh and dest_key:
                try:
                    self._run_coro(self.mesh.commands.send_msg(dest_key, data), timeout=8)
                    RNS.log(f"MeshCore: sent {len(data)} bytes to {dest_key[:8]}", RNS.LOG_DEBUG)
                except Exception as e:
                    RNS.log(f"MeshCore: failed final send: {e}", RNS.LOG_WARNING)
            
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
            
        handler = PacketHandler(data, self.packet_index, max_payload=160, custom_destination_id=dest)
        for key in handler.get_keys():
            self.packet_i_queue.append((handler.index, key))
        self.outgoing_packet_storage[handler.index] = handler
        self.packet_index = (self.packet_index + 1) % 256
        
    def should_ingress_limit(self):
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

