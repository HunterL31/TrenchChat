"""
What a message may carry to a client that is not TrenchChat.

A conversation is the one thing TrenchChat sends that legitimately arrives at
Sideband, NomadNet or anything else speaking LXMF, and its words travel as
ordinary LXMF content. Some of what TrenchChat writes into those words is its
own: a custom emoji is a ``:name@<sha256>:`` token the other end resolves by
asking us for the image, and a shared theme is a whole palette packed into a
``tct1:`` code. Neither means anything to a client that has never heard of
either, and both are large: a 64-character hash, or a few hundred characters
of base64, where a word should be.

So a message bound for such a client is rewritten to what it can read. A
custom emoji keeps its name and loses the hash, which is the half that was
only ever addressed to us. A theme code is dropped outright, since nothing of
it survives the translation and no part of it is worth the airtime.

The rewrite is what the sender stores too, so both ends read the same words
and the difference is visible rather than mysterious. Where it would leave
nothing at all, the send is refused rather than delivered as an empty message,
which is what the other client would otherwise show.

This is a degrade, never a gate. What may pass between two peers is
FriendsManager's answer alone (see direct.py); this only decides how it is
written.
"""

import re

import RNS

# Matches :name@hexhash: (unambiguous) or :name: (legacy, name lookup only).
# Group 1 = name, group 2 = 64-char SHA-256 hex, absent on legacy tokens.
EMOJI_TOKEN_RE = re.compile(r":([a-zA-Z0-9_-]+)(?:@([0-9a-fA-F]{64}))?:")

# A whole theme packed into one token: tct1:<base64url-no-padding>. Written by
# the client's appearance editor; see flutter_ui/lib/theme/theme_code.dart.
THEME_CODE_RE = re.compile(r"tct1:[A-Za-z0-9_-]+")

_HORIZONTAL_RUN_RE = re.compile(r"[ \t]{2,}")


def plain_lxmf_content(content: str) -> str:
    """Rewrite a message body into what a foreign LXMF client can read."""
    if not content:
        return content
    text = EMOJI_TOKEN_RE.sub(_emoji_name_only, content)
    if THEME_CODE_RE.search(text):
        text = _HORIZONTAL_RUN_RE.sub(" ", THEME_CODE_RE.sub("", text)).strip()
    return text


def carries_only_trenchchat_markup(content: str) -> bool:
    """Whether rewriting this body for a foreign client leaves nothing."""
    return bool(content and content.strip()
                and not plain_lxmf_content(content).strip())


def peer_reads_trenchchat(is_trenchchat, peer_hex: str) -> bool:
    """Whether a peer is known to run TrenchChat, given the gate or None.

    True when no gate is wired, which is what a build without the directory
    and the conversation store gets. An erroring gate answers False: the point
    of asking is to keep TrenchChat's own traffic off a client that cannot
    read it, and a failed lookup is not evidence that it can.
    """
    if is_trenchchat is None:
        return True
    try:
        return bool(is_trenchchat(peer_hex))
    except Exception as e:
        RNS.log(
            f"TrenchChat [interop]: TrenchChat check for {peer_hex[:12]}… "
            f"errored: {e}",
            RNS.LOG_ERROR,
        )
        return False


def _emoji_name_only(match: re.Match) -> str:
    if match.group(2) is None:
        return match.group(0)
    return f":{match.group(1)}:"
