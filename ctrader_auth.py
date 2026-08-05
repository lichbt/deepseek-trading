#!/usr/bin/env python3
"""
One-time OAuth flow for cTrader Open API.

Run this once to authorize the app and save access/refresh tokens locally.
After this, the trading bot uses the refresh token automatically.

Usage:
    1. Set env vars: CTRADER_CLIENT_ID, CTRADER_CLIENT_SECRET
    2. Run: python ctrader_auth.py
    3. Open the printed URL in your browser, log in, grant access
    4. You'll be redirected to localhost:5000/callback — tokens are saved
    5. Done. The bot reads tokens from .ctrader_tokens.json automatically.
"""

import json
import os
import socket
import sys
import time
import urllib.parse
from http.server import HTTPServer, BaseHTTPRequestHandler
from threading import Thread
from pathlib import Path

import requests


class _V6Server(HTTPServer):
    """IPv6 twin of HTTPServer — see the dual-stack note in main()."""
    address_family = socket.AF_INET6

CLIENT_ID = os.environ.get('CTRADER_CLIENT_ID', '')
CLIENT_SECRET = os.environ.get('CTRADER_CLIENT_SECRET', '')
REDIRECT_URI = 'http://localhost:5000/callback'
TOKEN_FILE = os.path.join(os.path.dirname(__file__), '.ctrader_tokens.json')

AUTH_URL = 'https://openapi.ctrader.com/apps/auth'
TOKEN_URL = 'https://openapi.ctrader.com/apps/token'


class CallbackHandler(BaseHTTPRequestHandler):
    """Catches the OAuth redirect and extracts the authorization code."""

    auth_code = None

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)

        if 'code' in params:
            CallbackHandler.auth_code = params['code'][0]
            self.send_response(200)
            self.send_header('Content-Type', 'text/html')
            self.end_headers()
            self.wfile.write(
                b'<h2>Authorization successful!</h2>'
                b'<p>You can close this tab and return to the terminal.</p>')
        else:
            self.send_response(400)
            self.send_header('Content-Type', 'text/html')
            self.end_headers()
            error = params.get('error', ['unknown'])[0]
            self.wfile.write(f'<h2>Error: {error}</h2>'.encode())

    def log_message(self, format, *args):
        pass  # suppress access logs


def exchange_code_for_tokens(code: str) -> dict:
    """Exchange authorization code for access + refresh tokens."""
    payload = {
        'grant_type': 'authorization_code',
        'code': code,
        'client_id': CLIENT_ID,
        'client_secret': CLIENT_SECRET,
        'redirect_uri': REDIRECT_URI,
    }
    r = requests.post(TOKEN_URL, data=payload, timeout=15)
    r.raise_for_status()
    data = r.json()
    return {
        'access_token': data['access_token'],
        'refresh_token': data['refresh_token'],
        'expires_at': time.time() + data.get('expires_in', 2592000),
        'created_at': time.time(),
    }


def main():
    if not CLIENT_ID or not CLIENT_SECRET:
        print("Error: Set CTRADER_CLIENT_ID and CTRADER_CLIENT_SECRET env vars first.")
        print("  export CTRADER_CLIENT_ID='your_client_id'")
        print("  export CTRADER_CLIENT_SECRET='your_client_secret'")
        sys.exit(1)

    # Build authorization URL
    auth_params = urllib.parse.urlencode({
        'client_id': CLIENT_ID,
        'redirect_uri': REDIRECT_URI,
        'response_type': 'code',
        'scope': 'trading',
    })
    auth_link = f'{AUTH_URL}?{auth_params}'

    print("\n" + "=" * 60)
    print("cTrader OAuth Authorization")
    print("=" * 60)
    print(f"\n1. Open this URL in your browser:\n\n   {auth_link}\n")
    print("2. Log in with your cTrader ID (cTID)")
    print("3. Check your trading account(s) and click 'Allow'")
    print("4. You'll be redirected back here automatically\n")
    print("Waiting for callback on http://localhost:5000 ...")

    # Start local servers to catch the redirect — ON BOTH STACKS.
    #
    # `localhost` resolves to BOTH 127.0.0.1 and ::1, and browsers generally try
    # ::1 first. HTTPServer(('localhost', 5000)) binds AF_INET only, so on this Mac
    # — where ControlCenter/AirPlay holds the *:5000 wildcard — the browser's IPv6
    # attempt reaches AirPlay instead of this process and the callback NEVER
    # arrives. It does not error; it just hangs forever waiting.
    #
    # Binding a specific address still succeeds while AirPlay holds the wildcard
    # (verified), so listen on both and take whichever the browser picks.
    servers = []
    for cls, addr in ((HTTPServer, ('127.0.0.1', 5000)),
                      (_V6Server, ('::1', 5000))):
        try:
            srv = cls(addr, CallbackHandler)
            srv.timeout = 1
            servers.append(srv)
        except OSError as exc:
            print(f"  note: could not bind {addr[0]}:{addr[1]} ({exc})")

    if not servers:
        print("\nERROR: nothing could bind port 5000. Disable AirPlay Receiver "
              "(System Settings > General > AirDrop & Handoff) and retry.", file=sys.stderr)
        sys.exit(1)
    print(f"  listening on: {', '.join(s.server_address[0] for s in servers)}")

    def _serve(srv):
        while CallbackHandler.auth_code is None:
            srv.handle_request()          # returns after srv.timeout with no request

    for srv in servers:
        Thread(target=_serve, args=(srv,), daemon=True).start()

    deadline = time.time() + 300
    while CallbackHandler.auth_code is None and time.time() < deadline:
        time.sleep(1)
    if CallbackHandler.auth_code is None:
        print("\nERROR: no callback within 5 minutes.", file=sys.stderr)
        sys.exit(1)

    auth_code = CallbackHandler.auth_code
    print(f"\nReceived authorization code: {auth_code[:10]}...")

    # Exchange code for tokens
    print("Exchanging code for tokens...")
    tokens = exchange_code_for_tokens(auth_code)

    # Save tokens
    with open(TOKEN_FILE, 'w') as f:
        json.dump(tokens, f, indent=2)

    print(f"\nTokens saved to {TOKEN_FILE}")
    print(f"  Access token expires: {time.ctime(tokens['expires_at'])}")
    print(f"  Refresh token: never expires (auto-refreshed by the bot)")
    print("\nDone! You can now run the trading bot with BROKER=ctrader.")


if __name__ == '__main__':
    main()
