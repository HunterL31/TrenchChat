"""
Node-wide Reticulum config reading and writing: the [reticulum] and
[logging] sections.

Pure ConfigObj manipulation, like interfaces_config.py (which owns the
[interfaces] section and the three discovery keys' dedicated writer);
devtools/testenv/api.py's /reticulum/config endpoints call these directly.

The option set is exactly what RNS.Reticulum.__apply_config reads, and every
default recorded here is the value RNS falls back to when the key is absent.
"""

import os

import RNS
from configobj import ConfigObj

from trenchchat.core.interfaces_config import InterfaceConfigError

# Identity hashes in a hash_list are truncated hashes:
# (RNS.Reticulum.TRUNCATED_HASHLENGTH // 8) * 2 hex characters.
HASH_HEX_LENGTH = 32

MIN_LOGLEVEL = 0
MAX_LOGLEVEL = 7

_TRUE_VALUES = ("yes", "true", "on", "1")
_FALSE_VALUES = ("no", "false", "off", "0")

CATEGORY_LOGGING = "Logging"
CATEGORY_INSTANCE = "Instance"
CATEGORY_TRANSPORT = "Transport & routing"
CATEGORY_REMOTE = "Remote management"
CATEGORY_DISCOVERY = "Interface discovery"
CATEGORY_BLACKHOLE = "Blackholes"
CATEGORY_ANNOUNCE_RATE = "Announce rate limits"
CATEGORY_INGRESS = "Ingress & egress control"

INTERFACE_MODES = [
    "full", "access_point", "pointtopoint", "roaming", "boundary", "gateway",
    "internal",
]

# One entry per option RNS.Reticulum.__apply_config reads out of the
# [reticulum] and [logging] sections. "default" is a display string: what RNS
# uses when the key is absent, shown as the field's placeholder.
RETICULUM_OPTIONS: list[dict] = [
    {
        "key": "loglevel",
        "section": "logging",
        "category": CATEGORY_LOGGING,
        "label": "Log level",
        "kind": "int",
        "default": "4",
        "description": (
            "How much detail Reticulum writes to the log: 0 is critical only, 4 is the "
            "normal level, and 7 traces every path decision. High levels fill the log "
            "quickly and slow down constrained devices."
        ),
    },
    {
        "key": "logtimestamps",
        "section": "logging",
        "category": CATEGORY_LOGGING,
        "label": "Log timestamps",
        "kind": "bool",
        "default": "Yes",
        "description": (
            "Prefix every log line with the time it was written. Turning it off makes "
            "lines shorter, but leaves you unable to tell when anything happened."
        ),
    },
    {
        "key": "share_instance",
        "section": "reticulum",
        "category": CATEGORY_INSTANCE,
        "label": "Share instance",
        "kind": "bool",
        "default": "Yes",
        "description": (
            "Let other Reticulum programs on this machine (Sideband, NomadNet, rnstatus) "
            "share this running instance instead of starting their own. Turning it off "
            "forces every other local program to bring up its own stack and its own copy "
            "of every configured interface."
        ),
    },
    {
        "key": "shared_instance_type",
        "section": "reticulum",
        "category": CATEGORY_INSTANCE,
        "label": "Shared instance type",
        "kind": "choice",
        "choices": ["tcp", "unix"],
        "default": "unix (tcp on Windows)",
        "description": (
            "How local programs reach the shared instance: 'unix' uses a private socket "
            "file, 'tcp' listens on localhost. Windows only supports tcp, and a TCP "
            "listener is reachable by every user account on the machine."
        ),
    },
    {
        "key": "instance_name",
        "section": "reticulum",
        "category": CATEGORY_INSTANCE,
        "label": "Instance name",
        "kind": "str",
        "default": "default",
        "description": (
            "Names the shared instance's socket so several independent instances can run "
            "side by side on one machine. It only applies to unix sockets, and other "
            "programs must be started with the same name or they will not find this one."
        ),
    },
    {
        "key": "shared_instance_port",
        "section": "reticulum",
        "category": CATEGORY_INSTANCE,
        "label": "Shared instance port",
        "kind": "int",
        "default": "37428",
        "description": (
            "Localhost port other programs connect to when the shared instance uses TCP. "
            "Change it only to avoid a port collision — every local Reticulum program has "
            "to be told the same number."
        ),
    },
    {
        "key": "instance_control_port",
        "section": "reticulum",
        "category": CATEGORY_INSTANCE,
        "label": "Instance control port",
        "kind": "int",
        "default": "37429",
        "description": (
            "Localhost port the control utilities (rnstatus, rnpath, rnprobe) use to talk "
            "to this instance. Change it only to avoid a collision; tools still pointed at "
            "the old port stop working."
        ),
    },
    {
        "key": "rpc_key",
        "section": "reticulum",
        "category": CATEGORY_INSTANCE,
        "label": "RPC key (hex)",
        "kind": "hex",
        "default": "derived automatically",
        "description": (
            "Hex key local programs must present to talk to this instance over the "
            "shared-instance socket. It is normally derived automatically — set the wrong "
            "value and tools like rnstatus are locked out."
        ),
    },
    {
        "key": "force_shared_instance_bitrate",
        "section": "reticulum",
        "category": CATEGORY_INSTANCE,
        "label": "Force shared instance bitrate (bps)",
        "kind": "int",
        "default": "real interface speed",
        "description": (
            "Report a fixed bitrate to programs connected to the shared instance instead of "
            "the real interface speed, mainly to test slow-link behaviour. A wrong value "
            "makes those programs mis-tune timeouts and transfer sizes."
        ),
    },
    {
        "key": "enable_transport",
        "section": "reticulum",
        "category": CATEGORY_TRANSPORT,
        "label": "Enable transport",
        "kind": "bool",
        "default": "No",
        "description": (
            "Route traffic for other peers, making this node a transport hub. It uses more "
            "bandwidth, memory and battery, and should stay off on machines that move "
            "between networks — paths learned through a roaming transport node break for "
            "everyone using them."
        ),
    },
    {
        "key": "static_transport_identity",
        "section": "reticulum",
        "category": CATEGORY_TRANSPORT,
        "label": "Static transport identity",
        "kind": "bool",
        "default": "No",
        "description": (
            "Keep the stored transport identity instead of generating a fresh one at every "
            "start. Without transport enabled the identity is ephemeral by default, which is "
            "what stops a non-transport node being recognised across restarts; turning this "
            "on makes this node trackable over time."
        ),
    },
    {
        "key": "network_identity",
        "section": "reticulum",
        "category": CATEGORY_TRANSPORT,
        "label": "Network identity file",
        "kind": "str",
        "default": "none",
        "description": (
            "Path to an identity file used to sign this node's network-level publications — "
            "interface discovery and blackhole lists; it is created if it does not exist. "
            "Anyone who obtains the file can publish as this node."
        ),
    },
    {
        "key": "local_hops_delta",
        "section": "reticulum",
        "category": CATEGORY_TRANSPORT,
        "label": "Local hops delta",
        "kind": "bool",
        "default": "No",
        "description": (
            "Start packets originating here at a random hop count instead of zero, so "
            "observers cannot tell the traffic began on this node. The inflated hop counts "
            "make timeout and path calculations for this node's own traffic less accurate."
        ),
    },
    {
        "key": "link_mtu_discovery",
        "section": "reticulum",
        "category": CATEGORY_TRANSPORT,
        "label": "Link MTU discovery",
        "kind": "bool",
        "default": "Yes",
        "description": (
            "Negotiate larger packets on links whose whole path supports them, which "
            "considerably speeds up transfers on fast networks. Turn it off only to "
            "troubleshoot a link that will not settle; on slow media it changes nothing."
        ),
    },
    {
        "key": "use_implicit_proof",
        "section": "reticulum",
        "category": CATEGORY_TRANSPORT,
        "label": "Use implicit proof",
        "kind": "bool",
        "default": "Yes",
        "description": (
            "Send compact delivery proofs that omit the packet hash the receiver already "
            "knows, saving airtime on slow links. Leave it on: explicit proofs are twice the "
            "size for no practical gain."
        ),
    },
    {
        "key": "respond_to_probes",
        "section": "reticulum",
        "category": CATEGORY_TRANSPORT,
        "label": "Respond to probes",
        "kind": "bool",
        "default": "No",
        "description": (
            "Answer rnprobe echo requests so others can test whether this node is reachable. "
            "Useful on a public hub, but it lets anyone confirm the node exists and measure "
            "the link to it."
        ),
    },
    {
        "key": "panic_on_interface_error",
        "section": "reticulum",
        "category": CATEGORY_TRANSPORT,
        "label": "Panic on interface error",
        "kind": "bool",
        "default": "No",
        "description": (
            "Shut the whole program down when an interface fails unrecoverably, instead of "
            "retrying it. Sensible under a supervisor that restarts the process; on a desktop "
            "it turns one flaky USB radio into a full crash."
        ),
    },
    {
        "key": "default_gravity",
        "section": "reticulum",
        "category": CATEGORY_TRANSPORT,
        "label": "Default gravity",
        "kind": "int",
        "default": "0",
        "description": (
            "Preference weight given to interfaces that do not set their own. When the same "
            "announce arrives on two interfaces, the one with higher gravity wins the path "
            "entry, so raising it everywhere changes nothing — set it per interface to prefer "
            "one link over another."
        ),
    },
    {
        "key": "enable_remote_management",
        "section": "reticulum",
        "category": CATEGORY_REMOTE,
        "label": "Enable remote management",
        "kind": "bool",
        "default": "No",
        "description": (
            "Allow the identities in the allowed list to query and manage this node over the "
            "network. Leave it off unless you administer this node remotely, and never enable "
            "it without a strict allowed list."
        ),
    },
    {
        "key": "remote_management_allowed",
        "section": "reticulum",
        "category": CATEGORY_REMOTE,
        "label": "Remote management allowed",
        "kind": "hash_list",
        "default": "none",
        "description": (
            "Identity hashes permitted to use remote management, comma-separated, each "
            "exactly 32 hex characters. Treat an entry here like an admin credential: whoever "
            "holds that identity can inspect and reconfigure this node."
        ),
    },
    {
        "key": "discover_interfaces",
        "section": "reticulum",
        "category": CATEGORY_DISCOVERY,
        "label": "Discover interfaces",
        "kind": "bool",
        "default": "No",
        "description": (
            "Collect network entry points that transport instances announce on the mesh, so "
            "this node can find new ways to connect. It costs only the announces it listens "
            "to, but everything in the resulting list was published by a stranger."
        ),
    },
    {
        "key": "autoconnect_discovered_interfaces",
        "section": "reticulum",
        "category": CATEGORY_DISCOVERY,
        "label": "Autoconnect discovered interfaces",
        "kind": "int",
        "default": "0 (off)",
        "description": (
            "How many discovered entry points this node may connect to on its own. Anything "
            "above zero hands your traffic's metadata to hubs you have never vetted; zero "
            "keeps discovery read-only."
        ),
    },
    {
        "key": "required_discovery_value",
        "section": "reticulum",
        "category": CATEGORY_DISCOVERY,
        "label": "Required discovery value",
        "kind": "int",
        "default": "none",
        "description": (
            "Minimum proof-of-work stamp value a discovery announce must carry to be "
            "remembered. Raising it filters out cheap, spammy entry points; set too high it "
            "discards good ones and can leave nothing to connect to."
        ),
    },
    {
        "key": "interface_discovery_sources",
        "section": "reticulum",
        "category": CATEGORY_DISCOVERY,
        "label": "Interface discovery sources",
        "kind": "hash_list",
        "default": "any publisher",
        "description": (
            "Only accept discovered interfaces published by these network identity hashes, "
            "comma-separated, 32 hex characters each. It pins discovery to publishers you "
            "trust, at the cost of missing every entry point they do not carry."
        ),
    },
    {
        "key": "autoconnect_interface_mode",
        "section": "reticulum",
        "category": CATEGORY_DISCOVERY,
        "label": "Autoconnect interface mode",
        "kind": "choice",
        "choices": INTERFACE_MODES,
        "default": "full",
        "description": (
            "Interface mode given to auto-connected interfaces, which decides how announces "
            "propagate across them. A wrong mode on a transport node can silently stop "
            "announces being forwarded."
        ),
    },
    {
        "key": "autoconnect_interface_gravity",
        "section": "reticulum",
        "category": CATEGORY_DISCOVERY,
        "label": "Autoconnect interface gravity",
        "kind": "int",
        "default": "the default gravity",
        "description": (
            "Preference weight given to auto-connected interfaces. Raise it and a discovered "
            "hub can take over path entries from your own configured links; lower it and "
            "discovered hubs stay a fallback."
        ),
    },
    {
        "key": "autoconnect_announces_to_internal",
        "section": "reticulum",
        "category": CATEGORY_DISCOVERY,
        "label": "Autoconnect announces to internal",
        "kind": "bool",
        "default": "No",
        "description": (
            "Let announces arriving on auto-connected interfaces be forwarded onto "
            "internal-mode interfaces. It bridges a discovered hub into your private segment, "
            "which also means outside announce traffic lands there."
        ),
    },
    {
        "key": "publish_blackhole",
        "section": "reticulum",
        "category": CATEGORY_BLACKHOLE,
        "label": "Publish blackhole list",
        "kind": "bool",
        "default": "No",
        "description": (
            "Serve this node's blackhole list to anyone who asks, so other nodes can adopt "
            "your blocking decisions. Only meaningful on a transport node, and it makes those "
            "decisions public."
        ),
    },
    {
        "key": "blackhole_sources",
        "section": "reticulum",
        "category": CATEGORY_BLACKHOLE,
        "label": "Blackhole sources",
        "kind": "hash_list",
        "default": "none",
        "description": (
            "Identity hashes to pull blackhole lists from, comma-separated, 32 hex characters "
            "each. It outsources your blocking policy: a compromised or careless source can "
            "silently block legitimate peers."
        ),
    },
    {
        "key": "blackhole_update_interval",
        "section": "reticulum",
        "category": CATEGORY_BLACKHOLE,
        "label": "Blackhole update interval (minutes)",
        "kind": "float",
        "default": "60",
        "description": (
            "How often to fetch a fresh list from each blackhole source. Shorter reacts to new "
            "entries faster but links out to every source that much more often; values under "
            "2 minutes are clamped to 2."
        ),
    },
    {
        "key": "default_ar_target",
        "section": "reticulum",
        "category": CATEGORY_ANNOUNCE_RATE,
        "label": "Default announce rate target (s)",
        "kind": "int",
        "default": "3600",
        "description": (
            "Shortest interval, in seconds, a peer is expected to leave between announces on "
            "an interface that sets no target of its own. Raising it protects slow shared "
            "media from chatty peers; set too high, legitimate peers get blocked."
        ),
    },
    {
        "key": "default_ar_penalty",
        "section": "reticulum",
        "category": CATEGORY_ANNOUNCE_RATE,
        "label": "Default announce rate penalty (s)",
        "kind": "int",
        "default": "0",
        "description": (
            "Extra seconds a peer that keeps announcing too fast stays blocked, on top of the "
            "rate target. A large penalty makes a peer that misbehaves once invisible for a "
            "long time."
        ),
    },
    {
        "key": "default_ar_grace",
        "section": "reticulum",
        "category": CATEGORY_ANNOUNCE_RATE,
        "label": "Default announce rate grace",
        "kind": "int",
        "default": "5",
        "description": (
            "How many times a peer may announce faster than the target before it is blocked. "
            "A small number punishes normal bursts, such as a peer restarting."
        ),
    },
    {
        "key": "ic_max_held_announces",
        "section": "reticulum",
        "category": CATEGORY_INGRESS,
        "label": "Max held announces",
        "kind": "int",
        "default": "256",
        "description": (
            "How many announces an interface may hold back while ingress limiting is active. "
            "A larger buffer loses fewer announces during a flood, at the cost of memory; a "
            "smaller one drops them outright."
        ),
    },
    {
        "key": "ic_burst_hold",
        "section": "reticulum",
        "category": CATEGORY_INGRESS,
        "label": "Burst hold (s)",
        "kind": "float",
        "default": "15",
        "description": (
            "How long ingress limiting stays on after the announce rate falls back below the "
            "threshold. Longer avoids flapping in and out of limiting; it also delays "
            "announces after the flood is over."
        ),
    },
    {
        "key": "ic_burst_freq_new",
        "section": "reticulum",
        "category": CATEGORY_INGRESS,
        "label": "Burst frequency, new interface (Hz)",
        "kind": "float",
        "default": "3",
        "description": (
            "Incoming announces per second that trip ingress limiting on an interface younger "
            "than the new-interface time. It is deliberately low, because a fresh interface "
            "gets a burst of history; raising it lets that burst straight through."
        ),
    },
    {
        "key": "ic_burst_freq",
        "section": "reticulum",
        "category": CATEGORY_INGRESS,
        "label": "Burst frequency (Hz)",
        "kind": "float",
        "default": "10",
        "description": (
            "Incoming announces per second that trip ingress limiting on a settled interface. "
            "Lower protects constrained links from announce floods but delays first contact "
            "from new peers; higher lets floods through."
        ),
    },
    {
        "key": "ic_pr_burst_freq_new",
        "section": "reticulum",
        "category": CATEGORY_INGRESS,
        "label": "Path request burst frequency, new interface (Hz)",
        "kind": "float",
        "default": "3",
        "description": (
            "Incoming path requests per second that trip limiting on an interface younger than "
            "the new-interface time. Same trade as the announce equivalent: too low and peers "
            "wait longer for a path to resolve."
        ),
    },
    {
        "key": "ic_pr_burst_freq",
        "section": "reticulum",
        "category": CATEGORY_INGRESS,
        "label": "Path request burst frequency (Hz)",
        "kind": "float",
        "default": "8",
        "description": (
            "Incoming path requests per second that trip limiting on a settled interface. "
            "Lower shields a slow link from path-request storms, at the price of slower path "
            "resolution for everyone on it."
        ),
    },
    {
        "key": "ec_pr_freq",
        "section": "reticulum",
        "category": CATEGORY_INGRESS,
        "label": "Egress path request frequency (Hz)",
        "kind": "float",
        "default": "5",
        "description": (
            "Outgoing path requests per second above which this node throttles itself, when "
            "egress control is on. Lower keeps this node from flooding a shared medium; too "
            "low and its own lookups stall."
        ),
    },
    {
        "key": "egress_control",
        "section": "reticulum",
        "category": CATEGORY_INGRESS,
        "label": "Egress control",
        "kind": "bool",
        "default": "No",
        "description": (
            "Rate-limit the path requests this node sends, not just the ones it receives. "
            "It makes this node a better citizen on a congested shared medium, and slows its "
            "own path lookups when it is busy."
        ),
    },
    {
        "key": "ic_new_time",
        "section": "reticulum",
        "category": CATEGORY_INGRESS,
        "label": "New interface time (s)",
        "kind": "float",
        "default": "7200",
        "description": (
            "How long an interface counts as new and uses the stricter burst thresholds. "
            "Longer keeps a link cautious well past startup; shorter lets it accept high "
            "announce rates sooner."
        ),
    },
    {
        "key": "ic_burst_penalty",
        "section": "reticulum",
        "category": CATEGORY_INGRESS,
        "label": "Burst penalty (s)",
        "kind": "float",
        "default": "15",
        "description": (
            "How long held announces are kept back after limiting is first triggered. Longer "
            "gives a flood time to subside; it also delays every legitimate announce caught in "
            "the same burst."
        ),
    },
    {
        "key": "ic_held_release_interval",
        "section": "reticulum",
        "category": CATEGORY_INGRESS,
        "label": "Held release interval (s)",
        "kind": "float",
        "default": "5",
        "description": (
            "Seconds between releasing one held announce and the next once the rate has "
            "settled. Longer drains the backlog more gently; with a large backlog it can take "
            "a long time for the last peer to become reachable."
        ),
    },
]

_OPTIONS_BY_KEY: dict[str, dict] = {opt["key"]: opt for opt in RETICULUM_OPTIONS}


def _raw_value(section: dict, key: str) -> str:
    """Read one key as the string form the editor writes back.

    ConfigObj parses a comma-separated value into a list; flatten those.
    """
    if key not in section:
        return ""
    value = section[key]
    if isinstance(value, list):
        return ", ".join(str(v) for v in value)
    return str(value)


def load_reticulum_config(config_path: str) -> list[dict]:
    """Read the [reticulum] and [logging] sections of the Reticulum config.

    Returns the schema entries in RETICULUM_OPTIONS order, each with a "value"
    holding the current raw setting, or "" when the key is unset. A missing or
    unreadable config file leaves every option unset.
    """
    sections: dict[str, dict] = {}
    if os.path.isfile(config_path):
        try:
            cfg = ConfigObj(config_path)
        except Exception:
            cfg = None
        if cfg is not None:
            for name in ("reticulum", "logging"):
                section = cfg.get(name, {})
                if isinstance(section, dict):
                    sections[name] = section

    result = []
    for option in RETICULUM_OPTIONS:
        entry = dict(option)
        entry["value"] = _raw_value(sections.get(option["section"], {}), option["key"])
        result.append(entry)
    return result


def _validate_bool(key: str, value: str) -> str:
    lowered = value.lower()
    if lowered in _TRUE_VALUES:
        return "Yes"
    if lowered in _FALSE_VALUES:
        return "No"
    raise InterfaceConfigError(f"'{key}' must be a yes/no value")


def _validate_int(key: str, value: str) -> str:
    try:
        parsed = int(value)
    except ValueError as e:
        raise InterfaceConfigError(f"'{key}' must be a whole number") from e
    if key == "loglevel" and not MIN_LOGLEVEL <= parsed <= MAX_LOGLEVEL:
        raise InterfaceConfigError(
            f"'{key}' must be between {MIN_LOGLEVEL} and {MAX_LOGLEVEL}")
    return str(parsed)


def _validate_float(key: str, value: str) -> str:
    try:
        float(value)
    except ValueError as e:
        raise InterfaceConfigError(f"'{key}' must be a number") from e
    return value


def _validate_choice(key: str, value: str, choices: list[str]) -> str:
    lowered = value.lower()
    if lowered not in choices:
        raise InterfaceConfigError(f"'{key}' must be one of: {', '.join(choices)}")
    return lowered


def _validate_hex(key: str, value: str) -> str:
    try:
        bytes.fromhex(value)
    except ValueError as e:
        raise InterfaceConfigError(f"'{key}' must be a hexadecimal string") from e
    return value


def _validate_hash_list(key: str, value: str) -> str:
    entries = [part.strip() for part in value.split(",")]
    entries = [part for part in entries if part]
    if not entries:
        raise InterfaceConfigError(f"'{key}' must list at least one identity hash")
    for entry in entries:
        if len(entry) != HASH_HEX_LENGTH:
            raise InterfaceConfigError(
                f"'{key}' entry '{entry}' must be {HASH_HEX_LENGTH} hexadecimal characters")
        try:
            bytes.fromhex(entry)
        except ValueError as e:
            raise InterfaceConfigError(
                f"'{key}' entry '{entry}' is not a valid identity hash") from e
    return ", ".join(entries)


def _validated_value(option: dict, value: str) -> str:
    key = option["key"]
    match option["kind"]:
        case "bool":
            return _validate_bool(key, value)
        case "int":
            return _validate_int(key, value)
        case "float":
            return _validate_float(key, value)
        case "choice":
            return _validate_choice(key, value, option.get("choices", []))
        case "hex":
            return _validate_hex(key, value)
        case "hash_list":
            return _validate_hash_list(key, value)
        case _:
            return value


def write_reticulum_config(config_path: str, values: dict[str, str]) -> None:
    """Write node-wide options to the [reticulum] and [logging] sections.

    Only the supplied keys are touched, so everything else in the file --
    [interfaces], comments, and options this schema does not cover -- survives.
    An empty value removes the key, returning that option to the RNS default.

    Raises InterfaceConfigError naming the offending key when a key is unknown
    or its value fails validation, and on file read/write errors.
    """
    validated: dict[str, tuple[str, str | None]] = {}
    for key, raw in values.items():
        option = _OPTIONS_BY_KEY.get(key)
        if option is None:
            raise InterfaceConfigError(f"'{key}' is not a known Reticulum option")
        value = str(raw).strip()
        validated[key] = (
            option["section"],
            None if not value else _validated_value(option, value),
        )

    try:
        file_cfg = ConfigObj(config_path)
    except Exception as e:
        raise InterfaceConfigError(f"could not read config file: {e}") from e

    for key, (section_name, value) in validated.items():
        if section_name not in file_cfg or not isinstance(file_cfg[section_name], dict):
            file_cfg[section_name] = {}
        section = file_cfg[section_name]
        if value is None:
            section.pop(key, None)
        else:
            section[key] = value

    try:
        file_cfg.write()
    except Exception as e:
        raise InterfaceConfigError(f"could not write config file: {e}") from e

    RNS.log(f"TrenchChat [reticulum]: updated {len(validated)} node-wide "
            "config option(s)", RNS.LOG_NOTICE)
