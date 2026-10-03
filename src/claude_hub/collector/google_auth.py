"""Sign a Google account in for the hub.

The sign-in happens in the browser of this computer, in a window of Google.
This program never sees a password. It receives a code from Google on a
local port, exchanges it for a lasting sign-in, and hands that to the hub.

Standard library only.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import time
import urllib.error
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlencode, urlsplit

from .client import Client, HubError
from .config import Config

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
CLIENT_COPY = "google_client.json"
WAIT_SECONDS = 300

DONE_PAGE = ("<!doctype html><meta charset='utf-8'><title>Claude hub</title>"
             "<body style='font-family:sans-serif;margin:3rem'><h1>{title}</h1><p>{text}</p>")


class SignInError(Exception):
    pass


def read_client(path: Path) -> tuple[str, str]:
    """The client ID and secret from the JSON file of the Google Cloud console."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise SignInError(f"Cannot read {path}: {exc}") from exc
    except ValueError as exc:
        raise SignInError(f"{path} is not valid JSON.") from exc
    client = raw.get("installed") if isinstance(raw, dict) else None
    if not isinstance(client, dict) or not client.get("client_id") or not client.get("client_secret"):
        kind = "a web client" if isinstance(raw, dict) and "web" in raw else "not a client file"
        raise SignInError(f"{path} is {kind}. Create a client with the application type "
                          "'Desktop app' and download its JSON file.")
    return str(client["client_id"]), str(client["client_secret"])


def find_client(given: Path | None, config_path: Path) -> tuple[str, str]:
    """Read the client file. A given file is copied beside the configuration for later calls."""
    copy = config_path.parent / CLIENT_COPY
    if given is None:
        if not copy.exists():
            raise SignInError("Name the client file once: hub google-add LABEL --client PATH_TO_CLIENT_JSON")
        return read_client(copy)
    client = read_client(given)
    if given.resolve() != copy.resolve():
        copy.parent.mkdir(parents=True, exist_ok=True)
        copy.write_bytes(given.read_bytes())
        try:
            os.chmod(copy, 0o600)
        except OSError:
            pass
    return client


def pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode()).digest()
    return verifier, base64.urlsafe_b64encode(digest).decode().rstrip("=")


def auth_url(client_id: str, redirect_uri: str, scopes: list[str], state: str,
             challenge: str) -> str:
    return AUTH_URL + "?" + urlencode({
        "client_id": client_id, "redirect_uri": redirect_uri, "response_type": "code",
        "scope": " ".join(scopes), "state": state,
        "code_challenge": challenge, "code_challenge_method": "S256",
        # "offline" asks for a sign-in that lasts. "consent" makes Google hand
        # one out again when the account was connected before.
        "access_type": "offline", "prompt": "consent select_account",
    })


class _Listener(HTTPServer):
    answer: dict[str, str] | None = None
    state = ""


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - name set by the base class
        query = {key: values[0] for key, values in parse_qs(urlsplit(self.path).query).items()}
        server: _Listener = self.server  # type: ignore[assignment]
        if "code" not in query and "error" not in query:
            self.send_response(404)
            self.end_headers()
            return
        good = query.get("state") == server.state and "code" in query
        server.answer = query if query.get("state") == server.state else {"error": "wrong state"}
        page = DONE_PAGE.format(
            title="Signed in" if good else "Not signed in",
            text="Return to the terminal. You can close this window." if good
            else "Google did not grant access. Return to the terminal.")
        body = page.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: Any) -> None:
        pass


def post_form(url: str, fields: dict[str, str]) -> tuple[int, Any]:
    request = urllib.request.Request(url, data=urlencode(fields).encode(), method="POST")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, json.loads(response.read() or b"null")
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw or b"null")
        except ValueError:
            return exc.code, {"error": raw.decode("utf-8", "replace")[:300]}
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise SignInError(f"Google is not reachable: {exc}") from exc


def exchange(client_id: str, client_secret: str, code: str, redirect_uri: str, verifier: str,
             post: Callable[[str, dict[str, str]], tuple[int, Any]] = post_form) -> dict[str, Any]:
    status, payload = post(TOKEN_URL, {
        "grant_type": "authorization_code", "code": code, "client_id": client_id,
        "client_secret": client_secret, "redirect_uri": redirect_uri, "code_verifier": verifier})
    if status != 200 or not isinstance(payload, dict):
        detail = payload.get("error_description") or payload.get("error") if isinstance(payload, dict) else ""
        raise SignInError(f"Google refused the code ({status}): {detail}")
    if not payload.get("refresh_token"):
        raise SignInError("Google returned no lasting sign-in. Remove the app under Google Account > "
                          "Security > Your connections to third-party apps, then run the command again.")
    return payload


def add_account(cfg: Config, config_path: Path, label: str, client_file: Path | None,
                open_browser: Callable[[str], Any] = webbrowser.open,
                post: Callable[[str, dict[str, str]], tuple[int, Any]] = post_form,
                say: Callable[[str], None] = print, wait_seconds: int = WAIT_SECONDS) -> str:
    """Run the sign-in for one account and hand the result to the hub. Returns the address."""
    hub = Client(cfg.server_url, cfg.token, timeout=60)
    try:
        # Ask the hub first: it names the permissions, and a hub that is not
        # reachable should fail before the browser opens.
        _, known = hub.request("GET", "/api/v1/google/accounts")
    except HubError as exc:
        raise SignInError(f"The hub is not reachable: {exc}") from exc
    client_id, client_secret = find_client(client_file, config_path)
    verifier, challenge = pkce()
    listener = _Listener(("127.0.0.1", 0), _Handler)
    try:
        listener.state = secrets.token_urlsafe(24)
        listener.timeout = 1
        redirect_uri = f"http://127.0.0.1:{listener.server_address[1]}"
        url = auth_url(client_id, redirect_uri, list(known["scopes"]), listener.state, challenge)
        say(f"Sign in with the Google account for '{label}'. If no browser opens, open this address:")
        say(url)
        open_browser(url)
        deadline = time.time() + wait_seconds
        while listener.answer is None and time.time() < deadline:
            listener.handle_request()
    finally:
        listener.server_close()
    answer = listener.answer
    if answer is None:
        raise SignInError("No answer from the browser in time. Run the command again.")
    if "code" not in answer:
        raise SignInError(f"Google did not grant access: {answer.get('error', 'no reason given')}")
    tokens = exchange(client_id, client_secret, answer["code"], redirect_uri, verifier, post)
    try:
        _, account = hub.request("PUT", f"/api/v1/google/accounts/{label}", {
            "client_id": client_id, "client_secret": client_secret,
            "refresh_token": tokens["refresh_token"],
            "scopes": str(tokens.get("scope") or "").split()})
    except HubError as exc:
        raise SignInError(f"The hub did not take the sign-in: {exc}") from exc
    return str(account.get("email") or "")
