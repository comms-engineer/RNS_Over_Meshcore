# MeshCore Dynamic Interface for Reticulum

A [Reticulum](https://reticulum.network) interface driver that tunnels RNS traffic over a [MeshCore](https://github.com/ripplebiz/MeshCore) LoRa mesh radio network.

Supports hybrid routing: channel broadcast for path discovery and announces, unicast direct messages for established sessions. Peer discovery is demand-driven — no static node configuration is required.

---

## Features

- **Three transport backends** — serial, TCP, and BLE connections to a MeshCore node
- **Automatic peer discovery** via a pull-based RNSBIND protocol; no address lists to maintain
- **Hybrid routing** — broadcasts where necessary, switches to firmware-ACK'd unicast direct messages once a path is established
- **Edge node capability advertisement** — nodes with no upstream connectivity declare themselves at discovery time so peers do not attempt to route transit traffic through them
- **Downstream client support** — edge nodes can serve phones and laptops behind a hotspot while still participating fully in the mesh
- **Announce and path-request rate limiting** — independent per-destination throttles that operate alongside RNS's own rate caps to protect constrained LoRa channels
- **Automatic peer expiry** — stale peer entries and associated route table entries are cleaned up on a configurable TTL

---

## Requirements

| Dependency | Version | Notes |
|---|---|---|
| Python | ≥ 3.8 | |
| [Reticulum](https://github.com/markqvist/Reticulum) | ≥ 0.7.0 | `pip install rns` |
| [meshcore-py](https://github.com/ripplebiz/meshcore-py) | latest | `pip install meshcore` |
| MeshCore firmware | any recent | Flashed onto the LoRa radio node |

The interface has been tested with MeshCore nodes connected over USB serial (CP210x / CH340 adapters) and over TCP via the MeshCore companion app.

---

## Installation

1. Copy `MeshCore_Dynamic_Interface.py` to your Reticulum interfaces directory.  The default path is `~/.reticulum/interfaces/`.  Create the directory if it does not exist:

   ```bash
   mkdir -p ~/.reticulum/interfaces
   cp MeshCore_Dynamic_Interface.py ~/.reticulum/interfaces/
   ```

2. Add an interface block to `~/.reticulum/config` (see [Configuration](#configuration) below).

3. Restart `rnsd` (or your RNS host application).

---

## How It Works

### Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│  Wider Reticulum mesh (Internet / backbone transport nodes)     │
└────────────────────────────┬────────────────────────────────────┘
                             │ BackboneInterface (boundary)
                  ┌──────────▼──────────┐
                  │   Infrastructure    │  enable_transport = yes
                  │   (gateway) node    │  MeshCore iface: access_point
                  │                     │  can_route = yes
                  └──────────┬──────────┘
                             │ MeshCore Dynamic Interface
                             │ LoRa channel + unicast direct
                  ┌──────────▼──────────┐
                  │   Mobile / edge     │  enable_transport = yes
                  │   node              │  MeshCore iface: roaming
                  │                     │  can_route = no
                  └──────────┬──────────┘
                             │ TCPServerInterface / AutoInterface
                  ┌──────────▼──────────┐
                  │ Client devices      │  phones, laptops, tablets
                  │ (Sideband, NomadNet,│  connected via hotspot
                  │  any RNS client)    │
                  └─────────────────────┘
```

Infrastructure nodes have `enable_transport = yes` and a backbone connection to the wider mesh. Mobile/edge nodes also run transport, allowing them to route between the MeshCore radio link and any downstream client devices connected to a hotspot.

### Wire Format

Each outgoing RNS packet is split into fragments. Each fragment is encoded as a MeshCore message:

```
"RNS:" + base64url( [frag_idx : 1 byte] [pkt_id : 1 byte] [frag_total : 1 byte] + payload )
```

No base64 padding characters are transmitted. The receiver pads and decodes each message, then reassembles fragments by `(sender, pkt_id)` key.

The `payload_size` parameter controls how many raw bytes go into each fragment. A larger value means fewer fragments per packet, but MeshCore firmware silently truncates messages that exceed its internal character limit (~128 chars on common builds). The default of 64 bytes encodes to ~94 characters, which is safe even with moderately long node names prepended by the firmware.

To calculate the safe limit for your specific node name length:

```
budget      = firmware_limit - len(node_name) - 2
max_payload = floor((budget - 4) * 3/4) - 3
```

### Peer Discovery

Discovery follows a pull-based protocol inspired by ARP and RFC 2236 (IGMP report suppression):

1. A node with no known peers broadcasts `RNSBIND_REQ:<pubkey>:<cap>` on the shared channel.
2. Every overhearing node records the sender immediately (passive learning), then waits a random 3–15 second backoff before responding with `RNSBIND:<pubkey>:<cap>`.
3. The random backoff prevents a simultaneous response burst on the half-duplex LoRa channel.
4. All nodes overhearing any `RNSBIND` response also learn the responder — a single discovery round populates every peer table on the channel.
5. Once peers are known, a quiet hourly heartbeat maintains the tables without soliciting responses.

The `<cap>` field is either `R` (routing node, has upstream connectivity) or `E` (edge node, no upstream). This is informational — it is logged and stored per peer but does not affect per-packet delivery decisions (see [Edge Nodes and Downstream Clients](#edge-nodes-and-downstream-clients)).

### Routing

**Outbound path selection:**

```
Is this packet a broadcast type? (ANNOUNCE or two-byte header)
  YES → send on the shared channel
  NO  → is there a cached unicast route in the RNS→MC map?
          YES → send as MeshCore direct message (firmware ACK + retry)
          NO  → send on the shared channel
```

**Dynamic route learning:** Every fully-reassembled inbound packet contributes to the route table. Bytes 1–10 of the RNS packet (the origin token, consistent across announce / path-reply / data sequences) are mapped to the MeshCore public key of the sender. Future outbound packets whose next-hop token matches an entry in this map are sent unicast, reducing channel load.

### Edge Nodes and Downstream Clients

An edge node (`can_route = no`) can host downstream client devices (phones, laptops) via a hotspot. RNS running on the edge node routes transparently between the MeshCore interface and the hotspot server interface. Client devices appear as first-class participants in the mesh from the perspective of infrastructure nodes.

The `can_route = no` flag is advertised in RNSBIND messages so that other MeshCore peers do not attempt to use the edge node as a transit gateway to the wider mesh. It does **not** prevent the edge node from being used as a delivery path to its own identity or to clients behind it — the route map is built from observed traffic and is trusted unconditionally.

---

## Configuration

### Placing the Interface File

Reticulum discovers custom interface types by scanning the `interfaces/` subdirectory of the RNS config directory for Python files that define `interface_class`. The default config directory is `~/.reticulum/` on Linux/macOS and `%APPDATA%\reticulum\` on Windows.

```bash
# Linux / macOS default
~/.reticulum/interfaces/MeshCore_Dynamic_Interface.py

# Explicit path (if your config is elsewhere)
$RNS_CONFIG_DIR/interfaces/MeshCore_Dynamic_Interface.py
```

### Parameter Reference

| Parameter | Type | Default | Description |
|---|---|---|---|
| `transport` | `serial` \| `tcp` \| `ble` | `serial` | MeshCore connection method |
| `port` | string | `/dev/ttyUSB0` | Serial device path (serial transport) |
| `baudrate` | int | `115200` | Serial baud rate |
| `host` | string | `127.0.0.1` | TCP host address (tcp transport) |
| `tcp_port` | int | `4403` | TCP port (tcp transport) |
| `ble_name` | string | *(empty)* | BLE device name; blank = first found (ble transport) |
| `channel_idx` | int | `0` | MeshCore channel index |
| `channel_name` | string | `RNSTunnel` | MeshCore channel name |
| `channel_secret` | hex string | *(placeholder)* | 128-bit channel key as 32 hex chars — **must be changed** |
| `freq` | float | `0` | Radio centre frequency in MHz (0 = use stored value) |
| `bw` | float | `0` | Radio bandwidth in kHz (0 = use stored value) |
| `sf` | int | `0` | Spreading factor 7–12 (0 = use stored value) |
| `cr` | int | `0` | Coding rate denominator 5–8 (0 = use stored value) |
| `payload_size` | int | `64` | Raw bytes per fragment before base64 encoding |
| `fragment_delay` | float | `2.5` | Seconds between channel-broadcast fragments |
| `direct_frag_delay` | float | `0.5` | Seconds between unicast direct-message fragments |
| `fragment_timeout` | int | `3600` | Seconds before incomplete fragment sets are discarded |
| `rate_limit` | int | `0` | Hard bandwidth cap in bits/second (0 = disabled) |
| `outgoing_announce_rate` | int | `600` | Min seconds between forwarding announces per destination (0 = disabled) |
| `outgoing_path_req_rate` | int | `1800` | Min seconds between forwarding path requests per destination (0 = disabled) |
| `can_route` | `yes` \| `no` | `yes` | Advertise upstream routing capability to peers |
| `allow_direct` | `yes` \| `no` | `yes` | Use unicast direct messages when a route is cached |
| `peer_ttl` | int | `86400` | Seconds of silence before a peer entry is removed |
| `debug_level` | `info` \| `debug` | `info` | Logging verbosity for this interface |

> **Radio overrides:** `freq`, `bw`, `sf`, and `cr` are only pushed to the MeshCore node if all four are non-zero. Leave them at `0` to use whatever parameters are stored on the node.

> **Channel secret:** Generate a fresh key with `openssl rand -hex 16`. All nodes on the same tunnel must share the same `channel_idx`, `channel_name`, and `channel_secret`.

---

## Network Topology Guide

### Interface Mode Quick Reference

| Node role | MeshCore interface mode | Backbone interface mode | `can_route` |
|---|---|---|---|
| Fixed infrastructure / gateway | `access_point` | `boundary` | `yes` |
| Mobile / edge node | `roaming` | `boundary` | `no` |

These modes come from the [Reticulum interface mode documentation](https://reticulum.network/manual/interfaces.html):

- **`access_point`** — Quiet by default. Announces are not proactively broadcast on this interface. Path requests from clients are still resolved. Use this on LoRa-facing interfaces at infrastructure nodes so the channel is not pre-populated with the entire known announce table from the backbone.

- **`roaming`** — For physically mobile interfaces. Paths via roaming interfaces expire faster, preventing stale routing entries from accumulating at infrastructure nodes.

- **`boundary`** — Marks the edge between significantly different network segments. Use this on backbone/TCP interfaces at both infrastructure and mobile nodes to prevent the faster network from being treated as a client-facing access network.

> **⚠️  Never use `gateway` mode on a LoRa interface that is backed by a high-connectivity transport node.** Gateway mode proactively pushes every known announce to clients on that interface. With thousands of routes from the public Reticulum mesh, this will flood a shared LoRa channel continuously.

### Infrastructure Node

A fixed node with a reliable backbone connection to the wider Reticulum mesh. Runs `rnsd` with `enable_transport = yes`. Listens for path requests from mobile nodes and resolves them via the backbone.

Key settings:
- MeshCore interface: `mode = access_point`, `can_route = yes`
- Backbone interface: `mode = boundary` with `announce_rate_target` / `announce_rate_grace` / `announce_rate_penalty`
- Longer `peer_ttl` (default 86400 s / 24 h) appropriate for fixed nodes

### Mobile / Edge Node

A portable node that carries a MeshCore radio and optionally a hotspot for downstream clients. May or may not have backbone connectivity depending on location. Runs `rnsd` with `enable_transport = yes`.

Key settings:
- MeshCore interface: `mode = roaming`, `can_route = no`
- Backbone interface: `mode = boundary`, `interface_enabled = no` (enable manually or conditionally when on home LAN)
- Shorter `peer_ttl` (e.g. `7200` s / 2 h) so infrastructure nodes stop generating path requests after the node goes offline
- Hotspot server interface (`TCPServerInterface` or `AutoInterface`) for downstream clients

### Client Devices

End-user devices (Android phones running Sideband, laptops running NomadNet, etc.) that connect to a mobile node's hotspot. These do not run rnsd in transport mode. Configure them to connect to the mobile node's hotspot IP and RNS server port.

---

## Troubleshooting

### Announce flooding on the LoRa channel

**Symptom:** Continuous stream of `RNS:` messages on the MeshCore channel even with no active sessions.

**Cause:** Almost always `gateway` or `access_point`-with-wrong-mode on the MeshCore interface of a node that has a high-connectivity backbone connection. In gateway mode, RNS pushes every known announce to all clients on that interface.

**Fix:**
1. Confirm the MeshCore interface on your infrastructure node is `mode = access_point` (not `gateway`).
2. Add `announce_rate_target`, `announce_rate_grace`, and `announce_rate_penalty` to the backbone interface.
3. Set `outgoing_announce_rate` on the MeshCore interface config.

---

### Silent truncation / broken base64

**Symptom:** Packets arrive but are never fully reassembled. Debug logs show fragments arriving but the packet never completes.

**Cause:** `payload_size` is set too high. MeshCore firmware silently truncates messages that exceed its internal character limit (~128 chars on most builds). The truncated message decodes as a different (shorter) fragment, which never combines with the others.

**Fix:** Lower `payload_size`. The default of 64 is conservative and correct for most deployments. Use the formula in the [Wire Format](#wire-format) section to find the exact limit for your node name length.

---

### Path request flooding (offline node)

**Symptom:** Continuous `RNS:` messages on the channel after a mobile node goes offline, even though no one is trying to communicate with it.

**Cause:** Remote nodes on the wider Reticulum mesh still have a path to the offline node and are sending path requests to find it. AP mode does NOT suppress path requests — it only blocks announce re-broadcasting.

**Fix:**
1. Set `outgoing_path_req_rate = 1800` (or higher) on the infrastructure node's MeshCore interface.
2. Set a short `peer_ttl` on the mobile node's configuration so that infrastructure nodes expire the stale peer entry sooner and stop generating path requests for it.

---

### No peers discovered

**Symptom:** The interface starts, sends RNSBIND_REQ, but no peers respond. Retries exhaust and the interface falls back to heartbeat mode.

**Checks:**
1. Confirm all nodes share the same `channel_idx`, `channel_name`, and `channel_secret`.
2. Verify the MeshCore node is actually online and connected (`rnsd` log will show the node name and key if `_async_setup` succeeds).
3. Check that `debug_level = debug` is set and look for `REQ from` log entries — if the remote node sees the REQ, it will log it.
4. Confirm no firewall or serial permission issue is preventing the driver from connecting (the `Driver init error` log line will appear if so).

---

### Direct messages not being used

**Symptom:** All traffic goes over the channel even after sessions are established.

**Checks:**
1. Confirm `allow_direct = yes` on both nodes.
2. Check that the MeshCore library version supports `send_msg` — the interface logs `has_direct_api = False` at startup if the method is absent.
3. Route learning requires at least one inbound packet from the remote peer. If the remote node has not yet sent anything, the route map entry will not exist.

---

## Known Limitations

- **Fragment ordering assumes in-order delivery.** MeshCore channel messages are generally delivered in order within a single burst. Out-of-order delivery (possible with retries or multi-hop routing) will still reassemble correctly because fragments are indexed, but stale fragments from a previous incomplete burst with the same `pkt_id` could corrupt reassembly. The `fragment_timeout` setting limits the window during which this can occur.

- **`pkt_id` is 8-bit (0–255).** Rollover is possible under sustained high-throughput conditions. In practice, with per-fragment delays of 0.5–2.5 seconds, rollover takes several minutes and is extremely unlikely to cause a collision in the reassembly buffer.

- **BLE transport is untested by the author.** The code follows the meshcore-py API but BLE connection reliability is hardware and OS dependent.

- **Radio parameter overrides are best-effort.** If the MeshCore node rejects the `set_radio` command (e.g. due to firmware version differences), the interface logs a warning and continues with stored radio parameters.

---

## Related Projects

- [Reticulum Network Stack](https://github.com/markqvist/Reticulum)
- [MeshCore firmware](https://github.com/ripplebiz/MeshCore)
- [meshcore-py](https://github.com/ripplebiz/meshcore-py) — Python library for communicating with MeshCore nodes
- [Sideband](https://github.com/markqvist/Sideband) — RNS messaging app for Android and desktop
- [NomadNet](https://github.com/markqvist/NomadNet) — Decentralised mesh communication platform built on RNS

---

## Contributing

Issues and pull requests are welcome. If you find a bug or have a question, please include:

- The relevant section of your `~/.reticulum/config`
- The interface log output at `debug_level = debug`
- Your MeshCore firmware version and transport type (serial / TCP / BLE)
- Your meshcore-py version (`pip show meshcore`)

---

Yes, I absolutely had help from Claude on this. I'm not a software person, I'm just dumb enough to think I can beat my head against something until it works. PLEASE feel free to offer improvements and corrections.
