"""Phase 0 spike: UPnP-IGD and NAT-PMP port mapping with nothing but the standard library.

A node behind a NAT can sometimes ask the router for an inbound port, which becomes a
`mapped` candidate in the upgrade offer. Two protocols cover almost every home router:
UPnP-IGD (SSDP discovery, then SOAP over HTTP) and NAT-PMP (a 12-byte UDP request to the
default gateway). Both are best effort: no gateway, no answer, or a refusal all mean the
node simply has no mapped candidate, and the punch has to work without one.

Run it against a real router with:

    python portmap.py probe
    python portmap.py map --port 4433 --protocol UDP --lifetime 3600
    python portmap.py unmap --port 4433 --protocol UDP

This file has never been run against real router hardware. The wire encoders and
parsers are unit-tested in test_portmap.py; the network paths are not.
"""

import argparse
import json
import socket
import struct
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ElementTree

SSDP_ADDRESS = "239.255.255.250"
SSDP_PORT = 1900
SSDP_TARGET = "urn:schemas-upnp-org:device:InternetGatewayDevice:1"
SSDP_MX_SECS = 2
SSDP_TIMEOUT_SECS = 3.0
SOAP_TIMEOUT_SECS = 5.0
SOAP_ENVELOPE_NS = "http://schemas.xmlsoap.org/soap/envelope/"
UPNP_DEVICE_NS = "urn:schemas-upnp-org:device-1-0"
UPNP_CONTROL_SERVICES = (
    "urn:schemas-upnp-org:service:WANIPConnection:2",
    "urn:schemas-upnp-org:service:WANIPConnection:1",
    "urn:schemas-upnp-org:service:WANPPPConnection:1",
)
MAPPING_DESCRIPTION = "TrenchChat direct session"
DEFAULT_LEASE_SECS = 3600

NATPMP_PORT = 5351
NATPMP_VERSION = 0
NATPMP_OP_EXTERNAL = 0
NATPMP_OP_MAP_UDP = 1
NATPMP_OP_MAP_TCP = 2
NATPMP_RESPONSE_FLAG = 128
NATPMP_TIMEOUT_SECS = 0.25
NATPMP_ATTEMPTS = 4
NATPMP_RESULTS = {
    0: "success",
    1: "unsupported version",
    2: "not authorized",
    3: "network failure",
    4: "out of resources",
    5: "unsupported opcode",
}


class PortMapError(Exception):
    """A mapping attempt failed. The caller has no mapped candidate and carries on."""


def build_ssdp_search(target: str = SSDP_TARGET, mx: int = SSDP_MX_SECS) -> bytes:
    """Build the SSDP M-SEARCH datagram that asks gateways on this link to answer."""
    lines = [
        "M-SEARCH * HTTP/1.1",
        f"HOST: {SSDP_ADDRESS}:{SSDP_PORT}",
        'MAN: "ssdp:discover"',
        f"MX: {mx}",
        f"ST: {target}",
        "",
        "",
    ]
    return "\r\n".join(lines).encode()


def parse_ssdp_response(data: bytes) -> dict[str, str]:
    """Parse an SSDP reply into lower-cased headers. An unparsable reply yields an empty dict."""
    text = data.decode(errors="replace")
    lines = text.split("\r\n")
    if not lines or not lines[0].upper().startswith("HTTP/1.1 200"):
        return {}
    headers = {}
    for line in lines[1:]:
        name, sep, value = line.partition(":")
        if sep:
            headers[name.strip().lower()] = value.strip()
    return headers


def parse_device_description(xml: bytes, location: str) -> tuple[str, str]:
    """Find the WAN connection service in a device description; return its type and control URL."""
    root = ElementTree.fromstring(xml)
    base = root.findtext(f"{{{UPNP_DEVICE_NS}}}URLBase") or location
    found: dict[str, str] = {}
    for service in root.iter(f"{{{UPNP_DEVICE_NS}}}service"):
        service_type = (service.findtext(f"{{{UPNP_DEVICE_NS}}}serviceType") or "").strip()
        control_url = (service.findtext(f"{{{UPNP_DEVICE_NS}}}controlURL") or "").strip()
        if service_type in UPNP_CONTROL_SERVICES and control_url:
            found[service_type] = urllib.parse.urljoin(base, control_url)
    for service_type in UPNP_CONTROL_SERVICES:
        if service_type in found:
            return service_type, found[service_type]
    raise PortMapError("device description carries no WAN connection service")


def build_soap_request(service_type: str, action: str,
                       arguments: list[tuple[str, str]]) -> tuple[bytes, dict[str, str]]:
    """Build the SOAP body and headers for one IGD action."""
    body = "".join(f"<{name}>{value}</{name}>" for name, value in arguments)
    envelope = (
        '<?xml version="1.0"?>'
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
        's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
        "<s:Body>"
        f'<u:{action} xmlns:u="{service_type}">{body}</u:{action}>'
        "</s:Body></s:Envelope>"
    ).encode()
    headers = {
        "Content-Type": 'text/xml; charset="utf-8"',
        "SOAPAction": f'"{service_type}#{action}"',
        "Connection": "close",
    }
    return envelope, headers


def parse_soap_fault(xml: bytes) -> tuple[int, str] | None:
    """Return the UPnP error code and description from a SOAP fault, or None if it is not one."""
    try:
        root = ElementTree.fromstring(xml)
    except ElementTree.ParseError:
        return None
    fault = root.find(f".//{{{SOAP_ENVELOPE_NS}}}Fault")
    if fault is None:
        return None
    for error in fault.iter():
        if error.tag.endswith("UPnPError"):
            code = 0
            description = ""
            for child in error:
                if child.tag.endswith("errorCode"):
                    code = int((child.text or "0").strip())
                elif child.tag.endswith("errorDescription"):
                    description = (child.text or "").strip()
            return code, description
    return 0, "unspecified SOAP fault"


def parse_soap_response(xml: bytes, action: str) -> dict[str, str]:
    """Return the output arguments of an IGD action response, raising on a fault."""
    fault = parse_soap_fault(xml)
    if fault is not None:
        raise PortMapError(f"IGD refused {action}: error {fault[0]} {fault[1]}")
    root = ElementTree.fromstring(xml)
    for element in root.iter():
        if element.tag.endswith(f"{action}Response"):
            return {child.tag.split("}")[-1]: (child.text or "") for child in element}
    raise PortMapError(f"no {action}Response in the IGD reply")


def build_natpmp_request(opcode: int, internal_port: int = 0, external_port: int = 0,
                         lifetime: int = 0) -> bytes:
    """Build a NAT-PMP request: two bytes to ask for the external address, twelve to map."""
    if opcode == NATPMP_OP_EXTERNAL:
        return struct.pack("!BB", NATPMP_VERSION, opcode)
    if opcode not in (NATPMP_OP_MAP_UDP, NATPMP_OP_MAP_TCP):
        raise ValueError(f"unsupported NAT-PMP opcode {opcode}")
    return struct.pack("!BBHHHI", NATPMP_VERSION, opcode, 0, internal_port, external_port,
                       lifetime)


def parse_natpmp_response(data: bytes) -> dict:
    """Parse a NAT-PMP reply into its fields, raising on a short packet or a non-zero result."""
    if len(data) < 4:
        raise PortMapError("NAT-PMP reply shorter than a header")
    version, opcode, result = struct.unpack("!BBH", data[:4])
    if not opcode & NATPMP_RESPONSE_FLAG:
        raise PortMapError("NAT-PMP reply is not marked as a response")
    request_opcode = opcode - NATPMP_RESPONSE_FLAG
    if result != 0:
        raise PortMapError(f"NAT-PMP result {result}: "
                           f"{NATPMP_RESULTS.get(result, 'unknown')}")
    parsed: dict = {"version": version, "opcode": request_opcode, "result": result}
    if request_opcode == NATPMP_OP_EXTERNAL:
        if len(data) < 12:
            raise PortMapError("NAT-PMP external address reply is short")
        parsed["epoch"] = struct.unpack("!I", data[4:8])[0]
        parsed["external_address"] = socket.inet_ntoa(data[8:12])
        return parsed
    if len(data) < 16:
        raise PortMapError("NAT-PMP mapping reply is short")
    epoch, internal_port, external_port, lifetime = struct.unpack("!IHHI", data[4:16])
    parsed["epoch"] = epoch
    parsed["internal_port"] = internal_port
    parsed["external_port"] = external_port
    parsed["lifetime"] = lifetime
    return parsed


def local_address_towards(host: str) -> str:
    """The local address the kernel would use to reach a host, without sending anything."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.connect((host, 9))
        return probe.getsockname()[0]


def default_gateway() -> str:
    """The IPv4 default gateway from the kernel routing table. Linux only."""
    try:
        with open("/proc/net/route") as handle:
            rows = handle.read().splitlines()
    except OSError as exc:
        raise PortMapError(f"cannot read the routing table: {exc}") from exc
    for row in rows[1:]:
        fields = row.split()
        if len(fields) > 2 and fields[1] == "00000000":
            packed = struct.pack("<I", int(fields[2], 16))
            return socket.inet_ntoa(packed)
    raise PortMapError("no IPv4 default route")


def discover_igd(timeout: float = SSDP_TIMEOUT_SECS) -> tuple[str, str]:
    """Find an IGD on this link and return its service type and control URL."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_IP, socket.IP_MULTICAST_TTL, 2)
    sock.settimeout(timeout)
    locations: list[str] = []
    try:
        sock.sendto(build_ssdp_search(), (SSDP_ADDRESS, SSDP_PORT))
        while True:
            try:
                data, _ = sock.recvfrom(4096)
            except socket.timeout:
                break
            headers = parse_ssdp_response(data)
            if headers.get("location"):
                locations.append(headers["location"])
    except OSError as exc:
        raise PortMapError(f"SSDP search failed: {exc}") from exc
    finally:
        sock.close()
    if not locations:
        raise PortMapError(f"no IGD answered SSDP within {timeout} seconds")
    for location in locations:
        try:
            with urllib.request.urlopen(location, timeout=SOAP_TIMEOUT_SECS) as response:
                description = response.read()
            return parse_device_description(description, location)
        except (urllib.error.URLError, OSError, ElementTree.ParseError, PortMapError):
            continue
    raise PortMapError("every IGD that answered refused or failed its description fetch")


def soap_call(control_url: str, service_type: str, action: str,
              arguments: list[tuple[str, str]]) -> dict[str, str]:
    """Send one SOAP action to an IGD control URL and return its output arguments."""
    body, headers = build_soap_request(service_type, action, arguments)
    request = urllib.request.Request(control_url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=SOAP_TIMEOUT_SECS) as response:
            return parse_soap_response(response.read(), action)
    except urllib.error.HTTPError as exc:
        payload = exc.read()
        fault = parse_soap_fault(payload)
        if fault is not None:
            raise PortMapError(f"IGD refused {action}: error {fault[0]} {fault[1]}") from exc
        raise PortMapError(f"IGD answered {action} with HTTP {exc.code}") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise PortMapError(f"IGD unreachable for {action}: {exc}") from exc


def upnp_add_mapping(port: int, protocol: str, lifetime: int = DEFAULT_LEASE_SECS) -> dict:
    """Ask an IGD for an inbound mapping to this node's port."""
    service_type, control_url = discover_igd()
    gateway_host = urllib.parse.urlparse(control_url).hostname or SSDP_ADDRESS
    internal_client = local_address_towards(gateway_host)
    soap_call(control_url, service_type, "AddPortMapping", [
        ("NewRemoteHost", ""),
        ("NewExternalPort", str(port)),
        ("NewProtocol", protocol),
        ("NewInternalPort", str(port)),
        ("NewInternalClient", internal_client),
        ("NewEnabled", "1"),
        ("NewPortMappingDescription", MAPPING_DESCRIPTION),
        ("NewLeaseDuration", str(lifetime)),
    ])
    external = soap_call(control_url, service_type, "GetExternalIPAddress", [])
    return {
        "method": "upnp",
        "service_type": service_type,
        "control_url": control_url,
        "internal_client": internal_client,
        "external_address": external.get("NewExternalIPAddress", ""),
        "external_port": port,
        "lifetime": lifetime,
    }


def upnp_delete_mapping(port: int, protocol: str) -> dict:
    """Remove a mapping this node asked an IGD for."""
    service_type, control_url = discover_igd()
    soap_call(control_url, service_type, "DeletePortMapping", [
        ("NewRemoteHost", ""),
        ("NewExternalPort", str(port)),
        ("NewProtocol", protocol),
    ])
    return {"method": "upnp", "deleted_port": port, "protocol": protocol}


def natpmp_call(gateway: str, request: bytes) -> bytes:
    """Send one NAT-PMP request, retrying with the doubling delay RFC 6886 asks for."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    delay = NATPMP_TIMEOUT_SECS
    try:
        for _ in range(NATPMP_ATTEMPTS):
            sock.settimeout(delay)
            try:
                sock.sendto(request, (gateway, NATPMP_PORT))
                data, source = sock.recvfrom(64)
            except socket.timeout:
                delay *= 2
                continue
            except OSError as exc:
                raise PortMapError(f"NAT-PMP send failed: {exc}") from exc
            if source[0] == gateway:
                return data
    finally:
        sock.close()
    raise PortMapError(f"gateway {gateway} did not answer NAT-PMP")


def natpmp_add_mapping(port: int, protocol: str, lifetime: int = DEFAULT_LEASE_SECS) -> dict:
    """Ask the default gateway for a NAT-PMP mapping to this node's port."""
    gateway = default_gateway()
    opcode = NATPMP_OP_MAP_UDP if protocol.upper() == "UDP" else NATPMP_OP_MAP_TCP
    external = parse_natpmp_response(
        natpmp_call(gateway, build_natpmp_request(NATPMP_OP_EXTERNAL))
    )
    mapping = parse_natpmp_response(
        natpmp_call(gateway, build_natpmp_request(opcode, port, port, lifetime))
    )
    return {
        "method": "natpmp",
        "gateway": gateway,
        "external_address": external["external_address"],
        "external_port": mapping["external_port"],
        "internal_port": mapping["internal_port"],
        "lifetime": mapping["lifetime"],
    }


def natpmp_delete_mapping(port: int, protocol: str) -> dict:
    """Remove a NAT-PMP mapping by asking for it again with a zero lifetime."""
    gateway = default_gateway()
    opcode = NATPMP_OP_MAP_UDP if protocol.upper() == "UDP" else NATPMP_OP_MAP_TCP
    parse_natpmp_response(natpmp_call(gateway, build_natpmp_request(opcode, port, 0, 0)))
    return {"method": "natpmp", "gateway": gateway, "deleted_port": port, "protocol": protocol}


def probe(port: int, protocol: str, lifetime: int) -> dict:
    """Try both protocols and report what each one did, without raising on a failure."""
    report: dict = {}
    for name, attempt in (("natpmp", natpmp_add_mapping), ("upnp", upnp_add_mapping)):
        try:
            report[name] = attempt(port, protocol, lifetime)
        except PortMapError as exc:
            report[name] = {"method": name, "failed": str(exc)}
    return report


def main(argv: list[str] | None = None) -> int:
    """Parse the command line and run one mapping action against the local router."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("action", choices=["probe", "map", "unmap", "discover"])
    parser.add_argument("--port", type=int, default=4433)
    parser.add_argument("--protocol", default="UDP", choices=["UDP", "TCP"])
    parser.add_argument("--lifetime", type=int, default=DEFAULT_LEASE_SECS)
    parser.add_argument("--method", default="auto", choices=["auto", "upnp", "natpmp"])
    args = parser.parse_args(argv)

    try:
        if args.action == "probe":
            print(json.dumps(probe(args.port, args.protocol, args.lifetime), indent=2))
            return 0
        if args.action == "discover":
            service_type, control_url = discover_igd()
            print(json.dumps({"service_type": service_type, "control_url": control_url},
                             indent=2))
            return 0
        if args.action == "map":
            if args.method == "upnp":
                result = upnp_add_mapping(args.port, args.protocol, args.lifetime)
            elif args.method == "natpmp":
                result = natpmp_add_mapping(args.port, args.protocol, args.lifetime)
            else:
                try:
                    result = natpmp_add_mapping(args.port, args.protocol, args.lifetime)
                except PortMapError:
                    result = upnp_add_mapping(args.port, args.protocol, args.lifetime)
        elif args.method == "natpmp":
            result = natpmp_delete_mapping(args.port, args.protocol)
        else:
            result = upnp_delete_mapping(args.port, args.protocol)
    except PortMapError as exc:
        print(json.dumps({"failed": str(exc)}), file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
