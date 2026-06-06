"""
Loads config.yaml at startup and provides a singleton `rc`.
The file is re-read on every rc.get() call so edits take effect
without a container restart.
"""
import logging
import os
from typing import Any, Optional

import yaml

log = logging.getLogger(__name__)
CONFIG_PATH = os.environ.get("CONFIG_PATH", "/app/config.yaml")
_LOCAL_PATH = "config.yaml"  # for local dev outside Docker


class RuntimeConfig:
    def get(self, key: str, default: Any = None) -> Any:
        try:
            path = CONFIG_PATH if os.path.exists(CONFIG_PATH) else _LOCAL_PATH
            with open(path) as f:
                data = yaml.safe_load(f) or {}
            # support dot-notation: "scraping.roles"
            parts = key.split(".")
            val = data
            for p in parts:
                if not isinstance(val, dict):
                    return default
                val = val.get(p)
                if val is None:
                    return default
            return val
        except Exception as e:
            log.warning("RuntimeConfig.get(%s) failed: %s", key, e)
            return default

    def set_nested(self, key: str, value: Any) -> bool:
        """Write a top-level or dot-nested key back to config.yaml."""
        try:
            path = CONFIG_PATH if os.path.exists(CONFIG_PATH) else _LOCAL_PATH
            with open(path) as f:
                data = yaml.safe_load(f) or {}
            parts = key.split(".")
            d = data
            for p in parts[:-1]:
                d = d.setdefault(p, {})
            d[parts[-1]] = value
            with open(path, "w") as f:
                yaml.dump(data, f, allow_unicode=True, default_flow_style=False)
            return True
        except Exception as e:
            log.error("RuntimeConfig.set(%s) failed: %s", key, e)
            return False


rc = RuntimeConfig()
