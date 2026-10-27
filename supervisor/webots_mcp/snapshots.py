"""Snapshot storage for repeatable experiments.

Snapshots live in memory and are also written to ``<SAVE_DIR>/snapshots/<name>.json``
so they survive a world reload / Webots restart (which restarts this process) and can
be reused across sessions as experiment baselines.
"""
import json
import os
import time

from .config import SAVE_DIR, log
from .runtime import root_children

SNAPSHOT_DIR = SAVE_DIR / "snapshots"

# ============================================================
# Snapshots (repeatable experiments without reloading the world)
# ============================================================

snapshots = {}
# node id -> include_descendants flag it was enabled with (contact tracking is per node)
contact_tracking = {}


def persist_snapshot(name, data):
    """Write one snapshot to disk (atomic). Returns the path."""
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    path = SNAPSHOT_DIR / f"{name}.json"
    tmp = path.with_suffix(".json.tmp")
    payload = {"version": 1, "name": name, "saved_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "nodes": data}
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    return path


def load_snapshot_file(name):
    """Load <name>.json into memory if present; returns the nodes dict or None."""
    path = SNAPSHOT_DIR / f"{name}.json"
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        nodes = payload["nodes"]
        if not isinstance(nodes, dict):
            raise ValueError("nodes is not an object")
        snapshots[name] = nodes
        return nodes
    except (OSError, ValueError, KeyError) as exc:
        log.warning("Ignoring unreadable snapshot file %s: %s", path, exc)
        return None


def load_all_snapshots():
    """Load every snapshot file that is not already in memory; returns the names loaded."""
    loaded = []
    if SNAPSHOT_DIR.is_dir():
        for path in sorted(SNAPSHOT_DIR.glob("*.json")):
            if path.stem not in snapshots and load_snapshot_file(path.stem) is not None:
                loaded.append(path.stem)
    return loaded


def default_snapshot_defs():
    defs = []
    for i in range(root_children.getCount()):
        node = root_children.getMFNode(i)
        if node is None:
            continue
        name = node.getDef()
        if name and node.getField("translation") is not None \
                and node.getField("rotation") is not None:
            defs.append(name)
    return defs

load_all_snapshots()
