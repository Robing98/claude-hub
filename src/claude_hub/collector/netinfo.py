"""The network adapters of this machine, so that the hub can wake it over the network."""

from __future__ import annotations

import csv
import io
import subprocess
import sys
from pathlib import Path
from typing import Any

from .gitscan import _FLAGS

# Adapters that are no network card with a cable: wireless, virtual, tunnels.
NOT_WIRED = ("wi-fi", "wifi", "wlan", "wireless", "bluetooth", "virtual", "vmware", "hyper-v",
             "vethernet", "loopback", "tap-", "tunnel", "vpn", "wireguard", "tailscale", "docker")


def parse_getmac(text: str) -> list[dict[str, Any]]:
    """Read the output of ``getmac /FO CSV /V /NH`` on Windows."""
    found = []
    for row in csv.reader(io.StringIO(text)):
        if len(row) < 4 or row[2].count("-") != 5:
            continue
        name, device, mac, transport = (cell.strip() for cell in row[:4])
        label = f"{name} {device}".lower()
        found.append({
            "name": f"{name} ({device})" if device else name,
            "mac": mac.lower().replace("-", ":"),
            # A connected adapter names its device. A disconnected one carries
            # a text in the language of the system instead.
            "connected": transport.startswith("\\Device\\"),
            "wired": not any(word in label for word in NOT_WIRED),
        })
    return found


def linux_adapters(root: Path = Path("/sys/class/net")) -> list[dict[str, Any]]:
    found = []
    for folder in sorted(root.iterdir()) if root.is_dir() else []:
        try:
            mac = (folder / "address").read_text().strip()
            state = (folder / "operstate").read_text().strip()
        except OSError:
            continue
        # Only real hardware has a device entry. That leaves out lo, bridges, and tunnels.
        if not (folder / "device").exists() or mac == "00:00:00:00:00:00":
            continue
        found.append({"name": folder.name, "mac": mac.lower(), "connected": state == "up",
                      "wired": not (folder / "wireless").exists()})
    return found


def adapters() -> list[dict[str, Any]] | None:
    """The adapters, or None when this system has no known way to list them."""
    try:
        if sys.platform == "win32":
            done = subprocess.run(["getmac", "/FO", "CSV", "/V", "/NH"], capture_output=True,
                                  timeout=20, creationflags=_FLAGS)
            if done.returncode != 0:
                return None
            return parse_getmac(done.stdout.decode("oem", "replace"))
        if sys.platform.startswith("linux"):
            return linux_adapters()
    except (OSError, subprocess.SubprocessError, LookupError):
        return None
    return None
