"""Two-point HOME/REF calibration between gantry (raw) and bed coordinates."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class Calibration:
    """Per-axis linear map from raw gantry readings to bed coordinates.

    HOME pins an offset (raw reading -> known bed point). REF, captured after
    moving the gantry to a second known point, fixes the scale for each axis.
    The scale survives re-homing so only HOME is needed after a power cycle.
    """

    home_raw: Optional[tuple[float, float]] = None
    home_bed: tuple[float, float] = (0.0, 0.0)
    scale: tuple[float, float] = (1.0, 1.0)

    @property
    def homed(self) -> bool:
        return self.home_raw is not None

    def set_home(self, raw: tuple[float, float], bed: tuple[float, float]) -> None:
        self.home_raw = raw
        self.home_bed = bed

    def set_reference(self, raw: tuple[float, float], bed: tuple[float, float]) -> None:
        if self.home_raw is None:
            raise ValueError("Set HOME before the reference point.")
        scale = []
        for axis, name in enumerate("XY"):
            raw_span = raw[axis] - self.home_raw[axis]
            bed_span = bed[axis] - self.home_bed[axis]
            if raw_span == 0 or bed_span == 0:
                raise ValueError(
                    f"REF must differ from HOME on the {name} axis (both raw and bed)."
                )
            scale.append(bed_span / raw_span)
        self.scale = (scale[0], scale[1])

    def to_bed(self, raw: tuple[float, float]) -> tuple[float, float]:
        if self.home_raw is None:
            return raw
        return (
            self.home_bed[0] + (raw[0] - self.home_raw[0]) * self.scale[0],
            self.home_bed[1] + (raw[1] - self.home_raw[1]) * self.scale[1],
        )

    def to_raw(self, bed: tuple[float, float]) -> tuple[float, float]:
        if self.home_raw is None:
            return bed
        return (
            self.home_raw[0] + (bed[0] - self.home_bed[0]) / self.scale[0],
            self.home_raw[1] + (bed[1] - self.home_bed[1]) / self.scale[1],
        )
