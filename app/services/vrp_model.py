from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import List, Dict, Tuple, Optional
from enum import Enum

from pydantic import BaseModel, Field
import yaml
from pathlib import Path


class HotelEventType(Enum):
    """Types of hotel events that require visiting the hotel."""

    NONE = "none"  # No hotel visit required (default day)
    CHECK_IN = "check_in"  # Must arrive at hotel (14:00-16:00)
    CHECK_OUT = "check_out"  # Must depart from hotel (10:00-12:00)


@dataclass
class HotelEvent:
    """Represents a mandatory hotel visit event."""

    event_type: HotelEventType
    hotel_id: str
    hotel_name: str
    lat: float
    lon: float
    window: Tuple[int, int]  # (start_min, end_min) time window
    service_time: int = 30  # Minutes for check-in/out process


@dataclass
class DaySpec:
    """
    Specification for a single day in the itinerary.

    The hotel is NOT a universal depot. It only matters when there's a real-world
    hotel event (check-in or check-out). Most days start at a time, not a location.
    """

    day_index: int
    date: dt.date
    start_min: int
    end_min: int
    depot_id: str  # Kept for backward compatibility, but may be None for free days
    # Hotel events for this day (check-in, check-out, or none)
    hotel_events: List[HotelEvent] = field(default_factory=list)

    @property
    def has_hotel_event(self) -> bool:
        """Check if this day has any mandatory hotel events."""
        return len(self.hotel_events) > 0

    @property
    def has_check_in(self) -> bool:
        """Check if this day has a check-in event."""
        return any(e.event_type == HotelEventType.CHECK_IN for e in self.hotel_events)

    @property
    def has_check_out(self) -> bool:
        """Check if this day has a check-out event."""
        return any(e.event_type == HotelEventType.CHECK_OUT for e in self.hotel_events)

    @property
    def is_transition_day(self) -> bool:
        """Check if this is a city transition day (has both check-out and check-in)."""
        return self.has_check_in and self.has_check_out


@dataclass
class Node:
    """Represents a single location (POI or depot) in the VRP model."""

    idx: int
    poi_id: str
    name: str
    role: str
    lat: float
    lon: float
    service: int
    themes: Optional[List[str]]
    windows_by_day: Dict[int, List[Tuple[int, int]]]
    is_mandatory: bool = False
    maut_score: float = 0.0  # MAUT score for review/quality prioritization
    # Hotel event type: "checkin" or "checkout" for accommodation nodes
    hotel_event_type: Optional[str] = None


class VRPConfig(BaseModel):
    """Unified configuration for all VRP solvers (OR-Tools and ACS)."""

    # Pacing and Service Times
    pace_day_budget_min: Dict[str, int] = Field(
        default={
            "relaxed": 12 * 60,
            "balanced": 12 * 60,
            "packed": 16 * 60,
        }
    )
    pace_day_start_min: Dict[str, int] = Field(
        default={
            "relaxed": 10 * 60,
            "balanced": 10 * 60,
            "packed": 8 * 60,
        }
    )
    service_time_min: Dict[str, Dict[str, int]] = Field(
        default={
            "attraction": {"relaxed": 210, "balanced": 150, "packed": 90},
            "meal": {"relaxed": 90, "balanced": 75, "packed": 60},
            "accommodation": {"relaxed": 0, "balanced": 0, "packed": 0},
        }
    )

    # Default Time Windows (minutes from midnight)
    default_role_windows: Dict[str, Tuple[int, int]] = Field(
        default={
            "attraction": (9 * 60, 22 * 60),
            "meal": (10 * 60, 22 * 60),
            "accommodation": (0, 24 * 60),
            "depot": (0, 24 * 60),
        }
    )
    breakfast_win: Tuple[int, int] = Field(default=(7 * 60, 10 * 60))
    lunch_win: Tuple[int, int] = Field(default=(12 * 60, 14 * 60))
    dinner_win: Tuple[int, int] = Field(default=(18 * 60, 21 * 60))
    meal_hard_tol_min: int = Field(
        default=90,
        description="Tolerance for how far a meal can be from a preferred window (ACS hard, OR-Tools soft)",
    )

    # Hotel Event Time Windows (minutes from midnight)
    # Check-in: typically 14:00-16:00 (840-960 minutes)
    hotel_check_in_window: Tuple[int, int] = Field(
        default=(14 * 60, 16 * 60),
        description="Time window for hotel check-in (arrival at hotel)",
    )
    # Check-out: typically 10:00-12:00 (600-720 minutes)
    hotel_check_out_window: Tuple[int, int] = Field(
        default=(10 * 60, 12 * 60),
        description="Time window for hotel check-out (departure from hotel)",
    )
    # Service time for check-in/out process
    hotel_service_time: int = Field(
        default=30,
        description="Minutes required for hotel check-in/out process",
    )

    # Penalties (in 'minute-cost' units)
    penalty_meal_to_meal: int = Field(default=3000, description="Penalty for consecutive meals")
    penalty_same_theme: int = Field(default=500, description="Penalty for consecutive same-theme POIs")
    drop_poi_penalty: int = Field(default=200, description="Base penalty for dropping a non-mandatory POI")
    meal_shortfall_penalty: int = Field(
        default=200,
        description="Penalty per missing meal (high to enforce min 2 meals)",
    )
    mandatory_miss_penalty: int = Field(default=60 * 24 * 7, description="Penalty for missing a mandatory POI")

    # ACS POI coverage bonus (negative cost = reward for visiting)
    poi_visit_bonus: int = Field(
        default=120,
        description="Bonus (cost reduction) per POI visited to encourage more visits",
    )

    # Theme balance bonus - reward balanced coverage of user-selected themes
    theme_diversity_bonus: int = Field(
        default=100,
        description="Bonus per user-selected theme covered in a day",
    )
    theme_concentration_penalty: int = Field(
        default=50,
        description="Penalty when theme distribution is heavily skewed",
    )
    meal_window_bonus: int = Field(
        default=150,
        description="Bonus per meal scheduled within preferred time window",
    )

    # Solver-Specific Parameters
    acs_n_ants: int = Field(default=30)
    acs_n_iterations: int = Field(default=60)
    acs_alpha: float = Field(default=2.0, description="Pheromone importance")
    acs_beta: float = Field(default=2.0, description="Heuristic importance")
    acs_evaporation_rate: float = Field(default=0.5)
    acs_q: float = Field(default=100.0, description="Pheromone deposit factor")

    @property
    def meal_windows(self) -> List[Tuple[int, int]]:
        return [self.breakfast_win, self.lunch_win, self.dinner_win]


def load_config(config_path: Optional[Path] = None) -> VRPConfig:
    """
    Load VRP configuration from YAML file.

    Args:
        config_path: Optional path to config file. If None, uses default location.

    Returns:
        VRPConfig instance with loaded or default values.
    """
    if config_path is None:
        # Default: look in app/core/vrp_config.yaml
        config_path = Path(__file__).parent.parent / "core" / "vrp_config.yaml"

    if config_path.exists():
        with open(config_path, "r") as f:
            params = yaml.safe_load(f) or {}
        return VRPConfig(**params)
    return VRPConfig()


def save_config(config: VRPConfig, config_path: Optional[Path] = None) -> Path:
    """
    Save VRP configuration to YAML file with clean formatting.

    Args:
        config: VRPConfig instance to save.
        config_path: Optional path. If None, saves to app/core/vrp_config.yaml.

    Returns:
        Path where config was saved.
    """
    if config_path is None:
        config_path = Path(__file__).parent.parent / "core" / "vrp_config.yaml"

    # Round float values to 2 decimal places for cleaner output
    def round_value(v):
        if isinstance(v, float):
            return round(v, 2)
        return v

    # Get values with rounding
    acs_alpha = round_value(config.acs_alpha)
    acs_beta = round_value(config.acs_beta)
    acs_evaporation_rate = round_value(config.acs_evaporation_rate)
    acs_q = round_value(config.acs_q)

    # Write YAML with comments for better readability
    yaml_content = f"""
    # ACS-specific algorithm parameters
    acs_alpha: {acs_alpha}
    acs_beta: {acs_beta}
    acs_evaporation_rate: {acs_evaporation_rate}
    acs_n_ants: {config.acs_n_ants}
    acs_n_iterations: {config.acs_n_iterations}
    acs_q: {acs_q}

    # POI coverage reward (ACS uses this to prioritize visiting more POIs)
    poi_visit_bonus: {config.poi_visit_bonus}
    theme_diversity_bonus: {config.theme_diversity_bonus}
    theme_concentration_penalty: {config.theme_concentration_penalty}

    # Shared penalty parameters (used by both solvers)
    drop_poi_penalty: {config.drop_poi_penalty}
    mandatory_miss_penalty: {config.mandatory_miss_penalty}
    meal_shortfall_penalty: {config.meal_shortfall_penalty}
    penalty_meal_to_meal: {config.penalty_meal_to_meal}
    penalty_same_theme: {config.penalty_same_theme}
    """

    with open(config_path, "w") as f:
        f.write(yaml_content)

    return config_path


# Load default config at module import
vrp_config = load_config()
