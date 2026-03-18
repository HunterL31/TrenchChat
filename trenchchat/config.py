import json
import os
from pathlib import Path

DATA_DIR = Path.home() / ".trenchchat"
CONFIG_PATH = DATA_DIR / "config.json"

_DEFAULTS = {
    "display_name": "Anonymous",
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
}


def _deep_merge(base: dict, override: dict) -> dict:
    result = dict(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


class Config:
    def __init__(self):
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self._data: dict = _deep_merge(_DEFAULTS, self._load_from_disk())

    def _load_from_disk(self) -> dict:
        if CONFIG_PATH.exists():
            try:
                with open(CONFIG_PATH, "r") as f:
                    return json.load(f)
            except (json.JSONDecodeError, OSError):
                pass
        return {}

    def save(self):
        with open(CONFIG_PATH, "w") as f:
            json.dump(self._data, f, indent=2)

    # --- display name ---

    @property
    def display_name(self) -> str:
        return self._data["display_name"]

    @display_name.setter
    def display_name(self, value: str):
        self._data["display_name"] = value
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
        assert value in ("allowlist", "all")
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

    # --- outbound propagation node ---

    @property
    def outbound_propagation_node(self) -> str | None:
        return self._data.get("outbound_propagation_node")

    @outbound_propagation_node.setter
    def outbound_propagation_node(self, value: str | None):
        self._data["outbound_propagation_node"] = value
        self.save()
