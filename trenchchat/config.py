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
    },
    "ui_theme": {},
    "ui_theme_library": {},
    "voice": {
        "input_device": None,
        "output_device": None,
        "mode": "vad",
        "bitrate": 16000,
        "vad_threshold_db": -45.0,
    },
    "nomad_node": {
        "enabled": False,
        "node_name": "",
    },
}

# Opus bitrate bounds. The upper bound keeps VBR peaks under the 255-byte
# voice_wire frame cap (VOICE_MAX_FRAME_BYTES); the lower is Opus's practical
# floor for intelligible speech.
VOICE_MIN_BITRATE = 6000
VOICE_MAX_BITRATE = 64000

# A propagation-node storage limit below zero is meaningless.
MIN_PROPAGATION_STORAGE_MB = 0

# Caps on the opaquely-stored UI theme data. config.json is re-serialised on
# every setter write, so without these a misbehaving client could grow it
# without bound.
MAX_THEME_BYTES = 64 * 1024
MAX_THEME_LIBRARY_ENTRIES = 100


def _type_matches(default, value) -> bool:
    """Whether an on-disk value may stand in for a default of a known type.

    None defaults are unconstrained (the field is genuinely X | None).
    Numeric defaults accept int or float but not bool; bool defaults accept
    only bool.
    """
    if default is None:
        return True
    if isinstance(default, bool):
        return isinstance(value, bool)
    if isinstance(default, (int, float)):
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if isinstance(default, str):
        return isinstance(value, str)
    if isinstance(default, list):
        return isinstance(value, list)
    if isinstance(default, dict):
        return isinstance(value, dict)
    return True


def _deep_merge(base: dict, override: dict) -> dict:
    # Must deep-copy: setters mutate nested dicts (e.g. "propagation_node")
    # in place, and a shallow copy would leave those as shared references
    # to _DEFAULTS -- and to every other Config instance's data.
    result = copy.deepcopy(base)
    for key, value in override.items():
        if key not in result:
            result[key] = value
            continue
        default = result[key]
        if isinstance(default, dict) and isinstance(value, dict):
            result[key] = _deep_merge(default, value)
        elif _type_matches(default, value):
            result[key] = value
        else:
            RNS.log(
                f"TrenchChat: ignoring config value of wrong type for {key!r} "
                f"(expected {type(default).__name__}, got {type(value).__name__})",
                RNS.LOG_WARNING,
            )
    return result


class Config:
    def __init__(self, data_dir: Path | None = None):
        self._data_dir = data_dir or _DEFAULT_DATA_DIR
        self._config_path = self._data_dir / "config.json"
        self._data_dir.mkdir(parents=True, exist_ok=True)
        self._data: dict = _deep_merge(_DEFAULTS, self._load_from_disk())
        self._drop_retired_keys()

    def _drop_retired_keys(self) -> None:
        """Discard settings that no longer exist.

        _deep_merge carries unknown on-disk keys through untouched, so a
        retired setting would otherwise be rewritten on every save forever.
        """
        self._data.get("propagation_node", {}).pop("channel_filter", None)
        self._data.pop("outbound_propagation_node", None)

    def _load_from_disk(self) -> dict:
        if self._config_path.exists():
            try:
                with open(self._config_path, "r") as f:
                    data = json.load(f)
                if not isinstance(data, dict):
                    RNS.log(
                        "TrenchChat: config on disk is not an object, ignoring",
                        RNS.LOG_WARNING,
                    )
                    return {}
                return data
            except (json.JSONDecodeError, OSError) as e:
                RNS.log(f"TrenchChat: failed to load config from disk: {e}", RNS.LOG_WARNING)
        return {}

    @property
    def data_dir(self) -> Path:
        return self._data_dir

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
        limit = int(value)
        if limit < MIN_PROPAGATION_STORAGE_MB:
            raise ValueError(
                f"propagation_storage_limit_mb must be >= "
                f"{MIN_PROPAGATION_STORAGE_MB}, got {limit}"
            )
        self._data["propagation_node"]["storage_limit_mb"] = limit
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
        bitrate = int(value)
        if not VOICE_MIN_BITRATE <= bitrate <= VOICE_MAX_BITRATE:
            raise ValueError(
                f"voice_bitrate must be between {VOICE_MIN_BITRATE} and "
                f"{VOICE_MAX_BITRATE}, got {bitrate}"
            )
        self._data["voice"]["bitrate"] = bitrate
        self.save()

    @property
    def voice_vad_threshold_db(self) -> float:
        return float(self._data["voice"]["vad_threshold_db"])

    @voice_vad_threshold_db.setter
    def voice_vad_threshold_db(self, value: float):
        self._data["voice"]["vad_threshold_db"] = float(value)
        self.save()

    # --- nomad node hosting ---

    @property
    def nomad_hosting_enabled(self) -> bool:
        return bool(self._data["nomad_node"]["enabled"])

    @nomad_hosting_enabled.setter
    def nomad_hosting_enabled(self, value: bool):
        self._data["nomad_node"]["enabled"] = bool(value)
        self.save()

    @property
    def nomad_node_name(self) -> str:
        return str(self._data["nomad_node"]["node_name"])

    @nomad_node_name.setter
    def nomad_node_name(self, value: str):
        self._data["nomad_node"]["node_name"] = str(value)
        self.save()

    # --- ui theme ---

    @property
    def ui_theme(self) -> dict:
        """Client-side theme object, stored opaquely. Empty dict when never set."""
        return self._data.get("ui_theme", {})

    @ui_theme.setter
    def ui_theme(self, value: dict):
        self._check_theme_size(value, "ui_theme")
        self._data["ui_theme"] = value
        self.save()

    # --- ui theme library ---

    @property
    def ui_theme_library(self) -> dict:
        """Saved themes by name, stored opaquely. Empty dict when never set."""
        return self._data.get("ui_theme_library", {})

    def save_ui_theme(self, name: str, theme: dict) -> None:
        """Store a theme under a name, replacing any theme already saved there.

        Rejects a theme larger than MAX_THEME_BYTES, or a new name once the
        library holds MAX_THEME_LIBRARY_ENTRIES, logging a warning in both cases.
        """
        self._check_theme_size(theme, f"theme {name!r}")
        library = self._data.setdefault("ui_theme_library", {})
        if name not in library and len(library) >= MAX_THEME_LIBRARY_ENTRIES:
            RNS.log(
                f"TrenchChat: rejecting theme {name!r} — library at capacity "
                f"({MAX_THEME_LIBRARY_ENTRIES})",
                RNS.LOG_WARNING,
            )
            raise ValueError(
                f"theme library is full (max {MAX_THEME_LIBRARY_ENTRIES})"
            )
        library[name] = theme
        self.save()

    def _check_theme_size(self, theme: dict, label: str) -> None:
        """Reject a theme whose JSON encoding exceeds MAX_THEME_BYTES."""
        try:
            size = len(json.dumps(theme).encode("utf-8"))
        except (TypeError, ValueError) as e:
            raise ValueError(f"{label} is not JSON-serialisable: {e}") from e
        if size > MAX_THEME_BYTES:
            RNS.log(
                f"TrenchChat: rejecting {label} — {size} bytes exceeds "
                f"{MAX_THEME_BYTES}",
                RNS.LOG_WARNING,
            )
            raise ValueError(
                f"{label} is {size} bytes, exceeds {MAX_THEME_BYTES} limit"
            )

    def delete_ui_theme(self, name: str) -> bool:
        """Remove a saved theme. False when no theme is stored under that name."""
        library = self._data.setdefault("ui_theme_library", {})
        if name not in library:
            return False
        del library[name]
        self.save()
        return True
