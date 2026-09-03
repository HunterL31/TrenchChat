class ChannelPermissions {
  const ChannelPermissions({
    required this.invite,
    required this.kick,
    required this.manageRoles,
    required this.manageChannel,
    required this.sendMessage,
    this.shareFiles = false,
    required this.voiceChat,
  });

  /// Fails closed against an older backend that doesn't report it.
  final bool invite;

  final bool kick;
  final bool manageRoles;
  final bool manageChannel;

  /// Defaults to true when the backend omits the key, so an older backend
  /// keeps compose enabled rather than locking everyone out.
  final bool sendMessage;

  /// Whether a file may be attached here. Fails closed: an older backend, or
  /// an open-join channel (which has no member list to authorise a serve
  /// against), reports nothing and the attach control stays hidden.
  final bool shareFiles;

  /// Fails closed against an older backend that doesn't report it.
  final bool voiceChat;

  factory ChannelPermissions.fromJson(Map<String, dynamic> json) => ChannelPermissions(
        invite: json['invite'] as bool? ?? false,
        kick: json['kick'] as bool? ?? false,
        manageRoles: json['manage_roles'] as bool? ?? false,
        manageChannel: json['manage_channel'] as bool? ?? false,
        sendMessage: json['send_message'] as bool? ?? true,
        shareFiles: json['share_files'] as bool? ?? false,
        voiceChat: json['voice_chat'] as bool? ?? false,
      );
}
