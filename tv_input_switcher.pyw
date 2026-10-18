#!/usr/bin/env python3
"""
TV Input Switcher
-----------------
A small Tkinter GUI + system tray / menu bar icon that changes the input on a
Samsung SmartThings TV (e.g. Q90R / Q90T) using the SmartThings REST API
(https://api.smartthings.com). On Windows it also switches the inputs and
power of your monitors over DDC/CI, from the same tray menu.

Works on Windows, macOS and Linux (monitor control is Windows only), using
only the Python standard library (plus two optional packages for the tray).

Requirements
  * Python 3.10+ with Tkinter (included with the python.org installers)
  * A SmartThings sign-in, one of:
      - Personal access token (quickest): create one at
        https://account.smartthings.com/tokens with the Devices scopes.
        Tokens created after 30 Dec 2024 expire after 24 hours.
      - OAuth app (renews itself automatically): see "Account…" in the app
        and the README for the one-time setup.
  * Optional, for the tray / menu bar icon:  pip install pystray pillow
  * Monitors: Windows, with DDC/CI enabled in the monitor's own menu.

Usage
  Windows: double-click tv_input_switcher.pyw (runs with no console window),
           or:  pythonw tv_input_switcher.pyw
  macOS:   python3 tv_input_switcher.pyw

  The app starts hidden in the tray / menu bar. Right-click the icon and
  choose "Show window" to open it.

  Command line (Windows monitors; run with python, not pythonw, to see output):
    python tv_input_switcher.pyw --list                   list monitors, inputs, power
    python tv_input_switcher.pyw --set MONITOR INPUT      e.g. --set 2 hdmi1
    python tv_input_switcher.pyw --power MONITOR on|off   e.g. --power all off
  MONITOR is a number from --list, part of a monitor's name, or "all".

How the tray works
  The tray icon runs in its own small helper process. macOS requires both
  Tkinter and the menu bar icon to own the main thread of their process, so
  they can't share one; running them separately makes the app behave the same
  way on every platform. The two processes talk over a pair of queues.
"""

import argparse
import base64
import ctypes
import http.server
import json
import logging
import logging.handlers
import multiprocessing as mp
import os
import queue
import re
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
import tkinter as tk
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from tkinter import messagebox, simpledialog, ttk

try:
    import pystray
    from PIL import Image, ImageDraw
    HAVE_TRAY = True
    TRAY_IMPORT_ERROR = None
except ImportError as exc:
    HAVE_TRAY = False
    TRAY_IMPORT_ERROR = exc

IS_MAC = sys.platform == "darwin"
IS_WIN = sys.platform.startswith("win")

if IS_WIN:
    import winreg
    from ctypes import wintypes

__version__ = "0.3.0"

APP_NAME = "TV Input Switcher"
CONFIG_PATH = Path.home() / ".tv_input_switcher.json"
LOG_PATH = Path.home() / ".tv_input_switcher.log"
APP_DIR = Path(os.environ.get("APPDATA") or Path.home()) / "TVInputSwitcher"
MONITOR_CONFIG_PATH = APP_DIR / "monitors.json"
# Where Monitor Changer kept its config; copied over once if ours doesn't exist.
LEGACY_MONITOR_CONFIG_PATH = Path(os.environ.get("APPDATA") or Path.home()) / "MonitorChanger" / "config.json"

log = logging.getLogger("tv_input_switcher")

API = "https://api.smartthings.com/v1"
OAUTH_AUTHORIZE = "https://api.smartthings.com/oauth/authorize"
OAUTH_TOKEN = "https://api.smartthings.com/oauth/token"
OAUTH_SCOPES = "r:devices:* x:devices:*"
DEFAULT_REDIRECT = "https://httpbin.org/get"
TOKENS_PAGE = "https://account.smartthings.com/tokens"
HTTP_TIMEOUT = 20

# Local-network sign-in sync: plain JSON over TCP, no encryption or auth.
# Every instance listens on this port; only a "master" instance ever sends.
DEFAULT_SYNC_PORT = 53934

# Capabilities Samsung TVs use for input selection (standard one first).
INPUT_CAPS = ("mediaInputSource", "samsungvd.mediaInputSource")

FRIENDLY_NAMES = {
    "digitaltv": "TV", "dtv": "TV",
    "hdmi1": "HDMI 1", "hdmi2": "HDMI 2", "hdmi3": "HDMI 3", "hdmi4": "HDMI 4",
    "usb": "USB", "am": "AM", "fm": "FM",
}


class APIError(Exception):
    pass


class AuthError(APIError):
    """Missing, expired or rejected credentials."""


# ---------------------------------------------------------------- config
_config_lock = threading.Lock()


def load_config():
    try:
        cfg = json.loads(CONFIG_PATH.read_text())
    except Exception:
        cfg = {}
    cfg.setdefault("auth", {})
    cfg["auth"].setdefault("mode", "pat")
    cfg["auth"].setdefault("redirect_uri", DEFAULT_REDIRECT)
    cfg.setdefault("device_id", None)
    cfg.setdefault("master_mode", False)
    cfg.setdefault("peers", [])
    cfg.setdefault("sync_port", DEFAULT_SYNC_PORT)
    cfg.setdefault("default_switch", {})
    return cfg


def save_config(cfg):
    with _config_lock:
        try:
            CONFIG_PATH.write_text(json.dumps(cfg, indent=2))
            if not IS_WIN:
                os.chmod(CONFIG_PATH, 0o600)  # the file holds your tokens
        except OSError:
            pass


# ---------------------------------------------------------------- HTTP
def http_json(method, url, headers=None, body=None, form=None):
    headers = dict(headers or {})
    data = None
    if form is not None:
        data = urllib.parse.urlencode(form).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    elif body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    headers.setdefault("Accept", "application/json")
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
            raw = r.read().decode()
            return json.loads(raw) if raw.strip() else {}
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")
        try:
            j = json.loads(detail)
            err = j.get("error")
            detail = (err.get("message") if isinstance(err, dict) else None) \
                or j.get("error_description") or err or detail
        except Exception:
            pass
        if e.code in (401, 403):
            raise AuthError(f"SmartThings rejected the sign-in ({e.code}): {detail}")
        raise APIError(f"SmartThings API error {e.code}: {detail}")
    except urllib.error.URLError as e:
        raise APIError(f"Couldn't reach SmartThings: {e.reason}")


# ---------------------------------------------------------------- auth
class SmartThingsAuth:
    """Hands out a valid bearer token, refreshing OAuth tokens as needed.
    `on_change` is called (from any thread) when tokens are updated."""

    def __init__(self, auth_cfg, on_change):
        self.cfg = auth_cfg
        self.on_change = on_change
        self.lock = threading.Lock()

    def _basic(self):
        pair = f"{self.cfg.get('client_id', '')}:{self.cfg.get('client_secret', '')}"
        return "Basic " + base64.b64encode(pair.encode()).decode()

    def _store(self, tok):
        self.cfg["access_token"] = tok["access_token"]
        if tok.get("refresh_token"):
            self.cfg["refresh_token"] = tok["refresh_token"]
        self.cfg["expires_at"] = time.time() + float(tok.get("expires_in", 86400))
        self.on_change()

    def authorize_url(self, state):
        return OAUTH_AUTHORIZE + "?" + urllib.parse.urlencode({
            "client_id": self.cfg["client_id"],
            "response_type": "code",
            "redirect_uri": self.cfg["redirect_uri"],
            "scope": OAUTH_SCOPES,
            "state": state,
        })

    def exchange_code(self, code):
        tok = http_json("POST", OAUTH_TOKEN, headers={"Authorization": self._basic()}, form={
            "grant_type": "authorization_code",
            "code": code,
            "client_id": self.cfg["client_id"],
            "redirect_uri": self.cfg["redirect_uri"],
        })
        with self.lock:
            self._store(tok)

    def _refresh(self):
        if not self.cfg.get("refresh_token"):
            raise AuthError("Not signed in. Open Account… and sign in.")
        try:
            tok = http_json("POST", OAUTH_TOKEN, headers={"Authorization": self._basic()}, form={
                "grant_type": "refresh_token",
                "client_id": self.cfg["client_id"],
                "refresh_token": self.cfg["refresh_token"],
            })
        except AuthError:
            raise AuthError(
                "Your SmartThings sign-in has expired (the app wasn't used "
                "for about 30 days). Open Account… and sign in again."
            )
        self._store(tok)

    def token(self, min_valid=300):
        if self.cfg.get("mode") == "pat":
            pat = (self.cfg.get("pat") or "").strip()
            if not pat:
                raise AuthError("No access token set. Open Account… to add one.")
            return pat
        with self.lock:
            if not self.cfg.get("client_id"):
                raise AuthError("OAuth isn't set up. Open Account… to set it up.")
            if time.time() + min_valid >= float(self.cfg.get("expires_at", 0)):
                self._refresh()
            return self.cfg["access_token"]

    def keep_alive(self):
        """Refresh early so the 30-day refresh token keeps renewing while the
        app runs in the tray."""
        if self.cfg.get("mode") == "oauth" and self.cfg.get("refresh_token"):
            self.token(min_valid=4 * 3600)


# ---------------------------------------------------------------- API
class SmartThingsAPI:
    def __init__(self, auth):
        self.auth = auth

    def _call(self, method, path_or_url, body=None):
        url = path_or_url if path_or_url.startswith("http") else API + path_or_url
        try:
            return http_json(method, url, headers={
                "Authorization": f"Bearer {self.auth.token()}"}, body=body)
        except AuthError as e:
            if self.auth.cfg.get("mode") == "pat":
                raise AuthError(
                    f"{e}\n\nPersonal access tokens created after 30 Dec 2024 "
                    "only last 24 hours. Create a new one, or switch to OAuth "
                    "under Account… so the app renews its sign-in itself."
                )
            raise

    def list_tvs(self):
        """[(device_id, label)] for devices that support input switching."""
        tvs, url = [], "/devices"
        while url:
            page = self._call("GET", url)
            for d in page.get("items", []):
                caps = {c.get("id") for comp in d.get("components", [])
                        for c in comp.get("capabilities", [])}
                if caps & set(INPUT_CAPS):
                    tvs.append((d["deviceId"], d.get("label") or d.get("name") or d["deviceId"]))
            url = ((page.get("_links") or {}).get("next") or {}).get("href")
        return tvs

    def tv_state(self, device_id):
        status = self._call("GET", f"/devices/{device_id}/status")
        main = status.get("components", {}).get("main", {})
        power = main.get("switch", {}).get("switch", {}).get("value")
        for cap in INPUT_CAPS:
            data = main.get(cap)
            if not data:
                continue
            current = (data.get("inputSource") or {}).get("value")
            inputs = []
            if cap == "samsungvd.mediaInputSource":
                for item in (data.get("supportedInputSourcesMap") or {}).get("value") or []:
                    inputs.append((item.get("id"), item.get("name") or item.get("id")))
            else:
                for src in (data.get("supportedInputSources") or {}).get("value") or []:
                    inputs.append((src, FRIENDLY_NAMES.get(src.lower(), src)))
            if inputs:
                return {"power": power, "cap": cap, "current": current, "inputs": inputs}
        raise APIError("This TV didn't report any input sources. Make sure it's "
                       "on and try Refresh.")

    def command(self, device_id, capability, command, *args):
        self._call("POST", f"/devices/{device_id}/commands", body={"commands": [{
            "component": "main", "capability": capability,
            "command": command, "arguments": list(args),
        }]})


def extract_code(text):
    """Pull an OAuth code out of a pasted redirect URL, page text, or bare code."""
    text = text.strip()
    q = urllib.parse.parse_qs(urllib.parse.urlparse(text).query)
    if q.get("code"):
        return q["code"][0]
    m = re.search(r'"code"\s*:\s*"([^"]+)"', text) or re.search(r"[?&]code=([^&\s\"]+)", text)
    if m:
        return m.group(1)
    return text if re.fullmatch(r"[A-Za-z0-9_\-]+", text) else None


def capture_code_locally(redirect_uri, state, timeout=180):
    """If the redirect URI points at this computer, catch the code with a tiny
    local web server. Returns the code, or None if it didn't arrive."""
    u = urllib.parse.urlparse(redirect_uri)
    result = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if q.get("state", [None])[0] == state and q.get("code"):
                result["code"] = q["code"][0]
                msg = "Signed in. You can close this tab and return to TV Input Switcher."
            else:
                msg = "Sign-in failed or was cancelled."
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(f"<p style='font:16px sans-serif'>{msg}</p>".encode())

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer((u.hostname, u.port or 80), Handler)
    srv.timeout = 1
    end = time.time() + timeout
    while "code" not in result and time.time() < end:
        srv.handle_request()
    srv.server_close()
    return result.get("code")


# ---------------------------------------------------------------- local network sync
def local_ip():
    """This computer's LAN IP address, so it can be entered as a peer on
    other instances. Doesn't actually send anything: connecting a UDP socket
    just asks the OS which local address would be used for that route."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


class TokenSyncServer:
    """Listens on the local network for sign-in pushes from whichever instance
    has "master" mode on, and applies them here. Plain JSON over TCP, with no
    encryption and no authentication — this is meant for a trusted home LAN
    only, never the open internet."""

    def __init__(self, app):
        self.app = app
        self.listening = None  # None = still starting, then True/False
        self.error = None
        self.last_seen_from = None  # IP that most recently pushed us tokens
        self.last_seen_at = None  # time.time() of that push
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        port = self.app.cfg.get("sync_port", DEFAULT_SYNC_PORT)
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("0.0.0.0", port))
            sock.listen(5)
        except OSError as e:
            self.listening = False
            self.error = str(e)
            return
        self.listening = True
        while True:
            try:
                conn, addr = sock.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(conn, addr[0]), daemon=True).start()

    def _handle(self, conn, addr):
        try:
            with conn:
                conn.settimeout(5)
                data = b""
                while not data.endswith(b"\n") and len(data) < 65536:
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    data += chunk
                msg = json.loads(data.decode())
                if msg.get("type") == "ping":
                    reply = {"type": "pong", "master_mode": bool(self.app.cfg.get("master_mode"))}
                    conn.sendall((json.dumps(reply) + "\n").encode())
                    return
                if msg.get("type") == "request_push":
                    # Only reply if we're the master: a non-master's tokens
                    # may be stale, and could overwrite a fresher sign-in.
                    if self.app.cfg.get("master_mode"):
                        reply = {"type": "tokens", "auth": dict(self.app.cfg["auth"])}
                        conn.sendall((json.dumps(reply) + "\n").encode())
                    return
        except Exception:
            return
        if msg.get("type") == "tokens" and isinstance(msg.get("auth"), dict):
            self.last_seen_from = addr
            self.last_seen_at = time.time()
            self.app.after(0, lambda: self.app.apply_synced_auth(msg["auth"]))


def check_peer(peer, default_port, timeout=1.5):
    """Try to reach one peer's sync server with a ping, for the Network
    window's "Check network" button. Returns a small status dict."""
    host, _, p = peer.strip().partition(":")
    port = int(p) if p.isdigit() else default_port
    try:
        with socket.create_connection((host, port), timeout=timeout) as s:
            s.sendall(b'{"type": "ping"}\n')
            s.settimeout(timeout)
            data = b""
            while not data.endswith(b"\n") and len(data) < 4096:
                chunk = s.recv(4096)
                if not chunk:
                    break
                data += chunk
        reply = json.loads(data.decode()) if data else {}
        return {"peer": peer, "online": True, "master": bool(reply.get("master_mode"))}
    except Exception:
        return {"peer": peer, "online": False, "master": False}


def push_tokens_to_peers(cfg):
    """Send the current sign-in to every configured peer, in a background
    thread. Plain TCP, no encryption: local network only, by design."""
    peers = list(cfg.get("peers", []))
    if not peers:
        return
    payload = (json.dumps({"type": "tokens", "auth": dict(cfg["auth"])}) + "\n").encode()
    port_default = cfg.get("sync_port", DEFAULT_SYNC_PORT)

    def work():
        for peer in peers:
            host, _, p = peer.strip().partition(":")
            if not host:
                continue
            port = int(p) if p.isdigit() else port_default
            try:
                with socket.create_connection((host, port), timeout=3) as s:
                    s.sendall(payload)
            except OSError:
                pass

    threading.Thread(target=work, daemon=True).start()


def request_push_from_peers(cfg, default_port, timeout=2):
    """Ask every configured peer to push its sign-in back to us right now,
    for the Network window's "Request now" button. Only a peer with master
    mode on will actually reply. Returns {peer: auth_dict} for every peer
    that did."""
    got = {}
    for peer in list(cfg.get("peers", [])):
        host, _, p = peer.strip().partition(":")
        if not host:
            continue
        port = int(p) if p.isdigit() else default_port
        try:
            with socket.create_connection((host, port), timeout=timeout) as s:
                s.sendall(b'{"type": "request_push"}\n')
                s.settimeout(timeout)
                data = b""
                while not data.endswith(b"\n") and len(data) < 65536:
                    chunk = s.recv(4096)
                    if not chunk:
                        break
                    data += chunk
            if data:
                reply = json.loads(data.decode())
                if reply.get("type") == "tokens" and isinstance(reply.get("auth"), dict):
                    got[peer] = reply["auth"]
        except Exception:
            pass
    return got


# ---------------------------------------------------------------- monitors: MCCS
# Monitors are switched over DDC/CI (Windows only). This part is plain Python:
# the capabilities-string parser, input names and power modes.

#: VCP code for "Input Select" (a.k.a. input source).
VCP_INPUT_SOURCE = 0x60

#: Input source values from the MCCS 2.2a spec, plus common vendor extensions.
INPUT_NAMES = {
    0x01: "VGA 1",
    0x02: "VGA 2",
    0x03: "DVI 1",
    0x04: "DVI 2",
    0x05: "Composite 1",
    0x06: "Composite 2",
    0x07: "S-Video 1",
    0x08: "S-Video 2",
    0x09: "Tuner 1",
    0x0A: "Tuner 2",
    0x0B: "Tuner 3",
    0x0C: "Component 1",
    0x0D: "Component 2",
    0x0E: "Component 3",
    0x0F: "DisplayPort 1",
    0x10: "DisplayPort 2",
    0x11: "HDMI 1",
    0x12: "HDMI 2",
    # Not in the spec, but widely used (Dell, HP, Lenovo, ...) for USB-C / Thunderbolt.
    0x1B: "USB-C",
}

#: Shown when a monitor answers DDC/CI but doesn't list its inputs in its capabilities.
FALLBACK_INPUTS = (0x0F, 0x10, 0x11, 0x12, 0x1B, 0x03, 0x01)

#: VCP code for "Power Mode", and its "on" value.
VCP_POWER_MODE = 0xD6
POWER_ON = 0x01
#: "Off" values in order of preference. DPM off (0x04), suspend and standby keep
#: the monitor listening for DDC/CI so it can be turned back on; 0x05 is like
#: pressing the power button and usually can't be undone remotely.
POWER_OFF_PREFERENCE = (0x04, 0x03, 0x02, 0x05)

# A VCP code, optionally followed by its value list (tolerating a truncated string).
_HEX_CODE = re.compile(r"([0-9A-Fa-f]{2})\s*(?:\(([^()]*)(?:\)|$))?")
_HEX_BYTE = re.compile(r"[0-9A-Fa-f]{2}")


def input_name(code):
    """Human readable name for an input source value."""
    return INPUT_NAMES.get(code, f"Input 0x{code:02X}")


def _squash(text):
    return re.sub(r"[\s_-]", "", text).casefold()


_INPUT_ALIASES = {_squash(name): code for code, name in INPUT_NAMES.items()}
_INPUT_ALIASES.update({f"dp{n}": 0x0E + n for n in (1, 2)})
_INPUT_ALIASES.update({"usbc": 0x1B, "typec": 0x1B, "thunderbolt": 0x1B})


def parse_input(text):
    """Parse an input given as a code ("0x11", "17") or a name ("HDMI 1", "hdmi1", "dp2")."""
    try:
        code = int(text, 0)
    except ValueError:
        code = _INPUT_ALIASES.get(_squash(text))
    if code is None or not 0 <= code <= 0xFF:
        raise ValueError(f"unknown input {text!r}")
    return code


def _top_level_groups(text):
    """Yield ``(key, body)`` for each ``key(body)`` group at the top level of *text*."""
    i, n = 0, len(text)
    while i < n:
        open_at = text.find("(", i)
        if open_at < 0:
            return
        key = text[i:open_at].strip()
        depth = 0
        for close_at in range(open_at, n):
            if text[close_at] == "(":
                depth += 1
            elif text[close_at] == ")":
                depth -= 1
                if depth == 0:
                    break
        else:
            # Unbalanced: treat the rest of the string as the body.
            close_at = n
        # Stray ')' characters from sloppy firmware end up in the key; drop them.
        yield key.replace(")", "").strip().lower(), text[open_at + 1:close_at]
        i = close_at + 1


def parse_capabilities(caps):
    """Parse a DDC/CI capabilities string.

    Returns a dict of the top-level groups (``model``, ``type``, ``mccs_ver``, ...)
    with the ``vcp`` group replaced by a dict mapping each VCP code to the list of
    values it supports (empty when the monitor didn't enumerate them).
    """
    caps = (caps or "").replace("\x00", "").strip()
    # The whole string is normally wrapped in one pair of parentheses.
    groups = list(_top_level_groups(caps))
    if groups and groups[0][0] == "":
        groups = list(_top_level_groups(groups[0][1])) + groups[1:]

    result = {}
    for key, body in groups:
        if key == "vcp":
            vcp = {}
            for match in _HEX_CODE.finditer(body):
                code = int(match.group(1), 16)
                values = match.group(2)
                vcp[code] = [int(v, 16) for v in _HEX_BYTE.findall(values)] if values else []
            result["vcp"] = vcp
        elif key:
            result[key] = body.strip()
    return result


def supported_inputs(caps):
    """Input source values a monitor advertises in its capabilities string."""
    return parse_capabilities(caps).get("vcp", {}).get(VCP_INPUT_SOURCE, [])


def best_power_off_value(values):
    """The best "off" value among the power modes a monitor lists, e.g. ``D6(01 04 05)``."""
    for value in POWER_OFF_PREFERENCE:
        if value in values:
            return value
    return POWER_OFF_PREFERENCE[0]


# ---------------------------------------------------------------- monitors: Windows API
# Everything that touches dxva2/user32/kernel32 lives behind IS_WIN so the rest
# of the file still imports on macOS and Linux.

if IS_WIN:
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    dxva2 = ctypes.WinDLL("dxva2", use_last_error=True)

    UINT32 = ctypes.c_uint32
    MONITORINFOF_PRIMARY = 0x1
    QDC_ONLY_ACTIVE_PATHS = 0x2
    DISPLAYCONFIG_DEVICE_INFO_GET_SOURCE_NAME = 1
    DISPLAYCONFIG_DEVICE_INFO_GET_TARGET_NAME = 2
    ERROR_SUCCESS = 0
    ERROR_INSUFFICIENT_BUFFER = 122
    ERROR_ALREADY_EXISTS = 183
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    SW_HIDE = 0
    WM_APP_REFRESH_MENU = 0x8000 + 1  # WM_APP + 1

    class PHYSICAL_MONITOR(ctypes.Structure):
        _fields_ = [
            ("hPhysicalMonitor", wintypes.HANDLE),
            ("szPhysicalMonitorDescription", wintypes.WCHAR * 128),
        ]

    class MONITORINFOEXW(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("rcMonitor", wintypes.RECT),
            ("rcWork", wintypes.RECT),
            ("dwFlags", wintypes.DWORD),
            ("szDevice", wintypes.WCHAR * 32),
        ]

    class LUID(ctypes.Structure):
        _fields_ = [("LowPart", wintypes.DWORD), ("HighPart", wintypes.LONG)]

    class DISPLAYCONFIG_RATIONAL(ctypes.Structure):
        _fields_ = [("Numerator", UINT32), ("Denominator", UINT32)]

    class DISPLAYCONFIG_PATH_SOURCE_INFO(ctypes.Structure):
        _fields_ = [
            ("adapterId", LUID),
            ("id", UINT32),
            ("modeInfoIdx", UINT32),
            ("statusFlags", UINT32),
        ]

    class DISPLAYCONFIG_PATH_TARGET_INFO(ctypes.Structure):
        _fields_ = [
            ("adapterId", LUID),
            ("id", UINT32),
            ("modeInfoIdx", UINT32),
            ("outputTechnology", UINT32),
            ("rotation", UINT32),
            ("scaling", UINT32),
            ("refreshRate", DISPLAYCONFIG_RATIONAL),
            ("scanLineOrdering", UINT32),
            ("targetAvailable", wintypes.BOOL),
            ("statusFlags", UINT32),
        ]

    class DISPLAYCONFIG_PATH_INFO(ctypes.Structure):
        _fields_ = [
            ("sourceInfo", DISPLAYCONFIG_PATH_SOURCE_INFO),
            ("targetInfo", DISPLAYCONFIG_PATH_TARGET_INFO),
            ("flags", UINT32),
        ]

    class DISPLAYCONFIG_MODE_INFO(ctypes.Structure):
        # We never read the mode union, so it's kept opaque (48 bytes, 8-byte aligned).
        _fields_ = [
            ("infoType", UINT32),
            ("id", UINT32),
            ("adapterId", LUID),
            ("_mode", ctypes.c_uint64 * 6),
        ]

    class DISPLAYCONFIG_DEVICE_INFO_HEADER(ctypes.Structure):
        _fields_ = [
            ("type", UINT32),
            ("size", UINT32),
            ("adapterId", LUID),
            ("id", UINT32),
        ]

    class DISPLAYCONFIG_SOURCE_DEVICE_NAME(ctypes.Structure):
        _fields_ = [
            ("header", DISPLAYCONFIG_DEVICE_INFO_HEADER),
            ("viewGdiDeviceName", wintypes.WCHAR * 32),
        ]

    class DISPLAYCONFIG_TARGET_DEVICE_NAME(ctypes.Structure):
        _fields_ = [
            ("header", DISPLAYCONFIG_DEVICE_INFO_HEADER),
            ("flags", UINT32),
            ("outputTechnology", UINT32),
            ("edidManufactureId", ctypes.c_uint16),
            ("edidProductCodeId", ctypes.c_uint16),
            ("connectorInstance", UINT32),
            ("monitorFriendlyDeviceName", wintypes.WCHAR * 64),
            ("monitorDevicePath", wintypes.WCHAR * 128),
        ]

    MONITORENUMPROC = ctypes.WINFUNCTYPE(
        wintypes.BOOL, wintypes.HMONITOR, wintypes.HDC, ctypes.POINTER(wintypes.RECT), wintypes.LPARAM
    )

    def _bind(dll, name, restype, *argtypes):
        func = getattr(dll, name)
        func.restype = restype
        func.argtypes = argtypes
        return func

    _EnumDisplayMonitors = _bind(
        user32, "EnumDisplayMonitors", wintypes.BOOL,
        wintypes.HDC, ctypes.POINTER(wintypes.RECT), MONITORENUMPROC, wintypes.LPARAM,
    )
    _GetMonitorInfoW = _bind(
        user32, "GetMonitorInfoW", wintypes.BOOL, wintypes.HMONITOR, ctypes.POINTER(MONITORINFOEXW)
    )
    _GetDisplayConfigBufferSizes = _bind(
        user32, "GetDisplayConfigBufferSizes", wintypes.LONG,
        UINT32, ctypes.POINTER(UINT32), ctypes.POINTER(UINT32),
    )
    _QueryDisplayConfig = _bind(
        user32, "QueryDisplayConfig", wintypes.LONG,
        UINT32, ctypes.POINTER(UINT32), ctypes.POINTER(DISPLAYCONFIG_PATH_INFO),
        ctypes.POINTER(UINT32), ctypes.POINTER(DISPLAYCONFIG_MODE_INFO), ctypes.c_void_p,
    )
    _DisplayConfigGetDeviceInfo = _bind(
        user32, "DisplayConfigGetDeviceInfo", wintypes.LONG, ctypes.POINTER(DISPLAYCONFIG_DEVICE_INFO_HEADER)
    )
    _PostMessageW = _bind(
        user32, "PostMessageW", wintypes.BOOL, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
    )
    _ShowWindow = _bind(user32, "ShowWindow", wintypes.BOOL, wintypes.HWND, ctypes.c_int)
    _GetConsoleWindow = _bind(kernel32, "GetConsoleWindow", wintypes.HWND)
    _GetConsoleProcessList = _bind(
        kernel32, "GetConsoleProcessList", wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), wintypes.DWORD
    )
    _OpenProcess = _bind(kernel32, "OpenProcess", wintypes.HANDLE, wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    _QueryFullProcessImageNameW = _bind(
        kernel32, "QueryFullProcessImageNameW", wintypes.BOOL,
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD),
    )
    _CloseHandle = _bind(kernel32, "CloseHandle", wintypes.BOOL, wintypes.HANDLE)
    _CreateMutexW = _bind(
        kernel32, "CreateMutexW", wintypes.HANDLE, ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR
    )
    _GetNumberOfPhysicalMonitorsFromHMONITOR = _bind(
        dxva2, "GetNumberOfPhysicalMonitorsFromHMONITOR", wintypes.BOOL,
        wintypes.HMONITOR, ctypes.POINTER(wintypes.DWORD),
    )
    _GetPhysicalMonitorsFromHMONITOR = _bind(
        dxva2, "GetPhysicalMonitorsFromHMONITOR", wintypes.BOOL,
        wintypes.HMONITOR, wintypes.DWORD, ctypes.POINTER(PHYSICAL_MONITOR),
    )
    _DestroyPhysicalMonitor = _bind(dxva2, "DestroyPhysicalMonitor", wintypes.BOOL, wintypes.HANDLE)
    _GetCapabilitiesStringLength = _bind(
        dxva2, "GetCapabilitiesStringLength", wintypes.BOOL, wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)
    )
    _CapabilitiesRequestAndCapabilitiesReply = _bind(
        dxva2, "CapabilitiesRequestAndCapabilitiesReply", wintypes.BOOL,
        wintypes.HANDLE, ctypes.c_char_p, wintypes.DWORD,
    )
    _GetVCPFeatureAndVCPFeatureReply = _bind(
        dxva2, "GetVCPFeatureAndVCPFeatureReply", wintypes.BOOL,
        wintypes.HANDLE, ctypes.c_ubyte, ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD),
    )
    _SetVCPFeature = _bind(dxva2, "SetVCPFeature", wintypes.BOOL, wintypes.HANDLE, ctypes.c_ubyte, wintypes.DWORD)

    def _check(ok):
        if not ok:
            raise ctypes.WinError(ctypes.get_last_error())

    # Every call that talks to a monitor goes over the I2C bus in the video
    # cable and can take tens of milliseconds (capabilities strings take
    # seconds), so the tray keeps these off the UI thread and runs them one at
    # a time.

    def get_vcp(handle, code):
        """Return ``(current, maximum)`` for a VCP feature."""
        current, maximum = wintypes.DWORD(), wintypes.DWORD()
        _check(_GetVCPFeatureAndVCPFeatureReply(handle, code, None, ctypes.byref(current), ctypes.byref(maximum)))
        return current.value, maximum.value

    def set_vcp(handle, code, value):
        _check(_SetVCPFeature(handle, code, value))

    def get_capabilities(handle):
        length = wintypes.DWORD()
        _check(_GetCapabilitiesStringLength(handle, ctypes.byref(length)))
        buffer = ctypes.create_string_buffer(length.value)
        _check(_CapabilitiesRequestAndCapabilitiesReply(handle, buffer, length))
        return buffer.value.decode("ascii", errors="replace")

    def _display_monitors():
        handles = []

        @MONITORENUMPROC
        def callback(hmonitor, hdc, rect, lparam):
            handles.append(hmonitor)
            return True

        _check(_EnumDisplayMonitors(None, None, callback, 0))
        return handles

    def _monitor_info(hmonitor):
        info = MONITORINFOEXW()
        info.cbSize = ctypes.sizeof(info)
        _check(_GetMonitorInfoW(hmonitor, ctypes.byref(info)))
        return info

    def _physical_monitors(hmonitor):
        count = wintypes.DWORD()
        _check(_GetNumberOfPhysicalMonitorsFromHMONITOR(hmonitor, ctypes.byref(count)))
        if not count.value:
            return []
        monitors = (PHYSICAL_MONITOR * count.value)()
        _check(_GetPhysicalMonitorsFromHMONITOR(hmonitor, count.value, monitors))
        return list(monitors)

    def _friendly_names():
        """Map GDI device names (\\\\.\\DISPLAY1) to the EDID names of the monitors showing them."""
        names = {}
        num_paths, num_modes = UINT32(), UINT32()
        for _ in range(5):  # the topology can change between the two calls
            if _GetDisplayConfigBufferSizes(QDC_ONLY_ACTIVE_PATHS, ctypes.byref(num_paths), ctypes.byref(num_modes)):
                return names
            paths = (DISPLAYCONFIG_PATH_INFO * num_paths.value)()
            modes = (DISPLAYCONFIG_MODE_INFO * num_modes.value)()
            rc = _QueryDisplayConfig(
                QDC_ONLY_ACTIVE_PATHS, ctypes.byref(num_paths), paths, ctypes.byref(num_modes), modes, None
            )
            if rc == ERROR_SUCCESS:
                break
            if rc != ERROR_INSUFFICIENT_BUFFER:
                return names
        else:
            return names

        for path in paths[: num_paths.value]:
            source = DISPLAYCONFIG_SOURCE_DEVICE_NAME()
            source.header.type = DISPLAYCONFIG_DEVICE_INFO_GET_SOURCE_NAME
            source.header.size = ctypes.sizeof(source)
            source.header.adapterId = path.sourceInfo.adapterId
            source.header.id = path.sourceInfo.id
            target = DISPLAYCONFIG_TARGET_DEVICE_NAME()
            target.header.type = DISPLAYCONFIG_DEVICE_INFO_GET_TARGET_NAME
            target.header.size = ctypes.sizeof(target)
            target.header.adapterId = path.targetInfo.adapterId
            target.header.id = path.targetInfo.id
            if _DisplayConfigGetDeviceInfo(ctypes.byref(source.header)) == ERROR_SUCCESS and \
                    _DisplayConfigGetDeviceInfo(ctypes.byref(target.header)) == ERROR_SUCCESS:
                names.setdefault(source.viewGdiDeviceName, []).append(target.monitorFriendlyDeviceName)
        return names

    def enumerate_monitors():
        """List the physical monitors attached to the desktop, ordered left to right.

        This is fast; call :meth:`Monitor.probe` on each to talk DDC/CI to it.
        Call :meth:`Monitor.close` on each when done.
        """
        try:
            friendly = _friendly_names()
        except OSError as exc:
            log.warning("Could not read monitor names: %s", exc)
            friendly = {}

        monitors = []
        for hmonitor in _display_monitors():
            info = _monitor_info(hmonitor)
            common = dict(
                device=info.szDevice,
                position=(info.rcMonitor.left, info.rcMonitor.top),
                primary=bool(info.dwFlags & MONITORINFOF_PRIMARY),
            )
            edid_names = friendly.get(info.szDevice, [])
            try:
                physical = _physical_monitors(hmonitor)
            except OSError as exc:
                log.warning("%s: no physical monitors: %s", info.szDevice, exc)
                physical = []
            if not physical:
                monitors.append(Monitor(
                    name=edid_names[0] if edid_names else info.szDevice,
                    handle=None,
                    error="Windows exposes no physical monitor for this display",
                    **common,
                ))
            for i, pm in enumerate(physical):
                name = edid_names[i] if i < len(edid_names) and edid_names[i] else pm.szPhysicalMonitorDescription
                # Physical monitor handles are small integers and 0 is valid, but
                # ctypes reads a NULL HANDLE as None.
                monitors.append(Monitor(name=name, handle=pm.hPhysicalMonitor or 0, **common))

        monitors.sort(key=lambda m: m.position)
        for index, monitor in enumerate(monitors, start=1):
            monitor.index = index
        return monitors

    def _process_name(pid):
        handle = _OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return None
        try:
            size = wintypes.DWORD(260)
            buffer = ctypes.create_unicode_buffer(size.value)
            if not _QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
                return None
            return Path(buffer.value).name
        finally:
            _CloseHandle(handle)

    def _hide_own_console():
        """Hide the console window if it only exists for us, e.g. after double-clicking
        a .py file. (Closing that window would also close the app.) A console shared
        with a shell means we were started from a terminal, so leave it alone.
        Returns True if the window was hidden.
        """
        hwnd = _GetConsoleWindow()
        if not hwnd:
            return False  # pythonw: no console
        pids = (wintypes.DWORD * 16)()
        count = _GetConsoleProcessList(pids, len(pids))
        if not 0 < count <= len(pids):
            return False
        for pid in pids[:count]:
            name = _process_name(pid)
            # Ours: python.exe, pythonw.exe, py.exe, and other Python launchers.
            if not name or not name.lower().startswith("py"):
                return False
        _ShowWindow(hwnd, SW_HIDE)
        return True

    def already_running():
        """Single instance: the mutex lives until the process exits."""
        ctypes.set_last_error(0)
        _CreateMutexW(None, False, "Local\\TVInputSwitcher")
        return ctypes.get_last_error() == ERROR_ALREADY_EXISTS

else:
    def _hide_own_console():
        return False

    def already_running():
        return False


# ---------------------------------------------------------------- monitors: DDC/CI
def _retry(func, attempts=3, delay=0.15):
    """DDC/CI is a flaky bus; most transient failures go away on a retry."""
    for attempt in range(attempts):
        try:
            return func()
        except OSError:
            if attempt == attempts - 1:
                raise
            time.sleep(delay)


@dataclass(eq=False)
class Monitor:
    """A physical monitor and what we know about it over DDC/CI."""

    name: str
    device: str  # GDI device name, e.g. \\.\DISPLAY1
    position: tuple  # top-left corner on the virtual desktop
    primary: bool
    handle: int | None  # physical monitor handle; None when there isn't one
    index: int = 0  # 1-based, ordered left to right
    capabilities: str = ""
    inputs: list = field(default_factory=list)  # input source values the monitor supports
    current_input: int | None = None
    power_off_value: int | None = None  # power mode value for "off"; None if power control isn't offered
    power_on: bool | None = None  # None when unknown
    error: str | None = None

    @property
    def supported(self):
        return self.handle is not None and self.error is None

    def probe(self):
        """Query DDC/CI for the current and supported inputs, and power control.

        Slow (capabilities requests regularly take 1-2 seconds per monitor).
        Failures are recorded on ``error`` rather than raised.
        """
        if self.handle is None:
            self.error = self.error or "no physical monitor handle"
            return
        try:
            self.current_input = self.read_input()
        except OSError as exc:
            self.error = f"DDC/CI not available: {exc.strerror or exc}"
            log.info("%s: %s", self.name, self.error)
            return

        try:
            self.capabilities = _retry(lambda: get_capabilities(self.handle))
        except OSError as exc:
            log.warning("%s: could not read capabilities: %s", self.name, exc)

        parsed = parse_capabilities(self.capabilities)
        if parsed.get("model") and (not self.name or self.name.startswith("Generic ")):
            self.name = parsed["model"]
        self.inputs = list(dict.fromkeys(parsed.get("vcp", {}).get(VCP_INPUT_SOURCE, [])))
        if not self.inputs:
            log.info("%s: capabilities don't list inputs, using common defaults", self.name)
            self.inputs = list(FALLBACK_INPUTS)
        if self.current_input is not None and self.current_input not in self.inputs:
            self.inputs.append(self.current_input)

        vcp = parsed.get("vcp")
        # Offer power control when it's advertised, or when we can't tell.
        if not vcp or VCP_POWER_MODE in vcp:
            self.power_off_value = best_power_off_value((vcp or {}).get(VCP_POWER_MODE, []))
            try:
                self.power_on = self.read_power()
            except OSError as exc:
                log.info("%s: could not read power mode: %s", self.name, exc)

        log.info(
            "%s: inputs=%s current=%s power_on=%s power_off_value=%s",
            self.name,
            [input_name(c) for c in self.inputs],
            input_name(self.current_input) if self.current_input is not None else None,
            self.power_on,
            f"0x{self.power_off_value:02X}" if self.power_off_value is not None else None,
        )

    def read_input(self):
        current, _ = _retry(lambda: get_vcp(self.handle, VCP_INPUT_SOURCE))
        # Some monitors put junk in the high byte; the input value is the low byte.
        return current & 0xFF

    def set_input(self, code):
        _retry(lambda: set_vcp(self.handle, VCP_INPUT_SOURCE, code))
        self.current_input = code

    def read_power(self):
        current, _ = _retry(lambda: get_vcp(self.handle, VCP_POWER_MODE))
        return (current & 0xFF) == POWER_ON

    def set_power(self, on, off_value=None):
        """Turn the monitor on, or off with *off_value* (default: the detected best value)."""
        if on:
            value = POWER_ON
        else:
            value = off_value or self.power_off_value or POWER_OFF_PREFERENCE[0]
        # A monitor that's off can be slow to answer, so try harder when waking it.
        _retry(
            lambda: set_vcp(self.handle, VCP_POWER_MODE, value),
            attempts=5 if on else 3,
            delay=0.3 if on else 0.15,
        )
        self.power_on = on

    def close(self):
        if self.handle is not None:
            _DestroyPhysicalMonitor(self.handle)
            self.handle = None


def close_all(monitors):
    for monitor in monitors:
        monitor.close()


# ---------------------------------------------------------------- monitors: config
# %APPDATA%\TVInputSwitcher\monitors.json

MONITOR_CONFIG_DEFAULTS = {
    # How often to re-read each monitor's current input and power state so the
    # menu stays correct when they're changed with the monitor's own buttons.
    # 0 disables.
    "poll_seconds": 30,
    # Leave monitors that don't answer DDC/CI (e.g. laptop panels) out of the menu.
    "hide_unsupported": False,
    # Per-monitor overrides, keyed by monitor name as shown in the menu:
    #   "label":  name to show instead of the monitor's own
    #   "inputs": {"0x0F": "Desktop"} renames inputs, and adds any the monitor doesn't report
    #   "hide":   ["0x12"] removes inputs from the menu
    #   "power_off": "0x05" power mode value to send for "Power off" (default: detected)
    "monitors": {},
}


def parse_code(value):
    """Parse a code written as an int, "0x0F" or "15". Returns None if invalid."""
    if isinstance(value, int):
        code = value
    else:
        try:
            code = int(str(value).strip(), 0)
        except ValueError:
            return None
    return code if 0 <= code <= 0xFF else None


class MonitorConfig:
    def __init__(self, data=None):
        self.data = {**MONITOR_CONFIG_DEFAULTS, **(data or {})}

    @classmethod
    def load(cls, path=None):
        """Load the config file. Returns ``(config, error_message_or_None)``."""
        path = path or MONITOR_CONFIG_PATH
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                raise ValueError("top level must be a JSON object")
            return cls(data), None
        except FileNotFoundError:
            return cls(), None
        except (OSError, ValueError) as exc:
            log.error("Ignoring invalid config %s: %s", path, exc)
            return cls(), f"Config file is invalid and was ignored: {exc}"

    @property
    def poll_seconds(self):
        try:
            return max(0.0, float(self.data.get("poll_seconds") or 0))
        except (TypeError, ValueError):
            return MONITOR_CONFIG_DEFAULTS["poll_seconds"]

    @property
    def hide_unsupported(self):
        return bool(self.data.get("hide_unsupported"))

    def _monitor_settings(self, monitor):
        monitors = self.data.get("monitors")
        if not isinstance(monitors, dict):
            return {}
        wanted = monitor.name.casefold()
        for name, settings in monitors.items():
            if name.casefold() == wanted and isinstance(settings, dict):
                return settings
        return {}

    def label_for(self, monitor):
        return str(self._monitor_settings(monitor).get("label") or monitor.name)

    def power_off_value(self, monitor):
        """The configured power mode value for "off", or None to use the detected one."""
        value = self._monitor_settings(monitor).get("power_off")
        code = None if value is None else parse_code(value)
        if value is not None and not code:
            log.warning("%s: ignoring invalid power_off %r in config", monitor.name, value)
        return code or None

    def inputs_for(self, monitor):
        """The ``(code, label)`` pairs to offer for a monitor, sorted by code."""
        settings = self._monitor_settings(monitor)
        renamed = {}
        for key, label in (settings.get("inputs") or {}).items():
            code = parse_code(key)
            if code is None:
                log.warning("%s: ignoring invalid input code %r in config", monitor.name, key)
            else:
                renamed[code] = str(label)
        hidden = {parse_code(c) for c in (settings.get("hide") or [])}
        codes = sorted(set(monitor.inputs) | set(renamed))
        return [(c, renamed.get(c) or input_name(c)) for c in codes if c not in hidden]


def write_template(monitors, path=None):
    """Create a config file pre-filled with the detected monitors so it's easy to edit."""
    path = path or MONITOR_CONFIG_PATH
    data = json.loads(json.dumps(MONITOR_CONFIG_DEFAULTS))
    for monitor in monitors:
        if monitor.supported:
            data["monitors"][monitor.name] = {
                "label": "",
                "inputs": {f"0x{c:02X}": input_name(c) for c in sorted(monitor.inputs)},
                "hide": [],
            }
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.write("\n")


# ---------------------------------------------------------------- start with Windows
# A per-user entry under HKCU\...\Run that starts the app with pythonw.

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "TVInputSwitcher"


def autostart_command():
    """The command line that starts the app without a console window."""
    if getattr(sys, "frozen", False):  # e.g. a PyInstaller build
        return f'"{sys.executable}"'
    python = Path(sys.executable)
    pythonw = python.with_name("pythonw.exe")
    if pythonw.exists():
        python = pythonw
    return f'"{python}" "{Path(__file__).resolve()}"'


def autostart_enabled():
    if not IS_WIN:
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.QueryValueEx(key, RUN_VALUE)
        return True
    except FileNotFoundError:
        return False


def set_autostart(enabled):
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            winreg.SetValueEx(key, RUN_VALUE, 0, winreg.REG_SZ, autostart_command())
        else:
            try:
                winreg.DeleteValue(key, RUN_VALUE)
            except FileNotFoundError:
                pass


# ---------------------------------------------------------------- monitors: manager
class MonitorManager:
    """Finds the monitors, keeps their state fresh, and switches their input or
    power. All DDC/CI traffic runs on one worker thread, never the UI's.

    `on_change()` and `notify(message)` are called from any thread.
    """

    def __init__(self, on_change, notify):
        self.on_change = on_change
        self.notify = notify
        self.monitors = []
        self.busy = True  # detecting monitors
        self.generation = 0  # bumped by every detection, so clicks in a stale menu are ignored
        self._config_mtime = None
        self._config_error = None
        self._load_config()
        self._ddc = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ddc")
        self._stopping = threading.Event()
        self._poll_queued = False
        self._quiet_until = 0.0

    # --- lifecycle -------------------------------------------------------

    def start(self):
        if self._config_error:
            self.notify(self._config_error)
        self.refresh()
        threading.Thread(target=self._background_loop, name="monitor-poll", daemon=True).start()

    def stop(self):
        self._stopping.set()
        self._ddc.shutdown(wait=True, cancel_futures=True)
        close_all(self.monitors)
        log.info("Monitor manager stopped")

    def _submit(self, func, *args):
        def run():
            try:
                func(*args)
            except Exception:
                log.exception("Error in %s", func.__name__)
        try:
            self._ddc.submit(run)
        except RuntimeError:
            pass  # shutting down

    # --- config ------------------------------------------------------------

    def _load_config(self):
        if not MONITOR_CONFIG_PATH.exists() and LEGACY_MONITOR_CONFIG_PATH.exists():
            try:
                MONITOR_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(LEGACY_MONITOR_CONFIG_PATH, MONITOR_CONFIG_PATH)
                log.info("Copied monitor config from %s", LEGACY_MONITOR_CONFIG_PATH)
            except OSError as exc:
                log.warning("Could not copy the old Monitor Changer config: %s", exc)
        try:
            self._config_mtime = MONITOR_CONFIG_PATH.stat().st_mtime
        except OSError:
            self._config_mtime = None
        self.config, self._config_error = MonitorConfig.load()

    def _reload_config_if_changed(self):
        try:
            mtime = MONITOR_CONFIG_PATH.stat().st_mtime
        except OSError:
            mtime = None
        if mtime != self._config_mtime:
            log.info("Monitor config changed, reloading")
            self._load_config()
            if self._config_error:
                self.notify(self._config_error)
            self.on_change()

    def open_config(self):
        try:
            if not MONITOR_CONFIG_PATH.exists():
                write_template(self.monitors)
            open_file(MONITOR_CONFIG_PATH)
        except OSError as exc:
            log.error("Could not open config: %s", exc)
            self.notify(f"Couldn't open {MONITOR_CONFIG_PATH}: {exc}")

    # --- DDC/CI work (always on the single DDC thread) ---------------------

    def refresh(self):
        self.busy = True
        self.on_change()
        self._submit(self._detect)

    def _detect(self):
        log.info("Detecting monitors")
        monitors = []
        try:
            monitors = enumerate_monitors()
            for monitor in monitors:
                monitor.probe()
        finally:
            old, self.monitors = self.monitors, monitors
            self.generation += 1
            close_all(old)
            self.busy = False
            self.on_change()
        log.info("Found %d monitor(s), %d with DDC/CI", len(monitors), sum(m.supported for m in monitors))

    def _find(self, generation, index):
        """The monitor a menu item refers to, or None if the menu is out of date."""
        if self.busy or generation != self.generation:
            return None  # clicked in a menu built before a refresh
        return next((m for m in self.monitors if m.index == index and m.supported), None)

    def switch(self, generation, index, code):
        self._submit(self._switch, generation, index, code)

    def _switch(self, generation, index, code):
        monitor = self._find(generation, index)
        if monitor is None:
            return
        name, label = self.config.label_for(monitor), self._input_label(monitor, code)
        try:
            monitor.set_input(code)
        except OSError as exc:
            log.error("%s: switching to %s failed: %s", monitor.name, label, exc)
            self.notify(f"Couldn't switch {name} to {label}.\n{exc.strerror or exc}")
            return
        log.info("%s: switched to %s (0x%02X)", monitor.name, label, code)
        # Monitors take a few seconds to switch; don't let a poll read the old input meanwhile.
        self._quiet_until = time.monotonic() + 8
        self.on_change()

    def power(self, generation, indexes, on):
        """Turn monitors on or off. *indexes* is a list of monitor numbers, or None for all."""
        self._submit(self._power, generation, indexes, on)

    def _power(self, generation, indexes, on):
        state = "on" if on else "off"
        failed = []
        for index in (indexes if indexes is not None else [m.index for m in self.monitors]):
            monitor = self._find(generation, index)
            if monitor is None or not self._can_power(monitor):
                continue
            try:
                monitor.set_power(on, self.config.power_off_value(monitor))
            except OSError as exc:
                log.error("%s: turning %s failed: %s", monitor.name, state, exc)
                failed.append(self.config.label_for(monitor))
            else:
                log.info("%s: turned %s", monitor.name, state)
        self._quiet_until = time.monotonic() + 8
        self.on_change()
        if failed:
            hint = "\nSome monitors stop listening for DDC/CI while off; use the power button." if on else ""
            self.notify(f"Couldn't turn {state} {', '.join(failed)}.{hint}")

    def _poll(self):
        self._poll_queued = False
        changed = False
        for monitor in self.monitors:
            if self.busy or time.monotonic() < self._quiet_until:
                return
            if not monitor.supported:
                continue
            readers = {"current_input": monitor.read_input}
            if self._can_power(monitor):
                readers["power_on"] = monitor.read_power
            for attr, read in readers.items():
                try:
                    value = read()
                except OSError:
                    continue  # asleep, showing another input, ...
                if value != getattr(monitor, attr):
                    log.info("%s: %s changed to %r", monitor.name, attr, value)
                    setattr(monitor, attr, value)
                    changed = True
        if changed:
            self.on_change()

    def _background_loop(self):
        last_poll = time.monotonic()
        while not self._stopping.wait(2):
            self._reload_config_if_changed()
            interval = self.config.poll_seconds
            now = time.monotonic()
            if interval and now - last_poll >= max(interval, 5):
                last_poll = now
                if not (self.busy or self._poll_queued or now < self._quiet_until):
                    self._poll_queued = True
                    self._submit(self._poll)

    # --- what the UI shows ---------------------------------------------------

    def _input_label(self, monitor, code):
        return dict(self.config.inputs_for(monitor)).get(code) or input_name(code)

    def _display_names(self, monitors):
        """Monitor names for the UI, numbered when several monitors share one."""
        names = [self.config.label_for(m) for m in monitors]
        return [f"{name} ({m.index})" if names.count(name) > 1 else name for m, name in zip(monitors, names)]

    def _can_power(self, monitor):
        return monitor.supported and bool(monitor.power_off_value or self.config.power_off_value(monitor))

    def _status(self, monitor):
        """Short state shown next to a monitor's name: "Off" or its current input."""
        if monitor.power_on is False:
            return "Off"
        if monitor.current_input is None:
            return ""
        return self._input_label(monitor, monitor.current_input)

    def tooltip_lines(self):
        monitors = self.monitors
        return [f"{name}: {self._status(m)}" for m, name in zip(monitors, self._display_names(monitors))
                if m.supported and self._status(m)]

    def snapshot(self):
        """Plain data for the tray process to build its monitor menu from."""
        monitors = [m for m in self.monitors if m.supported or not self.config.hide_unsupported]
        return {
            "monitors_supported": True,
            "monitors_busy": self.busy,
            "monitors_gen": self.generation,
            "monitors": [
                {
                    "index": m.index,
                    "title": name,
                    "supported": m.supported,
                    "status": self._status(m) if m.supported else "",
                    "inputs": self.config.inputs_for(m) if m.supported else [],
                    "current": m.current_input,
                    "can_power": self._can_power(m),
                }
                for m, name in zip(monitors, self._display_names(monitors))
            ],
        }


def open_file(path):
    """Open a file with whatever the system associates with it."""
    try:
        if IS_WIN:
            os.startfile(path)
        else:
            subprocess.Popen(["open" if IS_MAC else "xdg-open", str(path)])
    except OSError:
        if not IS_WIN:
            raise
        subprocess.Popen(["notepad.exe", str(path)])  # no program associated with the extension


# ---------------------------------------------------------------- monitors: command line
def cli_list():
    monitors = enumerate_monitors()
    try:
        for monitor in monitors:
            monitor.probe()
            primary = ", primary" if monitor.primary else ""
            print(f"{monitor.index}: {monitor.name}  ({monitor.device}{primary})")
            if not monitor.supported:
                print(f"     {monitor.error}")
                continue
            for code in sorted(monitor.inputs):
                current = "  <- current" if code == monitor.current_input else ""
                print(f"     0x{code:02X}  {input_name(code)}{current}")
            if monitor.power_off_value is not None:
                state = {True: "on", False: "off", None: "unknown"}[monitor.power_on]
                print(f"     power: {state} (off = 0x{monitor.power_off_value:02X})")
    finally:
        close_all(monitors)
    return 0


def _for_each_monitor(monitor_text, action):
    """Run ``action(monitor) -> message`` on the monitors matching *monitor_text*.

    *monitor_text* is a number from --list, part of a monitor's name, or "all".
    """
    monitors = enumerate_monitors()
    try:
        if monitor_text.casefold() == "all":
            targets = [m for m in monitors if m.handle is not None]
        else:
            targets = [
                m for m in monitors
                if monitor_text == str(m.index) or monitor_text.casefold() in m.name.casefold()
            ]
            if len(targets) > 1:
                print(f"error: more than one monitor matches {monitor_text!r}; see --list", file=sys.stderr)
                return 2
        if not targets:
            print(f"error: no monitor matches {monitor_text!r}; see --list", file=sys.stderr)
            return 2

        status = 0
        for monitor in targets:
            if monitor.handle is None:
                print(f"error: {monitor.name}: {monitor.error}", file=sys.stderr)
                status = 1
                continue
            try:
                print(f"{monitor.name}: {action(monitor)}")
            except OSError as exc:
                print(f"error: {monitor.name}: {exc}", file=sys.stderr)
                status = 1
        return status
    finally:
        close_all(monitors)


def cli_set_input(monitor_text, input_text):
    try:
        code = parse_input(input_text)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    def switch(monitor):
        monitor.set_input(code)
        return f"switched to {input_name(code)}"

    return _for_each_monitor(monitor_text, switch)


def cli_set_power(monitor_text, state):
    state = state.casefold()
    if state not in ("on", "off"):
        print(f"error: power state must be 'on' or 'off', not {state!r}", file=sys.stderr)
        return 2
    config, _ = MonitorConfig.load()

    def power(monitor):
        off_value = config.power_off_value(monitor)
        if state == "off" and off_value is None:
            monitor.probe()  # reads the capabilities to pick the right "off" value (slow)
        monitor.set_power(state == "on", off_value)
        return f"turned {state}"

    return _for_each_monitor(monitor_text, power)


# ---------------------------------------------------------------- tray process
def make_tray_image(size=64):
    """Draw a simple TV icon so no image file is needed."""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    s = size / 64
    d.rounded_rectangle([4 * s, 8 * s, 60 * s, 46 * s], radius=5 * s,
                        fill=(30, 30, 30, 255), outline=(200, 200, 200, 255),
                        width=max(1, int(3 * s)))
    d.rectangle([10 * s, 14 * s, 54 * s, 40 * s], fill=(40, 120, 220, 255))
    d.rectangle([28 * s, 46 * s, 36 * s, 53 * s], fill=(200, 200, 200, 255))
    d.rectangle([18 * s, 53 * s, 46 * s, 58 * s], fill=(200, 200, 200, 255))
    return img


def _menu_text(text):
    # A lone '&' marks a keyboard accelerator in Win32 menus.
    return str(text).replace("&", "&&")


if HAVE_TRAY and IS_WIN:
    class TrayIcon(pystray.Icon):
        """pystray's Win32 icon, changed to accept menu rebuilds from other
        threads without breaking a menu that's open."""

        _WM_RBUTTONUP = 0x0205

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._menu_open = False
            self._refresh_pending = False
            self._message_handlers[WM_APP_REFRESH_MENU] = self._on_refresh_menu

        def refresh_menu(self):
            """Rebuild the menu on the tray thread. Safe to call from any thread."""
            if self._hwnd:
                _PostMessageW(self._hwnd, WM_APP_REFRESH_MENU, 0, 0)

        def _on_refresh_menu(self, wparam, lparam):
            # The popup's modal loop dispatches our messages too. Rebuilding then
            # would destroy the menu under the user and remap item IDs, so wait.
            if self._menu_open:
                self._refresh_pending = True
            else:
                self.update_menu()

        def _on_notify(self, wparam, lparam):
            if lparam != self._WM_RBUTTONUP:
                return super()._on_notify(wparam, lparam)
            self._menu_open = True
            try:
                return super()._on_notify(wparam, lparam)
            finally:
                self._menu_open = False
                if self._refresh_pending:
                    self._refresh_pending = False
                    self.update_menu()
else:
    TrayIcon = pystray.Icon if HAVE_TRAY else None


def tray_process(state_q, cmd_q):
    """Runs in a separate process: owns the tray / menu bar icon.

    Receives state snapshots on `state_q` (None means exit) and sends
    user commands as tuples on `cmd_q`. Wrapped so a crash here — which would
    otherwise vanish silently, especially now that the launching terminal is
    hidden — leaves a trace on disk instead.
    """
    try:
        _run_tray(state_q, cmd_q)
    except Exception:
        import traceback
        try:
            with open(Path.home() / ".tv_input_switcher_tray_error.log", "a",
                      encoding="utf-8") as f:
                f.write(f"\n--- {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n")
                traceback.print_exc(file=f)
        except Exception:
            pass


def _run_tray(state_q, cmd_q):
    call_on_main = lambda fn: fn()  # noqa: E731
    if IS_MAC:
        try:
            import AppKit  # keep this helper out of the Dock
            AppKit.NSApplication.sharedApplication().setActivationPolicy_(
                AppKit.NSApplicationActivationPolicyAccessory)
        except Exception:
            pass
        try:
            from PyObjCTools import AppHelper  # AppKit needs the main thread
            call_on_main = AppHelper.callAfter
        except Exception:
            pass

    snap = {
        "tvs": [], "device": None, "inputs": [], "current": None,
        "default_device": None, "default_input": None, "default_label": None,
        "master_mode": False, "tooltip": APP_NAME,
        "monitors_supported": False, "monitors_busy": False, "monitors_gen": 0, "monitors": [],
    }

    def send(*msg):
        return lambda icon, item: cmd_q.put(msg)

    def tv_items():
        if not snap["tvs"]:
            yield pystray.MenuItem("(no TVs found yet)", None, enabled=False)
            return
        for idx, (dev_id, label) in enumerate(snap["tvs"]):
            yield pystray.MenuItem(label, send("select_tv", idx),
                                   checked=lambda item, d=dev_id: snap["device"] == d,
                                   radio=True)

    def input_items():
        if not snap["inputs"]:
            yield pystray.MenuItem("(refresh to load inputs)", None, enabled=False)
            return
        for src_id, name in snap["inputs"]:
            yield pystray.MenuItem(name, send("input", src_id),
                                   checked=lambda item, s=src_id: snap["current"] == s,
                                   radio=True)

    def default_items():
        if not snap["inputs"]:
            yield pystray.MenuItem("(refresh to load inputs)", None, enabled=False)
            return
        dev = snap["device"]
        for src_id, name in snap["inputs"]:
            yield pystray.MenuItem(
                name, send("set_default", dev, src_id, name),
                checked=lambda item, s=src_id: (
                    snap["default_device"] == dev and snap["default_input"] == s
                ),
                radio=True,
            )

    def default_text():
        if snap["default_input"]:
            return f"Switch to {snap['default_label'] or snap['default_input']}"
        return "Switch to… (set a default below)"

    def monitor_items():
        Item, SEP = pystray.MenuItem, pystray.Menu.SEPARATOR
        gen, busy, mons = snap["monitors_gen"], snap["monitors_busy"], snap["monitors"]
        if busy:
            yield Item("Detecting monitors…", None, enabled=False)
        elif not mons:
            yield Item("No monitors found", None, enabled=False)
        for m in mons:
            name = _menu_text(m["title"])
            if not m["supported"]:
                yield Item(f"{name}  (no DDC/CI)", None, enabled=False)
                continue
            items = [
                Item(_menu_text(label), send("monitor_input", gen, m["index"], code),
                     checked=lambda item, c=code, cur=m["current"]: cur == c,
                     radio=True, enabled=not busy)
                for code, label in m["inputs"]
            ]
            if m["can_power"]:
                items += [
                    SEP,
                    Item("Power on", send("monitor_power", gen, [m["index"]], True), enabled=not busy),
                    Item("Power off", send("monitor_power", gen, [m["index"]], False), enabled=not busy),
                ]
            title = f"{name}\t{_menu_text(m['status'])}"
            if len(mons) == 1:
                # One monitor: list everything directly, one click to switch.
                yield Item(title, None, enabled=False)
                yield from items
            else:
                yield Item(title, pystray.Menu(*items))
        if sum(m["can_power"] for m in mons) > 1:
            yield SEP
            yield Item("Power on all monitors", send("monitor_power", gen, None, True), enabled=not busy)
            yield Item("Power off all monitors", send("monitor_power", gen, None, False), enabled=not busy)
        yield SEP
        yield Item("Refresh monitors", send("monitor_refresh"), enabled=not busy)

    def root_items():
        # Fires when the tray icon itself is clicked, not just the menu item.
        yield pystray.MenuItem(default_text(), send("switch_default"),
                               default=True, enabled=bool(snap["default_input"]))
        yield pystray.Menu.SEPARATOR
        yield from tv_items()
        yield pystray.Menu.SEPARATOR
        yield pystray.MenuItem("Default switch device", pystray.Menu(default_items))
        yield pystray.MenuItem("Input", pystray.Menu(input_items))
        yield pystray.Menu.SEPARATOR
        yield pystray.MenuItem("Power On", send("power", "on"))
        yield pystray.MenuItem("Power Off", send("power", "off"))
        yield pystray.MenuItem("Refresh", send("refresh"))
        yield pystray.MenuItem("Reload device list", send("reload"))
        if snap["monitors_supported"]:
            yield pystray.Menu.SEPARATOR
            yield from monitor_items()
        yield pystray.Menu.SEPARATOR
        yield pystray.MenuItem("Master (share sign-in on the network)", send("toggle_master"),
                               checked=lambda item: snap["master_mode"])
        yield pystray.MenuItem("Network devices…", send("network"))
        yield pystray.Menu.SEPARATOR
        if snap["monitors_supported"]:
            yield pystray.MenuItem("Edit monitor config…", send("monitor_config"))
            yield pystray.MenuItem("Start with Windows", send("toggle_autostart"),
                                   checked=lambda item: autostart_enabled())
        yield pystray.MenuItem("Open log", send("open_log"))
        yield pystray.Menu.SEPARATOR
        yield pystray.MenuItem("Show window", send("show"))
        yield pystray.MenuItem("Quit", send("quit"))

    icon = TrayIcon("tv_input_switcher", make_tray_image(), APP_NAME, pystray.Menu(root_items))
    # Other threads may only rebuild the Windows menu through its message queue.
    refresh = getattr(icon, "refresh_menu", icon.update_menu)

    def set_title():
        icon.title = snap["tooltip"]

    def notify(text):
        try:
            icon.notify(text, APP_NAME)
        except Exception:
            pass

    def listen():
        while True:
            try:
                msg = state_q.get()
            except (EOFError, OSError):
                msg = None
            if msg is None:
                call_on_main(icon.stop)
                return
            note = msg.pop("notify", None)
            snap.update(msg)
            if "tooltip" in msg:
                call_on_main(set_title)
            call_on_main(refresh)
            if note:
                call_on_main(lambda: notify(note))

    def setup(icon):
        icon.visible = True
        threading.Thread(target=listen, daemon=True).start()

    icon.run(setup=setup)


# ---------------------------------------------------------------- account dialog
class AccountDialog(tk.Toplevel):
    """Choose between a personal access token and an OAuth app, and sign in."""

    def __init__(self, app):
        super().__init__(app)
        self.app = app
        self.title("SmartThings Account")
        self.resizable(False, False)
        self.transient(app)
        a = app.cfg["auth"]

        frm = ttk.Frame(self, padding=12)
        frm.pack(fill="both", expand=True)

        self.mode = tk.StringVar(value=a.get("mode", "pat"))
        ttk.Radiobutton(frm, text="Personal access token (quick; new tokens last 24 hours)",
                        variable=self.mode, value="pat", command=self._toggle
                        ).grid(row=0, column=0, columnspan=3, sticky="w")
        self.pat = tk.StringVar(value=a.get("pat", ""))
        ttk.Label(frm, text="Token:").grid(row=1, column=0, sticky="w", padx=(20, 6))
        self.pat_entry = ttk.Entry(frm, textvariable=self.pat, width=44, show="•")
        self.pat_entry.grid(row=1, column=1, sticky="ew")
        self.pat_btn = ttk.Button(frm, text="Get token…",
                                  command=lambda: webbrowser.open(TOKENS_PAGE))
        self.pat_btn.grid(row=1, column=2, padx=(6, 0))

        ttk.Separator(frm).grid(row=2, column=0, columnspan=3, sticky="ew", pady=10)

        ttk.Radiobutton(frm, text="OAuth app (renews itself; one-time setup, see README)",
                        variable=self.mode, value="oauth", command=self._toggle
                        ).grid(row=3, column=0, columnspan=3, sticky="w")
        self.client_id = tk.StringVar(value=a.get("client_id", ""))
        self.client_secret = tk.StringVar(value=a.get("client_secret", ""))
        self.redirect = tk.StringVar(value=a.get("redirect_uri", DEFAULT_REDIRECT))
        self.oauth_widgets = []
        for r, (label, var, show) in enumerate([
            ("Client ID:", self.client_id, ""),
            ("Client secret:", self.client_secret, "•"),
            ("Redirect URI:", self.redirect, ""),
        ], start=4):
            ttk.Label(frm, text=label).grid(row=r, column=0, sticky="w", padx=(20, 6))
            e = ttk.Entry(frm, textvariable=var, width=44, show=show)
            e.grid(row=r, column=1, columnspan=2, sticky="ew", pady=2)
            self.oauth_widgets.append(e)
        signed_in = "Signed in" if a.get("refresh_token") else "Not signed in"
        self.oauth_status = ttk.Label(frm, text=signed_in, foreground="#666")
        self.oauth_status.grid(row=7, column=1, sticky="w")
        self.signin_btn = ttk.Button(frm, text="Sign in…", command=self.sign_in)
        self.signin_btn.grid(row=7, column=2, sticky="e", pady=(4, 0))
        self.oauth_widgets.append(self.signin_btn)

        btns = ttk.Frame(frm)
        btns.grid(row=8, column=0, columnspan=3, sticky="e", pady=(14, 0))
        ttk.Button(btns, text="Cancel", command=self.destroy).pack(side="right")
        ttk.Button(btns, text="Save", command=self.save).pack(side="right", padx=6)

        self._toggle()
        self.grab_set()
        nudge_repaint(self)

    def _toggle(self):
        oauth = self.mode.get() == "oauth"
        for w in self.oauth_widgets:
            w.config(state="normal" if oauth else "disabled")
        for w in (self.pat_entry, self.pat_btn):
            w.config(state="disabled" if oauth else "normal")

    def _apply(self):
        a = self.app.cfg["auth"]
        old = (a.get("client_id"), a.get("client_secret"), a.get("redirect_uri"))
        a["mode"] = self.mode.get()
        a["pat"] = self.pat.get().strip()
        a["client_id"] = self.client_id.get().strip()
        a["client_secret"] = self.client_secret.get().strip()
        a["redirect_uri"] = self.redirect.get().strip() or DEFAULT_REDIRECT
        if old != (a["client_id"], a["client_secret"], a["redirect_uri"]):
            for k in ("access_token", "refresh_token", "expires_at"):
                a.pop(k, None)
        save_config(self.app.cfg)

    def save(self):
        self._apply()
        self.destroy()
        self.app.load_tvs()

    def sign_in(self):
        self._apply()
        a = self.app.cfg["auth"]
        if not (a["client_id"] and a["client_secret"]):
            messagebox.showerror(APP_NAME, "Enter the OAuth client ID and secret first.", parent=self)
            return
        state = secrets.token_urlsafe(16)
        webbrowser.open(self.app.auth.authorize_url(state))

        u = urllib.parse.urlparse(a["redirect_uri"])
        if u.scheme == "http" and u.hostname in ("localhost", "127.0.0.1"):
            # Redirect comes back to this computer: catch it automatically.
            self.oauth_status.config(text="Waiting for the browser…")

            def work():
                code = capture_code_locally(a["redirect_uri"], state)
                if not code:
                    raise AuthError("Didn't receive a sign-in from the browser.")
                self.app.auth.exchange_code(code)
            self._run_signin(work)
        else:
            text = simpledialog.askstring(
                APP_NAME,
                "Sign in and approve access in the browser. You'll land on a "
                "page whose address contains ?code=…\n\n"
                "Copy that whole address (or the code) and paste it here:",
                parent=self,
            )
            code = extract_code(text or "")
            if not code:
                return
            self._run_signin(lambda: self.app.auth.exchange_code(code))

    def _run_signin(self, work):
        self.signin_btn.config(state="disabled")

        def target():
            try:
                work()
                err = None
            except Exception as e:  # noqa: BLE001
                err = e
            self.after(0, lambda: self._signin_done(err))
        threading.Thread(target=target, daemon=True).start()

    def _signin_done(self, err):
        if not self.winfo_exists():
            return
        self.signin_btn.config(state="normal")
        if err:
            self.oauth_status.config(text="Sign-in failed")
            messagebox.showerror(APP_NAME, str(err), parent=self)
        else:
            self.oauth_status.config(text="Signed in")
            messagebox.showinfo(APP_NAME, "Signed in to SmartThings.", parent=self)


# ---------------------------------------------------------------- network dialog
class NetworkDialog(tk.Toplevel):
    """Toggle master mode and manage the IP addresses of other instances on
    the local network. Sync is plain, unencrypted TCP — local network only."""

    def __init__(self, app):
        super().__init__(app)
        self.app = app
        self.title("Network")
        self.resizable(False, False)
        self.transient(app)

        frm = ttk.Frame(self, padding=12)
        frm.pack(fill="both", expand=True)

        self.port = app.cfg.get("sync_port", DEFAULT_SYNC_PORT)
        self.conn_var = tk.StringVar()
        ttk.Label(frm, textvariable=self.conn_var, foreground="#888").grid(
            row=0, column=0, columnspan=2, sticky="w")

        self.last_push_var = tk.StringVar()
        ttk.Label(frm, textvariable=self.last_push_var, foreground="#888").grid(
            row=1, column=0, columnspan=2, sticky="w")

        self.master_var = tk.BooleanVar(value=bool(app.cfg.get("master_mode")))
        ttk.Checkbutton(
            frm,
            text="Make this computer the master\n"
                 "(refreshes sign-in and shares it with the devices below)",
            variable=self.master_var, command=self._toggle_master,
        ).grid(row=2, column=0, columnspan=2, sticky="w", pady=(10, 0))

        ttk.Label(frm, text="Other devices on the network (IP address):").grid(
            row=3, column=0, columnspan=2, sticky="w", pady=(12, 2))

        list_frame = ttk.Frame(frm)
        list_frame.grid(row=4, column=0, columnspan=2, sticky="nsew")
        self.listbox = tk.Listbox(list_frame, height=6, width=34, exportselection=False)
        self.listbox.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(list_frame, command=self.listbox.yview)
        sb.pack(side="left", fill="y")
        self.listbox.config(yscrollcommand=sb.set)
        for peer in app.cfg.get("peers", []):
            self.listbox.insert("end", peer)

        btns = ttk.Frame(frm)
        btns.grid(row=5, column=0, columnspan=2, sticky="w", pady=(6, 0))
        ttk.Button(btns, text="Add…", command=self._add).pack(side="left")
        ttk.Button(btns, text="Edit…", command=self._edit).pack(side="left", padx=6)
        ttk.Button(btns, text="Remove", command=self._remove).pack(side="left")

        sync_btns = ttk.Frame(frm)
        sync_btns.grid(row=6, column=0, columnspan=2, sticky="w", pady=(6, 0))
        self.check_btn = ttk.Button(sync_btns, text="Check network", command=self._check_network)
        self.check_btn.pack(side="left")
        self.push_btn = ttk.Button(sync_btns, text="Push now", command=self._force_push)
        self.push_btn.pack(side="left", padx=6)
        self.request_btn = ttk.Button(sync_btns, text="Request now", command=self._request_push)
        self.request_btn.pack(side="left")

        self.status_var = tk.StringVar(value="Click \"Check network\" to test these devices.")
        ttk.Label(frm, textvariable=self.status_var, foreground="#444",
                  wraplength=320, justify="left").grid(
            row=7, column=0, columnspan=2, sticky="w", pady=(8, 0))

        ttk.Label(
            frm,
            text=f"Sync port {self.port} must be the same on every device. No "
                 "encryption is used — keep this on a trusted home network.",
            foreground="#666", wraplength=320, justify="left",
        ).grid(row=8, column=0, columnspan=2, sticky="w", pady=(10, 0))

        close_row = ttk.Frame(frm)
        close_row.grid(row=9, column=0, columnspan=2, sticky="e", pady=(14, 0))
        ttk.Button(close_row, text="Close", command=self.destroy).pack(side="right")

        self._refresh_local_status()
        nudge_repaint(self)

    def _toggle_master(self):
        self.app.set_master_mode(self.master_var.get())

    # ---- debugging tools
    def _refresh_local_status(self):
        srv = self.app.sync_server
        if srv.listening is True:
            status = "listening for pushes"
        elif srv.listening is False:
            status = f"NOT listening ({srv.error or 'unknown error'})"
        else:
            status = "starting…"
            self.after(300, self._refresh_local_status)
        self.conn_var.set(f"This computer: {local_ip()}:{self.port} — {status}")

        if srv.last_seen_from:
            when = time.strftime("%H:%M:%S", time.localtime(srv.last_seen_at))
            self.last_push_var.set(f"Last sign-in received from {srv.last_seen_from} at {when}")
        else:
            self.last_push_var.set("No sign-in received from another device yet")

    def _check_network(self):
        self._refresh_local_status()
        peers = list(self.listbox.get(0, "end"))
        if not peers:
            self.status_var.set("No devices added yet — click \"Add…\" first.")
            return
        self.check_btn.config(state="disabled")
        self.status_var.set("Checking…")
        port = self.port

        def work():
            results = [check_peer(p, port) for p in peers]
            self.after(0, lambda: self._show_check_results(results))

        threading.Thread(target=work, daemon=True).start()

    def _show_check_results(self, results):
        if not self.winfo_exists():
            return
        self.check_btn.config(state="normal")
        lines = []
        for r in results:
            if r["online"]:
                lines.append(f"✓ {r['peer']} — online" + (", master" if r["master"] else ""))
            else:
                lines.append(f"✗ {r['peer']} — unreachable")
        self.status_var.set("\n".join(lines))

    def _force_push(self):
        """Send this computer's current sign-in to every peer right now,
        regardless of master mode — useful for testing the connection."""
        peers = list(self.listbox.get(0, "end"))
        if not peers:
            self.status_var.set("No devices added yet — click \"Add…\" first.")
            return
        push_tokens_to_peers(self.app.cfg)
        self.status_var.set(f"Pushed this computer's sign-in to {len(peers)} device(s).")

    def _request_push(self):
        """Ask every peer to push its sign-in back to us right now, instead
        of waiting for its next scheduled refresh."""
        peers = list(self.listbox.get(0, "end"))
        if not peers:
            self.status_var.set("No devices added yet — click \"Add…\" first.")
            return
        self.request_btn.config(state="disabled")
        self.status_var.set("Requesting a sign-in from the network…")
        cfg = self.app.cfg

        def work():
            got = request_push_from_peers(cfg, self.port)
            self.after(0, lambda: self._request_done(got))

        threading.Thread(target=work, daemon=True).start()

    def _request_done(self, got):
        if not self.winfo_exists():
            return
        self.request_btn.config(state="normal")
        if not got:
            self.status_var.set(
                "No device responded with a sign-in. Make sure one of them "
                "has \"Master\" turned on.")
            return
        peer, auth = next(iter(got.items()))
        self.app.apply_synced_auth(auth)
        extra = f" (and {len(got) - 1} more)" if len(got) > 1 else ""
        self.status_var.set(f"Received a fresh sign-in from {peer}{extra}.")

    def _add(self):
        ip = simpledialog.askstring("Add device", "IP address (optionally ip:port):", parent=self)
        ip = (ip or "").strip()
        if ip:
            self.listbox.insert("end", ip)
            self._save()

    def _edit(self):
        sel = self.listbox.curselection()
        if not sel:
            return
        ip = simpledialog.askstring(
            "Edit device", "IP address (optionally ip:port):",
            initialvalue=self.listbox.get(sel[0]), parent=self,
        )
        ip = (ip or "").strip()
        if ip:
            self.listbox.delete(sel[0])
            self.listbox.insert(sel[0], ip)
            self._save()

    def _remove(self):
        sel = self.listbox.curselection()
        if sel:
            self.listbox.delete(sel[0])
            self._save()

    def _save(self):
        self.app.cfg["peers"] = list(self.listbox.get(0, "end"))
        save_config(self.app.cfg)


def nudge_repaint(win, _tries=0):
    """Work around a Tk/Cocoa bug (still present in the Tcl/Tk builds several
    python.org installers bundle) where a window's contents render blank on
    macOS while the system is in Dark Mode. Tk sometimes finishes drawing a
    window without telling the compositor to display it; a resize alone can
    get coalesced away with no visible effect, so also toggle alpha, which
    forces Cocoa to recomposite the window's layer."""
    if not IS_MAC or not win.winfo_exists():
        return
    if not win.winfo_viewable() and _tries < 20:
        win.after(25, lambda: nudge_repaint(win, _tries + 1))
        return
    try:
        win.update_idletasks()
        w, h = win.winfo_width(), win.winfo_height()
        win.attributes("-alpha", 0.0)
        win.geometry(f"{w}x{h + 1}")
        win.update_idletasks()
        win.geometry(f"{w}x{h}")
        win.attributes("-alpha", 1.0)
    except tk.TclError:
        pass


# ---------------------------------------------------------------- main window
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        # Start in the tray, window hidden. Without a tray there'd be no way
        # to reach the app, so the window stays visible in that case.
        if HAVE_TRAY:
            self.withdraw()
        self.title(APP_NAME)
        self.minsize(360, 200)
        self.resizable(True, False)

        self.cfg = load_config()
        self.auth = SmartThingsAuth(self.cfg["auth"], self._on_auth_changed)
        self.api = SmartThingsAPI(self.auth)
        self.tvs = []  # [(id, label)]
        self.state_info = None
        self.busy = False
        self.sync_server = TokenSyncServer(self)

        pad = {"padx": 10, "pady": 6}

        top = ttk.Frame(self)
        top.pack(fill="x", **pad)
        ttk.Label(top, text="TV:").pack(side="left")
        self.tv_combo = ttk.Combobox(top, state="readonly")
        self.tv_combo.pack(side="left", fill="x", expand=True, padx=(6, 6))
        self.tv_combo.bind("<<ComboboxSelected>>", self.on_tv_selected)
        self.refresh_btn = ttk.Button(top, text="Refresh", command=self.refresh_state)
        self.refresh_btn.pack(side="left")
        self.account_btn = ttk.Button(top, text="Account…", command=self.open_account)
        self.account_btn.pack(side="left", padx=(4, 0))
        self.network_btn = ttk.Button(top, text="Network…", command=self.open_network)
        self.network_btn.pack(side="left", padx=(4, 0))

        port = self.cfg.get("sync_port", DEFAULT_SYNC_PORT)
        ttk.Label(self, text=f"This computer: {local_ip()}:{port}", foreground="#888").pack(
            anchor="w", padx=10)

        power = ttk.Frame(self)
        power.pack(fill="x", **pad)
        self.on_btn = ttk.Button(power, text="Power On", command=lambda: self.power("on"))
        self.off_btn = ttk.Button(power, text="Power Off", command=lambda: self.power("off"))
        self.on_btn.pack(side="left", expand=True, fill="x", padx=(0, 4))
        self.off_btn.pack(side="left", expand=True, fill="x", padx=(4, 0))

        heading_size = 13 if IS_MAC else 10
        ttk.Label(self, text="Inputs",
                  font=("TkDefaultFont", heading_size, "bold")).pack(anchor="w", padx=10)
        self.inputs_frame = ttk.Frame(self)
        self.inputs_frame.pack(fill="x", **pad)

        self.status_var = tk.StringVar(value="Starting…")
        ttk.Label(self, textvariable=self.status_var, foreground="#666",
                  wraplength=460, justify="left").pack(anchor="w", padx=10, pady=(0, 10))

        self.tray_proc = None
        self.mon = None  # MonitorManager (Windows); monitors are driven from the tray menu
        if HAVE_TRAY:
            self.start_tray()
            if IS_WIN:
                self.mon = MonitorManager(self._monitors_changed, self.notify)
                self.mon.start()
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        if IS_MAC:
            self.createcommand("tk::mac::Quit", self.quit_app)
            self.createcommand("tk::mac::ReopenApplication", self.show_window)

        self.after(3 * 3600 * 1000, self.keep_alive)

        a = self.cfg["auth"]
        if (a["mode"] == "pat" and not a.get("pat")) or \
           (a["mode"] == "oauth" and not a.get("refresh_token")):
            self.status_var.set("Sign in to SmartThings to get started.")
            self.after(200, self.open_account)
        else:
            self.load_tvs()

        if not HAVE_TRAY:
            self.after(50, lambda: nudge_repaint(self))

    # ---- tray communication
    def start_tray(self):
        ctx = mp.get_context("spawn")
        self.tray_state_q = ctx.Queue()
        self.tray_cmd_q = ctx.Queue()
        self.tray_proc = ctx.Process(target=tray_process,
                                     args=(self.tray_state_q, self.tray_cmd_q), daemon=True)
        self.tray_proc.start()
        self.after(200, self.poll_tray)

    def tray_alive(self):
        return self.tray_proc is not None and self.tray_proc.is_alive()

    def poll_tray(self):
        if not self.tray_alive():
            self.tray_proc = None
            self.show_window()
            return
        while True:
            try:
                msg = self.tray_cmd_q.get_nowait()
            except queue.Empty:
                break
            self.handle_tray_command(msg)
        self.after(200, self.poll_tray)

    def handle_tray_command(self, msg):
        action, *args = msg
        if action == "show":
            self.show_window()
        elif action == "select_tv":
            self.select_tv(args[0])
        elif action == "input":
            self.set_input(args[0])
        elif action == "power":
            self.power(args[0])
        elif action == "refresh":
            self.refresh_state()
        elif action == "reload":
            self.load_tvs()
        elif action == "set_default":
            dev_id, src_id, label = args
            self.cfg["default_switch"] = {"device_id": dev_id, "input": src_id, "label": label}
            save_config(self.cfg)
        elif action == "switch_default":
            self.switch_default()
        elif action == "toggle_master":
            self.set_master_mode(not self.cfg.get("master_mode", False))
        elif action == "network":
            self.open_network()
        elif action == "monitor_input" and self.mon:
            self.mon.switch(*args)
        elif action == "monitor_power" and self.mon:
            self.mon.power(*args)
        elif action == "monitor_refresh" and self.mon:
            self.mon.refresh()
        elif action == "monitor_config" and self.mon:
            self.mon.open_config()
        elif action == "toggle_autostart":
            self.toggle_autostart()
        elif action == "open_log":
            self.open_log()
        elif action == "quit":
            self.quit_app()
            return
        self.update_tray()

    def update_tray(self):
        if not self.tray_alive():
            return
        info = self.state_info or {}
        d = self.cfg.get("default_switch") or {}
        try:
            self.tray_state_q.put({
                "tvs": list(self.tvs),
                "device": self.current_device(),
                "inputs": list(info.get("inputs", [])),
                "current": info.get("current"),
                "default_device": d.get("device_id"),
                "default_input": d.get("input"),
                "default_label": d.get("label"),
                "master_mode": bool(self.cfg.get("master_mode")),
                "tooltip": self.tooltip(),
                **(self.mon.snapshot() if self.mon else {}),
            })
        except Exception:
            log.exception("Could not update the tray")

    def tooltip(self):
        """The tray icon's hover text: the TV's and each monitor's current input."""
        lines = [APP_NAME]
        info, dev = self.state_info, self.current_device()
        if info and dev:
            label = next((name for i, name in self.tvs if i == dev), None)
            current = dict(info["inputs"]).get(info["current"], info["current"])
            if label and current:
                lines.append(f"{label}: {current}")
        if self.mon:
            lines += self.mon.tooltip_lines()
        text = "\n".join(lines)
        return text if len(text) <= 127 else text[:126] + "…"  # the Windows limit

    def _monitors_changed(self):
        """Called from any thread when monitor state changes."""
        try:
            self.after(0, self.update_tray)
        except (RuntimeError, tk.TclError):
            pass  # shutting down

    def notify(self, message):
        """Show a message from any thread: a tray notification, or the status line without a tray."""
        def show():
            if self.tray_alive():
                self.tray_state_q.put({"notify": message[:255]})
            else:
                self.status_var.set(message.splitlines()[0] if message else "")
        try:
            self.after(0, show)
        except (RuntimeError, tk.TclError):
            pass

    def open_log(self):
        try:
            open_file(LOG_PATH)
        except OSError as exc:
            self.notify(f"Couldn't open {LOG_PATH}: {exc}")

    def toggle_autostart(self):
        try:
            set_autostart(not autostart_enabled())
        except OSError as exc:
            log.error("Could not change autostart: %s", exc)
            self.notify(f"Couldn't change the startup setting: {exc}")

    # ---- window management
    def on_close(self):
        if self.tray_alive():
            self.withdraw()
        else:
            self.quit_app()

    def show_window(self):
        self.deiconify()
        self.lift()
        self.attributes("-topmost", True)
        self.after(150, lambda: self.attributes("-topmost", False))
        self.focus_force()
        nudge_repaint(self)

    def quit_app(self):
        if self.mon is not None:
            self.mon.stop()
            self.mon = None
        if self.tray_proc is not None:
            try:
                self.tray_state_q.put(None)
                self.tray_proc.join(timeout=2)
                if self.tray_proc.is_alive():
                    self.tray_proc.terminate()
            except Exception:
                pass
            self.tray_proc = None
        self.destroy()

    def open_account(self):
        self.show_window()
        AccountDialog(self)

    def open_network(self):
        self.show_window()
        NetworkDialog(self)

    # ---- local network sign-in sync
    def _on_auth_changed(self):
        """Called (from any thread) whenever the stored tokens change."""
        save_config(self.cfg)
        if self.cfg.get("master_mode"):
            push_tokens_to_peers(self.cfg)

    def apply_synced_auth(self, auth):
        """Called on the UI thread when a master pushes us fresh tokens."""
        self.cfg["auth"].update(auth)
        save_config(self.cfg)
        self.status_var.set("Received a fresh sign-in from the network master.")

    def set_master_mode(self, enabled):
        self.cfg["master_mode"] = bool(enabled)
        save_config(self.cfg)
        self.update_tray()
        if enabled:
            push_tokens_to_peers(self.cfg)

    def switch_default(self):
        d = self.cfg.get("default_switch") or {}
        dev, src, label = d.get("device_id"), d.get("input"), d.get("label")
        if not dev or not src:
            self.show_window()
            messagebox.showinfo(
                APP_NAME,
                "No default switch device set yet.\n\n"
                "Right-click the tray icon → \"Default switch device\" to choose one.",
            )
            return

        def work():
            cap = (self.state_info or {}).get("cap") if dev == self.current_device() else None
            if not cap:
                cap = self.api.tv_state(dev)["cap"]
            self.api.command(dev, cap, "setInputSource", src)

        def done(_):
            self.status_var.set(f"Switched to {label or src}")
            if dev == self.current_device() and self.state_info:
                self.state_info["current"] = src
                self.build_input_buttons()

        self.run_bg(f"Switching to {label or src}…", work, done)

    def keep_alive(self):
        threading.Thread(target=lambda: self._quietly(self.auth.keep_alive), daemon=True).start()
        self.after(3 * 3600 * 1000, self.keep_alive)

    @staticmethod
    def _quietly(fn):
        try:
            fn()
        except Exception:
            pass

    # ---- background work
    def run_bg(self, label, work, done):
        """Run `work()` in a thread, then `done(result)` on the UI thread."""
        if self.busy:
            return
        self.busy = True
        self.set_controls_enabled(False)
        self.status_var.set(label)

        def target():
            try:
                result, err = work(), None
            except Exception as e:  # noqa: BLE001
                result, err = None, e
            self.after(0, lambda: self._finish(done, result, err))

        threading.Thread(target=target, daemon=True).start()

    def _finish(self, done, result, err):
        self.busy = False
        self.set_controls_enabled(True)
        if err:
            self.status_var.set("Error — see message.")
            if isinstance(err, AuthError):
                if messagebox.askyesno(APP_NAME, f"{err}\n\nOpen account settings now?"):
                    self.open_account()
            else:
                messagebox.showerror(APP_NAME, str(err))
        else:
            done(result)
        self.update_tray()

    def set_controls_enabled(self, enabled):
        st = "normal" if enabled else "disabled"
        for w in (self.refresh_btn, self.on_btn, self.off_btn, self.account_btn, self.network_btn):
            w.config(state=st)
        self.tv_combo.config(state="readonly" if enabled else "disabled")
        for w in self.inputs_frame.winfo_children():
            w.config(state=st)

    # ---- actions
    def current_device(self):
        idx = self.tv_combo.current()
        return self.tvs[idx][0] if 0 <= idx < len(self.tvs) else None

    def load_tvs(self):
        def done(tvs):
            self.tvs = tvs
            self.tv_combo["values"] = [label for _, label in tvs]
            if not tvs:
                self.tv_combo.set("")
                self.status_var.set("No TVs with input control found on this SmartThings account.")
                return
            ids = [i for i, _ in tvs]
            saved = self.cfg.get("device_id")
            self.tv_combo.current(ids.index(saved) if saved in ids else 0)
            self.on_tv_selected()

        self.run_bg("Looking for TVs…", self.api.list_tvs, done)

    def on_tv_selected(self, _event=None):
        dev = self.current_device()
        if dev:
            self.cfg["device_id"] = dev
            save_config(self.cfg)
            self.refresh_state()

    def select_tv(self, idx):
        if self.busy or idx == self.tv_combo.current():
            return
        self.tv_combo.current(idx)
        self.state_info = None
        self.on_tv_selected()

    def refresh_state(self):
        dev = self.current_device()
        if not dev:
            return

        def done(info):
            self.state_info = info
            self.build_input_buttons()
            power = info["power"] or "unknown"
            current = dict(info["inputs"]).get(info["current"], info["current"])
            self.status_var.set(f"Power: {power}   •   Current input: {current or '—'}")

        self.run_bg("Reading TV status…", lambda: self.api.tv_state(dev), done)

    def build_input_buttons(self):
        for w in self.inputs_frame.winfo_children():
            w.destroy()
        info = self.state_info
        if not info:
            return
        cols = 3
        for i, (src_id, name) in enumerate(info["inputs"]):
            text = f"● {name}" if src_id == info["current"] else name
            b = ttk.Button(self.inputs_frame, text=text,
                           command=lambda s=src_id: self.set_input(s))
            b.grid(row=i // cols, column=i % cols, sticky="ew", padx=3, pady=3)
        for c in range(cols):
            self.inputs_frame.columnconfigure(c, weight=1)

    def set_input(self, src_id):
        dev = self.current_device()
        if not dev or not self.state_info:
            return
        cap = self.state_info["cap"]

        def done(_):
            self.state_info["current"] = src_id
            self.build_input_buttons()
            name = dict(self.state_info["inputs"]).get(src_id, src_id)
            self.status_var.set(f"Switched to {name}")

        self.run_bg(f"Switching to {src_id}…",
                    lambda: self.api.command(dev, cap, "setInputSource", src_id), done)

    def power(self, state):
        dev = self.current_device()
        if not dev:
            return

        def done(_):
            self.status_var.set(f"Power {state} sent")
            if state == "on":
                self.after(5000, self.refresh_state)  # let the TV wake first

        self.run_bg(f"Turning TV {state}…",
                    lambda: self.api.command(dev, "switch", state), done)


def warn_if_apple_system_python():
    """Apple's system Python (/usr/bin/python3) bundles Tk 8.5, which predates
    Dark Mode and is known to render windows blank or unresizable on modern
    macOS. That broken Tk build can't reliably show a dialog about itself, so
    print straight to the terminal instead."""
    if IS_MAC and sys.executable in ("/usr/bin/python3", "/usr/bin/python"):
        print(
            "Warning: running under macOS's built-in Python at "
            f"{sys.executable}. It bundles an old Tcl/Tk with serious "
            "display bugs on modern macOS (blank or unresizable windows, "
            "especially in Dark Mode).\n"
            "Install Python from https://python.org/downloads/macos/ and "
            "run this script with THAT python3 instead.",
            file=sys.stderr,
        )


def hide_launching_terminal():
    """Best-effort: hide the console/terminal window used to start the app,
    since it's meant to run quietly in the tray from here on."""
    if IS_WIN:
        try:
            _hide_own_console()  # leaves a console that's shared with a shell alone
        except Exception:
            pass
    elif IS_MAC and os.environ.get("TERM_PROGRAM") == "Apple_Terminal":
        try:
            subprocess.run(
                ["osascript", "-e",
                 'tell application "Terminal" to set miniaturized of front window to true'],
                capture_output=True, timeout=3,
            )
        except Exception:
            pass


class _LogStream:
    """Stands in for stdout/stderr under pythonw, which has none, so tracebacks
    and prints end up in the log instead of vanishing."""

    def __init__(self):
        self._busy = False

    def write(self, text):
        if self._busy:  # the logging system itself failed; don't recurse
            return len(text)
        self._busy = True
        try:
            for line in text.splitlines():
                if line.strip():
                    logging.getLogger("stderr").error("%s", line)
        finally:
            self._busy = False
        return len(text)

    def flush(self):
        pass

    def isatty(self):
        return False


def setup_logging():
    handlers = [logging.handlers.RotatingFileHandler(LOG_PATH, maxBytes=512_000, backupCount=1, encoding="utf-8")]
    if sys.stderr is not None:  # None under pythonw
        handlers.append(logging.StreamHandler())
    logging.basicConfig(
        level=logging.INFO, handlers=handlers,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if sys.stderr is None:
        sys.stdout = sys.stderr = _LogStream()


# ---------------------------------------------------------------- missing tray packages
TRAY_PACKAGES = ["pystray", "Pillow"]
TRAY_MODULES = {"pystray", "PIL", "six"}  # top-level modules those packages provide
RETRY_ENV = "TV_INPUT_SWITCHER_AFTER_INSTALL"
CREATE_NO_WINDOW = 0x08000000


def _python_flags():
    """Interpreter flags that change where packages are imported from.

    In isolated mode (-I) or with -s, the per-user site-packages folder is
    skipped, so a package installed there is missing for us even though plain
    pip reports it as installed. pip and the restart must use the same flags.
    """
    if sys.flags.isolated:
        return ["-I"]
    if sys.flags.no_user_site:
        return ["-s"]
    return []


def _missing_module(error):
    """Top-level name of the module that couldn't be found, if that's what went wrong."""
    # pystray re-raises a missing dependency as a generic "this platform is not
    # supported" ImportError, so the module name is only in the message.
    match = re.search(r"No module named '([\w.]+)'", str(error))
    return match.group(1).partition(".")[0] if match else None


def offer_tray_install(error):
    """Offer to pip install the tray's packages into this Python, then restart.

    Returns True if a new instance was started. If the user declines, or it
    doesn't help, the app carries on without the tray icon.
    """
    root = tk.Tk()
    root.withdraw()
    try:
        if _missing_module(error) not in TRAY_MODULES or os.environ.get(RETRY_ENV):
            # Not a missing package, or installing already didn't help: don't loop.
            messagebox.showwarning(
                APP_NAME,
                f"The tray icon isn't available:\n\n{error!r}\n\n"
                f"Python: {sys.executable} {' '.join(_python_flags())}\n\n"
                "The app will run without it.",
                parent=root,
            )
            return False
        python = Path(sys.executable).with_name("python.exe") if IS_WIN else Path(sys.executable)
        if not python.exists():
            python = Path(sys.executable)
        command = [str(python), *_python_flags(), "-m", "pip", "install", *TRAY_PACKAGES]
        if not messagebox.askyesno(
            APP_NAME,
            f"{error}\n\nThe tray icon needs some Python packages that aren't installed. "
            f"Install them now?\n\n{subprocess.list2cmdline(command)}",
            parent=root,
        ):
            return False
        result = subprocess.run(command, capture_output=True, text=True,
                                creationflags=CREATE_NO_WINDOW if IS_WIN else 0)
        if result.returncode != 0:
            log.error("pip install failed:\n%s%s", result.stdout, result.stderr)
            messagebox.showerror(
                APP_NAME,
                f"Installing failed. Run this in a terminal to see why:\n\n{subprocess.list2cmdline(command)}",
                parent=root,
            )
            return False
    finally:
        root.destroy()
    # A fresh process picks up the new packages without import cache surprises.
    # The marker stops it offering to install again if that didn't help.
    subprocess.Popen(
        [sys.executable, *_python_flags(), str(Path(__file__).resolve()), *sys.argv[1:]],
        env={**os.environ, RETRY_ENV: "1"},
    )
    return True


# ---------------------------------------------------------------- entry point
def run_app():
    warn_if_apple_system_python()
    setup_logging()
    if TRAY_IMPORT_ERROR is not None and offer_tray_install(TRAY_IMPORT_ERROR):
        return 0
    hide_launching_terminal()
    if already_running():
        log.info("Already running, exiting")
        return 0
    log.info("Starting %s%s", __version__, "" if HAVE_TRAY else " (no tray)")
    App().mainloop()
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="tv_input_switcher.pyw",
        description="Switch TV and monitor inputs from the system tray. With no options, runs the app. "
                    "The options below control Windows monitors over DDC/CI; MONITOR is a number from "
                    "--list, part of a monitor's name, or 'all'.",
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--list", action="store_true", help="list monitors, their inputs and power state, then exit")
    group.add_argument(
        "--set", nargs=2, metavar=("MONITOR", "INPUT"),
        help="switch MONITOR to INPUT (e.g. hdmi1, dp2, usbc, 0x11), then exit",
    )
    group.add_argument(
        "--power", nargs=2, metavar=("MONITOR", "on|off"),
        help="turn MONITOR on or off, then exit",
    )
    group.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    args = parser.parse_args(argv)

    if args.list or args.set or args.power:
        if not IS_WIN:
            print("error: monitor control is only available on Windows", file=sys.stderr)
            return 2
        if args.list:
            return cli_list()
        if args.set:
            return cli_set_input(*args.set)
        return cli_set_power(*args.power)
    return run_app()


if __name__ == "__main__":
    mp.freeze_support()
    sys.exit(main())
