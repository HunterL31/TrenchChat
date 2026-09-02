# File sharing in private channels: implementation plan

Status: plan, not yet built. Delete this file once the work lands, folding the
trust model and the rejected alternatives into docstrings and
`docs/security-improvements.md` (see `.claude/rules/docs-worth-committing.md`).

## What is being built

A member of an invite-only channel attaches any file to a message. Every other
member sees a file card (name, size, state) in the transcript and can download
it. The file reaches them whether or not the sender is still online, provided
some member who holds it is. Nothing new is stored for anyone by anyone who is
not a member, and no peer outside the member list can fetch a byte of it.

Scope for the first release: invite-only channels only. Open-join channels and
direct messages are follow-ups (see "Deliberately out of scope").

## Zen check

The seven checks from `.claude/rules/reticulum-zen.md`, applied before the design:

1. **No center.** Whose absence breaks a download? Nobody's, once one other
   member has downloaded the file: every member who holds the bytes serves
   them, the same shape as sync. Until then the sender is the only holder,
   exactly as they are for a text message before anyone has received it.
2. **Hostile peers.** The file is content-addressed: the message carries its
   SHA-256, the author's signature covers that hash, and a receiver verifies
   downloaded bytes against it before storing. A holder can refuse or stall
   but cannot substitute. Serving is gated on the requester identifying on
   the link and being in the channel's stored member list. Every inbound
   size is capped and every serve is rate limited.
3. **Every byte costs.** Large files are never pushed. A message carries a
   manifest (name, size, hash: under 200 bytes); the bytes move only when a
   member asks, once per member, from one holder. Small files ride inline so
   they cost no extra link handshake. Sync relays manifests, not bytes, for
   anything above the inline ceiling.
4. **Store and forward.** A download whose holders are all offline is queued
   and retried when a member announces (`PeerAnnounceHandler`), never failed
   on a timer. A stalled transfer is what fails, not a slow one, and what it
   loses is one chunk, not the download: progress is persisted and resumes
   from any holder (see "Interrupted links and resume").
5. **Identity, not location.** Holders are identity hashes; the file plane
   destination is derived from the holder's identity. Nothing is keyed on an
   interface or a path.
6. **Intent, not medium.** The inline ceiling is a size, not a link type.
   Nothing branches on bandwidth; the LoRa scenario run is what sets the
   constants.
7. **Not neutral.** A holder learns which member downloaded which file. That
   is the same information the member list already gives every member, and
   nothing leaves the member set. Recorded below as the trust model.

## Reticulum and LXMF pieces reused

| Need | Existing piece | Where it already lives here |
|---|---|---|
| Carry a small file with the message | LXMF fields, auto-promoted to an RNS Resource above one packet; the router's 1000 KB per-transfer limit | `F_IMAGE_DATA` in `core/messaging.py`, capped by `image.MAX_IMAGE_BYTES` |
| Pull a large file from a peer | `RNS.Link` + `Destination.register_request_handler` + `Link.request(max_response_size, progress_callback)`; file responses stream as `RNS.Resource` (segmented, compressed, progress) | `network/node_transport.py` (nomad `/file/` serving and fetching) |
| Authenticate the requester | `Link.identify()`; the handler receives `remote_identity` | `node_transport.identify` and the six-argument `_serve` signature |
| Authenticate the holder | The link handshake proves the destination identity | every outbound Link here |
| Bound inbound work | per-link serve rate limit, inbound link cap, served-size cap | `NODE_SERVE_RATE_LIMIT`, `MAX_INBOUND_NODE_LINKS`, `MAX_SERVED_RESPONSE_BYTES` |
| Retry when a peer returns | `PeerAnnounceHandler.on_peer_appeared` | drives sync and pending flush today |
| Who is reachable now | `PresenceManager.get_online_for_channel` | `core/presence.py` |
| Bind the attachment to its author | `protocol.author_digest` + `authorship.sign_message` | already covers `image_data` |
| Relay history to a returning member | sync response rows with a byte budget | `sync._row_to_dict`, `MAX_RESPONSE_BYTES` |
| Store bytes at rest under the PIN lock | SQLite blob under SQLCipher, LRU prune by bytes | `nomad_file_cache`, `Storage.prune_nomad_files` |
| Hand a file to the browser | streamed response, `Content-Disposition`, `nosniff` | `GET /nomad/file/{node}` in `devtools/testenv/api.py` |
| Fetch progress to the client | WebSocket event per state change, content over REST | `nomad_fetch` event |
| Pick a file in the client | `file_picker` (already a dependency) | `flutter_ui/lib/attachments.dart` |

Nothing new is invented at the transport layer. The only new wire surface is
four envelope fields and one request path.

## Design

### One message shape, two transfer paths

A file message is an ordinary channel message (no `F_MSG_TYPE`) with a file
manifest in the envelope. New fields in a `0x90-0x9F` "File" range of
`core/protocol.py`:

| Field | Type | Meaning |
|---|---|---|
| `F_FILE_NAME` | str | display name, cleaned to a bare printable basename, max 128 chars |
| `F_FILE_SIZE` | int | byte length of the file |
| `F_FILE_HASH` | bytes[32] | SHA-256 of the file bytes; the file's address everywhere |
| `F_FILE_DATA` | bytes | the bytes themselves, present only when `size <= MAX_INLINE_FILE_BYTES` |
| `F_FILE_CHUNK_ROOT` | bytes[32] | SHA-256 over the concatenated SHA-256s of each `FILE_CHUNK_BYTES` chunk; present only on the pull path, and what makes resume safe |

- **Inline path** (`size <= MAX_INLINE_FILE_BYTES`): the bytes travel in the
  message, exactly as an image does today. They ride pending retry, missed
  delivery hints and sync unchanged. Receiver checks `sha256(data) == hash`
  and `len(data) == size` and strips the file (clearing the signature, as
  `image_stripped` does) on mismatch or when over the cap.
- **Pull path** (larger, up to `MAX_SHARED_FILE_BYTES`): the message carries
  the manifest only. A member downloads by asking a holder over the file
  plane below. Sync relays the manifest, never the bytes.

Constants, all in one place and all to be re-checked at `--link-profile
lora_fast` before the feature is called done:

| Constant | Proposed | Why |
|---|---|---|
| `MAX_INLINE_FILE_BYTES` | 256 KB | below the image ceiling; a file cannot be downscaled, so the push cost is the whole file times the member count |
| `MAX_SHARED_FILE_BYTES` | 5 MB | matches `node_browser.MAX_FILE_BYTES` and `MAX_SERVED_RESPONSE_BYTES`, the limits nomad file serving already runs under |
| `MAX_FILE_NAME_CHARS` | 128 | matches `node_transport.MAX_FILENAME_LEN` |
| `FILE_STORE_MAX_BYTES` | 256 MB | LRU budget for received files; own uploads are exempt so the sender never stops being a holder by accident |
| `FILE_CHUNK_BYTES` | 64 KB | the unit of verification and of lost work on a dropped link; about seven minutes on a 1.2 kbps LoRa link |
| `FILE_REQUEST_MAX_CHUNKS` | 16 | most chunks one request may ask for (1 MB, one RNS Resource segment) |
| `FILE_STALL_SECS` | 120 s | no progress on an in-flight request for this long ends the request, not the download |
| `PARTIAL_FILE_TTL_SECS` | `SYNC_WINDOW_SECS` | a partial download nobody has resumed in this long is dropped |

### Author signature

`protocol.author_digest` gains the file: when a manifest is present, append
`file_hash`, the chunk root, and the length-prefixed name and size to the
digest input. A
message without a file hashes exactly as today, so text and image messages
stay verifiable by older peers. A file message from a newer peer fails
verification on an older one and is dropped; that is the same outcome as
today's oversized-image case and better than showing the text with its
attachment silently missing. The signature covers the hash, not the bytes, so
a downloaded copy is verified by hash alone and the signature never has to be
re-checked after a pull.

### Storage

- `messages` gains `file_name TEXT`, `file_size INTEGER`, `file_hash TEXT`,
  `file_stripped INTEGER` (migration in `_migrate_*` style).
- New table `channel_files(hash TEXT PRIMARY KEY, content BLOB NOT NULL,
  size INTEGER, stored_at REAL, last_used REAL, own INTEGER)`.
  Content-addressed, so the same file shared twice or in two channels is
  stored once, and a pull from any holder is verified against the key.
- Bytes live in SQLite, not on disk: the database is already the thing the
  PIN lock encrypts, and it already has a byte-budget LRU prune to copy
  (`prune_nomad_files`). Files on disk would sit outside the lockbox.
- `Storage.file_channels(hash)` answers "which channels was this file shared
  in", which is what the serve handler authorises against.
- New table `file_parts(hash TEXT, index INTEGER, content BLOB NOT NULL,
  received_at REAL, PRIMARY KEY (hash, index))`: verified chunks of a
  download still in progress. Survives a restart; assembled into
  `channel_files` when the last chunk lands, then deleted. Pruned by
  `PARTIAL_FILE_TTL_SECS` and, under store pressure, before complete files.
- Serving a slice reads `substr(content, offset + 1, length)` so a 5 MB blob
  is never loaded whole to answer a 64 KB request.

### File plane: `network/file_transport.py`

A copy of `node_transport.py`'s shape (dial ladder, per-node queue, idle link
teardown, serve rate limit, inbound link cap, injectable base class for
tests), on a new destination aspect `APP_ASPECT_FILES = "files"` under
`APP_NAME`. Factor the shared pieces (`_NodeConn`, backoff, `_clean_filename`
into `core/fileutils.clean_filename`) rather than duplicating them.

- **One request path**, `/tc/file`, registered with `ALLOW_ALL` at the RNS
  level; the request `data` is `{"h": hash, "i": first chunk index, "n":
  chunk count}` for bytes, or `{"h": hash, "l": 1}` for the chunk-hash list.
  RNS's `allowed_list` is a static identity list per handler and membership
  is per channel and changes, so authorisation is done in the handler, not
  by RNS. A response is plain `bytes` (the slice, or the concatenated chunk
  hashes), which RNS sends as one Resource when it exceeds a packet.
- **Requester identifies** on the link (`Link.identify`) before the first
  request. Unlike nomad browsing this is not optional and needs no policy:
  the holder is a fellow member who already has the requester's identity
  from the member list.
- **Serve handler** (core enforcement, the layer that holds against a peer
  calling in directly), in order:
  1. rate limit per link (`_allow_request` shape);
  2. `remote_identity` present, else refuse;
  3. file held locally, else refuse (indistinguishable from step 4 on the
     wire, deliberately: a non-member learns nothing about what exists);
  4. some channel this file was shared in is invite-only and has
     `remote_identity` in its **stored** member list (`Storage.is_member`);
  5. concurrent outbound resources under `MAX_CONCURRENT_SERVES` (2), else
     refuse; the requester retries later. Airtime is shared.
  Refusals return `None`, which RNS answers with nothing; every refusal is
  logged at warning with the identities involved, per scenario rule 2.
- **Fetch**: `fetch(fetch_id, holder_hex, file_hash, first, count)` with
  `max_response_size = count * FILE_CHUNK_BYTES`, so an oversized answer is
  refused by RNS before it is buffered. The timeout is a **stall** timeout
  (no progress callback for `FILE_STALL_SECS`), never a total: a 5 MB
  transfer on LoRa takes hours and is not an error. A stall or a closed link
  fails that one request; the download keeps its verified chunks.
- The file plane bypasses LXMF, so none of `Router`'s throttles see it; its
  bounds are its own, as nomad's are.

### Downloads: `core/files.py` (`FileManager`)

Owns the download lifecycle, holder choice, and the store; never touches
LXMF fields (messaging does that).

- `share(channel, name, bytes)`: validates size and name, computes the hash,
  stores the file as `own`, returns the manifest for `Messaging.send_message`.
- `request_download(channel, message_id)`: queues a download for that
  message's manifest. States: `queued`, `fetching` (with progress), `done`,
  `unavailable` (no holder answered; retried on announce), `failed` (hash
  mismatch or refused by every holder).
- **Holder order**: the last peer who served this file, then the sender,
  then other channel members, each filtered through
  `PresenceManager.get_online_for_channel` and tried in last-seen order. A
  "not found" from a holder moves to the next; at most
  `MAX_HOLDER_ATTEMPTS` (4) per round. No "I hold this" control message is
  sent: a miss costs one link handshake, an announce would cost every
  member a message for every download.
- **Verify before store**: every chunk is checked against the chunk-hash
  list, itself checked against the signed chunk root, before it is written
  to `file_parts`; the assembled file is checked against `F_FILE_HASH`
  before it becomes a `channel_files` row. A bad chunk is dropped, the
  holder that served it is skipped for this file, and the next holder is
  tried from the same offset.
- **Retry on return**: `on_peer_appeared` re-runs any `unavailable` download
  whose channel that peer is in. No timer.
- **Everyone who downloads becomes a holder**: the stored row makes the
  serve handler answer for it, with no registration step.
- `prune()` on the existing ticker cadence: LRU over non-own rows to
  `FILE_STORE_MAX_BYTES`.

### Interrupted links and resume

What Reticulum already does, and what it does not:

- **Packet loss inside one transfer** is RNS's job. A Resource retries
  missing parts up to `MAX_RETRIES` (16) with a per-part timeout scaled to
  the measured link rate and a window that shrinks on loss. Nothing to
  build; the LoRa scenario is where this gets exercised.
- **A dead link** is noticed by the Resource's part timeouts long before the
  link's own staleness (`RNS.Link.STALE_TIME` is 720 s). When it dies, RNS
  cancels every Resource on it and deletes the partial response file it was
  appending to under `RNS.Reticulum.resourcepath`. **A response Resource
  never resumes across links**: a 5 MB file answered as one request that
  drops at segment four of five starts again from zero. On the medium this
  project targets that makes large files undeliverable, so the file plane
  does not ask for whole files.

What the plan adds, all on the requester side plus one slice lookup on the
holder:

1. **Chunked requests.** A download is a sequence of requests for chunk
   ranges. The requester starts at one chunk and doubles the count on each
   success up to `FILE_REQUEST_MAX_CHUNKS`, halving on any failure: it
   measures the link rather than detecting it. On a fast link the whole
   file goes in a handful of requests; on LoRa each request is small enough
   that a drop costs minutes, not hours.
2. **Per-chunk verification.** The first request to any holder is the
   chunk-hash list, checked against the manifest's signed `F_FILE_CHUNK_ROOT`
   (32 bytes on the message; the list itself is at most 2.5 KB and is fetched
   once). Every chunk is then verified on arrival. Without this, resuming
   from a second holder would mean trusting bytes nobody has signed until the
   very end, and a hostile member could waste an entire transfer with one
   bad chunk discovered last.
3. **Persisted progress.** Verified chunks go to `file_parts` as they land.
   A process restart, a PIN lock, or a phone reboot mid-download loses
   nothing: on start, `FileManager` rebuilds every unfinished download from
   `file_parts` as `unavailable` and it resumes on the next announce.
4. **Resume from any holder.** Chunks are addressed by (hash, index), so the
   next request can go to a different holder than the last. Holder A drops
   off at chunk 30; B announces; the download continues from chunk 30 on B.
   The holder order is re-evaluated at every request boundary, not once per
   download.
5. **One request in flight per download, one download in flight per node.**
   Downloads queue behind each other. Airtime is shared, and two parallel
   downloads on one LoRa link finish later than the same two in sequence.
6. **What the user sees.** Progress is chunks verified over chunks total, so
   it never goes backwards; a dropped link shows as "waiting for a member"
   with the bar where it was, not as an error.

The **inline path** has no resume and needs none: the message is one LXMF
Resource at most `MAX_INLINE_FILE_BYTES` long, LXMF retries delivery up to
`MAX_DELIVERY_ATTEMPTS` (5), and after that the ordinary pending queue, hints
and sync carry it. Keeping that ceiling small is what keeps this true.

### Messaging and sync

- `Messaging.send_message` takes an optional manifest (+ inline bytes) and
  writes the four fields; `_store_chat_message` reads them with the usual
  bytes/str coercion, checks name, size, hash shape, and the inline cap, and
  strips on any failure the way images are stripped.
- Inbound gate is unchanged: a file message from a non-member or a member
  without `SEND_MESSAGE` is dropped where text is (`_on_lxmf_message`).
- `sync._row_to_dict` adds the manifest; inline bytes ride only when the row
  holds them, and `row_wire_size` counts them so `MAX_RESPONSE_BYTES` holds.
- Direct-delivery and sync inbound paths share the same strip logic, as
  they do for images (`_insert_chat_message`).

### Permission: `SHARE_FILES`

A file is a message, so `SEND_MESSAGE` is the floor. A separate
`SHARE_FILES` permission lets an admin keep a channel text-only for some
roles, and the three-layer recipe in `.claude/rules/permission-enforcement.md`
is cheap to apply. Default granted to admin and member in both presets.

| Layer | Where |
|---|---|
| Client gate | attach-file control hidden when the role lacks it |
| Outbound guard | `actions.send_message` refuses a manifest without it (`no_share_permission`) |
| Core enforcement | `_on_lxmf_message` drops a message carrying a manifest from a sender without it |

Downloading needs membership only. Serving is not a permission: a holder
serves members because it is a member.

### API (`devtools/testenv/api.py`) and events

- `POST /channels/{hash}/messages` gains `file_name` + `file_data_b64`
  (mutually exclusive with `image_data_b64`). `MAX_REQUEST_BYTES` rises to
  8 MB so a 5 MB file fits base64-encoded; one endpoint, no multipart
  dependency.
- `GET /channels/{hash}/messages` rows gain `file: {name, size, hash, state,
  progress}` or null.
- `POST /channels/{hash}/files/{file_hash}/fetch` starts or joins a
  download; `GET .../fetch` reads its state.
- `GET /channels/{hash}/files/{file_hash}` streams the bytes with
  `Content-Disposition: attachment` and `X-Content-Type-Options: nosniff`,
  copied from `get_nomad_file`; 404 until held.
- WS event `file_fetch {channel, file_hash, message_id, state, progress,
  reason}`, the `nomad_fetch` shape. Content never rides the socket.

### Flutter (`flutter_ui/`)

- `attachments.dart`: `pickFileAttachment()` with `FileType.any` and a
  `maxAttachmentBytes` of 5 MB (the size gate stays before the read).
  `PickedAttachment` gains `kind` (image | file); images keep today's path.
- `compose_bar.dart`: a second picker action; the staged chip shows the
  file name and size.
- `message_list.dart`: a file card widget showing name, size, and one
  state: a Download button (inline or available), a progress bar
  (downloading), a Save button (done), "No member online has this yet"
  (unavailable), or "attachment refused" (stripped). Save opens the
  `GET .../files/{hash}` URL, the mechanism the nomad file download uses.
- `app_state.dart` + `client.dart`: `shareFile`, `fetchFile`, file state
  from `file_fetch` events; widget tests with `fake_backend.dart`.

## Trust model (to fold into `docs/security-improvements.md`)

- **What a member learns.** Every member sees every manifest. A holder learns
  which members downloaded which file from it, and when. Members already
  hold each other's identities via the member list; this adds "who fetched
  what" inside that set and nothing outside it.
- **What a non-member learns.** Nothing: the request path is hashed on the
  wire, the reply to an unauthorised or unknown request is silence, and the
  manifest never leaves the channel's encrypted messages.
- **What a holder can do.** Refuse, stall, or serve wrong bytes. The first
  two move the requester to the next holder at the same offset; the third is
  caught by the chunk hash on arrival and that holder is skipped, at the
  cost of one chunk of airtime. A holder cannot forge a file into a channel:
  the manifest, chunk root included, is signed by the author.
- **What a sender can do.** Share a manifest whose bytes nobody has. Members
  see "unavailable" until a holder appears; this is the same as a message
  nobody received, and is not treated as an attack.
- **Bounds.** Inbound: inline cap, manifest field caps, `max_response_size`
  from the chunk count, stall timeout per request, one request in flight
  per download, partial TTL, LRU store budget. Outbound: serve rate limit
  per link, inbound link cap,
  concurrent serve cap, served size cap.

## Rejected alternatives

- **Always push, raise LXMF's `delivery_limit`.** LXMF can move larger
  resources, but a push sends the whole file to every member whether they
  want it or not, and the receiver cannot decline an inbound resource the
  router has agreed to. Fails checks 2 and 3.
- **Always pull, no inline tier.** Cleaner (one path), but a 20 KB file would
  cost every member a link handshake and lose the offline path text already
  has (hints, sync). The inline tier is the image path that already exists.
- **Propagation nodes for files.** Channel messages are never propagated,
  nodes cap messages at 256 KB, and a node holding channel files for members
  is exactly the storing-for-others role check 1 says must stay weak.
- **"I hold this" announcements.** Costs every member a control message per
  download to save the requester one failed handshake. Presence and
  last-served memory get most of the benefit for free.
- **Files on disk under `~/.trenchchat/`.** Faster for large files, but
  outside the SQLCipher lockbox and outside the one prune policy. Revisit if
  `MAX_SHARED_FILE_BYTES` ever grows past what a blob column is happy with.
- **Whole-file requests, resume by restart.** The first draft of this plan.
  RNS's response Resources do not survive a link change, so every drop
  restarted a transfer that takes hours on LoRa: fails checks 3 and 4.
- **Chunking without a chunk root** (whole-file hash only, blame holders on
  a final mismatch). Saves 32 bytes per file message, but a hostile member
  can then spoil a whole transfer with one chunk and the requester cannot
  tell which holder did it until the end. The 32 bytes buy per-chunk
  verification and safe multi-holder resume.
- **Per-file request paths** (nomad's `/file/<name>` shape). Needs a
  register/deregister on every store and prune; one path with the hash in
  `data` needs none and is no less private, since paths are hashed anyway.

## Deliberately out of scope for the first release

- **Open-join channels.** No member table to authorise a serve against.
  Inline-only sharing there is a small follow-up (the outbound guard refuses
  a manifest on an open-join channel until then).
- **Direct messages.** A DM is plain LXMF so Sideband can read it; the inline
  tier maps onto LXMF's `FIELD_FILE_ATTACHMENTS` (`[name, bytes]`) for
  interop, and the pull tier works only TrenchChat to TrenchChat. Separate
  PR, separate interop test.
- **Deleting or expiring shared files.** The LRU budget bounds the store;
  an explicit "remove" is a later feature.
- **Fetching chunks from several holders at once.** Faster on a fast mesh,
  and the chunk scheme allows it, but it doubles the airtime a download can
  take from a shared link. Sequential first; measure before adding.

## Steps

Each step is a pytest-green commit; the scenarios in step 8 are what proves
the feature, per `.claude/rules/scenario-testing.md`.

1. **Protocol and digest.** Fields `0x90-0x94` in `core/protocol.py`,
   `chunk_root()` beside `author_digest`,
   registry docstring in `messaging.py`, `author_digest` extension,
   `clean_filename` moved to `core/fileutils.py`. Tests: `test_authorship.py`
   (file covered; text digest unchanged), `test_protocol_envelope.py`.
2. **Storage.** Migration, `channel_files` and `file_parts` tables, slice
   read, own-exempt LRU prune, partial TTL, `file_channels`. Tests:
   `test_storage.py`.
3. **Inline path end to end.** `Messaging` send/receive/strip, sync relay,
   `actions.send_message` guard. Tests: `test_messaging.py`,
   `test_sync.py`, `test_adversarial.py` (bad hash, oversized inline,
   non-member send).
4. **File plane.** `network/file_transport.py` with base class and
   `tests/fake_file_transport.py` (the `fake_node.py` shape). Tests:
   `test_file_transport.py` (dial, queue, chunk-range requests, stall
   timeout, refusal cases, rate limit, concurrent serve cap).
5. **FileManager.** Share, download lifecycle, holder order, chunk-list and
   per-chunk verify, window growth and halving, resume from `file_parts`
   after restart, resume on a second holder, retry on announce, prune; wired
   in `backend_core.py` after presence, ticked with the node browser.
   Tests: `test_files.py` (including: link dropped mid-download keeps the
   verified chunks; restart resumes at the same index; a holder that goes
   away is replaced at the request boundary), plus
   `test_adversarial.py::TestFileServeGate` (non-member, unidentified, a
   bad chunk from one holder is dropped and the rest come from another, a
   wrong chunk list is refused against the root, unknown hash).
6. **`SHARE_FILES` permission.** All three layers, presets, permissions
   editor; `test_permissions.py`, `test_adversarial.py`.
7. **API and events.** Endpoints, `MAX_REQUEST_BYTES`, `file_fetch` event;
   `test_api_files.py` (`test_api_security.py` style: token, body cap,
   nosniff header).
8. **Scenarios.** New family `files` in `devtools/testenv/scenarios/scen_files.py`
   and rows in `docs/testenv-scenarios.md`, each run five times:
   - files1: A shares 2 MB; B and C download from A.
   - files2: A shares; C offline; A killed; C returns and downloads from B
     (holder fallback, the check-1 proof).
   - files3: A shares 200 KB inline; C offline through it; C backfills it via
     sync from B.
   - files4: D, not a member, dials A's file plane with the real hash:
     silence, and a warning naming D in A's log.
   - files5: files1 at `--link-profile lora_fast` with a 200 KB file; the
     inline and shared ceilings and `FILE_CHUNK_BYTES` are set from what
     this measures.
   - files6: A shares 2 MB; B starts downloading; A's process is killed
     mid-transfer; C (who already holds it) is up; B finishes from C without
     re-fetching a verified chunk (assert on B's request log).
   - files7: B's process is killed mid-download and restarted; the download
     resumes from `file_parts` at the same chunk index.
   - files8: files1 under the `--link-profile` loss setting; RNS part
     retries, not the chunk scheme, are what carries it, and the run
     records how many requests the window halving cost.
9. **Flutter.** Picker, compose chip, file card, download flow, widget
   tests; `flutter analyze && flutter test`.
10. **Docs.** Fold the trust model into `docs/security-improvements.md`, the
    field range into `.claude/rules/protocol-constants.md`, the permission
    into `.claude/rules/permission-enforcement.md`, then delete this file.
