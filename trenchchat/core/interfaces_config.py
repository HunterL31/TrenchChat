"""
Reticulum interface config file reading.

Pure function, no Qt. See trenchchat/gui/interfaces_widget.py for the
editing UI built on top of this.
"""

import os

from configobj import ConfigObj


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
