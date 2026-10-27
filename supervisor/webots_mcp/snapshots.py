"""Snapshot storage for repeatable experiments."""
from .runtime import root_children

# ============================================================
# Snapshots (repeatable experiments without reloading the world)
# ============================================================

snapshots = {}
# node id -> include_descendants flag it was enabled with (contact tracking is per node)
contact_tracking = {}


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


