"""
The direct IP session plane: QUIC between two peers who can reach each other.

Reticulum stays every peer's first path. When two of them can also reach each
other over IP, they open one authenticated QUIC session and the same messages
travel over it instead, faster and with an acknowledgement behind the word
"delivered". Nothing above network/base.py knows which path a message took.

certificate.py mints the certificate a peer pins, frames.py is the wire
format, session.py is one connection and the HELLO that authenticates it, and
transport.py is the Transport implementation Router routes through.
"""
