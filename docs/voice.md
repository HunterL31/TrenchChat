# Live Group Voice Chat

Discord/TeamSpeak-style live voice, without a server: every channel
implicitly has one voice room, and each participant streams directly to
every other participant. This document covers the architecture and the
contract the (reworked) frontend codes against.

## Architecture

Voice is split across two planes:

**Signalling plane (LXMF).** `trenchchat/core/voice.py`'s `VoiceManager`
sends and receives `voice_join` / `voice_leave` / `voice_state` control
messages, addressed with `compute_channel_recipients` like any other
channel broadcast. Every message asserts state about **the sender only** —
nobody relays third-party presence, so a forged message can at worst
misrepresent its own author. A joiner learns current occupants because
each participant, on seeing the join, unicasts one `voice_state` back
describing itself. The result is an eventually-consistent roster; it is a
presence *hint*, deliberately low-rate (one refresh per minute plus
join/leave/mute events) so it stays far under the router's control-message
rate limit.

**Frame plane (RNS Links).** `trenchchat/network/voice_transport.py`'s
`RNSVoiceTransport` owns a `trenchchat.voice` destination and a full mesh
of links: for each participant pair, the peer with the lexicographically
smaller identity hash dials the other's voice destination (the other side
falls back to dialing after 10 s, covering one-way reachability). The
initiator identifies itself on the link (`link.identify`) and sends a
`VP_HELLO` naming the channel; the responder checks the identified peer
against membership + the `voice_chat` permission before replying
`VP_ACCEPT`. Only then do audio frames flow — Opus, 20 ms frames bundled
two per packet, sent as unreliable link packets (`create_receipt=False`,
no retransmission; losses are concealed by the codec). Established,
identified links are the ground truth for who you actually hear. Wire
layouts live in `trenchchat/network/voice_wire.py`.

**Audio (`trenchchat/core/audio/`).** Capture and playback via
sounddevice/PortAudio, Opus via opuslib, receiver-side mixing via numpy.
Each remote sender gets a jitter buffer (80 ms target depth) and a
stateful Opus decoder; a gap in a stream decodes as packet-loss
concealment. Everything heavy is imported lazily: on a machine without
libopus/libportaudio the client still joins voice recv-silent and reports
why via `audio_status()`. Headless testenv workers use `TonePipeline`,
which drives the same encode/transmit path with a generated 440 Hz tone.

## Limits and expectations

- `MAX_VOICE_PARTICIPANTS = 8`. Full mesh means each speaker uploads
  (N−1) × ~20 kbps; at 8 participants that is ~140 kbps up while talking.
- Voice needs fast links (TCP/IP, Wi-Fi mesh). It is not viable over LoRa
  or packet radio; surface `link_quality.score_path` per peer in the UI
  rather than masking this.
- Expect roughly 150–300 ms mouth-to-ear on direct or few-hop paths.
- A kicked or demoted participant is cut off by the next re-authorization
  sweep (about a second), not just removed from the roster.
- Channels created before this feature have no `voice_chat` entry in
  their stored permissions, so non-owner members are denied voice until
  an owner re-saves the channel's permissions. This fails closed on
  purpose; owners always pass.

## Frontend contract

Construction (already wired in `main.py`):

```python
voice_transport = RNSVoiceTransport(identity)
voice_mgr = VoiceManager(identity, storage, router, subscription_mgr,
                         config, transport=voice_transport)
# plus a 1 s QTimer driving voice_mgr.tick()
```

API surface:

```python
voice_mgr.join_voice(channel_hash_hex) -> bool   # via actions.join_voice_channel
voice_mgr.leave_voice()                          # via actions.leave_voice_channel
voice_mgr.set_muted(muted)                       # via actions.set_voice_muted
voice_mgr.current_channel -> str | None
voice_mgr.is_muted -> bool
voice_mgr.get_roster(channel_hash_hex) -> list[dict]
#   {identity_hash, muted, joined_at, speaking,
#    link_state: "self" | "streaming" | "connecting" | "unreachable" | "signalled"}
voice_mgr.frame_stats() / voice_mgr.audio_status()

voice_mgr.add_roster_callback(cb)     # cb(channel_hash_hex)
voice_mgr.add_speaking_callback(cb)   # cb(channel_hash_hex, peer_hex, speaking)
voice_mgr.add_session_callback(cb)    # cb("joined" | "left" | "audio_error")
```

Rules for the GUI, same as every other manager:

- **Callbacks fire on background threads.** Emit Qt signals; never touch
  widgets directly from them (`.claude/rules/gui-conventions.md`).
- **Gate the join control** on `storage.has_permission(channel_hash,
  self_hex, VOICE_CHAT)` — that is the GUI layer of the three-layer
  enforcement (`.claude/rules/permission-enforcement.md`); the actions
  guard and core enforcement already exist.
- `"audio_error"` means the session is up but capture/playback failed
  (missing system library, no device); show recv-only/muted state, don't
  treat it as a failed join.
- "In voice but unreachable" is an honest state: signalling says a peer is
  present while no link can reach them. Show it (grayed entry) rather than
  hiding the peer.

Push-to-talk: set `config.voice_mode = "ptt"` and map the PTT key to
`set_muted(False)` while held, `set_muted(True)` on release. In `"vad"`
mode (default) transmission is gated on voice activity above
`config.voice_vad_threshold_db`.

Config keys (all under `"voice"` in `~/.trenchchat/config.json`):
`input_device`, `output_device`, `mode` ("vad"/"ptt"), `bitrate`
(16000/24000), `vad_threshold_db`.

## Testing

- `tests/test_voice.py` — signalling/roster over the in-process LXMF shim.
- `tests/test_voice_transport.py` — wire format + streaming semantics via
  `tests/fake_voice.py` (an injectable transport double whose `connect`
  runs the target's authorize callback, so core enforcement is exercised).
- `tests/test_voice_audio.py` — jitter buffer (always runs), mixer/Opus
  (skip cleanly without numpy/libopus).
- `tests/test_adversarial.py::TestAdversarialVoice` — unauthorized
  signalling and link attempts, revocation mid-call.
- `devtools/testenv/smoke_test.py` — the real-network proof: two OS
  processes over a real TCP Reticulum link do the full invite → sync →
  chat flow, then join voice, exchange tone frames over real identified
  links, and verify both directions streamed.

## Packaging follow-ups (not yet done)

- `trenchchat.spec` / `packaging/hooks/`: collect sounddevice's bundled
  PortAudio binaries; ship libopus (`opus.dll` / `libopus.dylib`) for
  opuslib on Windows/macOS.
- Linux packages: add `libopus0, libportaudio2` to Depends.
- `setup.sh`: hint at installing `libportaudio2`/`libopus0` when missing.
