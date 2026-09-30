"""Substrate metadata and workflow state for the gantry simulator."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class Substrate:
    """Metadata and progress for one sample on the shelf."""

    substrate_id: str
    experiment_order: tuple[str, ...]
    metadata: dict[str, Any] = field(default_factory=dict)
    current_experiment_index: int = 0
    location: str = "shelf"
    completed: bool = False
    history: list[str] = field(default_factory=list)

    @property
    def next_experiment(self) -> Optional[str]:
        if self.completed or self.current_experiment_index >= len(self.experiment_order):
            return None
        return self.experiment_order[self.current_experiment_index]

    def mark_delivered(self, experiment_name: str) -> None:
        if self.next_experiment != experiment_name:
            raise ValueError(
                f"{self.substrate_id} expected {self.next_experiment}, "
                f"not {experiment_name}"
            )
        self.history.append(experiment_name)
        self.current_experiment_index += 1
        self.location = experiment_name

    def mark_returned_to_shelf(self) -> None:
        if self.next_experiment is not None:
            raise ValueError(f"{self.substrate_id} still has experiments to run")
        self.location = "shelf"
        self.completed = True
