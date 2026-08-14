class ChannelPermissions {
  const ChannelPermissions({
    required this.kick,
    required this.manageRoles,
    required this.manageChannel,
    required this.sendMessage,
  });

  final bool kick;
  final bool manageRoles;
  final bool manageChannel;

  /// TODO(phase-b): GET /channels/{h}/my_permissions doesn't return
  /// `send_message` yet -- until it does, compose stays enabled for everyone.
  final bool sendMessage;

  factory ChannelPermissions.fromJson(Map<String, dynamic> json) => ChannelPermissions(
        kick: json['kick'] as bool? ?? false,
        manageRoles: json['manage_roles'] as bool? ?? false,
        manageChannel: json['manage_channel'] as bool? ?? false,
        sendMessage: true,
      );
}
