# MeshCore Dynamic Interface

A [Reticulum Network Stack (RNS)](https://reticulum.network/) custom interface that tunnels RNS traffic over a [MeshCore](https://meshcore.co.uk/) LoRa mesh. It requires no static remote-node configuration — peers discover each other dynamically over the air — and uses a hybrid channel-broadcast / unicast-direct routing strategy to keep airtime usage on a shared, half-duplex LoRa channel as low as possible.

## Why this exists

RNS ships interfaces for TCP, serial, I2P, packet radio, and a handful of others, but nothing that speaks directly to MeshCore firmware. This interface fills that gap: it fragments and re-assembles RNS binary packets into MeshCore channel/direct messages, and layers a lightweight peer-discovery and routing protocol on top so that Reticulum can run natively over a MeshCore LoRa network — including in mixed deployments where a MeshCore mesh acts as the "last mile" for an existing RNS transport backbone.

## Features

- **Zero static config peer discovery** — nodes find each other with a demand-driven `RNSBIND_REQ` / `RNSBIND` handshake instead of periodic broadcast, based on the RFC 2236 (IGMP) report-suppression pattern to avoid response storms on a shared channel.
- **Hybrid routing** — channel broadcast for announces/discovery, unicast direct messages for established peer-to-peer sessions, with automatic fallback from direct to channel if a unicast send fails or goes unacknowledged.
- **RNS Link ID aware routing** — correctly follows Reticulum's ephemeral Link ID once a Link handshake completes, deriving the destination hash locally so routing doesn't break mid-session.
- **Capability-aware discovery** — peers advertise whether they can carry transit traffic (`R` router / `E` edge) at discovery time, useful for distinguishing infrastructure nodes from battery-powered edge devices.
- **Delivery-aware direct sends** — waits on the MeshCore firmware's `expected_ack` / `ACK` event pair for unicast messages rather than trusting the immediate `MSG_SENT` result, with a bounded timeout so a single slow/flood-mode peer can't stall the shared outgoing queue.
- **Configurable fragmentation** — RNS packets are split into MeshCore-message-sized fragments with a compact 6-byte binary header, sized to fit under firmware channel-message character limits.
- **Rate limiting** — independent throttles for outgoing announces, path requests, and (optionally) a hard bitrate cap, to keep the interface well-behaved on congested or bandwidth-constrained channels.
- **Multiple transports** — connects to the MeshCore node over serial, TCP, or BLE.

## Requirements

- Python 3.9+
- [Reticulum (`rns`)](https://pypi.org/project/rns/)
- The [`meshcore`](https://pypi.org/project/meshcore/) Python library
- A MeshCore-flashed radio (or a MeshCore companion app reachable over TCP/BLE) reachable from the host running `rnsd`

```bash
pip install rns meshcore
```

## Installation

1. Copy `MeshCore_Dynamic_Interface.py` into your Reticulum config's `interfaces` directory (typically `~/.reticulum/interfaces/`).
2. Add an interface block to `~/.reticulum/config` (see [Configuration](#configuration) below).
3. Restart `rnsd`, or reload interfaces if your setup supports it.
4. Every node participating in the same tunnel must use the same `channel_idx`, `channel_name`, and `channel_secret`.

## Configuration

Every node needs at minimum a transport block and matching channel identity. A full infrastructure/transport-node example:

```ini
[reticulum]
  enable_transport = yes
  share_instance = yes

[logging]
  loglevel = 4    # increase to 7 for debug

[interfaces]

  [[MeshCore Dynamic Interface]]
    type = MeshCore_Dynamic_Interface
    interface_enabled = yes

    # Role
    mode = access_point
    can_route = yes

    # Transport — uncomment exactly one block
    # Serial (most common):
    transport = serial
    port = /dev/ttyUSB0
    baudrate = 115200
    #
    # TCP (MeshCore node reachable over IP):
    # transport = tcp
    # host = 127.0.0.1
    # tcp_port = 4403
    #
    # BLE:
    # transport = ble
    # ble_name =            # blank = connect to first found device

    # Channel — all nodes on the same tunnel must share these values
    channel_idx = 0
    channel_name = RNSTunnel
    channel_secret = <32 hex chars>   # openssl rand -hex 16

    # Radio overrides — all four must be non-zero to take effect.
    # Leave commented to use the values already stored on the MeshCore node.
    # freq = 915.0        # MHz centre frequency
    # bw   = 250.0        # kHz bandwidth (125 / 250 / 500)
    # sf   = 10            # spreading factor (7-12)
    # cr   = 5             # coding rate denominator (5=4/5 ... 8=4/8)

    # Fragmentation
    payload_size = 64         # bytes/fragment - see "Payload size" below
    fragment_delay = 2.5      # seconds between channel-mode fragments
    direct_frag_delay = 0.5   # seconds between direct-message fragments
    fragment_timeout = 300    # 5-minute reassembly window for high-latency meshes

    # Outgoing rate limiting (set to 0 to disable)
    outgoing_announce_rate = 600     # min seconds between announces per dest
    outgoing_path_req_rate = 1800    # min seconds between path requests per dest

    # Optional hard bandwidth cap in bits per second (0 = disabled)
    # rate_limit = 1200

    # Peer discovery
    allow_direct = yes    # use unicast direct messages when a route is known
    peer_ttl = 86400      # seconds before a silent peer expires

    debug_level = info    # info | debug

  [[Backbone Interface]]
    type = BackboneInterface
    interface_enabled = yes
    mode = boundary
    target_host = <backbone-server-hostname-or-ip>
    target_port = 4242
    # Rate-limit announce re-propagation from the fast network
    announce_rate_target  = 3600
    announce_rate_grace   = 2
    announce_rate_penalty = 7200
```

### Interface mode

Mode selection has a real impact on announce traffic, path expiry, and channel load — get it wrong and a LoRa channel can be flooded indefinitely.

**`access_point`** (recommended for infrastructure/transport nodes with backbone connectivity)
Announces are not automatically re-broadcast on this interface, and paths to destinations behind it expire faster, matching the transient nature of battery-powered or intermittently-connected field devices. Path requests from clients are still forwarded and resolved on their behalf.

> **Note:** AP mode only suppresses `ANNOUNCE` re-broadcasting. `DATA`+`PLAIN` path requests from the wider mesh for a recently-offline node still pass through AP mode onto the LoRa channel. Use `outgoing_path_req_rate` to throttle these independently.

> ⚠️ **Never use `gateway` mode on a LoRa interface on a node that is also connected to a high-connectivity backbone.** Gateway mode proactively pushes *all* known announces to clients on that interface — with thousands of routes on the public Reticulum mesh, this will flood a shared LoRa channel indefinitely.

**`boundary`**
Applied to the backbone/TCP interface connecting the slow radio segment to a fast LAN or the internet. Marks the network edge so the transport node doesn't treat the backbone as a client-facing interface for proactive path distribution.

Add announce rate control to the backbone interface to throttle how quickly announces from the wider network are re-propagated onto the radio side:

```ini
announce_rate_target  = 3600   # min seconds between re-announces per dest
announce_rate_grace   = 2      # violations tolerated before enforcement
announce_rate_penalty = 7200   # extended quiet period after a violation
```

### Payload size

MeshCore firmware silently truncates channel messages beyond a hardware-dependent character limit (commonly ~128 chars). The firmware also prepends the sender's node name when relaying channel messages, so the usable character budget for the encoded fragment is:

```
budget = firmware_limit - len(node_name) - 2        # ": " separator
```

Encoded message length for a given payload size:

```
msg_len = ceil((payload_size + HEADER_SIZE) * 4/3) + len("RNS:")
```

With the default 6-byte header and `payload_size = 64`:

```
msg_len = ceil(70 * 4/3) + 4 = 98 chars   →  safe for node names up to ~28 characters at a 128-char firmware limit
```

To size `payload_size` for your own node name length:

```
budget      = firmware_limit - len(node_name) - 2
max_payload = floor((budget - 4) * 3/4) - HEADER_SIZE
```

## How it works

### Wire format

Each RNS binary packet is split into `payload_size`-byte chunks. Each chunk is encoded as a MeshCore channel (or direct) message:

```
"RNS:" + base64url( [frag_idx:1][pkt_id:4][frag_total:1] + payload )
```

Base64 padding is stripped before transmission and restored on receipt.

### Peer discovery

Discovery is demand-driven rather than push/periodic, to minimize channel airtime:

1. A node with no known peers sends `RNSBIND_REQ:<pubkey>:<cap>` on the channel, advertising its own routing capability alongside its identity.
2. Overhearing nodes immediately record the requester (passive learning), wait a random backoff (`BIND_BACKOFF_MIN`–`BIND_BACKOFF_MAX` seconds), then reply with `RNSBIND:<pubkey>:<cap>`. The randomized backoff spreads responses out in time to avoid a simultaneous burst on the shared half-duplex channel.
3. Every node overhearing *any* `RNSBIND` response also records the responder, so a single discovery round passively populates every peer table on the channel.
4. Once peers are known, a quiet `RNSBIND` heartbeat goes out every `BIND_HEARTBEAT_S` (default: 1 hour) — no response is solicited.

The capability suffix (`R` = router, `E` = edge) tells peers at discovery time whether a node has upstream connectivity worth routing transit traffic through. It's recorded and logged but doesn't gate per-packet routing decisions — the interface's live route map is built from observed packet flow, and a path that has demonstrably worked (including through an edge node to reach a downstream client) is used regardless of the advertised capability.

### RNS header parsing

The interface inspects the RNS header byte to distinguish packet types (`DATA`, `ANNOUNCE`, `LINKREQUEST`, `PROOF`) and destination types (`SINGLE`, `GROUP`, `PLAIN`, `LINK`), and locally derives the destination hash for established Links (whose destination field becomes an ephemeral Link ID after handshake) so that direct-message routing continues to work for the life of the Link.

### Delivery confirmation

A MeshCore `MSG_SENT` result only confirms the local radio queued the frame — it isn't end-to-end delivery confirmation. For direct sends, the interface waits on the firmware's follow-up `ACK` event (matched via the `expected_ack` tag from `MSG_SENT`), bounded by `direct_ack_timeout` and a hard ceiling `direct_ack_timeout_max` so that a flood-mode peer with a long firmware-suggested timeout can't stall every other fragment behind it in the shared outgoing queue. A failed or unacknowledged direct send falls back to a channel broadcast.

## Transports

| Transport | Config keys |
|---|---|
| Serial (default) | `port`, `baudrate` |
| TCP | `host`, `tcp_port` |
| BLE | `ble_name` (blank = connect to first device found) |

## Tuning reference

| Key | Default | Purpose |
|---|---|---|
| `payload_size` | `64` | Fragment payload size in bytes; see [Payload size](#payload-size) |
| `fragment_delay` | `2.5` | Seconds between channel-mode fragments |
| `direct_frag_delay` | `0.5` | Seconds between direct-message fragments |
| `fragment_timeout` | `300` | Reassembly window for incomplete multi-fragment packets |
| `direct_ack_timeout` | `4.0` | Minimum wait for a direct-send delivery ACK |
| `direct_ack_timeout_max` | `8.0` | Hard ceiling on the ACK wait regardless of firmware suggestion |
| `outgoing_announce_rate` | `600` | Minimum seconds between announces per destination (`0` disables) |
| `outgoing_path_req_rate` | `1800` | Minimum seconds between path requests per destination (`0` disables) |
| `rate_limit` | `0` | Optional hard bandwidth cap in bits/second (`0` disables) |
| `allow_direct` | `yes` | Use unicast direct messages when a route to the peer is known |
| `peer_ttl` | `86400` | Seconds before a silent peer is dropped from the peer table |
| `can_route` | `yes` | Whether this node can carry transit traffic |
| `debug_level` | `info` | `info` or `debug` |

## Limitations

- MeshCore's channel-message character limit varies by firmware build and must be accounted for when choosing `payload_size` (see [Payload size](#payload-size)).
- `access_point` mode suppresses announce re-broadcasting but not `DATA`+`PLAIN` path requests; a node that flaps offline can still generate path-request traffic on the LoRa channel from remote nodes searching for it. Use `outgoing_path_req_rate` to bound this.
- This interface is built and tested against a specific `meshcore` library API surface; firmware/library version drift may require updates to event/attribute names.

Yes, I absolutely had help from Claude on this. I'm not a software person, I'm just stubborn enough to think I can beat my head against something until it works. PLEASE feel free to offer improvements and corrections.
