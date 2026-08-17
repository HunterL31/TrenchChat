// Fixed fixture data for the golden suite, echoing the same #general story
// beats as the mockup (Main Window Directions.dc.html options 1a/1b) so the
// rendered goldens are directly comparable to it.
import 'package:flutter_ui/api/models/friend.dart';
import 'package:flutter_ui/api/models/link_quality.dart';
import 'package:flutter_ui/api/models/member.dart';
import 'package:flutter_ui/api/models/message.dart';
import 'package:flutter_ui/api/models/permissions.dart';
import 'package:flutter_ui/api/models/server.dart';
import 'package:flutter_ui/app_state.dart';
import 'package:flutter_ui/screens/main_window/server_rail.dart';

const String kSelfHash = 'a9f13c02e7d84b119876543210fedcba';
const String kAliceHash = 'f3a1c2d4e5b6a798f3a1c2d4e5b6a798';
const String kBobHash = '7b8d41aa9c2e7b8d41aa9c2e7b8d41aa';
const String kCarolHash = 'c04e8812c04e8812c04e8812c04e8812';
const String kDaveHash = 'b1d75ff0b1d75ff0b1d75ff0b1d75ff0';

const String kServerHash = 'server-mesh-crew';
const String kServerHash2 = 'server-rf-ops';
const String kGeneralHash = 'channel-general';
const String kOpsHash = 'channel-ops';
const String kLoraHash = 'channel-lora-testing';
const String kPropagationHash = 'channel-propagation-nodes';

List<ServerRailEntry> fixtureRailServers() => const [
      ServerRailEntry(hash: kServerHash, name: 'mesh-crew'),
      ServerRailEntry(hash: kServerHash2, name: 'RF Ops'),
    ];

List<Channel> fixtureServerChannels() => const [
      Channel(
        hash: kGeneralHash,
        name: 'general',
        description: 'relay talk, coast mesh, nothing operational',
        creatorHash: kAliceHash,
        openJoin: true,
        createdAt: 0,
        serverHash: kServerHash,
      ),
      Channel(
        hash: kOpsHash,
        name: 'ops',
        description: '',
        creatorHash: kAliceHash,
        openJoin: false,
        createdAt: 0,
        serverHash: kServerHash,
      ),
    ];

List<Channel> fixtureDirectChannels() => const [
      Channel(
        hash: kLoraHash,
        name: 'lora-testing',
        description: '',
        creatorHash: kAliceHash,
        openJoin: true,
        createdAt: 0,
        serverHash: null,
      ),
      Channel(
        hash: kPropagationHash,
        name: 'propagation-nodes',
        description: '',
        creatorHash: kAliceHash,
        openJoin: false,
        createdAt: 0,
        serverHash: null,
      ),
    ];

List<PresenceEntry> fixturePresence() => const [
      PresenceEntry(identityHash: kAliceHash, isOnline: true),
      PresenceEntry(identityHash: kBobHash, isOnline: true),
      PresenceEntry(identityHash: kCarolHash, isOnline: false),
    ];

double _ts(int month, int day, int hour, int minute) =>
    DateTime(2026, month, day, hour, minute).millisecondsSinceEpoch / 1000;

Message _msg(String id, String sender, String senderName, double ts, String content,
        {List<Reaction> reactions = const []}) =>
    Message(
      messageId: id,
      senderHash: sender,
      senderName: senderName,
      content: content,
      timestamp: ts,
      replyTo: null,
      hasImage: false,
      reactions: reactions,
    );

List<Message> fixtureMessages() => [
      _msg('m1', kAliceHash, 'f3a1…9c2e', _ts(8, 10, 21, 4),
          'rebuilt the coast node with the new firmware, announces are landing again'),
      _msg('m2', kAliceHash, 'f3a1…9c2e', _ts(8, 10, 21, 11),
          'six hours stable now, no dropped announces'),
      _msg('m3', kBobHash, '7b8d…41aa', _ts(8, 10, 21, 26),
          "mine still needs a power cycle every night, I think it's the regulator"),
      _msg('m4', kCarolHash, 'c04e…8812', _ts(8, 10, 21, 40),
          'same board? the 3.3v rail sags under transmit on mine'),
      _msg('m5', kCarolHash, 'c04e…8812', _ts(8, 10, 21, 44),
          'I put a bigger cap across it and it stopped resetting'),
      _msg('m6', kAliceHash, 'f3a1…9c2e', _ts(8, 10, 22, 41),
          'member list signed and circulated, everyone should have it now'),
      _msg('m7', kDaveHash, 'b1d7…5ff0', _ts(8, 10, 22, 58),
          'got it, verified against the admin key'),
      _msg('m8', kBobHash, '7b8d…41aa', _ts(8, 11, 13, 52),
          'anyone got a spare relay node near the coast? mine keeps dropping packets after '
          'about 40 minutes',
          reactions: const [Reaction(emojiHash: '👍', count: 2, reactedByMe: false)]),
      _msg('m9', kAliceHash, 'f3a1…9c2e', _ts(8, 11, 13, 58),
          'try lowering your interface bitrate, fixed it for me last week'),
      _msg('m10', kAliceHash, 'f3a1…9c2e', _ts(8, 11, 13, 59),
          'also check your antenna SWR before you blame the stack'),
      _msg('m11', kSelfHash, 'you', _ts(8, 11, 14, 3),
          'I can run a propagation node tonight if that helps — it has mains power so it '
          'can stay up'),
      _msg('m12', kCarolHash, 'c04e…8812', _ts(8, 11, 14, 4),
          'that would help, my link drops after dark and the store-and-forward would cover it'),
      _msg('m13', kBobHash, '7b8d…41aa', _ts(8, 11, 14, 7), 'sending the interface config over, one sec',
          reactions: const [Reaction(emojiHash: '🔥', count: 1, reactedByMe: true)]),
    ];

/// Timestamps are relative to render time rather than a fixed epoch, so the
/// "now"/"2h"/"3d"/"never" buckets formatRelative() renders stay stable no
/// matter when the golden suite runs.
List<Friend> fixtureFriends() {
  final now = DateTime.now().millisecondsSinceEpoch / 1000;
  return [
    Friend(
      identityHash: kAliceHash,
      nickname: 'Alice R.',
      note: 'runs the coast relay node',
      displayName: 'f3a1…9c2e',
      addedAt: now - 30 * 86400,
      lastSeenAt: now - 30,
      isOnline: true,
    ),
    Friend(
      identityHash: kBobHash,
      nickname: '',
      note: '',
      displayName: '7b8d…41aa',
      addedAt: now - 10 * 86400,
      lastSeenAt: now - 2 * 3600,
      isOnline: false,
    ),
    Friend(
      identityHash: kDaveHash,
      nickname: 'Dave',
      note: 'relay op, night shift',
      displayName: 'b1d7…5ff0',
      addedAt: now - 5 * 86400,
      lastSeenAt: 0,
      isOnline: false,
    ),
  ];
}

/// Populates an already-constructed AppState with fixed fixture data (no
/// network calls) so goldens are deterministic and don't need a live backend.
void populateFixtureState(AppState state) {
  state.meHashHex = kSelfHash;
  state.meDisplayName = 'you';
  state.servers = const [
    Server(hash: kServerHash, name: 'mesh-crew', description: '', creatorHash: kAliceHash, createdAt: 0),
    Server(hash: kServerHash2, name: 'RF Ops', description: '', creatorHash: kAliceHash, createdAt: 0),
  ];
  state.channelsByServer[kServerHash] = fixtureServerChannels();
  state.channelsByServer[kServerHash2] = const [];
  state.serverMemberCounts[kServerHash] = 12;
  state.serverMemberCounts[kServerHash2] = 4;
  state.standaloneChannels = fixtureDirectChannels();

  state.selectedServerHash = kServerHash;
  state.selectedChannelHash = kGeneralHash;

  state.membersByChannel[kGeneralHash] = const [];
  state.messagesByChannel[kGeneralHash] = fixtureMessages();
  state.presenceByChannel[kGeneralHash] = fixturePresence();
  state.linkQualityByChannel[kGeneralHash] =
      const ChannelLinkQuality(level: LinkQualityLevel.excellent, hops: 2);
  state.permissionsByChannel[kGeneralHash] =
      const ChannelPermissions(kick: false, manageRoles: false, manageChannel: false, sendMessage: true);
  state.friends = fixtureFriends();

  // Seed every rendered identity as "no avatar" so building the tree never
  // reaches for the network; a cache miss would fire a real HTTP request.
  for (final m in state.messagesByChannel[kGeneralHash]!) {
    state.avatarCache[m.senderHash] = null;
  }
  for (final p in state.presenceByChannel[kGeneralHash]!) {
    state.avatarCache[p.identityHash] = null;
  }

  state.loading = false;
}
