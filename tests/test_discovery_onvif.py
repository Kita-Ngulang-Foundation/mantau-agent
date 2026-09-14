"""build_probe_message and parse_probe_matches are pure -- no network needed.
`discover()` itself needs a real ONVIF camera on the LAN and is intentionally
not covered here; see that function's docstring.
"""

from mantau_agent.discovery.onvif import (
    OnvifDevice,
    build_probe_message,
    parse_probe_matches,
)

FIXTURE_PROBE_MATCH = """<?xml version="1.0" encoding="UTF-8"?>
<soap:Envelope xmlns:soap="http://www.w3.org/2003/05/soap-envelope"
               xmlns:wsa="http://schemas.xmlsoap.org/ws/2004/08/addressing"
               xmlns:wsdd="http://schemas.xmlsoap.org/ws/2005/04/discovery"
               xmlns:dn="http://www.onvif.org/ver10/network/wsdl">
  <soap:Header>
    <wsa:MessageID>uuid:aaaa</wsa:MessageID>
    <wsa:Action>http://schemas.xmlsoap.org/ws/2005/04/discovery/ProbeMatches</wsa:Action>
  </soap:Header>
  <soap:Body>
    <wsdd:ProbeMatches>
      <wsdd:ProbeMatch>
        <wsa:EndpointReference>
          <wsa:Address>urn:uuid:11111111-2222-3333-4444-555555555555</wsa:Address>
        </wsa:EndpointReference>
        <wsdd:Types>dn:NetworkVideoTransmitter</wsdd:Types>
        <wsdd:Scopes>onvif://www.onvif.org/type/video_encoder onvif://www.onvif.org/hardware/IPC-Model</wsdd:Scopes>
        <wsdd:XAddrs>http://192.168.1.42/onvif/device_service</wsdd:XAddrs>
        <wsdd:MetadataVersion>1</wsdd:MetadataVersion>
      </wsdd:ProbeMatch>
    </wsdd:ProbeMatches>
  </soap:Body>
</soap:Envelope>"""


def test_build_probe_message_targets_network_video_transmitters():
    msg = build_probe_message(message_id="fixed-id")
    text = msg.decode()
    assert "uuid:fixed-id" in text
    assert "dn:NetworkVideoTransmitter" in text
    assert "<d:Probe>" in text


def test_build_probe_message_generates_a_unique_id_by_default():
    assert build_probe_message() != build_probe_message()


def test_parse_probe_matches_extracts_xaddrs_types_and_scopes():
    devices = parse_probe_matches(FIXTURE_PROBE_MATCH)
    assert len(devices) == 1
    device = devices[0]
    assert device.xaddrs == ["http://192.168.1.42/onvif/device_service"]
    assert "NetworkVideoTransmitter" in device.types
    assert "video_encoder" in device.scopes


def test_parse_probe_matches_accepts_bytes_too():
    devices = parse_probe_matches(FIXTURE_PROBE_MATCH.encode("utf-8"))
    assert devices[0].xaddrs == ["http://192.168.1.42/onvif/device_service"]


def test_parse_probe_matches_empty_for_garbage_input():
    assert parse_probe_matches("not xml at all") == []
    assert parse_probe_matches(b"") == []


def test_onvif_device_host_extracted_from_first_xaddr():
    device = OnvifDevice(xaddrs=["http://192.168.1.42/onvif/device_service"])
    assert device.host == "192.168.1.42"


def test_onvif_device_host_none_without_any_xaddrs():
    assert OnvifDevice().host is None
