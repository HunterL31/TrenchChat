import base64
import copy
import json
import os
from pathlib import Path

import RNS

from trenchchat.core.fileutils import atomic_write_bytes

DATA_DIR = Path.home() / ".trenchchat"
CONFIG_PATH = DATA_DIR / "config.json"

_DEFAULT_DATA_DIR = DATA_DIR

_DEFAULTS = {
    "display_name": "Anonymous",
    "avatar_bytes": None,
    "avatar_version": 0,
    "propagation_node": {
        "enabled": False,
        "node_name": "",
        "storage_limit_mb": 256,
        "channel_filter": {
            "mode": "allowlist",
            "channel_hashes": [],
        },
    },
    "outbound_propagation_node": None,
    "ui_theme": {},
    "ui_theme_library": {},
    "voice": {
        "input_device": None,
        "output_device": None,
        "mode": "vad",
        "bitrate": 16000,
        "vad_threshold_db": -45.0,
    },
}


def _deep_merge(base: dict, override: dict) -> dict:
    # Must deep-copy: setters mutate nested dicts (e.g. "propagation_node")
    # in place, and a shallow copy would leave those as shared references
    # to _DEFAULTS -- and to every other Config instance's data.
    result = copy.deepcopy(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


class Config:
    def __init__(self, data_dir: Path | None = None):
        self._data_dir = data_dir or _DEFAULT_DATA_DIR
        self._config_path = self._data_dir / "config.json"
        self._data_dir.mkdir(parents=True, exist_ok=True)
        self._data: dict = _deep_merge(_DEFAULTS, self._load_from_disk())

    def _load_from_disk(self) -> dict:
        if self._config_path.exists():
            try:
                with open(self._config_path, "r") as f:
                    return json.load(f)
            except (json.JSONDecodeError, OSError) as e:
                RNS.log(f"TrenchChat: failed to load config from disk: {e}", RNS.LOG_WARNING)
        return {}

    def save(self):
        atomic_write_bytes(
            self._config_path,
            json.dumps(self._data, indent=2).encode("utf-8"),
        )

    # --- display name ---

    @property
    def display_name(self) -> str:
        return self._data["display_name"]

    @display_name.setter
    def display_name(self, value: str):
        self._data["display_name"] = value
        self.save()

    # --- avatar ---

    @property
    def avatar_bytes(self) -> bytes | None:
        """The local user's current avatar as raw JPEG bytes, or None if not set."""
        encoded = self._data.get("avatar_bytes")
        if not encoded:
            return None
        try:
            return base64.b64decode(encoded)
        except Exception:
            return None

    @avatar_bytes.setter
    def avatar_bytes(self, value: bytes | None):
        self._data["avatar_bytes"] = base64.b64encode(value).decode() if value else None
        self.save()

    @property
    def avatar_version(self) -> int:
        """Monotonic counter incremented each time the avatar changes."""
        return int(self._data.get("avatar_version", 0))

    @avatar_version.setter
    def avatar_version(self, value: int):
        self._data["avatar_version"] = value
        self.save()

    # --- propagation node ---

    @property
    def propagation_enabled(self) -> bool:
        return self._data["propagation_node"]["enabled"]

    @propagation_enabled.setter
    def propagation_enabled(self, value: bool):
        self._data["propagation_node"]["enabled"] = value
        self.save()

    @property
    def propagation_node_name(self) -> str:
        return self._data["propagation_node"]["node_name"]

    @propagation_node_name.setter
    def propagation_node_name(self, value: str):
        self._data["propagation_node"]["node_name"] = value
        self.save()

    @property
    def propagation_storage_limit_mb(self) -> int:
        return self._data["propagation_node"]["storage_limit_mb"]

    @propagation_storage_limit_mb.setter
    def propagation_storage_limit_mb(self, value: int):
        self._data["propagation_node"]["storage_limit_mb"] = value
        self.save()

    @property
    def channel_filter_mode(self) -> str:
        return self._data["propagation_node"]["channel_filter"]["mode"]

    @channel_filter_mode.setter
    def channel_filter_mode(self, value: str):
        # Not an assert: stripped under `python -O`.
        if value not in ("allowlist", "all"):
            raise ValueError(
                f"channel_filter_mode must be 'allowlist' or 'all', got {value!r}"
            )
        self._data["propagation_node"]["channel_filter"]["mode"] = value
        self.save()

    @property
    def channel_filter_hashes(self) -> list[str]:
        return self._data["propagation_node"]["channel_filter"]["channel_hashes"]

    def add_channel_filter_hash(self, hex_hash: str):
        hashes = self.channel_filter_hashes
        if hex_hash not in hashes:
            hashes.append(hex_hash)
            self.save()

    def remove_channel_filter_hash(self, hex_hash: str):
        hashes = self.channel_filter_hashes
        if hex_hash in hashes:
            hashes.remove(hex_hash)
            self.save()

    def set_channel_filter_hashes(self, hashes: list[str]) -> None:
        """Replace the full set of channel filter hashes."""
        self._data["propagation_node"]["channel_filter"]["channel_hashes"] = hashes
        self.save()

    # --- voice ---

    @property
    def voice_input_device(self) -> str | int | None:
        """sounddevice input device name/index; None means system default."""
        return self._data["voice"]["input_device"]

    @voice_input_device.setter
    def voice_input_device(self, value: str | int | None):
        self._data["voice"]["input_device"] = value
        self.save()

    @property
    def voice_output_device(self) -> str | int | None:
        return self._data["voice"]["output_device"]

    @voice_output_device.setter
    def voice_output_device(self, value: str | int | None):
        self._data["voice"]["output_device"] = value
        self.save()

    @property
    def voice_mode(self) -> str:
        return self._data["voice"]["mode"]

    @voice_mode.setter
    def voice_mode(self, value: str):
        if value not in ("vad", "ptt"):
            raise ValueError(f"voice_mode must be 'vad' or 'ptt', got {value!r}")
        self._data["voice"]["mode"] = value
        self.save()

    @property
    def voice_bitrate(self) -> int:
        return int(self._data["voice"]["bitrate"])

    @voice_bitrate.setter
    def voice_bitrate(self, value: int):
        self._data["voice"]["bitrate"] = int(value)
        self.save()

    @property
    def voice_vad_threshold_db(self) -> float:
        return float(self._data["voice"]["vad_threshold_db"])

    @voice_vad_threshold_db.setter
    def voice_vad_threshold_db(self, value: float):
        self._data["voice"]["vad_threshold_db"] = float(value)
        self.save()

    # --- outbound propagation node ---

    @property
    def outbound_propagation_node(self) -> str | None:
        return self._data.get("outbound_propagation_node")

    @outbound_propagation_node.setter
    def outbound_propagation_node(self, value: str | None):
        self._data["outbound_propagation_node"] = value
        self.save()

    # --- ui theme ---

    @property
    def ui_theme(self) -> dict:
        """Client-side theme object, stored opaquely. Empty dict when never set."""
        return self._data.get("ui_theme", {})

    @ui_theme.setter
    def ui_theme(self, value: dict):
        self._data["ui_theme"] = value
        self.save()

    # --- ui theme library ---

    @property
    def ui_theme_library(self) -> dict:
        """Saved themes by name, stored opaquely. Empty dict when never set."""
        return self._data.get("ui_theme_library", {})

    def save_ui_theme(self, name: str, theme: dict) -> None:
        """Store a theme under a name, replacing any theme already saved there."""
        library = self._data.setdefault("ui_theme_library", {})
        library[name] = theme
        self.save()

    def delete_ui_theme(self, name: str) -> bool:
        """Remove a saved theme. False when no theme is stored under that name."""
        library = self._data.setdefault("ui_theme_library", {})
        if name not in library:
            return False
        del library[name]
        self.save()
        return True
