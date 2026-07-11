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
import sys
import time
import urllib.parse
from http.server import HTTPServer, BaseHTTPRequestHandler
from threading import Thread
from pathlib import Path

import requests

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

    # Start local server to catch the redirect
    server = HTTPServer(('localhost', 5000), CallbackHandler)
    server.timeout = 300  # 5 minute timeout

    while CallbackHandler.auth_code is None:
        server.handle_request()
        if CallbackHandler.auth_code is None:
            print("  (still waiting for browser callback...)")

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
