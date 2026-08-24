"""
Reticulum interface config file reading and writing.

Pure ConfigObj manipulation, no Qt. trenchchat/gui/interfaces_widget.py builds
the editing UI on top of this and owns the QMessageBox error paths and the
restart-required copy; devtools/testenv/api.py's write endpoints call the
same functions directly.
"""

import os

import RNS
from configobj import ConfigObj

# Interface types that can be created or edited. Anything else already in the
# config file is shown read-only.
EDITABLE_TYPES = [
    "AutoInterface",
    "TCPClientInterface",
    "TCPServerInterface",
    "UDPInterface",
    "SerialInterface",
    "RNodeInterface",
]

# Config keys that must be present (and not "0") for a given interface type.
REQUIRED_FIELDS: dict[str, list[str]] = {
    "TCPClientInterface": ["target_host"],
    "TCPServerInterface": ["listen_ip", "listen_port"],
    "SerialInterface": ["port"],
    "RNodeInterface": ["port"],
    "PipeInterface": ["command"],
}

# Values build_interface_config_dict treats as "unset" and omits.
_OMITTED_VALUES = ("", "0", "0.0")

# Community-hosted entry points offered as one-click bootstrap seeds. Marked
# bootstrap_only so RNS drops them once enough discovered interfaces are
# auto-connected, and brings them back if those connections are lost.
SUGGESTED_DEFAULTS: dict[str, dict[str, str]] = {
    "RMAP Bootstrap": {
        "type": "TCPClientInterface",
        "enabled": "Yes",
        "target_host": "rmap.world",
        "target_port": "4242",
        "connect_timeout": "5",
        "bootstrap_only": "Yes",
    },
    "Sydney RNS Bootstrap": {
        "type": "TCPClientInterface",
        "enabled": "Yes",
        "target_host": "sydney.reticulum.au",
        "target_port": "4242",
        "connect_timeout": "5",
        "bootstrap_only": "Yes",
    },
    "Thunderhost SJC Bootstrap": {
        "type": "TCPClientInterface",
        "enabled": "Yes",
        "target_host": "sjc.us.thunderhost.net",
        "target_port": "4242",
        "connect_timeout": "5",
        "bootstrap_only": "Yes",
    },
}

# [reticulum]-section options the discovery settings functions manage.
DISCOVERY_SETTING_KEYS = (
    "discover_interfaces",
    "autoconnect_discovered_interfaces",
    "required_discovery_value",
)

# Autoconnect count written when discovery is enabled without an explicit one.
DEFAULT_AUTOCONNECT_COUNT = 3

_TRUE_VALUES = ("yes", "true", "on", "1")

# Keys a caller may never supply as a field value: they are decided by the
# validated arguments, and letting a field overwrite "type" turns the
# EDITABLE_TYPES check into a check on a value that is then discarded --
# PipeInterface, which runs a shell command, is reachable that way.
_RESERVED_KEYS = ("type", "enabled")


class InterfaceConfigError(Exception):
    """Raised when reading or writing the Reticulum interface config fails."""


class DuplicateInterfaceError(InterfaceConfigError):
    """Raised by write_interface when is_new and the name is already taken."""


def load_interfaces_config(config_path: str) -> dict[str, dict]:
    """Read the [interfaces] section from the Reticulum config file.

    Returns a dict mapping interface name to its config dict (including 'type').
    Returns an empty dict if the file does not exist or has no [interfaces] section.
    """
    if not os.path.isfile(config_path):
        return {}
    try:
        cfg = ConfigObj(config_path)
    except Exception:
        return {}
    interfaces_section = cfg.get("interfaces", {})
    result = {}
    for name, section in interfaces_section.items():
        if isinstance(section, dict):
            result[name] = dict(section)
    return result


def build_interface_config_dict(
    name: str,
    iface_type: str,
    enabled: bool,
    type_values: dict[str, str],
    common_values: dict[str, str],
) -> dict[str, str]:
    """Assemble a flat config dict for a single interface section.

    All values are stored as strings (ConfigObj INI format). A value of ""
    or "0"/"0.0" is treated as unset and omitted, matching how the fields'
    QSpinBox/QLineEdit defaults surface an untouched field.
    """
    if iface_type not in EDITABLE_TYPES:
        raise InterfaceConfigError(f"'{iface_type}' is not an editable interface type")
    for values in (type_values, common_values):
        for key in values:
            if key in _RESERVED_KEYS:
                raise InterfaceConfigError(f"'{key}' cannot be set as a field value")

    cfg: dict[str, str] = {"type": iface_type, "enabled": "Yes" if enabled else "No"}
    for key, value in type_values.items():
        str_value = str(value)
        if str_value not in _OMITTED_VALUES:
            cfg[key] = str_value
    for key, value in common_values.items():
        str_value = str(value)
        if str_value not in _OMITTED_VALUES:
            cfg[key] = str_value
    return cfg


def missing_required_field(iface_type: str, values: dict[str, str]) -> str | None:
    """Return the first required config key that's empty or "0" for this
    interface type, or None if every required field is present."""
    for key in REQUIRED_FIELDS.get(iface_type, []):
        value = values.get(key, "")
        if not value or value == "0":
            return key
    return None


def write_interface(config_path: str, name: str, cfg_dict: dict[str, str],
                    is_new: bool) -> None:
    """Write one interface section to the Reticulum config file.

    Raises DuplicateInterfaceError if is_new and an interface with this name
    already exists, or InterfaceConfigError if the type is not editable or the
    file can't be read or written.

    The type is re-checked here rather than trusted from the caller: this is
    the last layer before an interface class reaches the Reticulum config, and
    some of the classes RNS will load run a subprocess.
    """
    cfg_type = cfg_dict.get("type", "")
    if cfg_type not in EDITABLE_TYPES:
        raise InterfaceConfigError(f"'{cfg_type}' is not an editable interface type")

    try:
        file_cfg = ConfigObj(config_path)
    except Exception as e:
        raise InterfaceConfigError(f"could not read config file: {e}") from e

    if "interfaces" not in file_cfg:
        file_cfg["interfaces"] = {}

    if is_new and name in file_cfg["interfaces"]:
        raise DuplicateInterfaceError(f"an interface named '{name}' already exists")

    file_cfg["interfaces"][name] = cfg_dict
    try:
        file_cfg.write()
    except Exception as e:
        raise InterfaceConfigError(f"could not write config file: {e}") from e

    action = "added" if is_new else "updated"
    RNS.log(f"TrenchChat [interfaces]: {action} interface '{name}'", RNS.LOG_NOTICE)


def write_interfaces_bulk(config_path: str, entries: dict[str, dict[str, str]]) -> None:
    """Write multiple interface sections at once (e.g. suggested defaults).

    Raises InterfaceConfigError if the file can't be read or written.
    """
    try:
        file_cfg = ConfigObj(config_path)
    except Exception as e:
        raise InterfaceConfigError(f"could not read config file: {e}") from e

    if "interfaces" not in file_cfg:
        file_cfg["interfaces"] = {}

    for name, cfg_dict in entries.items():
        file_cfg["interfaces"][name] = cfg_dict

    try:
        file_cfg.write()
    except Exception as e:
        raise InterfaceConfigError(f"could not write config file: {e}") from e

    for name in entries:
        RNS.log(f"TrenchChat [interfaces]: added suggested default '{name}'", RNS.LOG_NOTICE)


def get_missing_suggested_defaults(config_path: str) -> dict[str, dict[str, str]]:
    """Return suggested defaults not already present in the config.

    An interface is considered present if any existing interface has the same
    target_host and target_port as the suggested default, regardless of the
    name it was saved under.
    """
    existing = load_interfaces_config(config_path)
    existing_endpoints: set[tuple[str, str]] = {
        (iface.get("target_host", ""), iface.get("target_port", ""))
        for iface in existing.values()
    }
    result = {}
    for name, cfg in SUGGESTED_DEFAULTS.items():
        endpoint = (cfg.get("target_host", ""), cfg.get("target_port", ""))
        if endpoint not in existing_endpoints:
            result[name] = cfg
    return result


def load_discovery_settings(config_path: str) -> dict:
    """Read the interface-discovery options from the [reticulum] section.

    Returns discover_interfaces (bool), autoconnect_discovered_interfaces
    (int, 0 when unset) and required_discovery_value (int | None).
    """
    settings: dict = {
        "discover_interfaces": False,
        "autoconnect_discovered_interfaces": 0,
        "required_discovery_value": None,
    }
    if not os.path.isfile(config_path):
        return settings
    try:
        cfg = ConfigObj(config_path)
    except Exception:
        return settings
    section = cfg.get("reticulum", {})
    if not isinstance(section, dict):
        return settings

    value = str(section.get("discover_interfaces", "")).lower()
    settings["discover_interfaces"] = value in _TRUE_VALUES
    try:
        settings["autoconnect_discovered_interfaces"] = int(
            section.get("autoconnect_discovered_interfaces", 0))
    except (TypeError, ValueError):
        pass
    try:
        required = int(section.get("required_discovery_value", 0))
        settings["required_discovery_value"] = required if required > 0 else None
    except (TypeError, ValueError):
        pass
    return settings


def write_discovery_settings(config_path: str, discover_interfaces: bool,
                             autoconnect_discovered_interfaces: int,
                             required_discovery_value: int | None = None) -> None:
    """Write the interface-discovery options to the [reticulum] section.

    Only the keys in DISCOVERY_SETTING_KEYS are touched; everything else in
    the section is preserved. Raises InterfaceConfigError on file errors.
    """
    try:
        file_cfg = ConfigObj(config_path)
    except Exception as e:
        raise InterfaceConfigError(f"could not read config file: {e}") from e

    if "reticulum" not in file_cfg or not isinstance(file_cfg["reticulum"], dict):
        file_cfg["reticulum"] = {}
    section = file_cfg["reticulum"]

    section["discover_interfaces"] = "Yes" if discover_interfaces else "No"
    if autoconnect_discovered_interfaces > 0:
        section["autoconnect_discovered_interfaces"] = str(autoconnect_discovered_interfaces)
    else:
        section.pop("autoconnect_discovered_interfaces", None)
    if required_discovery_value is not None and required_discovery_value > 0:
        section["required_discovery_value"] = str(required_discovery_value)
    else:
        section.pop("required_discovery_value", None)

    try:
        file_cfg.write()
    except Exception as e:
        raise InterfaceConfigError(f"could not write config file: {e}") from e

    RNS.log("TrenchChat [interfaces]: updated discovery settings", RNS.LOG_NOTICE)


def apply_suggested_defaults(config_path: str) -> list[str]:
    """Write missing bootstrap seeds and enable interface discovery.

    The seeds are bootstrap_only, so they are only useful with discovery and
    auto-connection on; enabling both together keeps the config coherent.
    Returns the names of the interfaces added.
    """
    missing = get_missing_suggested_defaults(config_path)
    if missing:
        write_interfaces_bulk(config_path, missing)
    settings = load_discovery_settings(config_path)
    autoconnect = settings["autoconnect_discovered_interfaces"] or DEFAULT_AUTOCONNECT_COUNT
    if (not settings["discover_interfaces"]
            or settings["autoconnect_discovered_interfaces"] <= 0):
        write_discovery_settings(config_path, True, autoconnect,
                                 settings["required_discovery_value"])
    return list(missing)


def default_rns_config_path(configdir: str | None = None) -> str:
    """Resolve the config file path RNS will use, mirroring RNS.Reticulum's
    configdir fallback, without initialising RNS."""
    if configdir:
        return os.path.join(configdir, "config")
    if os.path.isdir("/etc/reticulum") and os.path.isfile("/etc/reticulum/config"):
        return "/etc/reticulum/config"
    userdir = os.path.expanduser("~")
    xdg_dir = os.path.join(userdir, ".config", "reticulum")
    if os.path.isdir(xdg_dir) and os.path.isfile(os.path.join(xdg_dir, "config")):
        return os.path.join(xdg_dir, "config")
    return os.path.join(userdir, ".reticulum", "config")


def seed_initial_config(config_path: str) -> bool:
    """Create a first-run Reticulum config: an AutoInterface for local peers,
    the bootstrap seeds, and interface discovery enabled.

    Only acts when no config file exists yet, so an existing install -- even
    one whose defaults were deliberately removed -- is never touched. Returns
    True when the config was created.
    """
    if os.path.isfile(config_path):
        return False

    parent = os.path.dirname(config_path)
    if parent:
        os.makedirs(parent, exist_ok=True)

    file_cfg = ConfigObj()
    file_cfg.filename = config_path
    file_cfg["reticulum"] = {
        "discover_interfaces": "Yes",
        "autoconnect_discovered_interfaces": str(DEFAULT_AUTOCONNECT_COUNT),
    }
    interfaces: dict[str, dict[str, str]] = {
        "Default Interface": {"type": "AutoInterface", "enabled": "Yes"},
    }
    interfaces.update(SUGGESTED_DEFAULTS)
    file_cfg["interfaces"] = interfaces
    try:
        file_cfg.write()
    except Exception as e:
        raise InterfaceConfigError(f"could not write config file: {e}") from e

    RNS.log("TrenchChat [interfaces]: created first-run config with bootstrap "
            "seeds and interface discovery enabled", RNS.LOG_NOTICE)
    return True


def delete_interface(config_path: str, name: str) -> bool:
    """Delete an interface section from the Reticulum config file.

    Returns False if no interface with this name exists. Raises
    InterfaceConfigError if the file can't be read or written.
    """
    try:
        file_cfg = ConfigObj(config_path)
    except Exception as e:
        raise InterfaceConfigError(f"could not read config file: {e}") from e

    interfaces = file_cfg.get("interfaces", {})
    if name not in interfaces:
        return False

    del interfaces[name]
    file_cfg["interfaces"] = interfaces
    try:
        file_cfg.write()
    except Exception as e:
        raise InterfaceConfigError(f"could not write config file: {e}") from e

    RNS.log(f"TrenchChat [interfaces]: deleted interface '{name}'", RNS.LOG_NOTICE)
    return True
