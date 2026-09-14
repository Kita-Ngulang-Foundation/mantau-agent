"""mantau_agent — Scenario 2's agent half: runs on the customer's LAN, pulls
RTSP from the camera locally, and opens an OUTBOUND connection to the server
(see `../../protocol/PROTOCOL.md`). This is what survives CGNAT: an outbound
connection is never blocked, regardless of what NAT sits between the
customer and the internet.

    discovery/  find the camera on the LAN: ONVIF WS-Discovery, or a manual IP
    camera/     the RTSP handshake, SDP-based stream inspection, frame pulling
    detect/     frame decimation + the mantau_core Detector protocol
    uplink/     tunnel abstraction, spool-backed envelope client
    health/     periodic heartbeat, reusing the same uplink client
"""

__version__ = "0.1.0"
