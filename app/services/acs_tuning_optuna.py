"""
ACS-CVRPTW Hyperparameter Tuning with Optuna.

This module provides hyperparameter optimization for the Ant Colony System
solver used in itinerary generation. It uses Optuna for Bayesian optimization.

Usage:
    # From command line:
    python -m app.services.acs_tuning_optuna --trials 50
    
    # Or programmatically:
    from app.services.acs_tuning_optuna import run_tuning
    best_params = run_tuning(n_trials=50, use_synthetic=True)

Note:
    - For offline tuning, use synthetic test cases (--synthetic flag).
    - Results are saved to app/core/vrp_config.yaml for production use.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

import optuna
from optuna.trial import Trial

from app.services.vrp_model import VRPConfig, save_config
from app.services.vrp_utils import build_problem
from app.services.acs_cvrptw import run_acs_cvrptw
from app.utils.logger import get_logger

logger = get_logger(__name__)

# Project root and paths
PROJECT_ROOT = Path(__file__).parent.parent.parent
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "app" / "core"


class TuningObjective:
    """
    Objective function for Optuna optimization.
    
    This class encapsulates the evaluation logic for VRP configuration tuning.
    It works with pre-processed test cases for reproducibility.
    """
    
    def __init__(
        self,
        test_cases: List[Dict[str, Any]],
        pacing: str = "balanced",
        verbose: bool = False,
    ):
        """
        Initialize the tuning objective.
        
        Args:
            test_cases: List of pre-processed test cases with structure:
                {
                    "maut_output": {...},  # MAUT-processed POIs
                    "hotel": {...},        # Selected hotel
                }
            pacing: Pacing preference ("relaxed", "balanced", "packed")
            verbose: Whether to print detailed progress
        """
        self.test_cases = test_cases
        self.pacing = pacing
        self.verbose = verbose
        
        if not test_cases:
            raise ValueError("At least one test case is required for tuning")
    
    def __call__(self, trial: Trial) -> float:
        """
        Evaluate a trial configuration.
        
        Returns:
            Combined score (lower is better) based on:
            - Total travel time/distance
            - Feasibility penalties
            - POI coverage
            - Meal distribution
        """
        # Create config from trial suggestions
        cfg = self._suggest_config(trial)
        
        total_score = 0.0
        n_evaluated = 0
        
        for i, test_case in enumerate(self.test_cases):
            try:
                score = self._evaluate_single(cfg, test_case)
                total_score += score
                n_evaluated += 1
                
                if self.verbose:
                    logger.info(f"Trial {trial.number}, Case {i}: score={score:.2f}")
                    
            except Exception as e:
                logger.warning(f"Trial {trial.number}, Case {i} failed: {e}")
                total_score += 50000
                n_evaluated += 1
        
        avg_score = total_score / max(n_evaluated, 1)
        
        trial.set_user_attr("n_evaluated", n_evaluated)
        trial.set_user_attr("avg_score", avg_score)
        
        return avg_score
    
    def _suggest_config(self, trial: Trial) -> VRPConfig:
        """Generate a VRPConfig from Optuna trial suggestions."""
        params = {}
        
        # ACS algorithm parameters
        params["acs_n_ants"] = trial.suggest_int("acs_n_ants", 15, 50)
        params["acs_n_iterations"] = trial.suggest_int("acs_n_iterations", 40, 120)
        params["acs_alpha"] = trial.suggest_float("acs_alpha", 0.5, 3.0)
        params["acs_beta"] = trial.suggest_float("acs_beta", 1.0, 5.0)
        params["acs_evaporation_rate"] = trial.suggest_float("acs_evaporation_rate", 0.2, 0.8)
        params["acs_q"] = trial.suggest_float("acs_q", 50.0, 200.0)
        
        # Constraint parameters
        params["acs_max_theme_per_day"] = trial.suggest_int("acs_max_theme_per_day", 2, 4)
        
        # Penalty parameters
        params["penalty_meal_to_meal"] = trial.suggest_int("penalty_meal_to_meal", 2000, 8000)
        params["penalty_same_theme"] = trial.suggest_int("penalty_same_theme", 200, 1000)
        params["penalty_theme_limit_exceeded"] = trial.suggest_int(
            "penalty_theme_limit_exceeded", 5000, 15000
        )
        params["drop_poi_penalty"] = trial.suggest_int("drop_poi_penalty", 1000, 3000)
        params["meal_shortfall_penalty"] = trial.suggest_int("meal_shortfall_penalty", 300, 900)
        params["mandatory_miss_penalty"] = trial.suggest_int("mandatory_miss_penalty", 5000, 15000)
        
        return VRPConfig(**params)
    
    def _evaluate_single(self, cfg: VRPConfig, test_case: Dict[str, Any]) -> float:
        """Evaluate a single test case with given config."""
        maut_output = test_case["maut_output"]
        hotel = test_case["hotel"]
        
        day_specs, nodes, travel = build_problem(
            maut_output, hotel, pacing=self.pacing
        )
        
        if not day_specs or len(nodes) <= 1:
            return 100000
        
        result = run_acs_cvrptw(
            day_specs=day_specs,
            nodes=nodes,
            travel=travel,
            meals_required=3,
            cfg=cfg,
        )
        
        score = 0.0
        
        # Travel distance
        total_distance = result.get("meta", {}).get("total_distance", 0)
        score += total_distance * 10
        
        # Infeasibility penalty
        infeasible_days = result.get("meta", {}).get("infeasible_days", [])
        score += len(infeasible_days) * 20000
        
        # Missed mandatory POIs
        missed = result.get("meta", {}).get("missed_mandatory", [])
        score += len(missed) * 30000
        
        # POI coverage bonus
        days = result.get("days", [])
        total_stops = sum(len(d.get("stops", [])) for d in days)
        actual_pois = total_stops - (len(days) * 2)
        score -= actual_pois * 100
        
        # Meal distribution
        for day in days:
            meals = day.get("meals", 0)
            if meals < 2:
                score += (2 - meals) * 1000
            elif meals > 3:
                score += (meals - 3) * 500
        
        return max(0, score)


def create_synthetic_test_case() -> Dict[str, Any]:
    """Create a synthetic test case for offline tuning."""
    from datetime import date, timedelta
    
    base_date = date.today()
    num_days = 3
    
    places = []
    themes = ["culture", "nature", "shopping", "family", "adventure"]
    
    # Attractions
    for i in range(15):
        places.append({
            "id": f"attraction_{i}",
            "name": f"Attraction {i}",
            "roles": ["attraction"],
            "themes": [themes[i % len(themes)]],
            "coordinates": {"lat": 1.30 + (i * 0.01), "lng": 103.80 + (i * 0.01)},
            "open_hours": {day: "9 am-6 pm" for day in 
                ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]},
            "area_name": "Test City",
        })
    
    # Meals
    for i in range(8):
        places.append({
            "id": f"meal_{i}",
            "name": f"Restaurant {i}",
            "roles": ["meal"],
            "themes": ["food_culinary"],
            "coordinates": {"lat": 1.31 + (i * 0.005), "lng": 103.81 + (i * 0.005)},
            "open_hours": {day: "10 am-10 pm" for day in
                ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]},
            "area_name": "Test City",
        })
    
    hotel = {"id": "hotel_1", "name": "Test Hotel", "lat": 1.29, "lon": 103.79}
    
    # Dates structure matching the frontend payload format
    dates_dict = {
        "type": "specific",
        "start_date": base_date.isoformat(),
        "end_date": (base_date + timedelta(days=num_days - 1)).isoformat(),
    }
    
    return {
        "maut_output": {
            "status": "ok",
            "places": places,
            "meta": {
                "num_days": num_days,
                "dates": dates_dict,
                "selected_hotel": {
                    "id": hotel["id"],
                    "name": hotel["name"],
                    "coordinates": {"lat": hotel["lat"], "lng": hotel["lon"]},
                },
            },
        },
        "hotel": hotel,
    }


def load_test_cases_from_file(filepath: Path) -> List[Dict[str, Any]]:
    """Load test cases from a JSON file."""
    if not filepath.exists():
        raise FileNotFoundError(f"Test file not found: {filepath}")
    
    with open(filepath, "r", encoding="utf-8") as f:
        data = json.load(f)
    
    if "maut_output" in data and "hotel" in data:
        return [data]
    
    if "places" in data and "meta" in data:
        hotel = data.get("meta", {}).get("selected_hotel")
        if not hotel:
            raise ValueError("Test file missing selected_hotel in meta")
        
        coords = hotel.get("coordinates", {})
        return [{
            "maut_output": data,
            "hotel": {
                "id": hotel.get("id"),
                "name": hotel.get("name"),
                "lat": coords.get("lat"),
                "lon": coords.get("lng"),
            }
        }]
    
    raise ValueError(f"Unrecognized test file format: {filepath}")


def run_tuning(
    n_trials: int = 50,
    test_files: Optional[List[str]] = None,
    use_synthetic: bool = False,
    pacing: str = "balanced",
    output_path: Optional[Path] = None,
    study_name: str = "acs_vrp_tuning",
    verbose: bool = True,
) -> Dict[str, Any]:
    """
    Run Optuna hyperparameter optimization for ACS-CVRPTW.
    
    Args:
        n_trials: Number of optimization trials
        test_files: List of test file paths (relative to project root)
        use_synthetic: If True, use synthetic test cases
        pacing: Pacing preference for evaluation
        output_path: Where to save best config
        study_name: Name for the Optuna study
        verbose: Print progress information
        
    Returns:
        Dictionary with best parameters and study statistics
    """
    test_cases = []
    
    if use_synthetic:
        logger.info("Using synthetic test case for tuning")
        test_cases.append(create_synthetic_test_case())
    elif test_files:
        for filepath in test_files:
            full_path = PROJECT_ROOT / filepath
            try:
                cases = load_test_cases_from_file(full_path)
                test_cases.extend(cases)
                logger.info(f"Loaded {len(cases)} test case(s) from {filepath}")
            except Exception as e:
                logger.warning(f"Failed to load {filepath}: {e}")
    else:
        logger.info("No test files provided, using synthetic test case")
        test_cases.append(create_synthetic_test_case())
    
    if not test_cases:
        raise ValueError("No test cases available for tuning")
    
    objective = TuningObjective(test_cases=test_cases, pacing=pacing, verbose=verbose)
    
    study = optuna.create_study(
        study_name=study_name,
        direction="minimize",
        sampler=optuna.samplers.TPESampler(seed=42),
    )
    
    if not verbose:
        optuna.logging.set_verbosity(optuna.logging.WARNING)
    
    logger.info(f"Starting Optuna optimization with {n_trials} trials...")
    study.optimize(objective, n_trials=n_trials, show_progress_bar=verbose)
    
    best_params = study.best_params
    best_value = study.best_value
    
    logger.info(f"Best score: {best_value:.2f}")
    logger.info(f"Best parameters: {best_params}")
    
    best_config = VRPConfig(**best_params)
    
    if output_path is None:
        output_path = DEFAULT_OUTPUT_DIR / "vrp_config.yaml"
    
    saved_path = save_config(best_config, output_path)
    logger.info(f"Best config saved to: {saved_path}")
    
    return {
        "best_params": best_params,
        "best_score": best_value,
        "n_trials": n_trials,
        "n_test_cases": len(test_cases),
        "saved_to": str(saved_path),
        "study_stats": {
            "best_trial": study.best_trial.number,
            "n_completed": len(study.trials),
        },
    }


def main():
    """CLI entry point for tuning."""
    parser = argparse.ArgumentParser(description="Tune ACS-CVRPTW hyperparameters")
    parser.add_argument("--trials", "-n", type=int, default=50, help="Number of trials")
    parser.add_argument("--test-files", "-t", nargs="+", help="Test files to use")
    parser.add_argument("--synthetic", "-s", action="store_true", help="Use synthetic data")
    parser.add_argument("--pacing", "-p", choices=["relaxed", "balanced", "packed"], default="balanced")
    parser.add_argument("--output", "-o", type=str, help="Output path for config")
    parser.add_argument("--quiet", "-q", action="store_true", help="Suppress output")
    
    args = parser.parse_args()
    
    result = run_tuning(
        n_trials=args.trials,
        test_files=args.test_files,
        use_synthetic=args.synthetic,
        pacing=args.pacing,
        output_path=Path(args.output) if args.output else None,
        verbose=not args.quiet,
    )
    
    print("\n" + "=" * 50)
    print("TUNING COMPLETE")
    print("=" * 50)
    print(f"Best Score: {result['best_score']:.2f}")
    print(f"Config saved to: {result['saved_to']}")
    print("\nBest Parameters:")
    for key, value in result['best_params'].items():
        print(f"  {key}: {value}")


if __name__ == "__main__":
    main()
