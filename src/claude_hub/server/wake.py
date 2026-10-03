"""Wake a computer over the network (Wake-on-LAN).

The hub server is always on. It sends the "magic packet" that a network
card listens for while its computer sleeps or is shut down: six bytes of
0xFF, then the hardware address of the card sixteen times.

The packet is a broadcast, so it only reaches computers in the same
network segment as the hub. It carries no secret: any device in that
network can send it.
"""

from __future__ import annotations

import json
import re
import socket
from typing import Any

MAC = re.compile(r"(?:[0-9a-f]{2}[:-]){5}[0-9a-f]{2}", re.IGNORECASE)
PORT = 9


def normalize(mac: str) -> str:
    """``AA-BB-CC-DD-EE-FF`` and ``aa:bb:...`` become ``aa:bb:cc:dd:ee:ff``. Empty when it is no address."""
    mac = (mac or "").strip()
    return mac.lower().replace("-", ":") if MAC.fullmatch(mac) else ""


def magic_packet(mac: str) -> bytes:
    address = normalize(mac)
    if not address:
        raise ValueError(f"Not a hardware address: {mac!r}")
    return b"\xff" * 6 + bytes.fromhex(address.replace(":", "")) * 16


def send(mac: str, broadcast: str = "255.255.255.255", port: int = PORT) -> None:
    packet = magic_packet(mac)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        # Network cards can miss a single packet while they wake up.
        for _ in range(3):
            sock.sendto(packet, (broadcast, port))


def adapters_of(machine: Any) -> list[dict[str, Any]]:
    """The network adapters that the collector of a machine reported."""
    try:
        found = json.loads(machine["adapters"] or "[]")
    except (ValueError, TypeError):
        return []
    return [item for item in found if isinstance(item, dict) and normalize(str(item.get("mac") or ""))]


def likely_adapter(adapters: list[dict[str, Any]]) -> str:
    """The address to wake by default: the one connected cable adapter, if that is clear.

    Wake-on-LAN works over a cable. Over Wi-Fi it mostly does not, so a
    wireless adapter is never picked without the person.
    """
    wired = [item for item in adapters if item.get("connected") and item.get("wired")]
    return normalize(str(wired[0]["mac"])) if len(wired) == 1 else ""
