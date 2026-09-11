# Phase 0 spike: the direct IP session

Evidence for the decisions `docs/ip-transport-plan.md` Phase 2 depends on. Everything
here is throwaway code that must run, not a library: nothing under `trenchchat/` imports
it, and it is deleted when the work it informs lands.

Measured on Linux 6.18 x86_64, CPython 3.11.15, aioquic 1.3.0, cryptography 46.0.5,
PyInstaller 6.22.2, in a container with no IPv6 and CAP_NET_ADMIN, on 2026-09-11.

## Recommendation: QUIC via aioquic

Take QUIC. Every check the plan set for `aioquic` passed. Certificate pinning works
through `QuicConfiguration.cadata`, a relaying third process is refused at the TLS
handshake before any application byte, unreliable datagrams work in both directions, the
one-file PyInstaller bundle runs against an unbundled peer, and the release ships abi3
wheels covering win_amd64, macOS x86_64 and arm64, and manylinux x86_64 and aarch64 for
every CPython from 3.10 up, under BSD-3-Clause. One thing `aioquic` will not do:
request or expose a client certificate through public API, so a session cannot be
mutually authenticated at the TLS layer. That costs less than it sounds, because the
design already authenticates with RNS identity keys in the HELLO, and the HELLO is what
a member list, an author signature and a permissions table all key on. The nonce design
below closes the gap the missing client certificate opens.

The price is throughput and CPU. On loopback, aioquic moved 50 MB in 3.5 to 5.1 seconds
(9.8 to 14.5 MB/s, 82 to 121 Mbit/s) and spent 3.1 to 3.3 CPU seconds on the receiving
side doing it, which is one core saturated. Stdlib TCP with TLS 1.3 moved the same 50 MB over
the same loopback in 0.05 to 0.07 seconds (700 to 1040 MB/s) for 0.09 to 0.12 CPU
seconds: roughly fifty times the throughput for a thirtieth of the CPU per byte. That
gap is real and it is the strongest argument the fallback has. It does not change the
decision, because 100 Mbit/s is above almost every home uplink this feature will ever
see, and the point of the direct path is to turn a 200 MB file from hours into seconds,
not into milliseconds. What the fallback would cost instead is two sockets, no
connection migration, and an AEAD datagram format written here rather than taken from a
reviewed implementation, which is the one kind of work the Zen's "trust the math" says
not to take on. Revisit the number in Phase 4 on a slow laptop and a phone-class CPU,
where one saturated core is a worse trade than it is here.

## quic_session.py

```bash
.venv/bin/python devtools/spikes/upgrade/quic_session.py demo
.venv/bin/python devtools/spikes/upgrade/quic_session.py baseline --peer-file /tmp/b.json
```

`demo` mints three peers, runs a server, a client and a relay as separate processes, and
prints one report. The roles also run by hand; `quic_session.py --help` lists them.

### What aioquic 1.3.0 exposes, precisely

- **Client certificates: not supported through public API.** `aioquic.tls.Context` has
  `_request_client_certificate`, underscore-private, default `False`, and its definition
  carries the comment `# For test purposes only`. There is no `QuicConfiguration` field
  for it. A server therefore never asks for a client certificate and never sees one.
- **The peer certificate: not exposed publicly.** It is at
  `QuicConnection.tls._peer_certificate` (and `_peer_certificate_chain`). `tls` is a
  public-looking attribute that is created in `QuicConnection._initialize`, not in
  `__init__`, and the certificate under it is underscore-private. `QuicConnection` has
  two public properties, `configuration` and `original_destination_connection_id`, and
  neither reaches the certificate.
- **A TLS keying-material exporter: absent.** There is no `export_keying_material`
  anywhere in the package, and no exporter label handling in `tls.py`. Channel binding
  is therefore not available at all.
- **`cadata` takes PEM, not DER.** `tls.verify_certificate` passes `cadata` to
  `load_pem_x509_certificates`, so the DER that rides in `F_UPGRADE_CERT` has to be
  re-encoded as PEM before it is set. Setting `cadata` also stops aioquic loading
  certifi's defaults, which is what makes the peer's certificate the connection's *only*
  trust root. Setting `server_name = None` skips hostname verification entirely; the
  verifier only checks a name when one is set.
- **`aioquic.asyncio.connect` is unusable on a host without IPv6.** It hardcodes
  `local_host = "::"` and opens an `AF_INET6` dual-stack socket, which raises
  `OSError: [Errno 97] Address family not supported by protocol` here. The spike builds
  its own datagram endpoint instead, which Phase 2 has to do anyway: the session has to
  run on the socket the punch opened, not on one aioquic chose.
- **Connection migration works on the receiving side.** `QuicConnection` keeps
  `_network_paths` and validates a new peer address with PATH_CHALLENGE and
  PATH_RESPONSE, so a peer that moves from Wi-Fi to LTE keeps its session. There is no
  public API for a node to deliberately migrate its own socket.
- **Unreliable datagrams** need `max_datagram_frame_size` on both configurations and are
  sent with `QuicConnection.send_datagram_frame`, both public.

### The HELLO, and what it proves

Because the server never sees a client certificate, the HELLO carries one. The exchange
on the first bidirectional stream is:

1. The client opens the stream with an empty `hi`.
2. The server answers `challenge {nonce}`, sixteen fresh random bytes per connection.
3. The client sends `hello {pub64, cert, ts, sig}` where `cert` is its own session
   certificate DER and `sig` is its RNS identity's Ed25519 signature over
   `sha256(own cert DER) || sha256(peer cert DER) || nonce || ts`.
4. The server checks `sha256(pub64)[:16]` against the identity it expects, checks `ts`
   is within 60 seconds, checks the signature, and checks eligibility. It answers with
   its own `hello {pub64, ts, sig}` over the same digest with the roles swapped.

**What this proves.** The server's certificate is pinned, so the QUIC handshake already
proved the far end holds that certificate's private key and the channel is confidential
to it. The nonce therefore never leaves the true pair. A valid signature over it proves
that whoever is on the client end of *this connection* holds the private key behind
`pub64`, and that they made the assertion for this connection rather than replaying an
old one. Both directions end up authenticated: the client by the pin plus the server's
signature, the server by the client's signature over its own nonce.

**What it does not prove.** The certificate the client puts in its HELLO is an
assertion, nothing more. Nothing in the TLS handshake binds it to the endpoint, because
nothing asked for it. It is useful only as the pin for a later connection in the other
direction, where that node is the listener, and it must be treated as a claim, not as a
verified fact. Signing over its fingerprint costs nothing and means the claim cannot be
swapped in transit, which is the whole of its value.

### Measured, `demo` on loopback

| Check | Result |
|---|---|
| Mutually authenticated session | ok, both sides verified the other's identity hash |
| Handshake, connect to HandshakeCompleted | 14 to 38 ms over eight runs, typically 16 to 18 ms |
| 50 MB on one bidirectional stream | 3.48 to 5.10 s, 9.8 to 14.5 MB/s, 82 to 121 Mbit/s |
| Receiver CPU for those 50 MB | 3.07 to 3.31 s, so the transfer is CPU bound, not link bound |
| Unreliable datagrams, 200 each way at 800 bytes | 200 of 200 seen by the peer and echoed in seven runs of eight; 172 of 200 in the eighth, dropped in the send burst, which is the point of a datagram |
| TCP with TLS 1.3 from the stdlib, same 50 MB | 0.048 to 0.070 s, 715 to 1044 MB/s, 0.087 to 0.121 CPU s |

The TCP baseline runs both ends in one process over `127.0.0.1`, with the sender on a
thread, so it shares a CPU with its own receiver. If anything it understates the gap.

### The relay, and the four ways it is refused

The `relay` role mints its own identity and certificate, re-originates a connection to
the real server, and listens for the client.

| Attempt | Result |
|---|---|
| Client connects to the relay's address with the server's certificate pinned | refused at the TLS handshake, `ConnectionError: self-signed certificate` from the certificate store, before any application byte |
| Relay replays the client's captured HELLO to the server | refused, `signature does not verify`: the signature covers the server's previous nonce, not this connection's |
| Relay claims the client's `pub64` and signs with its own identity key | refused, `signature does not verify`: the public key in the HELLO is the one the signature is checked against |
| Relay connects honestly as itself | refused, `ineligible identity`: it is not in the server's allow list, which stands in for the members-table query |

The server records every refusal, so the four rows above are read back out of its report
file rather than inferred from a silence.

## portmap.py and test_portmap.py

```bash
.venv/bin/python -m pytest devtools/spikes/upgrade/ -q      # 19 tests, 0.06 s
.venv/bin/python devtools/spikes/upgrade/portmap.py probe
```

UPnP-IGD (SSDP discovery, then SOAP `AddPortMapping`, `DeletePortMapping` and
`GetExternalIPAddress`) and NAT-PMP (RFC 6886), stdlib only: `socket`, `struct`,
`urllib`, `xml.etree`.

**It has never run against real router hardware.** There is no router on this machine and
no way to put one here. The unit tests cover the bytes on the wire only: the M-SEARCH
datagram, SSDP header parsing, device-description parsing including the URLBase and
LOCATION fallback and the WANIPConnection over WANPPPConnection preference, the SOAP
envelope and its argument order and `SOAPAction` header, SOAP fault decoding to a UPnP
error code, and every NAT-PMP request and response shape including the delete form and
the short, unmarked and non-zero-result rejections. Discovery, the SOAP round trip and
the NAT-PMP round trip are untested against anything.

With no gateway it fails fast and cleanly, which is the behaviour Phase 3 needs: a node
with no mapped candidate must carry on with the candidates it has. Measured here,
`portmap.py probe` returns in about 6 seconds total, `natpmp` giving up after four
doubling retries against a gateway that never answers and `upnp` after a 3 second SSDP
window. Both report a one-line reason and neither raises.

## netns_nat.sh and punch.py

```bash
sudo PYTHON=/path/to/.venv/bin/python devtools/spikes/upgrade/netns_nat.sh
sudo ... devtools/spikes/upgrade/netns_nat.sh cone
sudo ... devtools/spikes/upgrade/netns_nat.sh symmetric
```

Four namespaces, three veth pairs: two peers each behind their own nftables masquerading
NAT, with a directly connected `198.51.100.0/24` standing in for the internet. The cone
variant masquerades normally; the symmetric variant masquerades `fully-random`, so the
external port cannot be predicted from the candidate.

**It ran here.** The container is uid 0 with CAP_NET_ADMIN in `CapEff`, `unshare -n`
works, `ip netns add` and `ip link add ... type veth` work, and `nft` has nat and filter
hooks. The one missing piece was iproute2, which is not installed in the base image;
`apt-get install iproute2` fixed it. The script's preflight names whichever of those
steps fails, so a container that refuses says which refusal it was.

Five consecutive runs of both variants: cone punched both ways in 0.20 s every time,
two probes each; symmetric failed both ways every time, 80 probes each over 8 seconds,
with no probe and no acknowledgement received by either peer. Five for five.

**The finding worth keeping.** The first version of the harness failed the cone case
every time, and the reason is not obvious. A probe that reaches a NAT before that NAT has
made its own outbound mapping leaves an unreplied conntrack entry holding exactly the
tuple the mapping is about to want. `nf_nat` sees the tuple as taken and remaps to a
random external port, and from then on neither side's predicted candidate is right and
no ordering of probes recovers. Real consumer NATs drop an unsolicited inbound packet
rather than recording it, so the harness now drops `ct state new,invalid` arriving on
the WAN interface, which happens before conntrack confirms the entry and therefore never
creates it. With that rule the punch completes in 200 ms. Two things follow for Phase 3:
the observed-address exchange (`F_UPGRADE_OBSERVED`) is not a nicety, it is the only
recovery from a NAT that has remapped a port, and a punch that fails should be recorded
with its reason rather than retried blindly into a poisoned mapping.

`punch.py` takes a list of candidates and reports the first pair seen both ways, where
both ways means a probe arrived from an address *and* an acknowledgement echoing this
peer's own nonce arrived from the same address. Each run includes the peer's LAN address
as a candidate as well as its public one, and the unreachable LAN candidate is ignored
without upsetting the punch, which is what the eight-candidate list in the design needs.

## pyinstaller_check.sh

```bash
devtools/spikes/upgrade/pyinstaller_check.sh
```

Builds `quic_session.py` as a one-file bundle and runs the bundled binary as the client
against an unbundled server.

**It bundles cleanly.** No hook, no hidden import, no added data file. The build emitted
one warning, `Library Iphlpapi required via ctypes not found`, which is a Windows library
RNS looks for and is unrelated to aioquic. The bundled client completed the handshake,
authenticated, pulled 10 MB at 13.8 MB/s and exchanged 50 datagrams each way against an
unbundled server, and exited 0.

Collected into the bundle: `aioquic/_crypto.abi3.so` at 6.8 MiB, `aioquic/_buffer.abi3.so`
at 22 KiB, `cryptography/hazmat/bindings/_rust.abi3.so` at 12.2 MiB, `_cffi_backend` at
324 KiB, and `libssl.so.3` and `libcrypto.so.3` pulled in by CPython's own `_ssl`, not by
aioquic. `_crypto.abi3.so` links no external OpenSSL at all (`ldd` shows only libc and
libpthread), so aioquic's OpenSSL is static and there is nothing extra to ship. The whole
one-file spike binary is 18 MB; since the release already bundles RNS and cryptography,
the marginal cost of adding aioquic is about 6.8 MiB. `pylsqpack` is not collected,
because it is only needed for HTTP/3 and nothing here imports it.

This proves Linux only. A one-file build is per platform, and macOS and Windows still
have to be built on a machine of their own.

### aioquic 1.3.0 on PyPI

License **BSD-3-Clause**, `requires-python >=3.10`, dependencies `certifi`,
`cryptography>=42.0.0`, `pylsqpack>=0.3.3,<0.4.0`, `pyopenssl>=24`,
`service-identity>=24.1.0`.

Every wheel is `cp310-abi3`, so one wheel per platform covers CPython 3.10, 3.11, 3.12
and 3.13. The eleven wheels are macosx_10_9_x86_64, macosx_11_0_arm64,
manylinux_2_26/2_28 x86_64 and aarch64, manylinux_2_28_i686, musllinux_1_2 x86_64,
aarch64 and i686, win32, win_amd64 and win_arm64. Every platform this project builds for
is covered, for both 3.11 and 3.12, with no source build anywhere.

One dependency note for `requirements.txt`: aioquic's `pyopenssl>=24` and
`service-identity>=24.1.0` both float ahead of `cryptography==46.0.5`, and the current
releases of each demand `cryptography>=49` and `>=47`. pip resolves this correctly by
backtracking to pyOpenSSL 26.2.0 and service-identity 24.2.0, which is the combination
everything above was measured on. It does mean a `cryptography` bump and an `aioquic`
bump are now the same decision.

## What could not be checked here

- **UPnP-IGD and NAT-PMP against real hardware.** No router, no way to reach one. The
  encoders and parsers are unit-tested; nothing else is. This is the largest open item
  in Phase 0.
- **The PyInstaller bundle on macOS and Windows.** Linux only here. The wheel matrix says
  the dependency is available on both; it does not say the one-file build works there.
- **Punch success rates on real NATs and CGNAT.** The namespace harness models a
  port-restricted cone NAT and an unpredictable-port NAT. It says nothing about how many
  real pairs fall into each case, which only a real deployment answers.
- **Throughput on anything but this machine.** One container, loopback, one CPU class.
  Phase 4 has to re-measure on a laptop and over a real link before the direct-path
  limits in the plan are called done.
