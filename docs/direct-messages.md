# Direct messages

A direct message is a conversation between two identities, with no channel, no
membership document, and no third party. This records what that costs and what
it buys, because neither is visible from the code alone.

## What a friendship proves, and what it does not

A conversation carries traffic only between two identities that each hold the
other as an accepted friend. Both ends enforce it independently:
`DirectMessageManager.may_dm` is consulted before a send and again on
everything received, so a message flows only where both sides have agreed. A
one-sided add reaches nobody; it says who *we* accept, not who accepts us.

That gate proves one thing: the sender is an identity this node chose to
accept. It does not prove the sender is the person the user believes they
added. TrenchChat display names are self-asserted (see
`security-improvements.md`), so a friendship is only as trustworthy as the
channel the identity hash arrived through. A hash read off a screen in person
is strong; a hash a stranger typed into a friend request is not, and the
request's note is attacker-chosen text shown only so a person can decide.

The handshake exists for ergonomics, not for security. `add_friend` reaching
`accepted` directly is exactly as valid as a request the peer accepted: the
security property comes from *both* sides holding each other, and each side
reaches that state by whatever route it likes.

## Addressing, and why it is not a claim

A conversation's address is `sha256("trenchchat-dm-v1" || min(a,b) || max(a,b))`
truncated to 16 bytes (`naming.dm_hash_for`), the same width as a channel
hash, so it rides the ordinary message store with nothing added to it.

**It is never sent.** The receiver derives it from the sender it has just
authenticated, so there is nothing on the wire to aim, forge or believe: a
message lands in the conversation between its sender and its recipient, and
there is no other conversation it could be made to land in. A peer cannot file
a message into one it is not half of because it never gets to name one.

A conversation gets a `channels` row (`kind = 'dm'`) because `messages`
references it, and deliberately no `subscriptions` row. That absence is what
keeps it out of channel sync, presence beacons and avatar broadcast, all three
enumerate `get_subscriptions()`, so a conversation is invisible to them without
a single exclusion check to forget.

### Two hashes that look identical

Everything here is keyed on an **identity hash**. What a client or a bot
advertises is an **LXMF address**: the delivery destination hash,
`RNS.Destination.hash(identity_hash, "lxmf", "delivery")`. Both are 16 bytes,
both print as 32 hex characters, and nothing about one tells you it is not
the other. The derivation runs one way only.

So an LXMF address pasted where an identity hash is expected is hashed a
second time and addresses a destination nobody is listening on. There is no
error: the message queues, no path ever resolves, and it looks exactly like
the peer being offline. That indistinguishability is the whole problem, and
it is why `add_lxmf_address` exists as a separate call rather than the field
guessing.

Resolving is what closes the gap. The announce that made a peer reachable
carries its identity, so `RNS.Identity.recall(delivery_hash)` gives the
identity hash back, and from that point the contact is an ordinary one:
sending, receiving and conversation addressing all unchanged. An address
whose announce has not been heard cannot be resolved yet, so it waits on a
path request and `FriendsManager.tick()` finishes it; nothing blocks, and an
unreachable address simply never becomes a contact.

## Talking to clients that are not TrenchChat

A conversation is the one thing TrenchChat sends that can legitimately arrive
at somebody else's client, so it is carried as a plain LXMF message: the words
in the ordinary `content`, an attachment in LXMF's own `FIELD_IMAGE`, and
everything TrenchChat adds (message id, reply target, author signature)
inside `FIELD_CUSTOM_TYPE`/`FIELD_CUSTOM_DATA`, which the standard sets aside
for an application's own structures and every other client knows to ignore.
Sideband, NomadNet or anything else speaking LXMF can hold up its half, and a
message from one is accepted as an ordinary direct message.

This matters beyond politeness. TrenchChat's own field numbers overlap LXMF's
registry (`0x02` is `FIELD_TELEMETRY` there, `0x06` `FIELD_IMAGE`), so
nothing puts them on the wire as LXMF field keys: channels, sync, invites and
voice pack their whole field dict inside the same custom-payload fields under
their own envelope type (`protocol.pack_fields`), and a conversation uses the
standard fields directly under its own.

**The gate is unchanged.** Adding a contact was always a local decision (the
handshake is only the ergonomic way to reach it), so a peer on another client
is added by hash exactly as any other, and only an accepted friend gets
through. Dropping the envelope is what an attacker would try, since it carries
the signature; it buys nothing, because the friendship is checked against the
identity LXMF authenticated, not against anything the message says about
itself.

**The author signature is required from a TrenchChat sender and not from
anyone else**, which is a deliberate trade and not an oversight. That signature
exists for messages arriving *by relay* (sync, where the peer handing a
message over is not its author), and a conversation is never relayed. What
authenticates a direct message is LXMF's own signature over the whole thing,
which `Router._authenticate` checked before any of this ran. A client that does
not implement TrenchChat's extra signature is therefore not trusted any less
than one that does; but a sender *claiming* to be TrenchChat must still produce
one, or the envelope would be a way to assert authorship without proving it.

What another client cannot do is TrenchChat's own extras. Reactions are control
messages that would arrive as empty ones, so they are not sent to a peer that
has never identified itself as TrenchChat (`dm_conversations.peer_is_trenchchat`,
set when an envelope arrives). Both clients show such a conversation as `LXMF`
so the difference is visible rather than mysterious.

## Being refused is not the same as never happening

A message from someone not accepted used to be dropped where the gate refused
it, with a log line and nothing else. That was wrong in a way the gate is not.

LXMF proves a delivery packet as it arrives, before the message is assembled
and long before any of this runs, so the sender's client showed it delivered.
And a client speaking only plain LXMF cannot send `MT_FRIEND_REQUEST` -- that is
a TrenchChat control message -- so messaging *is* its only way to ask. Between
the two, a Sideband or MeshChat user could never start a conversation with
anyone who had not already added them out of band, and would be told it worked.
That undoes most of what carrying conversations in the standard format is for.

So a refused message is now **held** rather than dropped: the sender gets a
`pending_in` row and their words are stored, appearing wherever a friend request
already appears. Accepting files every held message into the conversation in the
order it arrived; declining drops both.

**The gate is unchanged, and holding grants nothing.** `may_dm` still answers
for an accepted friend and nobody else, on both sides, and a held message
creates no conversation, no membership and no way to reply. Only the user
accepting changes that -- which is the same decision a friend request asks for,
reached by the only route some clients have.

An outstanding request of **ours** is deliberately not a reason to drop one
either, though it once was, the reasoning being that their answer belonged
to the request rather than to a second queue. That holds for a TrenchChat
peer, which answers with `MT_FRIEND_ACCEPT`. A bot or a plain LXMF client has
no such message to send: it answers with words, and those were exactly what
got thrown away. Asking a verification bot to be friends and then losing the
code it sent back is the shape of that bug. Their words are held; our request
keeps its own `pending_out` state, because holding what they said is not us
deciding the handshake went the other way.

What is deliberately *not* held:

- **The attachment.** An unknown sender's binary payload is the surface worth
  refusing and the expensive half to store, so `FIELD_IMAGE` is not read on this
  path. Their words arrive; their picture does not.
- **Reactions.** `_may_react` refuses a conversation we are not half of, and a
  reaction from a stranger means nothing without the message it points at.

**Nothing is sent back.** A TrenchChat peer could not have messaged us at all
without already holding us as accepted -- its own outbound gate saw to that --
so accepting completes the friendship with nothing to announce. A plain LXMF
client has no friendship to be told about, and an `MT_FRIEND_ACCEPT` would reach
it as an empty message. Either side learns the same way: a reply arrives, or it
does not.

### Why the bounds are the whole design

A direct message carries no `F_MSG_TYPE`, which keeps it out of the router's
per-sender control throttle -- deliberately, because a limit there would drop
conversation. Friend requests are paced by that throttle; **this queue is not
paced by anything**, so every bound is enforced where the row is written
(`friends.py`, `storage.add_message_request`):

- the body is capped at `MAX_REQUEST_BODY_CHARS`, matched to the friend-request
  note so peer-written text is capped identically wherever it is shown;
- `MAX_HELD_PER_SENDER` messages per sender, newest kept;
- `MAX_HELD_MESSAGES` in total, and `MESSAGE_REQUEST_TTL_SECS` swept on write;
- the `pending_in` rows themselves stay under `MAX_PENDING_FRIEND_REQUESTS`,
  evicted oldest-first, taking their held messages with them.

Identities are free to mint, so the total caps are the ones that hold; the
per-sender one only paces a single honest peer.

## Offline delivery, and what the node sees

A channel survives an absent member because any other member can serve the gap
later (`offline-sync.md`). A conversation has nobody else in it, so an absent
peer's messages go to an LXMF propagation node instead, which holds them until
that peer collects them.

Consequences worth stating plainly:

- **"Propagated" is not "delivered", and never becomes it.** LXMF marks a
  propagated message `SENT` when the node accepts it (`__mark_propagated`),
  and there is no proof from the recipient to advance it; the state machine
  simply has no path from there to `DELIVERED`. So `DELIVERY_PROPAGATED` means
  a node took custody, nothing more. Whether the peer ever collects it is not
  observable from the sending side; the only confirmation is an answer coming
  back. Anything the UI says beyond that would be a claim it cannot support.
- **It only arrives if the recipient uses that same node.** Held mail is
  pulled, so a message left with a node the peer never syncs from sits there
  until it expires. Nothing tells either end that happened.
- **Which is why choosing propagation matters.** `_peer_is_reachable` decides,
  and it used to ask presence alone. Presence only knows peers that send
  TrenchChat beacons, so for a bot or another client's user it always answered
  "not online" (meaning "never heard of") and the first message to them went
  to a node rather than to the peer sitting right there. Presence now decides
  only for a peer that has identified itself as TrenchChat; for everyone else
  a resolved path does. The asymmetry justifies it: a direct attempt that
  fails falls back to propagation by itself, while a needless propagation is a
  message nobody may ever collect.
- **The node learns the pair.** It sees both endpoints' delivery addresses, the
  size, and the timing. It cannot read the content, which is encrypted end to
  end to the recipient. That these two identities corresponded, and roughly how
  much, is exposed to whoever runs it. Selecting the fewest-hop
  node keeps that as local as the mesh allows; it does not eliminate it.
- **Propagated mail is pulled, never pushed.** Nothing arrives without
  `Router.request_propagation_sync`. `PropagationCollector` owns when that
  happens: often for a settling window (15s apart, 3 minutes) after starting
  up, after the link returns, or after a node is chosen, then every 5 minutes.
  The window is the important half; a sender can still be uploading as the
  recipient arrives, because LXMF makes them generate a proof-of-work stamp
  first, so asking once on return and then not again for five minutes strands
  exactly the message that was in flight. `dm6` in the scenario suite is that
  case, and it failed until the cadence was built this way.
- **The node is remembered across restarts.** A propagation node announces
  when it is switched on and never again on a timer, so a client that forgot
  its node would have no way to hear of one; it is stored as
  `propagation_node.last_selected`, distinct from the user's explicit pin.
- **No node means no offline delivery.** LXMF fails a PROPAGATED send outright
  when no outbound node is configured, so the send path checks first and falls
  back to the in-memory pending queue, which does not survive a restart. On a
  mesh with no propagation node, a message to an offline friend is lost when
  the sender restarts.

## Deliberate non-fixes

- **No "friend removed" message.** Removing a friend is local. Telling the peer
  they were dropped leaks more than it helps; they find out when their messages
  stop being accepted, which is also what a network failure looks like. What
  they will see is their next message held as a request again, which says the
  same thing without asserting it.
- **A declined request can be re-sent.** Declining deletes the row rather than
  remembering the refusal, so the same peer can ask again. A durable blocklist
  is the fix if this is ever abused; the pending queue is capped
  (`MAX_PENDING_FRIEND_REQUESTS`) so the cost of ignoring one is bounded.
- **No read receipts, and no typing indicator.** Neither exists for channels,
  and adding them for conversations alone would be new protocol surface whose
  only purpose is telling a peer when someone is at their machine.
- **No history sync between a user's own devices.** An identity is a device
  here. A second device with the same identity would collect propagated mail
  sent while it was away, and hold nothing from before that.
- **A friend you share no channel with has no avatar.** `avatar.py` gates both
  the inbound accept and the outbound broadcast on `shares_any_channel`, so a
  DM-only friend shows the fallback. Reactions and custom emoji were extended
  to treat an accepted friend as a shared context; avatars deliberately were
  not, to keep this change to one surface. Extending them means the same
  one-line trust argument plus a friends reference in `AvatarManager`, on both
  the accept and the broadcast side, one without the other is half a fix.
- **A conversation with another LXMF client has no delivery state beyond the
  transport's.** Their client sends no receipts, and TrenchChat's indicator
  reports only what LXMF reports: handed over, or not.
- **A conversation is not backfilled.** If a message is lost with no node to
  hold it, nothing recovers it later; there is no member to ask. The delivery
  indicator is what tells the sender, and it is the only thing that does.
