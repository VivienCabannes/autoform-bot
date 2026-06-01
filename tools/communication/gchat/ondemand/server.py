#!/usr/bin/env python3
"""
Fort GChat Bridge Server

Lightweight HTTP server on the OnDemand that wraps phps FortGChatScript.
Binds to localhost only — access it from your dev machine via SSH tunnel.

Setup (on the OnDemand):
    export FORT_GCHAT_SECRET=$(openssl rand -hex 32)
    echo $FORT_GCHAT_SECRET  # save this for your dev machine
    python3 server.py --port 8765

SSH tunnel (from your dev machine, one-time Duo/YubiKey):
    ssh -N -L 8765:localhost:8765 100605.od.fbinfra.net

Then from your dev machine:
    curl -H "Authorization: Bearer <secret>" http://localhost:8765/health
    curl -H "Authorization: Bearer <secret>" http://localhost:8765/list_spaces
    curl -H "Authorization: Bearer <secret>" \
         "http://localhost:8765/list_messages?space=spaces/XXXX"
    curl -X POST -H "Authorization: Bearer <secret>" \
         -H "Content-Type: application/json" \
         -d '{"space": "spaces/XXXX", "message": "Hello from Fort"}' \
         http://localhost:8765/send_message
"""

import argparse
import json
import os
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

WWW_DIR = "/data/sandcastle/boxes/fbsource/www"
SCRIPT_NAME = "FortGChatScript"
VALID_ACTIONS = frozenset({"list_spaces", "list_messages", "send_message", "get_message"})

SECRET = ""


def get_secret() -> str:
    secret = os.environ.get("FORT_GCHAT_SECRET", "")
    if not secret:
        print(
            "ERROR: FORT_GCHAT_SECRET not set.\nRun: export FORT_GCHAT_SECRET=$(openssl rand -hex 32)",
            file=sys.stderr,
        )
        sys.exit(1)
    return secret


class GChatBridgeHandler(BaseHTTPRequestHandler):
    def _check_auth(self) -> bool:
        auth = self.headers.get("Authorization", "")
        if auth != f"Bearer {SECRET}":
            self._send_json(401, {"success": False, "error": "Unauthorized"})
            return False
        return True

    def _send_json(self, status: int, data: dict) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def _run_phps(self, action: str, params: dict) -> dict:
        cmd = ["phps", SCRIPT_NAME, action, "--json"]

        if params.get("space"):
            cmd.extend(["--space", params["space"]])
        if params.get("message"):
            cmd.extend(["--message", params["message"]])
        if params.get("message_name"):
            cmd.extend(["--message-name", params["message_name"]])
        if params.get("limit"):
            cmd.extend(["-n", str(params["limit"])])

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=60,
                cwd=WWW_DIR,
            )
            stdout = result.stdout.strip()
            if not stdout:
                return {"success": False, "error": result.stderr.strip() or "No output"}
            return json.loads(stdout)
        except subprocess.TimeoutExpired:
            return {"success": False, "error": "Command timed out (60s)"}
        except json.JSONDecodeError:
            return {"success": False, "error": f"Invalid JSON output: {stdout[:200]}"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def _handle_request(self, params: dict) -> None:
        if not self._check_auth():
            return

        action = urlparse(self.path).path.strip("/")

        if action == "health":
            self._send_json(200, {"success": True, "data": {"status": "ok"}})
            return

        if action not in VALID_ACTIONS:
            self._send_json(
                400,
                {
                    "success": False,
                    "error": f"Unknown action: {action}. Valid: {', '.join(sorted(VALID_ACTIONS))}",
                },
            )
            return

        result = self._run_phps(action, params)
        self._send_json(200 if result.get("success") else 500, result)

    def do_GET(self) -> None:
        params = {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}
        self._handle_request(params)

    def do_POST(self) -> None:
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length).decode() if content_length > 0 else "{}"
        try:
            params = json.loads(body)
        except json.JSONDecodeError:
            params = {k: v[0] for k, v in parse_qs(body).items()}
        self._handle_request(params)

    def log_message(self, fmt, *args):
        print(f"[fort-gchat] {args[0]}", file=sys.stderr)


def main():
    global SECRET

    parser = argparse.ArgumentParser(description="Fort GChat Bridge Server")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()

    SECRET = get_secret()

    server = HTTPServer(("127.0.0.1", args.port), GChatBridgeHandler)
    print(f"Fort GChat Bridge on 127.0.0.1:{args.port} (localhost only)", file=sys.stderr)
    print(f"SSH tunnel: ssh -N -L {args.port}:localhost:{args.port} {os.uname().nodename}", file=sys.stderr)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.", file=sys.stderr)
        server.server_close()


if __name__ == "__main__":
    main()
