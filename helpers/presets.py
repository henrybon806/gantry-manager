"""presets.json: every setting the GUI and gantry server would otherwise need re-entering.

Bed size, gantry address/home/move time, shelf location and slots, each
station's node address and location, calibration and sample routes.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from helpers.calibration import Calibration


DATA_DIR = Path(__file__).resolve().parent.parent / "data"
PRESETS_PATH = DATA_DIR / "presets.json"
# Settings file from before presets existed; read once if presets.json is missing.
LAYOUT_PATH = DATA_DIR / "gantry_layout.json"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_ROUTES = ["A,B,C", "C,A", "B,C"]

DEFAULT_STATIONS = [
    {"name": "shelf", "address": "", "x": 10.0, "y": 30.0},
    {"name": "experiment_a", "address": f"{DEFAULT_HOST}:8001", "x": 35.0, "y": 45.0},
    {"name": "experiment_b", "address": f"{DEFAULT_HOST}:8002", "x": 60.0, "y": 15.0},
    {"name": "experiment_c", "address": f"{DEFAULT_HOST}:8003", "x": 85.0, "y": 40.0},
]


def default_presets() -> dict[str, Any]:
    return {
        "bed": [100.0, 60.0],
        "gantry_source": "simulator",
        "gantry_address": f"{DEFAULT_HOST}:8100",
        "gantry_home": [0.0, 0.0],
        "move_time": 1.0,
        "shelf_slots": 10,
        "calibration": {"home_raw": None, "home_bed": [0.0, 0.0], "scale": [1.0, 1.0]},
        "stations": [dict(station) for station in DEFAULT_STATIONS],
        "routes": list(DEFAULT_ROUTES),
    }


def _pair(value: Any, name: str) -> list[float]:
    try:
        x, y = (float(part) for part in value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be two numbers") from None
    return [x, y]


def validate_presets(data: Any) -> dict[str, Any]:
    """Normalise a presets document; raise ValueError if it is not usable.

    Missing fields take their defaults, so older gantry_layout.json files load.
    """
    if not isinstance(data, dict):
        raise ValueError("presets must be an object")
    defaults = default_presets()
    bed = _pair(data.get("bed", defaults["bed"]), "bed")
    if bed[0] <= 0 or bed[1] <= 0:
        raise ValueError("bed size must be positive")
    source = data.get("gantry_source", "simulator")
    if source not in ("simulator", "lab"):
        raise ValueError("gantry_source must be simulator or lab")
    address = data.get("gantry_address", defaults["gantry_address"])
    if not isinstance(address, str):
        raise ValueError("gantry_address must be text")
    home = _pair(data.get("gantry_home", defaults["gantry_home"]), "gantry_home")
    try:
        move_time = float(data.get("move_time", defaults["move_time"]))
    except (TypeError, ValueError):
        raise ValueError("move_time must be a number") from None
    if not move_time > 0:
        raise ValueError("move_time must be positive")
    slots = data.get("shelf_slots", defaults["shelf_slots"])
    if isinstance(slots, bool) or not isinstance(slots, (int, float)) or slots != int(slots) or not 1 <= slots <= 30:
        raise ValueError("shelf_slots must be a whole number from 1 to 30")
    # Older Tk layouts stored only the scale at the top level.
    calibration = data.get("calibration") or {"scale": data.get("scale", (1.0, 1.0))}
    home_raw = calibration.get("home_raw")
    scale = _pair(calibration.get("scale", (1.0, 1.0)), "scale")
    if 0.0 in scale:
        raise ValueError("scale cannot be zero")
    stations = []
    for entry in data.get("stations") or []:
        if not isinstance(entry, dict) or not isinstance(entry.get("name"), str) or not entry["name"].strip():
            raise ValueError("every station needs a name")
        point = _pair((entry.get("x"), entry.get("y")), f"{entry['name']} position")
        station_address = entry.get("address", "")
        if not isinstance(station_address, str):
            raise ValueError(f"{entry['name']} address must be text")
        stations.append({"name": entry["name"].strip(), "address": station_address.strip(), "x": point[0], "y": point[1]})
    names = [station["name"] for station in stations]
    if len(set(names)) != len(names):
        raise ValueError("station names must be unique")
    if "home" in names:
        raise ValueError('"home" is the gantry\'s own spot, not a station name')
    if "shelf" not in names:
        stations.insert(0, dict(DEFAULT_STATIONS[0]))
    routes = data.get("routes", defaults["routes"])
    if not isinstance(routes, list) or not all(isinstance(route, str) for route in routes):
        raise ValueError("routes must be a list of text like \"A,B,C\"")
    routes = [route.strip() for route in routes if route.strip()]
    if len(routes) > int(slots):
        raise ValueError(f"{len(routes)} sample routes but the shelf has {int(slots)} slots")
    return {
        "bed": bed,
        "gantry_source": source,
        "gantry_address": address.strip(),
        "gantry_home": home,
        "move_time": move_time,
        "shelf_slots": int(slots),
        "calibration": {
            "home_raw": None if home_raw is None else _pair(home_raw, "home_raw"),
            "home_bed": _pair(calibration.get("home_bed", (0.0, 0.0)), "home_bed"),
            "scale": scale,
        },
        "stations": stations,
        "routes": routes,
    }


def load_presets(path: Path = PRESETS_PATH, legacy_path: Optional[Path] = LAYOUT_PATH) -> dict[str, Any]:
    """Read presets.json; without one, start from an old gantry_layout.json, else defaults."""
    for candidate in (path, legacy_path):
        if candidate is None:
            continue
        try:
            return validate_presets(json.loads(candidate.read_text()))
        except FileNotFoundError:
            continue
        except (OSError, ValueError) as error:
            raise ValueError(f"{candidate.name} is not valid: {error}") from None
    return default_presets()


def save_presets(presets: dict[str, Any], path: Path = PRESETS_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(validate_presets(presets), indent=2) + "\n")


def station_bed_to_raw(presets: dict[str, Any], point: list[float]) -> tuple[float, float]:
    """Bed coordinates (what the GUI shows) to gantry coordinates, via the saved calibration."""
    calibration = presets["calibration"]
    if calibration["home_raw"] is None:
        return float(point[0]), float(point[1])
    cal = Calibration(tuple(calibration["home_raw"]), tuple(calibration["home_bed"]), tuple(calibration["scale"]))
    return cal.to_raw((float(point[0]), float(point[1])))


def poll_targets(layout: dict[str, Any]) -> tuple[dict[str, str], Optional[str], str]:
    """What a NodePoller should read for this layout: stations, gantry address, gantry path."""
    stations = {
        station["name"]: station["address"]
        for station in layout["stations"]
        if station["name"] != "shelf" and station["address"]
    }
    path = "/state" if layout["gantry_source"] == "simulator" else "/position"
    return stations, layout["gantry_address"] or None, path
