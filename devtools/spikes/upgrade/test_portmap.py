"""Unit tests for the portmap spike's wire encoders and parsers.

    .venv/bin/python -m pytest devtools/spikes/upgrade/ -q

These cover only the bytes on the wire. Nothing here touches a router: the discovery,
SOAP and NAT-PMP network paths are untested against real hardware.
"""

import socket
import struct

import pytest

from portmap import (
    NATPMP_OP_EXTERNAL,
    NATPMP_OP_MAP_TCP,
    NATPMP_OP_MAP_UDP,
    PortMapError,
    build_natpmp_request,
    build_soap_request,
    build_ssdp_search,
    parse_device_description,
    parse_natpmp_response,
    parse_soap_fault,
    parse_soap_response,
    parse_ssdp_response,
)

DESCRIPTION_XML = b"""<?xml version="1.0"?>
<root xmlns="urn:schemas-upnp-org:device-1-0">
  <URLBase>http://192.168.1.1:5000/</URLBase>
  <device>
    <deviceType>urn:schemas-upnp-org:device:InternetGatewayDevice:1</deviceType>
    <deviceList><device>
      <deviceList><device>
        <serviceList>
          <service>
            <serviceType>urn:schemas-upnp-org:service:WANPPPConnection:1</serviceType>
            <controlURL>/ctl/PPPConn</controlURL>
          </service>
          <service>
            <serviceType>urn:schemas-upnp-org:service:WANIPConnection:1</serviceType>
            <controlURL>/ctl/IPConn</controlURL>
          </service>
        </serviceList>
      </device></deviceList>
    </device></deviceList>
  </device>
</root>
"""

FAULT_XML = b"""<?xml version="1.0"?>
<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/">
  <s:Body><s:Fault>
    <faultcode>s:Client</faultcode>
    <faultstring>UPnPError</faultstring>
    <detail>
      <UPnPError xmlns="urn:schemas-upnp-org:control-1-0">
        <errorCode>718</errorCode>
        <errorDescription>ConflictInMappingEntry</errorDescription>
      </UPnPError>
    </detail>
  </s:Fault></s:Body>
</s:Envelope>
"""

EXTERNAL_IP_XML = b"""<?xml version="1.0"?>
<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/">
  <s:Body>
    <u:GetExternalIPAddressResponse
        xmlns:u="urn:schemas-upnp-org:service:WANIPConnection:1">
      <NewExternalIPAddress>203.0.113.7</NewExternalIPAddress>
    </u:GetExternalIPAddressResponse>
  </s:Body>
</s:Envelope>
"""


def test_ssdp_search_is_a_well_formed_m_search():
    request = build_ssdp_search()
    lines = request.decode().split("\r\n")
    assert lines[0] == "M-SEARCH * HTTP/1.1"
    assert "HOST: 239.255.255.250:1900" in lines
    assert 'MAN: "ssdp:discover"' in lines
    assert "ST: urn:schemas-upnp-org:device:InternetGatewayDevice:1" in lines
    assert request.endswith(b"\r\n\r\n")


def test_ssdp_response_headers_are_lower_cased():
    reply = (b"HTTP/1.1 200 OK\r\nCACHE-CONTROL: max-age=120\r\n"
             b"LOCATION: http://192.168.1.1:5000/rootDesc.xml\r\n"
             b"ST: urn:schemas-upnp-org:device:InternetGatewayDevice:1\r\n\r\n")
    headers = parse_ssdp_response(reply)
    assert headers["location"] == "http://192.168.1.1:5000/rootDesc.xml"
    assert headers["cache-control"] == "max-age=120"


def test_ssdp_response_rejects_a_non_200_and_garbage():
    assert parse_ssdp_response(b"HTTP/1.1 404 Not Found\r\n\r\n") == {}
    assert parse_ssdp_response(b"\x00\x01\x02") == {}


def test_device_description_prefers_wan_ip_over_wan_ppp():
    service_type, control_url = parse_device_description(
        DESCRIPTION_XML, "http://192.168.1.1:5000/rootDesc.xml"
    )
    assert service_type == "urn:schemas-upnp-org:service:WANIPConnection:1"
    assert control_url == "http://192.168.1.1:5000/ctl/IPConn"


def test_device_description_falls_back_to_the_location_when_there_is_no_url_base():
    xml = DESCRIPTION_XML.replace(b"<URLBase>http://192.168.1.1:5000/</URLBase>", b"")
    _, control_url = parse_device_description(xml, "http://10.0.0.1:49152/desc.xml")
    assert control_url == "http://10.0.0.1:49152/ctl/IPConn"


def test_device_description_without_a_wan_service_is_an_error():
    xml = DESCRIPTION_XML.replace(b"WANIPConnection:1", b"Layer3Forwarding:1").replace(
        b"WANPPPConnection:1", b"Layer3Forwarding:1"
    )
    with pytest.raises(PortMapError):
        parse_device_description(xml, "http://192.168.1.1:5000/rootDesc.xml")


def test_soap_request_keeps_argument_order_and_sets_the_action_header():
    service_type = "urn:schemas-upnp-org:service:WANIPConnection:1"
    body, headers = build_soap_request(service_type, "AddPortMapping", [
        ("NewRemoteHost", ""),
        ("NewExternalPort", "4433"),
        ("NewProtocol", "UDP"),
        ("NewInternalPort", "4433"),
        ("NewInternalClient", "192.168.1.10"),
        ("NewEnabled", "1"),
        ("NewPortMappingDescription", "TrenchChat direct session"),
        ("NewLeaseDuration", "3600"),
    ])
    text = body.decode()
    assert headers["SOAPAction"] == f'"{service_type}#AddPortMapping"'
    assert headers["Content-Type"] == 'text/xml; charset="utf-8"'
    assert f'<u:AddPortMapping xmlns:u="{service_type}">' in text
    assert text.index("<NewExternalPort>") < text.index("<NewProtocol>")
    assert text.index("<NewProtocol>") < text.index("<NewInternalPort>")
    assert "<NewLeaseDuration>3600</NewLeaseDuration>" in text
    assert text.endswith("</s:Body></s:Envelope>")


def test_soap_response_returns_output_arguments():
    result = parse_soap_response(EXTERNAL_IP_XML, "GetExternalIPAddress")
    assert result == {"NewExternalIPAddress": "203.0.113.7"}


def test_soap_fault_is_read_as_a_upnp_error_code():
    assert parse_soap_fault(FAULT_XML) == (718, "ConflictInMappingEntry")
    assert parse_soap_fault(EXTERNAL_IP_XML) is None
    assert parse_soap_fault(b"not xml at all") is None


def test_soap_response_raises_on_a_fault():
    with pytest.raises(PortMapError, match="718"):
        parse_soap_response(FAULT_XML, "AddPortMapping")


def test_soap_response_without_the_expected_element_is_an_error():
    with pytest.raises(PortMapError, match="AddPortMappingResponse"):
        parse_soap_response(EXTERNAL_IP_XML, "AddPortMapping")


def test_natpmp_external_address_request_is_two_bytes():
    assert build_natpmp_request(NATPMP_OP_EXTERNAL) == b"\x00\x00"


def test_natpmp_map_request_matches_rfc_6886():
    request = build_natpmp_request(NATPMP_OP_MAP_UDP, 4433, 4433, 3600)
    assert len(request) == 12
    assert struct.unpack("!BBHHHI", request) == (0, 1, 0, 4433, 4433, 3600)
    assert struct.unpack("!BBHHHI", build_natpmp_request(NATPMP_OP_MAP_TCP, 1, 2, 3)) == (
        0, 2, 0, 1, 2, 3
    )


def test_natpmp_delete_is_a_map_with_no_external_port_and_no_lifetime():
    request = build_natpmp_request(NATPMP_OP_MAP_UDP, 4433, 0, 0)
    assert struct.unpack("!BBHHHI", request) == (0, 1, 0, 4433, 0, 0)


def test_natpmp_rejects_an_unknown_opcode():
    with pytest.raises(ValueError):
        build_natpmp_request(9)


def test_natpmp_external_address_response_is_parsed():
    reply = struct.pack("!BBHI", 0, 128, 0, 12345) + socket.inet_aton("203.0.113.7")
    parsed = parse_natpmp_response(reply)
    assert parsed["external_address"] == "203.0.113.7"
    assert parsed["epoch"] == 12345
    assert parsed["opcode"] == NATPMP_OP_EXTERNAL


def test_natpmp_mapping_response_is_parsed():
    reply = struct.pack("!BBHIHHI", 0, 129, 0, 999, 4433, 51820, 3600)
    parsed = parse_natpmp_response(reply)
    assert parsed["opcode"] == NATPMP_OP_MAP_UDP
    assert parsed["internal_port"] == 4433
    assert parsed["external_port"] == 51820
    assert parsed["lifetime"] == 3600


def test_natpmp_non_zero_result_is_an_error():
    with pytest.raises(PortMapError, match="not authorized"):
        parse_natpmp_response(struct.pack("!BBHIHHI", 0, 129, 2, 0, 0, 0, 0))


def test_natpmp_rejects_a_short_or_unmarked_reply():
    with pytest.raises(PortMapError, match="shorter"):
        parse_natpmp_response(b"\x00\x80")
    with pytest.raises(PortMapError, match="not marked"):
        parse_natpmp_response(struct.pack("!BBHI", 0, 1, 0, 0))
    with pytest.raises(PortMapError, match="short"):
        parse_natpmp_response(struct.pack("!BBHI", 0, 128, 0, 0))
