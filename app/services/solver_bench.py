"""
Solver benchmark runner: OR-Tools vs ACS across many payload variants.
Updates app/core/vrp_config.yaml when tuning is requested.
"""

from __future__ import annotations

import argparse
import copy
import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    import optuna  # type: ignore
except Exception:  # pragma: no cover
    optuna = None

from app.services.maut import run_maut
from app.services.pipeline import run_full_pipeline
from app.services.transformers import transform_frontend_payload
from app.services import vrp_model
from app.services.vrp_model import vrp_config
from app.services.vrp_utils import build_problem
from app.services.acs_cvrptw import run_acs_cvrptw
from app.services.or_tools_cvrptw import solve_cvrptw
from app.utils.date_utils import time_to_minutes

HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parent.parent
OUT_DIR = PROJECT_ROOT / "storage" / "bench"


# Resolve payload JSONs from multiple candidate locations to be robust to moves.
# Priority: storage/<file> -> project_root/<file> -> script_dir/<file>


def _resolve_payload_path(filename: str) -> Path:
    candidates = [
        PROJECT_ROOT / "tests" / filename,
    ]
    for p in candidates:
        if p.exists():
            return p
    raise FileNotFoundError(
        f"Could not find payload file '{filename}'. Tried: " + ", ".join(str(p) for p in candidates)
    )


SPEC_PATH = _resolve_payload_path("sample_payload_spec.json")
FLEX_PATH = _resolve_payload_path("sample_payload_flex.json")


def _read_json(path: Path) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _deep_update(d: Dict[str, Any], patch: Dict[str, Any]) -> Dict[str, Any]:
    """Recursively merge patch into d."""
    for k, v in patch.items():
        if isinstance(v, dict) and isinstance(d.get(k), dict):
            _deep_update(d[k], v)
        else:
            d[k] = v
    return d


def _get_base_poi_id(poi_id: str) -> str:
    return poi_id.rsplit("_day", 1)[0] if "_day" in poi_id else poi_id


def _calculate_tus(days: List[Dict[str, Any]], pacing: str) -> Tuple[float, List[float]]:
    t_max = vrp_config.pace_day_budget_min.get(pacing, 12 * 60)
    daily_util: List[float] = []
    for day in days:
        stops = day.get("stops", [])
        if len(stops) < 2:
            daily_util.append(0.0)
            continue
        first_depart = time_to_minutes(stops[0].get("depart", "00:00"))
        last_arrival = time_to_minutes(stops[-1].get("arrival", "00:00"))
        d_k = max(0, last_arrival - first_depart)
        daily_util.append((d_k / t_max) * 100 if t_max > 0 else 0.0)

    return (sum(daily_util) / len(daily_util) if daily_util else 0.0), daily_util


def _check_time_sequence(days: List[Dict[str, Any]]) -> Tuple[bool, List[str]]:
    issues: List[str] = []
    for day_idx, day in enumerate(days):
        prev_depart: Optional[int] = None
        for i, stop in enumerate(day.get("stops", [])):
            arr = stop.get("arrival")
            dep = stop.get("depart")
            if arr is None:
                continue
            arr_min = time_to_minutes(arr)
            dep_min = time_to_minutes(dep) if dep else arr_min
            if prev_depart is not None and arr_min < prev_depart:
                issues.append(f"Day {day_idx + 1} stop {i}: arrival before previous depart")
            if dep_min < arr_min:
                issues.append(f"Day {day_idx + 1} stop {i}: depart before arrival")
            prev_depart = dep_min
    return len(issues) == 0, issues


def _check_poi_coverage(days: List[Dict[str, Any]]) -> Tuple[int, int, int, int, List[str]]:
    visited: set[str] = set()
    duplicates: List[str] = []
    meals = 0
    attractions = 0
    total_stops = 0

    for day_idx, day in enumerate(days):
        for stop in day.get("stops", []):
            role = stop.get("role", "")
            if role in ("depot", "accommodation", "hotel"):
                continue
            total_stops += 1
            if role == "meal":
                meals += 1
            else:
                attractions += 1

            base_id = _get_base_poi_id(stop.get("poi_id", ""))
            if base_id in visited:
                duplicates.append(f"Day {day_idx + 1}: {stop.get('name')} ({base_id})")
            visited.add(base_id)

    return len(visited), total_stops, meals, attractions, duplicates


def _check_meal_constraints(days: List[Dict[str, Any]]) -> Tuple[bool, float, List[str]]:
    issues: List[str] = []
    total_meals = 0
    meals_in_window = 0

    for day_idx, day in enumerate(days):
        stops = day.get("stops", [])
        meal_count = sum(1 for s in stops if s.get("role") == "meal")
        if meal_count > 3:
            issues.append(f"Day {day_idx + 1}: {meal_count} meals exceeds max 3")

        for i in range(1, len(stops)):
            if stops[i - 1].get("role") == "meal" and stops[i].get("role") == "meal":
                issues.append(f"Day {day_idx + 1}: consecutive meals")

        for stop in stops:
            if stop.get("role") != "meal":
                continue
            total_meals += 1
            start = stop.get("arrival")
            if not start:
                continue
            start_min = time_to_minutes(start)
            for w_start, w_end in (
                vrp_model.vrp_config.breakfast_win,
                vrp_model.vrp_config.lunch_win,
                vrp_model.vrp_config.dinner_win,
            ):
                if w_start <= start_min <= w_end:
                    meals_in_window += 1
                    break

    compliance = (meals_in_window / total_meals * 100) if total_meals else 100.0
    return len(issues) == 0, compliance, issues


def _check_food_streak(days: List[Dict[str, Any]]) -> Tuple[bool, List[str]]:
    issues: List[str] = []
    for day_idx, day in enumerate(days):
        streak = 0
        for stop in day.get("stops", []):
            if stop.get("role") in ("depot", "accommodation", "hotel"):
                streak = 0
                continue
            if stop.get("role") == "meal":
                streak += 1
                if streak > 2:
                    issues.append(f"Day {day_idx + 1}: >2 consecutive food-like stops")
            else:
                streak = 0
    return len(issues) == 0, issues


def _check_theme_distribution(days: List[Dict[str, Any]]) -> Tuple[bool, List[str]]:
    # Informational only (soft penalties). We flag only extreme concentration.
    issues: List[str] = []
    for day_idx, day in enumerate(days):
        counts: Dict[str, int] = {}
        for stop in day.get("stops", []):
            if stop.get("role") != "attraction":
                continue
            themes = stop.get("themes", [])
            if not themes:
                continue
            primary = themes[0]
            counts[primary] = counts.get(primary, 0) + 1
        for theme, c in counts.items():
            if c > 6:
                issues.append(f"Day {day_idx + 1}: high theme concentration {theme}={c}")
    return len(issues) == 0, issues


@dataclass
class BenchResult:
    scenario: str
    solver: str
    pacing: str
    status: str
    execution_time_sec: float
    unique_pois: int
    meals: int
    tus: float
    meal_window_compliance: float
    distance_km: float
    feasible: bool
    violations: List[str]


@dataclass
class PreparedCase:
    name: str
    payload: Dict[str, Any]
    maut_output: Dict[str, Any]
    hotel: Dict[str, Any]
    pacing: str
    day_specs: Any
    nodes: Any
    travel: Any
    meals_required: int
    ortools_out: Dict[str, Any]
    ortools_eval: BenchResult
    ortools_exec_sec: float


def _score_components(a: BenchResult, b: BenchResult, weights: Dict[str, float]) -> Dict[str, float]:
    # 0..100 component scores
    scores: Dict[str, float] = {}

    max_pois = max(a.unique_pois, b.unique_pois)
    scores["poi_coverage"] = (a.unique_pois / max_pois * 100) if max_pois else 100.0

    scores["constraint_compliance"] = 100.0 if a.feasible else max(0.0, 100.0 - len(a.violations) * 10.0)

    scores["meal_compliance"] = float(a.meal_window_compliance)

    tus_ideal = 90.0
    scores["tus_quality"] = max(0.0, 100.0 - abs(a.tus - tus_ideal) * 2.0)

    max_dist = max(a.distance_km, b.distance_km)
    if max_dist > 0:
        scores["efficiency"] = min(100.0, (1.0 - a.distance_km / max_dist) * 100.0 + 50.0)
    else:
        scores["efficiency"] = 100.0

    max_time = max(a.execution_time_sec, b.execution_time_sec)
    if max_time > 0:
        scores["execution_time"] = min(100.0, (1.0 - a.execution_time_sec / max_time) * 100.0 + 50.0)
    else:
        scores["execution_time"] = 100.0

    total = 0.0
    for k, w in weights.items():
        total += scores[k] * (w / 100.0)
    scores["weighted_total"] = total
    return scores


def _evaluate_output(name: str, solver: str, pacing: str, output: Dict[str, Any], exec_sec: float) -> BenchResult:
    if output.get("status") not in ("success", "partial_success"):
        return BenchResult(
            scenario=name,
            solver=solver,
            pacing=pacing,
            status=str(output.get("status")),
            execution_time_sec=exec_sec,
            unique_pois=0,
            meals=0,
            tus=0.0,
            meal_window_compliance=0.0,
            distance_km=0.0,
            feasible=False,
            violations=["solver_failed"],
        )

    days = output.get("days", [])
    meta = output.get("meta", {})

    tus, _ = _calculate_tus(days, pacing)
    unique_pois, _total_stops, meals, _attractions, duplicates = _check_poi_coverage(days)
    time_ok, time_issues = _check_time_sequence(days)
    meal_ok, meal_window_comp, meal_issues = _check_meal_constraints(days)
    theme_ok, theme_issues = _check_theme_distribution(days)
    food_ok, food_issues = _check_food_streak(days)

    violations: List[str] = []
    violations.extend([f"dup:{d}" for d in duplicates])
    violations.extend([f"time:{i}" for i in time_issues])
    violations.extend([f"meal:{i}" for i in meal_issues])
    violations.extend([f"theme:{i}" for i in theme_issues])
    violations.extend([f"food:{i}" for i in food_issues])

    # Theme concentration is intentionally a soft signal (do not mark infeasible).
    feasible = time_ok and food_ok and len(duplicates) == 0 and meal_ok

    return BenchResult(
        scenario=name,
        solver=solver,
        pacing=pacing,
        status="success",
        execution_time_sec=exec_sec,
        unique_pois=unique_pois,
        meals=meals,
        tus=tus,
        meal_window_compliance=meal_window_comp,
        distance_km=float(meta.get("total_distance", 0.0) or 0.0),
        feasible=feasible,
        violations=violations,
    )


def _build_scenarios(
    *, include_multicity: bool = False, include_long: bool = False, long_days: int = 8
) -> List[Dict[str, Any]]:
    spec = _read_json(SPEC_PATH)
    flex = _read_json(FLEX_PATH)

    scenarios: List[Dict[str, Any]] = []

    # Baselines covering pacing and theme options
    scenarios.append(
        {
            "name": "no_themes_balanced",
            "base": "spec",
            "patch": {"preferences": {"pacing": "balanced", "interests": []}},
        }
    )
    scenarios.append(
        {
            "name": "no_themes_relaxed",
            "base": "spec",
            "patch": {"preferences": {"pacing": "relaxed", "interests": []}},
        }
    )
    scenarios.append(
        {
            "name": "no_themes_packed",
            "base": "spec",
            "patch": {"preferences": {"pacing": "packed", "interests": []}},
        }
    )

    # Single theme
    scenarios.append(
        {
            "name": "single_theme_shopping",
            "base": "spec",
            "patch": {"preferences": {"pacing": "balanced", "interests": ["shopping"]}},
        }
    )
    scenarios.append(
        {
            "name": "single_theme_nature",
            "base": "spec",
            "patch": {"preferences": {"pacing": "balanced", "interests": ["nature"]}},
        }
    )
    scenarios.append(
        {
            "name": "single_theme_food_culinary",
            "base": "spec",
            "patch": {"preferences": {"pacing": "balanced", "interests": ["food_culinary"]}},
        }
    )

    # Multi themes
    scenarios.append(
        {
            "name": "multi_theme_shopping_food_cultural_history",
            "base": "spec",
            "patch": {
                "preferences": {"pacing": "balanced", "interests": ["shopping", "food_culinary", "cultural_history"]}
            },
        }
    )

    # Dietary / flags / hotel and mandatory variants from flex
    scenarios.append(
        {
            "name": "mandatory_and_hotel",
            "base": "flex",
            "patch": {"preferences": {"pacing": "balanced", "interests": []}},
        }
    )
    scenarios.append(
        {
            "name": "muslim_vegetarian",
            "base": "flex",
            "patch": {
                "dietary_restrictions": "vegetarian",
                "flags": {"is_muslim": True},
                "preferences": {"pacing": "balanced", "interests": []},
            },
        }
    )
    scenarios.append(
        {
            "name": "wheelchair_accessible_only",
            "base": "spec",
            "patch": {"flags": {"wheelchair_accessible": True}, "preferences": {"pacing": "balanced", "interests": []}},
        }
    )
    scenarios.append(
        {
            "name": "kids_and_pets",
            "base": "spec",
            "patch": {
                "flags": {"kids_friendly": True, "pets_friendly": True},
                "preferences": {"pacing": "balanced", "interests": []},
            },
        }
    )

    # Edge pacing variants
    scenarios.append(
        {
            "name": "packed_no_interests",
            "base": "spec",
            "patch": {"preferences": {"pacing": "packed", "interests": []}},
        }
    )
    scenarios.append(
        {
            "name": "relaxed_many_interests",
            "base": "spec",
            "patch": {
                "preferences": {
                    "pacing": "relaxed",
                    "interests": ["shopping", "nature", "cultural_history", "family", "food_culinary"],
                }
            },
        }
    )
    scenarios.append(
        {
            "name": "no_diet_no_flags",
            "base": "spec",
            "patch": {"dietary_restrictions": "", "flags": {}, "preferences": {"pacing": "balanced", "interests": []}},
        }
    )

    # Optional heavy variants (gated)
    if include_multicity:
        # Multi-city via destinations array (requires MAUT to support multiple cities)
        scenarios.append(
            {
                "name": "multicity_spec_balanced",
                "base": "spec",
                "patch": {
                    "destinations": [{"city": "Singapore"}, {"city": "Johor Bahru"}],
                    "preferences": {"pacing": "balanced"},
                },
            }
        )
        scenarios.append(
            {
                "name": "multicity_flex_balanced",
                "base": "flex",
                "patch": {
                    "destinations": [{"city": "Singapore"}, {"city": "Kuala Lumpur"}],
                    "preferences": {"pacing": "balanced"},
                },
            }
        )

    if include_long:
        # Long duration (8-10 days). Use flexible dates to force duration.
        scenarios.append(
            {
                "name": f"long_{long_days}_days_no_themes_balanced",
                "base": "spec",
                "patch": {
                    "dates": {"type": "flexible", "days": long_days},
                    "preferences": {"pacing": "balanced", "interests": []},
                },
            }
        )
        scenarios.append(
            {
                "name": f"long_{long_days}_days_multi_theme_packed",
                "base": "spec",
                "patch": {
                    "dates": {"type": "flexible", "days": long_days},
                    "preferences": {
                        "pacing": "packed",
                        "interests": ["shopping", "food_culinary", "cultural_history"],
                    },
                },
            }
        )

    # Materialize payloads
    out: List[Dict[str, Any]] = []
    for s in scenarios:
        base_payload = spec if s["base"] == "spec" else flex
        payload = copy.deepcopy(base_payload)
        _deep_update(payload, s["patch"])
        out.append({"name": s["name"], "payload": payload})

    return out


def _apply_acs_params(params: Dict[str, Any]) -> None:
    # Mutate the shared vrp_config object in-place so modules that imported it keep seeing updates.
    for k, v in params.items():
        if not hasattr(vrp_model.vrp_config, k):
            raise ValueError(f"Unknown VRPConfig field: {k}")
        setattr(vrp_model.vrp_config, k, v)


def _prepare_cases(
    scenarios: List[Dict[str, Any]],
    *,
    only_pattern: Optional[str],
    max_scenarios: Optional[int],
    ortools_time_limit_sec: int,
) -> List[PreparedCase]:
    """Prepare MAUT + cached OR-Tools runs so Optuna can tune ACS quickly."""
    filt = re.compile(only_pattern) if only_pattern else None
    prepared: List[PreparedCase] = []

    for s in scenarios:
        if max_scenarios is not None and len(prepared) >= max_scenarios:
            break
        name = s["name"]
        if filt and not filt.search(name):
            continue

        print(f"PREP[{len(prepared) + 1}] {name}: building MAUT + VRP problem")

        payload = s["payload"]
        maut_request = transform_frontend_payload(payload)
        pacing = maut_request.get("pacing", payload.get("preferences", {}).get("pacing", "balanced"))

        maut_output = run_maut(maut_request)
        if maut_output.get("status") != "ok":
            print(f"  PREP: MAUT failed for {name}")
            continue

        maut_output.setdefault("meta", {})
        maut_output["meta"]["dates"] = payload.get("dates")
        maut_output["meta"]["num_days"] = maut_request.get("num_days")

        selected_hotel = (maut_output.get("meta") or {}).get("selected_hotel")
        if not selected_hotel:
            continue

        coords = selected_hotel.get("coordinates") or {}
        hotel = {
            "id": selected_hotel["id"],
            "name": selected_hotel["name"],
            "lat": coords.get("lat"),
            "lon": coords.get("lng"),
        }

        # Build VRP problem once per case (includes OSRM matrix).
        day_specs, nodes, travel = build_problem(maut_output, hotel, pacing=pacing)
        if not day_specs or not nodes or len(nodes) <= 1:
            print(f"  PREP: VRP build produced no nodes/days for {name}")
            continue

        print(f"  PREP: days={len(day_specs)} nodes={len(nodes)}")

        meal_nodes = sum(1 for n in nodes if getattr(n, "role", None) == "meal")
        meals_per_day_available = meal_nodes // len(day_specs) if len(day_specs) > 0 else 0
        meals_required = (
            min(3, max(2, meals_per_day_available))
            if meal_nodes >= len(day_specs) * 2
            else min(2, meals_per_day_available)
        )

        t0 = time.perf_counter()
        ortools_out = solve_cvrptw(
            day_specs,
            nodes,
            travel,
            meals_required=meals_required,
            time_limit_sec=ortools_time_limit_sec,
            slack_wait_min=120,
        )
        ortools_sec = time.perf_counter() - t0

        # Mirror production fallback behavior (see `app.services.cvrptw.run_cvrptw`).
        if not ortools_out.get("days"):
            t0_fb = time.perf_counter()
            ortools_out = solve_cvrptw(
                day_specs,
                nodes,
                travel,
                meals_required=0,
                time_limit_sec=max(10, ortools_time_limit_sec),
                slack_wait_min=300,
            )
            ortools_sec = ortools_sec + (time.perf_counter() - t0_fb)

        print(f"  PREP: OR-Tools done in {ortools_sec:.1f}s (days={len(ortools_out.get('days', []))})")

        # Normalize OR-Tools output shape to match evaluator expectations.
        ortools_out = {
            "status": "success" if ortools_out.get("days") else "error",
            "days": ortools_out.get("days", []),
            "meta": ortools_out.get("meta", {}),
        }

        ortools_eval = _evaluate_output(name, "OR-Tools", pacing, ortools_out, ortools_sec)
        prepared.append(
            PreparedCase(
                name=name,
                payload=payload,
                maut_output=maut_output,
                hotel=hotel,
                pacing=pacing,
                day_specs=day_specs,
                nodes=nodes,
                travel=travel,
                meals_required=meals_required,
                ortools_out=ortools_out,
                ortools_eval=ortools_eval,
                ortools_exec_sec=ortools_sec,
            )
        )

    return prepared


def _acs_quality_for_tuning(r: BenchResult) -> float:
    """Quality score focused on TUS + unique POIs (priority)."""
    if not r.feasible:
        return -1e9
    return (3.0 * float(r.tus)) + (20.0 * float(r.unique_pois))


def _weighted_total(a: BenchResult, b: BenchResult, weights: Dict[str, float]) -> float:
    return float(_score_components(a, b, weights)["weighted_total"])


def optuna_tune_acs_vs_ortools(
    cases: List[PreparedCase],
    *,
    n_trials: int,
    seed: int,
    write_yaml: bool,
) -> Dict[str, Any]:
    if optuna is None:
        raise RuntimeError("optuna is not installed in this environment")
    if not cases:
        raise ValueError("No cases available for tuning")

    # Snapshot current params so we can restore on failure.
    base_params = {
        "poi_visit_bonus": vrp_model.vrp_config.poi_visit_bonus,
        "theme_diversity_bonus": vrp_model.vrp_config.theme_diversity_bonus,
        "theme_concentration_penalty": vrp_model.vrp_config.theme_concentration_penalty,
        "meal_shortfall_penalty": vrp_model.vrp_config.meal_shortfall_penalty,
        "acs_n_ants": vrp_model.vrp_config.acs_n_ants,
        "acs_n_iterations": vrp_model.vrp_config.acs_n_iterations,
        "acs_alpha": vrp_model.vrp_config.acs_alpha,
        "acs_beta": vrp_model.vrp_config.acs_beta,
        "acs_evaporation_rate": vrp_model.vrp_config.acs_evaporation_rate,
        "acs_q": vrp_model.vrp_config.acs_q,
    }

    # Keep Optuna aligned with what `run_bench()` reports as "quality".
    weights_quality = {
        "poi_coverage": 30,
        "constraint_compliance": 30,
        "meal_compliance": 20,
        "tus_quality": 20,
        "efficiency": 0,
        "execution_time": 0,
    }

    def objective(trial: Any) -> float:
        params = {
            # ACS-only levers that matter for POI coverage + day fill
            "poi_visit_bonus": trial.suggest_int("poi_visit_bonus", 120, 650),
            "meal_shortfall_penalty": trial.suggest_int("meal_shortfall_penalty", 200, 1800),
            "theme_diversity_bonus": trial.suggest_int("theme_diversity_bonus", 0, 200),
            "theme_concentration_penalty": trial.suggest_int("theme_concentration_penalty", 0, 200),
            # ACS meta params
            "acs_n_ants": trial.suggest_int("acs_n_ants", 15, 60),
            "acs_n_iterations": trial.suggest_int("acs_n_iterations", 50, 180),
            "acs_alpha": trial.suggest_float("acs_alpha", 0.5, 5.0),
            "acs_beta": trial.suggest_float("acs_beta", 1.0, 6.0),
            "acs_evaporation_rate": trial.suggest_float("acs_evaporation_rate", 0.15, 0.75),
            "acs_q": trial.suggest_float("acs_q", 40.0, 200.0),
        }

        _apply_acs_params(params)

        total = 0.0
        n = 0
        infeasible = 0

        for c in cases:
            t1 = time.perf_counter()
            acs_out = run_acs_cvrptw(
                day_specs=c.day_specs,
                nodes=c.nodes,
                travel=c.travel,
                meals_required=c.meals_required,
                cfg=vrp_model.vrp_config,
            )
            acs_sec = time.perf_counter() - t1
            # Normalize ACS output shape to match evaluator expectations.
            acs_out = {
                "status": "success" if acs_out.get("days") else "error",
                "days": acs_out.get("days", []),
                "meta": acs_out.get("meta", {}),
            }
            acs_eval = _evaluate_output(c.name, "ACS", c.pacing, acs_out, acs_sec)

            if not acs_eval.feasible:
                infeasible += 1
                # Heavy penalty so trials that break feasibility get dominated.
                total += -1e6
            else:
                # Maximize improvement over OR-Tools on the same quality score used in reports.
                acs_q = _weighted_total(acs_eval, c.ortools_eval, weights_quality)
                ot_q = _weighted_total(c.ortools_eval, acs_eval, weights_quality)
                total += acs_q - ot_q
            n += 1

        trial.set_user_attr("cases", n)
        trial.set_user_attr("infeasible", infeasible)
        return total / max(1, n)

    sampler = optuna.samplers.TPESampler(seed=seed)
    study = optuna.create_study(direction="maximize", sampler=sampler)

    def _trial_cb(study: Any, trial: Any) -> None:
        # Keep output compact: print every 5 trials and always on best.
        is_best = study.best_trial is not None and trial.number == study.best_trial.number
        if is_best or ((trial.number + 1) % 5 == 0):
            val = trial.value
            infeasible = trial.user_attrs.get("infeasible")
            print(
                f"TRIAL {trial.number + 1}/{n_trials}: value={val:.3f} best={study.best_value:.3f} infeasible={infeasible}"
            )

    try:
        study.optimize(objective, n_trials=n_trials, callbacks=[_trial_cb])
    finally:
        # Restore baseline until we explicitly apply best.
        _apply_acs_params(base_params)

    best_params = dict(study.best_params)
    _apply_acs_params(best_params)

    out_path = None
    if write_yaml:
        out_path = vrp_model.save_config(vrp_model.vrp_config)

    return {
        "best_value": float(study.best_value),
        "best_params": best_params,
        "saved_to": str(out_path) if out_path else None,
    }


def run_bench(
    scenarios: List[Dict[str, Any]],
    only_pattern: Optional[str],
    max_scenarios: Optional[int],
    time_limit_sec: int,
) -> Dict[str, Any]:
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    filt = re.compile(only_pattern) if only_pattern else None

    weights_default = {
        "poi_coverage": 25,
        "constraint_compliance": 25,
        "meal_compliance": 15,
        "tus_quality": 15,
        "efficiency": 10,
        "execution_time": 10,
    }
    weights_quality = {
        "poi_coverage": 30,
        "constraint_compliance": 30,
        "meal_compliance": 20,
        "tus_quality": 20,
        "efficiency": 0,
        "execution_time": 0,
    }

    results: List[Dict[str, Any]] = []
    summary = {"acs_wins_default": 0, "acs_wins_quality": 0, "total": 0}

    for s in scenarios:
        name = s["name"]
        if filt and not filt.search(name):
            continue
        if max_scenarios is not None and summary["total"] >= max_scenarios:
            break

        payload = s["payload"]
        maut_request = transform_frontend_payload(payload)
        pacing = maut_request.get("pacing", payload.get("preferences", {}).get("pacing", "balanced"))

        maut_output = run_maut(maut_request)
        if maut_output.get("status") != "ok":
            results.append({"scenario": name, "status": "maut_failed", "detail": maut_output})
            summary["total"] += 1
            continue

        # Inject dates/num_days for pipeline
        maut_output.setdefault("meta", {})
        maut_output["meta"]["dates"] = payload.get("dates")
        maut_output["meta"]["num_days"] = maut_request.get("num_days")

        t0 = time.perf_counter()
        ot_out = run_full_pipeline(
            maut_output=maut_output,
            pacing=pacing,
            time_limit_sec=time_limit_sec,
            solver="ortools",
        )
        ot_sec = time.perf_counter() - t0

        t1 = time.perf_counter()
        acs_out = run_full_pipeline(
            maut_output=maut_output,
            pacing=pacing,
            solver="acs",
        )
        acs_sec = time.perf_counter() - t1

        ot = _evaluate_output(name, "OR-Tools", pacing, ot_out, ot_sec)
        acs = _evaluate_output(name, "ACS", pacing, acs_out, acs_sec)

        ot_scores_default = _score_components(ot, acs, weights_default)
        acs_scores_default = _score_components(acs, ot, weights_default)
        ot_scores_quality = _score_components(ot, acs, weights_quality)
        acs_scores_quality = _score_components(acs, ot, weights_quality)

        winner_default = (
            "ACS" if acs_scores_default["weighted_total"] > ot_scores_default["weighted_total"] else "OR-Tools"
        )
        winner_quality = (
            "ACS" if acs_scores_quality["weighted_total"] > ot_scores_quality["weighted_total"] else "OR-Tools"
        )

        summary["acs_wins_default"] += 1 if winner_default == "ACS" else 0
        summary["acs_wins_quality"] += 1 if winner_quality == "ACS" else 0
        summary["total"] += 1

        results.append(
            {
                "scenario": name,
                "pacing": pacing,
                "payload": payload,
                "ortools": ot.__dict__,
                "acs": acs.__dict__,
                "scores": {
                    "default": {
                        "weights": weights_default,
                        "ortools": ot_scores_default,
                        "acs": acs_scores_default,
                        "winner": winner_default,
                    },
                    "quality": {
                        "weights": weights_quality,
                        "ortools": ot_scores_quality,
                        "acs": acs_scores_quality,
                        "winner": winner_quality,
                    },
                },
            }
        )

        print(
            f"[{summary['total']}] {name}: default={winner_default} ({ot_scores_default['weighted_total']:.1f} vs {acs_scores_default['weighted_total']:.1f}), "
            f"quality={winner_quality} ({ot_scores_quality['weighted_total']:.1f} vs {acs_scores_quality['weighted_total']:.1f})"
        )

    report = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "vrp_config": {
            "poi_visit_bonus": vrp_model.vrp_config.poi_visit_bonus,
            "theme_diversity_bonus": vrp_model.vrp_config.theme_diversity_bonus,
            "theme_concentration_penalty": vrp_model.vrp_config.theme_concentration_penalty,
            "meal_shortfall_penalty": vrp_model.vrp_config.meal_shortfall_penalty,
            "acs_n_ants": vrp_model.vrp_config.acs_n_ants,
            "acs_n_iterations": vrp_model.vrp_config.acs_n_iterations,
            "acs_alpha": vrp_model.vrp_config.acs_alpha,
            "acs_beta": vrp_model.vrp_config.acs_beta,
            "acs_evaporation_rate": vrp_model.vrp_config.acs_evaporation_rate,
            "acs_q": vrp_model.vrp_config.acs_q,
        },
        "summary": summary,
        "results": results,
    }

    out_path = OUT_DIR / f"bench_results_{int(time.time())}.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)

    print(f"\nSaved report: {out_path}")
    print(f"ACS win-rate (default): {summary['acs_wins_default']}/{summary['total']}")
    print(f"ACS win-rate (quality): {summary['acs_wins_quality']}/{summary['total']}")

    return report


def tune_acs(
    scenarios: List[Dict[str, Any]],
    tune_scenarios: int,
    trials: int,
    time_limit_sec: int,
) -> Dict[str, Any]:
    # Small, safe parameter search focusing on ACS-only knobs + ACS meta params.
    # Goal: improve ACS win-rate on "quality" score.
    base_params = {
        "poi_visit_bonus": vrp_model.vrp_config.poi_visit_bonus,
        "theme_diversity_bonus": vrp_model.vrp_config.theme_diversity_bonus,
        "theme_concentration_penalty": vrp_model.vrp_config.theme_concentration_penalty,
        "meal_shortfall_penalty": vrp_model.vrp_config.meal_shortfall_penalty,
        "acs_n_ants": vrp_model.vrp_config.acs_n_ants,
        "acs_n_iterations": vrp_model.vrp_config.acs_n_iterations,
        "acs_alpha": vrp_model.vrp_config.acs_alpha,
        "acs_beta": vrp_model.vrp_config.acs_beta,
        "acs_evaporation_rate": vrp_model.vrp_config.acs_evaporation_rate,
        "acs_q": vrp_model.vrp_config.acs_q,
    }

    # Candidate presets (deterministic, fast)
    candidates: List[Dict[str, Any]] = [
        {**base_params, "name": "baseline"},
        {
            **base_params,
            "name": "coverage_plus",
            "poi_visit_bonus": int(base_params["poi_visit_bonus"] * 1.5),
            "acs_n_iterations": int(base_params["acs_n_iterations"] * 1.25),
        },
        {
            **base_params,
            "name": "meals_plus",
            "meal_shortfall_penalty": int(base_params["meal_shortfall_penalty"] * 1.5),
        },
        {
            **base_params,
            "name": "quality_plus",
            "poi_visit_bonus": int(base_params["poi_visit_bonus"] * 1.5),
            "meal_shortfall_penalty": int(base_params["meal_shortfall_penalty"] * 1.3),
            "theme_concentration_penalty": int(max(10, base_params["theme_concentration_penalty"] * 0.7)),
            "acs_n_ants": int(base_params["acs_n_ants"] * 1.25),
            "acs_n_iterations": int(base_params["acs_n_iterations"] * 1.25),
        },
    ]

    best = {"name": "baseline", "quality_win_rate": -1, "params": base_params}

    subset = scenarios[: max(1, min(tune_scenarios, len(scenarios)))]

    for idx, cand in enumerate(candidates[: max(1, trials)]):
        name = cand.pop("name")
        _apply_acs_params(cand)
        report = run_bench(subset, only_pattern=None, max_scenarios=len(subset), time_limit_sec=time_limit_sec)
        win_rate = report["summary"]["acs_wins_quality"] / max(1, report["summary"]["total"])
        if win_rate > best["quality_win_rate"]:
            best = {
                "name": name,
                "quality_win_rate": win_rate,
                "params": {k: getattr(vrp_model.vrp_config, k) for k in base_params},
            }
        print(f"TUNE[{idx + 1}] {name}: quality win-rate={win_rate:.2f}")

    # Restore best params
    _apply_acs_params(best["params"])
    return best


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", type=str, default=None, help="Regex to filter scenario names")
    parser.add_argument("--max-scenarios", type=int, default=8, help="Limit scenarios for runtime")
    parser.add_argument("--time-limit-sec", type=int, default=15, help="OR-Tools time limit")

    parser.add_argument("--tune", action="store_true", help="Run a small ACS tuning loop")
    parser.add_argument("--tune-scenarios", type=int, default=6, help="How many scenarios to tune on")
    parser.add_argument("--tune-trials", type=int, default=4, help="How many preset candidates to try")

    parser.add_argument("--optuna", action="store_true", help="Tune ACS vs cached OR-Tools using Optuna")
    parser.add_argument("--optuna-trials", type=int, default=25, help="Optuna trials (runtime grows linearly)")
    parser.add_argument("--optuna-scenarios", type=int, default=6, help="How many scenarios to tune against")
    parser.add_argument("--optuna-seed", type=int, default=7, help="Optuna sampler seed")
    parser.add_argument("--write-yaml", action="store_true", help="Write best params to app/core/vrp_config.yaml")
    parser.add_argument("--include-multicity", action="store_true", help="Include heavy multi-city scenarios")
    parser.add_argument("--include-long", action="store_true", help="Include long-duration scenarios")
    parser.add_argument("--long-days", type=int, default=8, help="Days for long-duration scenarios (8-10)")

    args = parser.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    scenarios = _build_scenarios(
        include_multicity=args.include_multicity,
        include_long=args.include_long,
        long_days=max(1, min(10, args.long_days)),
    )

    if args.tune:
        best = tune_acs(
            scenarios=scenarios,
            tune_scenarios=args.tune_scenarios,
            trials=args.tune_trials,
            time_limit_sec=args.time_limit_sec,
        )
        print("\nBEST TUNE:")
        print(json.dumps(best, indent=2))

    if args.optuna:
        if optuna is None:
            raise SystemExit("optuna not available; install it or disable --optuna")

        cases = _prepare_cases(
            scenarios,
            only_pattern=args.only,
            max_scenarios=args.optuna_scenarios,
            ortools_time_limit_sec=args.time_limit_sec,
        )
        print(f"Prepared {len(cases)} cases for Optuna tuning")
        best = optuna_tune_acs_vs_ortools(
            cases,
            n_trials=args.optuna_trials,
            seed=args.optuna_seed,
            write_yaml=args.write_yaml,
        )
        print("\nOPTUNA BEST:")
        print(json.dumps(best, indent=2))

    run_bench(
        scenarios=scenarios,
        only_pattern=args.only,
        max_scenarios=args.max_scenarios,
        time_limit_sec=args.time_limit_sec,
    )


if __name__ == "__main__":
    main()
