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
