"""STEP files that stand in for features the replay has no native rule for.

`--step-dir DIR` points at a folder with one sub-folder per part, named
`part_folder(part id)` ("/Demo/Plate" -> "Demo_Plate"), holding:

- `<feature id>.step`: the solid a `base_feature` creates (or, for a
  `base_feature` that joins, cuts or intersects a body, its tool solid),
  applied with the feature's own operation;
- `<feature id>.add.step` and/or `<feature id>.remove.step`: a "delta" for a
  feature that modifies one body (a `base_feature` or any unsupported
  feature, such as a fillet): the material the feature adds to its body, and
  the material it removes. They are added, then removed.

Each file becomes a dead body (no history) in the CATIA part. This needs
CATIA V5: 3DEXPERIENCE has no scripted STEP import. `catia_mcp.tree.
step_fallback` writes these files from a tree and the model's solids.
Nothing here needs CATIA or build123d.
"""

from __future__ import annotations

import os
import re
from typing import Any

SUFFIXES = (".step", ".stp")
DELTA_ROLES = ("add", "remove")


def part_folder(part_id: str) -> str:
    """A file-system-safe folder name for a part id."""
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", part_id.strip("/")).strip("._")
    return name or "part"


class StepSource:
    """The STEP files of a --step-dir, and whether this platform can use them.

    `platform` is "v5", "3dx", or None when not known yet (`check` without a
    connection): then files are accepted and `build` checks again once
    connected."""

    def __init__(self, directory: str, platform: str | None = None) -> None:
        self.directory = directory
        self.platform = platform

    def _find(self, part_id: str, stem: str) -> str | None:
        folder = os.path.join(self.directory, part_folder(part_id))
        for suffix in SUFFIXES:
            path = os.path.join(folder, stem + suffix)
            if os.path.isfile(path):
                return path
        return None

    def files(self, part_id: str, feature: dict[str, Any]) -> list[tuple[str, str]]:
        """(path, role) pairs for a feature, in the order they apply. The
        role is "solid" (apply with the feature's operation), "add" or
        "remove"."""
        fid = str(feature.get("id"))
        if feature.get("type") == "base_feature":
            solid = self._find(part_id, fid)
            if solid:
                return [(solid, "solid")]
        found = []
        for role in DELTA_ROLES:
            path = self._find(part_id, f"{fid}.{role}")
            if path:
                found.append((path, role))
        return found

    def refusal(self, part_id: str, feature: dict[str, Any], bodies: set[str]) -> str | None:
        """None when the files can stand in for this feature, else why not.
        Updates `bodies` with what the feature creates when accepted."""
        fid = feature.get("id")
        folder = os.path.join(self.directory, part_folder(part_id))
        if self.platform == "3dx":
            return ("no STEP fallback on 3DEXPERIENCE (no scripted STEP import: import "
                    "the STEP by hand and paste it with catia_paste_dead_body)")
        files = self.files(part_id, feature)
        if not files:
            return f"no STEP file for it in {folder} ({fid}.step or {fid}.add/.remove.step)"
        roles = {role for _, role in files}
        operation = feature.get("operation") or "new_body"
        created = feature.get("creates") or []
        targets = feature.get("bodies") or []
        if "solid" in roles:
            if operation == "new_body":
                if len(created) != 1:
                    return "a new-body base_feature must create exactly one body"
                bodies.update(created)
                return None
            if operation not in ("join", "cut", "intersect"):
                return f"base_feature operation '{operation}' is not supported"
        elif created:
            return ("a feature that creates bodies cannot be replaced by add/remove "
                    "deltas; give the base_feature solid instead")
        if len(targets) != 1 or targets[0] not in bodies:
            return "its STEP files must apply to exactly one known body"
        return None
