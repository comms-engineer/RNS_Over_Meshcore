#!/usr/bin/env python3
"""
meshcore_rx_debug.py
---------------------
Minimal MeshCore diagnostic tool.
Connects directly to a MeshCore serial device and prints every received event.

Usage:
  python3 meshcore_rx_debug.py /dev/ttyUSB0
"""

import asyncio
import sys
import base64
import textwrap
from meshcore import MeshCore, EventType

async def main(port):
    print(f"[+] Connecting to MeshCore device on {port} ...")
    mesh = await MeshCore.create_serial(port, 115200, debug=True)
    if not mesh:
        print("[!] Failed to connect to MeshCore device.")
        return

    print("[+] Connected. Fetching contacts...")
    try:
        res = await mesh.commands.get_contacts()
        print(f"[i] Contacts: {res.payload if hasattr(res, 'payload') else res}")
    except Exception as e:
        print(f"[!] Could not fetch contacts: {e}")

    # --- Event Handlers ---
    async def on_contact_msg(event):
        print("\n=== CONTACT_MSG_RECV ===")
        payload = getattr(event, "payload", None)
        if payload is None:
            print("[!] No payload found")
            return

        # Print structure
        print(f"Type: {type(payload)}")
        if isinstance(payload, dict):
            for k, v in payload.items():
                print(f"  {k}: {v if not isinstance(v, (bytes, bytearray)) else f'<{len(v)} bytes>'}")

        # Extract possible text/data fields
        for key in ("payload", "data", "text"):
            if isinstance(payload, dict) and key in payload:
                val = payload[key]
            elif not isinstance(payload, dict) and key == "text":
                val = payload
            else:
                continue

            print(f"\n-- {key} field --")
            if isinstance(val, (bytes, bytearray)):
                b = bytes(val)
            elif isinstance(val, str):
                try:
                    b = val.encode("latin-1")
                except Exception:
                    b = val.encode("utf-8", errors="ignore")
            else:
                continue

            # Show raw bytes
            print(f"Raw bytes ({len(b)}): {b[:80]!r}")

            # Try base64 decode once
            try:
                d1 = base64.b64decode(b, validate=True)
                print(f"1x b64 decoded ({len(d1)}): {d1[:80]!r}")
                # Try decoding again
                try:
                    d2 = base64.b64decode(d1, validate=True)
                    print(f"2x b64 decoded ({len(d2)}): {d2[:80]!r}")
                    try:
                        preview = d2.decode(errors='ignore')
                        print(f"Printable preview (2x): {preview[:80]!r}")
                    except Exception:
                        pass
                except Exception:
                    try:
                        preview = d1.decode(errors='ignore')
                        print(f"Printable preview (1x): {preview[:80]!r}")
                    except Exception:
                        pass
            except Exception:
                try:
                    preview = b.decode(errors='ignore')
                    print(f"Printable preview (raw): {preview[:80]!r}")
                except Exception:
                    print("[!] Not valid base64 or UTF-8 data")

    async def on_ack(event):
        print(f"[ACK] from {getattr(event, 'payload', None)}")

    async def on_new_contact(event):
        contact = getattr(event, "payload", None)
        print(f"[+] New contact: {contact}")

    mesh.subscribe(EventType.CONTACT_MSG_RECV, on_contact_msg)
    mesh.subscribe(EventType.ACK, on_ack)
    mesh.subscribe(EventType.NEW_CONTACT, on_new_contact)

    print("[*] Listening for incoming messages... Press Ctrl+C to exit.")
    try:
        await mesh.start_auto_message_fetching()
        while True:
            await asyncio.sleep(1)
    except asyncio.CancelledError:
        pass
    except KeyboardInterrupt:
        print("\n[!] Exiting.")
        await mesh.close()

if __name__ == "__main__":
    port = sys.argv[1] if len(sys.argv) > 1 else "/dev/ttyUSB0"
    asyncio.run(main(port))
