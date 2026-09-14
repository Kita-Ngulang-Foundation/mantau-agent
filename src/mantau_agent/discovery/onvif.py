"""ONVIF WS-Discovery -- multicast probe to find cameras on the LAN.

Split deliberately: `build_probe_message()` and `parse_probe_matches()` are
pure functions, testable without a network. `discover()` is the actual UDP
multicast send/receive, which needs a real network with multicast enabled
and at least one ONVIF-capable camera on it to return anything -- not
exercised by the test suite for that reason.

**What this does NOT do.** WS-Discovery only finds a camera's *device
service* endpoint (the XAddr). Getting the actual RTSP stream URI the
formally-correct ONVIF way requires a further SOAP exchange against the
Media service -- `GetCapabilities` -> `GetProfiles` -> `GetStreamUri`, with
WS-Security digest auth -- which is real additional protocol surface, not
implemented here. `OnvifDevice.host` is a best-effort extraction from the
XAddr, used to hand off to the same separate-path convention
(`CameraRef.paths`, e.g. `/stream1` / `/stream2`) the manual-config path
uses, not a true ONVIF-negotiated stream URI. Good enough to locate a
camera automatically; not a substitute for the full Media service exchange
if a camera's actual stream paths turn out not to follow that convention.
"""

from __future__ import annotations

import re
import socket
import uuid
from dataclasses import dataclass, field

WS_DISCOVERY_ADDRESS = ("239.255.255.250", 3702)

_PROBE_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<e:Envelope xmlns:e="http://www.w3.org/2003/05/soap-envelope"
            xmlns:w="http://schemas.xmlsoap.org/ws/2004/08/addressing"
            xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery"
            xmlns:dn="http://www.onvif.org/ver10/network/wsdl">
  <e:Header>
    <w:MessageID>uuid:{message_id}</w:MessageID>
    <w:To>urn:schemas-xmlsoap-org:ws:2005:04:discovery</w:To>
    <w:Action>http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe</w:Action>
  </e:Header>
  <e:Body>
    <d:Probe>
      <d:Types>dn:NetworkVideoTransmitter</d:Types>
    </d:Probe>
  </e:Body>
</e:Envelope>"""


def build_probe_message(*, message_id: str | None = None) -> bytes:
    """A WS-Discovery Probe for ONVIF NetworkVideoTransmitter devices."""
    mid = message_id or str(uuid.uuid4())
    return _PROBE_TEMPLATE.format(message_id=mid).encode("utf-8")


@dataclass
class OnvifDevice:
    xaddrs: list[str] = field(default_factory=list)
    types: str = ""
    scopes: str = ""

    @property
    def host(self) -> str | None:
        """Best-effort host extraction from the first XAddr -- see this
        module's docstring for what this is NOT (a real ONVIF media-profile
        stream URI)."""
        if not self.xaddrs:
            return None
        match = re.match(r"https?://([^/:]+)", self.xaddrs[0])
        return match.group(1) if match else None


_XADDRS_RE = re.compile(r"<[\w:]*XAddrs>(.*?)</[\w:]*XAddrs>", re.IGNORECASE | re.DOTALL)
_TYPES_RE = re.compile(r"<[\w:]*Types>(.*?)</[\w:]*Types>", re.IGNORECASE | re.DOTALL)
_SCOPES_RE = re.compile(r"<[\w:]*Scopes[^>]*>(.*?)</[\w:]*Scopes>", re.IGNORECASE | re.DOTALL)


def parse_probe_matches(response: bytes | str) -> list[OnvifDevice]:
    """Extract XAddrs/Types/Scopes from a WS-Discovery ProbeMatch response.

    Regex, not a strict XML parser, on purpose: real ONVIF devices are
    inconsistent about namespace prefixes (`d:`, `wsdd:`, none at all), and a
    parser that insists on one prefix breaks on real hardware more often
    than a permissive tag-name match does. This is parsing LAN multicast
    responses from a device the operator already put on their own network,
    not untrusted internet input, which is what makes that trade acceptable
    here.
    """
    text = response.decode("utf-8", errors="replace") if isinstance(response, bytes) else response
    xaddrs_match = _XADDRS_RE.search(text)
    if not xaddrs_match:
        return []
    xaddrs = xaddrs_match.group(1).split()
    types_match = _TYPES_RE.search(text)
    scopes_match = _SCOPES_RE.search(text)
    return [OnvifDevice(
        xaddrs=xaddrs,
        types=types_match.group(1).strip() if types_match else "",
        scopes=scopes_match.group(1).strip() if scopes_match else "",
    )]


def discover(*, timeout_s: float = 3.0) -> list[OnvifDevice]:
    """Send one WS-Discovery Probe and collect ProbeMatch responses for
    `timeout_s`. Real socket I/O -- see the module docstring for why this
    isn't covered by the test suite the way the two functions above are.
    """
    message = build_probe_message()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.settimeout(timeout_s)
    devices: list[OnvifDevice] = []
    try:
        sock.sendto(message, WS_DISCOVERY_ADDRESS)
        while True:
            try:
                data, _addr = sock.recvfrom(65535)
            except socket.timeout:
                break
            devices.extend(parse_probe_matches(data))
    finally:
        sock.close()
    return devices
