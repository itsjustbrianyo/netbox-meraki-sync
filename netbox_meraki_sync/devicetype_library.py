"""
Enrich Meraki DeviceTypes using the NetBox Community Device Type Library.

https://github.com/netbox-community/devicetype-library

Only metadata (u_height, weight, airflow, part_number, comments) and
elevation images are pulled in — NOT the library's `interfaces:`
template. This plugin creates real interfaces per-port from live
Meraki data, so importing template interfaces first would just create
placeholders the real sync then has to reconcile around.

The repo's file listing is fetched once via the GitHub Trees API and
cached to disk (default 7 days), since unauthenticated GitHub API
calls are rate-limited to ~60/hour. Individual YAML/image fetches use
raw.githubusercontent.com, which is not subject to that limit.
"""

import json
import logging
import os
import re
import time
from typing import Optional

try:
    import yaml
except ImportError:  # pragma: no cover - pyyaml should already be a NetBox dependency
    yaml = None

import requests

log = logging.getLogger(__name__)

_REPO = "netbox-community/devicetype-library"
_API_TREE_URL = f"https://api.github.com/repos/{_REPO}/git/trees/{{ref}}?recursive=1"
_RAW_BASE = f"https://raw.githubusercontent.com/{_REPO}/{{ref}}"
_INDEX_FILENAME = "devicetype_library_index.json"


class DeviceTypeLibrary:
    """
    Looks up device type definitions and elevation images for Meraki
    models from the community library. One instance is reused for the
    life of a sync run so its in-memory YAML cache is shared across
    devices.
    """

    def __init__(
        self,
        ref: str = "master",
        cache_dir: str = "/tmp/netbox-meraki-sync-cache",
        cache_days: int = 7,
        timeout: int = 15,
    ):
        self.ref = ref
        self.cache_dir = cache_dir
        self.cache_days = cache_days
        self.timeout = timeout
        self._index: Optional[dict] = None   # path -> True, lazily built
        self._yaml_cache: dict = {}          # path -> parsed dict or None

    # ------------------------------------------------------------------
    # Repo index (list of Meraki-related YAML paths), disk-cached
    # ------------------------------------------------------------------

    def _index_path(self) -> str:
        return os.path.join(self.cache_dir, _INDEX_FILENAME)

    def _load_index(self) -> dict:
        if self._index is not None:
            return self._index

        os.makedirs(self.cache_dir, exist_ok=True)
        path = self._index_path()

        if os.path.exists(path):
            age_days = (time.time() - os.path.getmtime(path)) / 86400
            if age_days < self.cache_days:
                try:
                    with open(path) as f:
                        self._index = json.load(f)
                    return self._index
                except Exception:
                    pass  # fall through and rebuild

        try:
            resp = requests.get(
                _API_TREE_URL.format(ref=self.ref),
                timeout=self.timeout,
                headers={"Accept": "application/vnd.github+json"},
            )
            resp.raise_for_status()
            tree = resp.json().get("tree", [])
        except Exception as exc:
            log.warning("Device Type Library: failed to fetch repo index: %s", exc)
            self._index = {}
            return self._index

        index = {
            entry["path"]: True
            for entry in tree
            if entry.get("path", "").startswith("device-types/")
            and entry["path"].endswith((".yaml", ".yml"))
            and "meraki" in entry["path"].lower()
        }
        self._index = index

        try:
            with open(path, "w") as f:
                json.dump(index, f)
        except Exception as exc:
            log.debug("Device Type Library: could not write index cache: %s", exc)

        return self._index

    # ------------------------------------------------------------------
    # Definition lookup
    # ------------------------------------------------------------------

    @staticmethod
    def _normalize(s: str) -> str:
        return re.sub(r"[\s_-]", "", s).lower()

    def find_definition(self, model: str) -> Optional[dict]:
        """
        Look up a device type definition by Meraki model string, e.g.
        "MS220-8P" or "MR36". Matches loosely against each candidate
        YAML's `model:` field, tolerating the "Meraki " prefix the
        library uses (e.g. "Meraki MS220-8P").
        """
        if yaml is None:
            log.warning("Device Type Library: PyYAML is not installed; skipping enrichment.")
            return None

        index = self._load_index()
        if not index:
            return None

        model_norm = self._normalize(model)
        candidates = [p for p in index if model_norm in self._normalize(p)]

        for path in candidates:
            data = self._fetch_yaml(path)
            if not data:
                continue
            yaml_model_norm = self._normalize(str(data.get("model", "")))
            if model_norm and (model_norm in yaml_model_norm or yaml_model_norm.endswith(model_norm)):
                data["_path"] = path
                return data
        return None

    def _fetch_yaml(self, path: str) -> Optional[dict]:
        if path in self._yaml_cache:
            return self._yaml_cache[path]

        url = f"{_RAW_BASE.format(ref=self.ref)}/{path}"
        data = None
        try:
            resp = requests.get(url, timeout=self.timeout)
            resp.raise_for_status()
            data = yaml.safe_load(resp.text)
        except Exception as exc:
            log.debug("Device Type Library: failed to fetch %s: %s", path, exc)

        self._yaml_cache[path] = data
        return data

    # ------------------------------------------------------------------
    # Elevation images
    # ------------------------------------------------------------------

    def fetch_image(self, definition: dict, face: str) -> Optional[tuple]:
        """
        Fetch front/rear elevation image bytes for a definition, if it
        declares one. `face` is "front" or "rear". Returns (filename,
        content_bytes) or None.
        """
        if not definition.get(f"{face}_image"):
            return None

        slug = definition.get("slug")
        path = definition.get("_path", "")
        if not slug or "/" not in path:
            return None

        manufacturer_dir = path.split("/")[1]  # device-types/<Manufacturer>/...
        for ext in ("png", "jpg", "jpeg", "webp"):
            filename = f"{slug}.{face}.{ext}"
            url = f"{_RAW_BASE.format(ref=self.ref)}/elevation-images/{manufacturer_dir}/{filename}"
            try:
                resp = requests.get(url, timeout=self.timeout)
                if resp.status_code == 200 and resp.content:
                    return filename, resp.content
            except Exception:
                continue
        return None
