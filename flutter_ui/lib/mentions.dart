// Mentions: `@<identity hash hex>` written into the message text itself.
//
// Nothing on the wire describes a ping (see trenchchat/core/protocol.py), so
// the author signature already covers it, sync already relays it, and no relay
// can add one, drop one, or aim it at somebody else. The name is resolved at
// render time from whatever this client knows the identity as now, so a rename
// never leaves a mention addressed to who the sender thought they were
// talking to.

/// Identity hashes are RNS truncated hashes: 16 bytes, 32 hex characters.
const int mentionHexChars = 32;

/// A mention in message text. The trailing exclusion keeps a longer hex run
/// from reading as a mention of whoever its first 32 characters name.
final RegExp mentionTokenRe = RegExp(r'@([0-9a-fA-F]{32})(?![0-9a-fA-F])');

/// The text a mention of this identity is written as.
String mentionToken(String identityHashHex) => '@$identityHashHex';

/// Every identity a message names, in the order written, without repeats.
List<String> mentionsIn(String content) {
  if (!content.contains('@')) return const [];
  final found = <String>[];
  for (final m in mentionTokenRe.allMatches(content)) {
    final hex = m.group(1)!.toLowerCase();
    if (!found.contains(hex)) found.add(hex);
  }
  return found;
}

/// Whether a message pings that identity. Deliberately not a substring test:
/// a hash that is only the start of a longer hex run names somebody else.
bool contentMentions(String content, String? identityHashHex) {
  if (identityHashHex == null || identityHashHex.isEmpty) return false;
  return mentionsIn(content).contains(identityHashHex.toLowerCase());
}

/// What a mention reads as when nothing here knows the identity's name: enough
/// hash to tell two peers apart, never a name this client cannot vouch for.
String shortMentionLabel(String identityHashHex) =>
    identityHashHex.length <= 8 ? identityHashHex : '${identityHashHex.substring(0, 8)}…';

/// Somebody the compose bar can offer to mention: an identity plus the name
/// this client currently knows it by.
class MentionCandidate {
  const MentionCandidate({required this.identityHash, required this.displayName});

  final String identityHash;
  final String displayName;
}

/// Candidates matching what has been typed after the `@`, best match first.
///
/// A name the query is a prefix of comes before one that merely contains it,
/// so typing the start of a name puts that person at the top where Enter
/// takes them.
List<MentionCandidate> matchMentionCandidates(
    List<MentionCandidate> candidates, String query,
    {int limit = 8}) {
  final q = query.trim().toLowerCase();
  if (q.isEmpty) return candidates.take(limit).toList();
  final prefix = <MentionCandidate>[];
  final contains = <MentionCandidate>[];
  for (final c in candidates) {
    final name = c.displayName.toLowerCase();
    if (name.startsWith(q)) {
      prefix.add(c);
    } else if (name.contains(q) || c.identityHash.startsWith(q)) {
      contains.add(c);
    }
  }
  return [...prefix, ...contains].take(limit).toList();
}
