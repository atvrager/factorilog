"""Blueprint codec: entity JSON to the game's exchange string.

The exchange string carries a format byte, then zlib, then base64:

    "0" + b64(zlib(json.dumps(document)))

The "0" prefix marks the 2.0 string format. The `version` field inside the
document packs the game version as (major << 48) | (minor << 32) | ...
"""

import base64
import json
import zlib
from typing import Any

# The single-character prefix of a 2.0 exchange string.
FORMAT_PREFIX = "0"

# Game version 2.0.0, packed for the `version` field of a blueprint.
VERSION_2_0 = 2 << 48


def encode(
    entities: list[dict[str, Any]],
    label: str,
    wires: list[list[int]] | None = None,
    icons: list[dict[str, Any]] | None = None,
) -> str:
    """Encode a list of entities into an exchange string."""
    if icons is None:
        icons = []
        seen: set[str] = set()
        for ent in entities:
            name = ent.get("name")
            if name and name not in seen:
                seen.add(name)
                icons.append(
                    {
                        "index": len(icons) + 1,
                        "signal": {"type": "item", "name": name},
                    }
                )
                if len(icons) == 4:
                    break
        if not icons:
            icons = [{"index": 1, "signal": {"type": "item", "name": "assembling-machine-1"}}]

    bp_dict: dict[str, Any] = {
        "item": "blueprint",
        "label": label,
        "version": VERSION_2_0,
        "icons": icons,
        "entities": entities,
    }
    if wires:
        bp_dict["wires"] = wires
    document = {"blueprint": bp_dict}
    raw = json.dumps(document, separators=(",", ":")).encode()
    return FORMAT_PREFIX + base64.b64encode(zlib.compress(raw, 9)).decode()


def decode(string: str) -> dict[str, Any]:
    """Exchange string -> blueprint document."""
    if not string.startswith(FORMAT_PREFIX):
        raise ValueError("not a 2.0 exchange string: bad format byte")
    packed = base64.b64decode(string[1:])
    return json.loads(zlib.decompress(packed))


def entities_of(string: str) -> list[dict[str, Any]]:
    """Exchange string -> the entity list."""
    return decode(string)["blueprint"]["entities"]


def wires_of(string: str) -> list[list[int]]:
    """Exchange string -> the wire list."""
    return decode(string)["blueprint"].get("wires", [])
