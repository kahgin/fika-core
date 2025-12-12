from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import List, Dict, Tuple, Optional

from pydantic import BaseModel, Field
import yaml
from pathlib import Path


@dataclass
class DaySpec:
    """Specification for a single day in the itinerary."""

    day_index: int
    date: dt.date
    start_min: int
    end_min: int
    depot_id: str


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


class VRPConfig(BaseModel):
    """Unified configuration for all VRP solvers (OR-Tools and ACS)."""

    # Pacing and Service Times
    pace_day_budget_min: Dict[str, int] = Field(
        default={
            "relaxed": 8 * 60,
            "balanced": 11 * 60,
            "packed": 14 * 60,
        }
    )
    pace_day_start_min: Dict[str, int] = Field(
        default={
            "relaxed": 10 * 60,
            "balanced": 9 * 60,
            "packed": 8 * 60,
        }
    )
    service_time_min: Dict[str, Dict[str, int]] = Field(
        default={
            "attraction": {"relaxed": 120, "balanced": 90, "packed": 60},
            "meal": {"relaxed": 90, "balanced": 75, "packed": 60},
            "accommodation": {"relaxed": 0, "balanced": 0, "packed": 0},
        }
    )

    # Default Time Windows (minutes from midnight)
    default_role_windows: Dict[str, Tuple[int, int]] = Field(
        default={
            "attraction": (9 * 60, 19 * 60),
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

    # Penalties (in 'minute-cost' units)
    penalty_meal_to_meal: int = Field(
        default=5000, description="Penalty for consecutive meals"
    )
    penalty_same_theme: int = Field(
        default=500, description="Penalty for consecutive same-theme POIs"
    )
    penalty_theme_limit_exceeded: int = Field(
        default=10000,
        description="High penalty for exceeding max attractions per theme per day",
    )
    drop_poi_penalty: int = Field(
        default=2000, description="Base penalty for dropping a non-mandatory POI"
    )
    meal_shortfall_penalty: int = Field(
        default=60 * 10, description="Penalty per missing meal"
    )
    mandatory_miss_penalty: int = Field(
        default=60 * 24 * 7, description="Penalty for missing a mandatory POI"
    )

    # Solver-Specific Parameters
    acs_n_ants: int = Field(default=30)
    acs_n_iterations: int = Field(default=60)
    acs_alpha: float = Field(default=2.0, description="Pheromone importance")
    acs_beta: float = Field(default=2.0, description="Heuristic importance")
    acs_evaporation_rate: float = Field(default=0.5)
    acs_q: float = Field(default=100.0, description="Pheromone deposit factor")
    acs_max_theme_per_day: int = Field(
        default=2, description="Max attractions with same primary theme per day"
    )

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
    Save VRP configuration to YAML file.
    
    Args:
        config: VRPConfig instance to save.
        config_path: Optional path. If None, saves to app/core/vrp_config.yaml.
        
    Returns:
        Path where config was saved.
    """
    if config_path is None:
        config_path = Path(__file__).parent.parent / "core" / "vrp_config.yaml"
    
    # Only save ACS-tunable and penalty parameters (not complex nested dicts)
    tunable_keys = [
        "acs_n_ants", "acs_n_iterations", "acs_alpha", "acs_beta",
        "acs_evaporation_rate", "acs_q", "acs_max_theme_per_day",
        "penalty_meal_to_meal", "penalty_same_theme", "penalty_theme_limit_exceeded",
        "drop_poi_penalty", "meal_shortfall_penalty", "mandatory_miss_penalty",
    ]
    
    params = {k: getattr(config, k) for k in tunable_keys}
    
    with open(config_path, "w") as f:
        yaml.safe_dump(params, f, default_flow_style=False)
    
    return config_path


# Load default config at module import
vrp_config = load_config()
