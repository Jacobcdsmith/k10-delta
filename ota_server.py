#!/usr/bin/env python3
"""
K10-Δ OTA Firmware Server
Serves .bin/.py firmware files over HTTP for K10 OTA updates.
Run alongside host.py on the same machine.
"""

import http.server
import os
import hashlib
import json

PORT = 8080
FIRMWARE_DIR = os.path.join(os.path.dirname(__file__), "firmware")


class FirmwareHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print(f"[OTA HTTP] {self.address_string()} - {fmt % args}")

    def do_GET(self):
        if self.path == "/manifest":
            self._serve_manifest()
        elif self.path.startswith("/firmware/"):
            self._serve_firmware(os.path.basename(self.path))
        else:
            self.send_error(404)

    def _serve_firmware(self, filename):
        # Reject path traversal attempts
        if ".." in filename or "/" in filename or "\\" in filename:
            self.send_error(400, "Invalid filename")
            return

        fpath = os.path.join(FIRMWARE_DIR, filename)
        if not os.path.isfile(fpath):
            self.send_error(404, "Firmware not found")
            return

        with open(fpath, "rb") as f:
            data = f.read()

        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-SHA256", hashlib.sha256(data).hexdigest())
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)

    def _serve_manifest(self):
        """Return JSON listing of all firmware files with their SHA-256 hashes."""
        entries = []
        if os.path.isdir(FIRMWARE_DIR):
            for fname in sorted(os.listdir(FIRMWARE_DIR)):
                fpath = os.path.join(FIRMWARE_DIR, fname)
                if os.path.isfile(fpath):
                    with open(fpath, "rb") as f:
                        h = hashlib.sha256(f.read()).hexdigest()
                    entries.append({"file": fname, "hash": h, "url": f"/firmware/{fname}"})

        body = json.dumps(entries, indent=2).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main():
    os.makedirs(FIRMWARE_DIR, exist_ok=True)
    print(f"[OTA Server] Serving firmware from {FIRMWARE_DIR} on :{PORT}")
    print(f"[OTA Server]   GET /manifest       — list all firmware files")
    print(f"[OTA Server]   GET /firmware/<name> — download a specific file")
    with http.server.ThreadingHTTPServer(("", PORT), FirmwareHandler) as httpd:
        httpd.serve_forever()


if __name__ == "__main__":
    main()
