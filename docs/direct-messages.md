# Direct messages

A direct message is a conversation between two identities, with no channel, no
membership document, and no third party. This records what that costs and what
it buys, because neither is visible from the code alone.

## What a friendship proves, and what it does not

A conversation carries traffic only between two identities that each hold the
other as an accepted friend. Both ends enforce it independently:
`DirectMessageManager.may_dm` is consulted before a send and again on
everything received, so a message flows only where both sides have agreed. A
one-sided add reaches nobody — it says who *we* accept, not who accepts us.

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
truncated to 16 bytes (`naming.dm_hash_for`) — the same width as a channel
hash, so it rides the ordinary message store and the ordinary chat message
format with nothing added to the wire.

The receiver recomputes it from the sender it has just authenticated. An
inbound message whose address is anything else is not addressed to a
conversation this node is in, and is dropped. So the address is not a field to
be believed, it is a check: a peer cannot file a message into a conversation it
is not half of, and there is no marker field to spoof.

A conversation gets a `channels` row (`kind = 'dm'`) because `messages`
references it, and deliberately no `subscriptions` row. That absence is what
keeps it out of channel sync, presence beacons and avatar broadcast — all three
enumerate `get_subscriptions()`, so a conversation is invisible to them without
a single exclusion check to forget.

## Offline delivery, and what the node sees

A channel survives an absent member because any other member can serve the gap
later (`offline-sync.md`). A conversation has nobody else in it, so an absent
peer's messages go to an LXMF propagation node instead, which holds them until
that peer collects them.

Consequences worth stating plainly:

- **The node learns the pair.** It sees both endpoints' delivery addresses, the
  size, and the timing. It cannot read the content, which is encrypted end to
  end to the recipient — but the fact that these two identities corresponded,
  and roughly how much, is exposed to whoever runs it. Selecting the fewest-hop
  node keeps that as local as the mesh allows; it does not eliminate it.
- **Propagated mail is pulled, never pushed.** Nothing arrives without
  `Router.request_propagation_sync`. `PropagationCollector` owns when that
  happens: often for a settling window (15s apart, 3 minutes) after starting
  up, after the link returns, or after a node is chosen, then every 5 minutes.
  The window is the important half — a sender can still be uploading as the
  recipient arrives, because LXMF makes them generate a proof-of-work stamp
  first, so asking once on return and then not again for five minutes strands
  exactly the message that was in flight. `dm6` in the scenario suite is that
  case, and it failed until the cadence was built this way.
- **The node is remembered across restarts.** A propagation node announces
  when it is switched on and never again on a timer, so a client that forgot
  its node would have no way to hear of one — it is stored as
  `propagation_node.last_selected`, distinct from the user's explicit pin.
- **No node means no offline delivery.** LXMF fails a PROPAGATED send outright
  when no outbound node is configured, so the send path checks first and falls
  back to the in-memory pending queue — which does not survive a restart. On a
  mesh with no propagation node, a message to an offline friend is lost when
  the sender restarts.

## Deliberate non-fixes

- **No "friend removed" message.** Removing a friend is local. Telling the peer
  they were dropped leaks more than it helps; they find out when their messages
  stop being accepted, which is also what a network failure looks like.
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
  the accept and the broadcast side — one without the other is half a fix.
- **A conversation is not backfilled.** If a message is lost with no node to
  hold it, nothing recovers it later — there is no member to ask. The delivery
  indicator is what tells the sender, and it is the only thing that does.
