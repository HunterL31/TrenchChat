"""
The port mapper's wire: what it sends a router and what it makes of the answer.

These cover the bytes only. Nothing here touches a router, because there is
none to touch: discovery, the SOAP round trip and the NAT-PMP round trip are
untested against real hardware, which trenchchat/network/ip/portmap.py says in
its own docstring and which stays true until somebody runs it at home.

The mapper itself is covered for the part that is testable without a gateway:
a node that cannot map anything asks on a schedule rather than on every tick,
and says so once.
"""

import socket
import struct

import pytest

from trenchchat.network.ip.portmap import (
    NATPMP_OP_EXTERNAL, NATPMP_OP_MAP_TCP, NATPMP_OP_MAP_UDP, RETRY_SECS,
    PortMapError, PortMapper, build_natpmp_request, build_soap_request,
    build_ssdp_search, parse_device_description, parse_natpmp_response,
    parse_soap_fault, parse_soap_response, parse_ssdp_response,
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


class TestPortMapper:
    """What a node with no gateway does, which is every node in this suite."""

    def test_a_port_of_zero_is_never_mapped(self, monkeypatch):
        asked = []
        monkeypatch.setattr("trenchchat.network.ip.portmap.add_mapping",
                            lambda *a, **k: asked.append(a))
        mapper = PortMapper(0)
        assert mapper.refresh() is None
        assert mapper.address() is None
        assert not asked

    def test_a_failure_is_not_retried_on_every_call(self, monkeypatch):
        attempts = []

        def _refuse(*_args, **_kwargs):
            attempts.append(1)
            raise PortMapError("no gateway")

        monkeypatch.setattr("trenchchat.network.ip.portmap.add_mapping", _refuse)
        mapper = PortMapper(42420)
        now = 1000.0
        assert mapper.refresh(now) is None
        assert mapper.refresh(now + 1) is None
        assert mapper.refresh(now + 30) is None
        assert len(attempts) == 1
        assert mapper.refresh(now + RETRY_SECS * 2 + 1) is None
        assert len(attempts) == 2

    def test_a_mapping_is_held_until_half_its_lease_has_passed(self, monkeypatch):
        attempts = []

        def _grant(*_args, **_kwargs):
            attempts.append(1)
            return {"method": "natpmp", "external_address": "203.0.113.7",
                    "external_port": 51820, "lifetime": 3600}

        monkeypatch.setattr("trenchchat.network.ip.portmap.add_mapping", _grant)
        mapper = PortMapper(42420)
        now = 1000.0
        mapping = mapper.refresh(now)
        assert mapper.address() == ("203.0.113.7", 51820)
        assert mapping.due_at() == now + 1800
        assert mapper.refresh(now + 1799) is mapping
        assert len(attempts) == 1
        mapper.refresh(now + 1801)
        assert len(attempts) == 2

    def test_an_answer_with_no_address_is_not_a_mapping(self, monkeypatch):
        monkeypatch.setattr(
            "trenchchat.network.ip.portmap.add_mapping",
            lambda *a, **k: {"method": "upnp", "external_address": ""})
        mapper = PortMapper(42420)
        assert mapper.refresh(1000.0) is None
        assert mapper.address() is None

    def test_releasing_without_a_mapping_asks_the_router_nothing(self, monkeypatch):
        asked = []
        monkeypatch.setattr("trenchchat.network.ip.portmap.delete_mapping",
                            lambda *a, **k: asked.append(a))
        PortMapper(42420).release()
        assert not asked
