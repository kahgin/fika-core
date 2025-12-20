"""
Chart Generator for Solver Comparison Results.

Generates publication-quality comparison charts for OR-Tools (Baseline) vs ACS-CVRPTW (System).
"""

import os
from typing import Any, Dict, List, Optional

import matplotlib.pyplot as plt
import numpy as np
import seaborn as sns


OUTPUT_DIR = os.path.dirname(__file__)

SCENARIO_LABELS = {
    "scenario_1": "Multi-City\nBalanced",
    "scenario_2": "Single\nTheme",
    "scenario_3": "Family\nConstraints",
    "scenario_4": "Mandatory\nPOI + Hotel",
    "scenario_5": "Multi-City\nAssigned Days",
}

BASELINE_COLOR = "#5B9BD5"
SYSTEM_COLOR = "#ED7D31"
BASELINE_LABEL = "OR-Tools (Baseline)"
SYSTEM_LABEL = "ACS-CVRPTW (System)"


def _setup_style() -> None:
    """Configure matplotlib and seaborn for publication-quality output."""
    sns.set_theme(style="whitegrid", context="paper", font_scale=1.1)
    plt.rcParams.update(
        {
            "font.family": "sans-serif",
            "font.sans-serif": ["DejaVu Sans", "Arial", "Helvetica"],
            "axes.titleweight": "bold",
            "axes.labelweight": "medium",
            "figure.dpi": 150,
            "savefig.dpi": 300,
            "savefig.bbox": "tight",
            "savefig.pad_inches": 0.1,
        }
    )


def _get_scenario_order(results: Dict[str, Any]) -> List[str]:
    """Return scenario keys in consistent order."""
    ordered = ["scenario_1", "scenario_2", "scenario_3", "scenario_4", "scenario_5"]
    return [k for k in ordered if k in results]


def _add_value_labels(ax: plt.Axes, bars: Any, fmt: str = ".1f", fontsize: int = 8) -> None:
    """Add value labels on top of bars."""
    for bar in bars:
        height = bar.get_height()
        if height > 0:
            ax.annotate(
                f"{height:{fmt}}",
                xy=(bar.get_x() + bar.get_width() / 2, height),
                xytext=(0, 3),
                textcoords="offset points",
                ha="center",
                va="bottom",
                fontsize=fontsize,
                fontweight="medium",
            )


def generate_execution_time_chart(
    results: Dict[str, Any],
    output_path: Optional[str] = None,
) -> str:
    """
    Generate execution time comparison chart.

    Args:
        results: Dictionary of scenario results
        output_path: Optional custom output path

    Returns:
        Path to the saved chart image
    """
    _setup_style()

    if output_path is None:
        output_path = os.path.join(OUTPUT_DIR, "chart_execution_time.png")

    scenarios = _get_scenario_order(results)
    x = np.arange(len(scenarios))
    bar_width = 0.35

    baseline_vals = [results[s].get("ortools", {}).get("execution_time_sec", 0) for s in scenarios]
    system_vals = [results[s].get("acs", {}).get("execution_time_sec", 0) for s in scenarios]

    fig, ax = plt.subplots(figsize=(10, 6))

    bars1 = ax.bar(
        x - bar_width / 2,
        baseline_vals,
        bar_width,
        label=BASELINE_LABEL,
        color=BASELINE_COLOR,
        edgecolor="white",
        linewidth=0.7,
    )
    bars2 = ax.bar(
        x + bar_width / 2,
        system_vals,
        bar_width,
        label=SYSTEM_LABEL,
        color=SYSTEM_COLOR,
        edgecolor="white",
        linewidth=0.7,
    )

    ax.set_xlabel("Scenario", fontsize=11)
    ax.set_ylabel("Execution Time (seconds)", fontsize=11)
    ax.set_title("Execution Time Comparison", fontsize=13, pad=15)
    ax.set_xticks(x)
    ax.set_xticklabels([SCENARIO_LABELS.get(s, s) for s in scenarios], fontsize=9)
    ax.legend(loc="upper right", framealpha=0.9)
    ax.set_ylim(bottom=0)

    _add_value_labels(ax, bars1, fmt=".1f")
    _add_value_labels(ax, bars2, fmt=".1f")

    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)

    plt.tight_layout()
    plt.savefig(output_path)
    plt.close(fig)

    return output_path


def generate_meal_compliance_chart(
    results: Dict[str, Any],
    output_path: Optional[str] = None,
) -> str:
    """
    Generate meal window compliance comparison chart.

    Args:
        results: Dictionary of scenario results
        output_path: Optional custom output path

    Returns:
        Path to the saved chart image
    """
    _setup_style()

    if output_path is None:
        output_path = os.path.join(OUTPUT_DIR, "chart_meal_compliance.png")

    scenarios = _get_scenario_order(results)
    x = np.arange(len(scenarios))
    bar_width = 0.35

    baseline_vals = [results[s].get("ortools", {}).get("meal_window_compliance", 0) for s in scenarios]
    system_vals = [results[s].get("acs", {}).get("meal_window_compliance", 0) for s in scenarios]

    fig, ax = plt.subplots(figsize=(10, 6))

    bars1 = ax.bar(
        x - bar_width / 2,
        baseline_vals,
        bar_width,
        label=BASELINE_LABEL,
        color=BASELINE_COLOR,
        edgecolor="white",
        linewidth=0.7,
    )
    bars2 = ax.bar(
        x + bar_width / 2,
        system_vals,
        bar_width,
        label=SYSTEM_LABEL,
        color=SYSTEM_COLOR,
        edgecolor="white",
        linewidth=0.7,
    )

    ax.set_xlabel("Scenario", fontsize=11)
    ax.set_ylabel("Meal Window Compliance (%)", fontsize=11)
    ax.set_title("Meal Window Compliance Comparison", fontsize=13, pad=15)
    ax.set_xticks(x)
    ax.set_xticklabels([SCENARIO_LABELS.get(s, s) for s in scenarios], fontsize=9)
    ax.legend(loc="lower right", framealpha=0.9)
    ax.set_ylim(0, 110)

    ax.axhline(y=100, color="gray", linestyle="--", linewidth=0.8, alpha=0.7)

    _add_value_labels(ax, bars1, fmt=".1f")
    _add_value_labels(ax, bars2, fmt=".1f")

    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)

    plt.tight_layout()
    plt.savefig(output_path)
    plt.close(fig)

    return output_path


def generate_total_distance_chart(
    results: Dict[str, Any],
    output_path: Optional[str] = None,
) -> str:
    """
    Generate total distance comparison chart.
    """
    _setup_style()

    if output_path is None:
        output_path = os.path.join(OUTPUT_DIR, "chart_total_distance.png")

    scenarios = _get_scenario_order(results)
    x = np.arange(len(scenarios))
    bar_width = 0.35

    baseline_vals = [results[s].get("ortools", {}).get("total_distance_km", 0) for s in scenarios]
    system_vals = [results[s].get("acs", {}).get("total_distance_km", 0) for s in scenarios]

    fig, ax = plt.subplots(figsize=(10, 6))

    bars1 = ax.bar(
        x - bar_width / 2,
        baseline_vals,
        bar_width,
        label=BASELINE_LABEL,
        color=BASELINE_COLOR,
        edgecolor="white",
        linewidth=0.7,
    )
    bars2 = ax.bar(
        x + bar_width / 2,
        system_vals,
        bar_width,
        label=SYSTEM_LABEL,
        color=SYSTEM_COLOR,
        edgecolor="white",
        linewidth=0.7,
    )

    ax.set_xlabel("Scenario", fontsize=11)
    ax.set_ylabel("Total Distance (km)", fontsize=11)
    ax.set_title("Total Distance Comparison", fontsize=13, pad=15)
    ax.set_xticks(x)
    ax.set_xticklabels([SCENARIO_LABELS.get(s, s) for s in scenarios], fontsize=9)
    ax.legend(loc="upper right", framealpha=0.9)
    ax.set_ylim(bottom=0)

    _add_value_labels(ax, bars1, fmt=".0f")
    _add_value_labels(ax, bars2, fmt=".0f")

    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)

    plt.tight_layout()
    plt.savefig(output_path)
    plt.close(fig)

    return output_path


def generate_theme_coverage_chart(
    results: Dict[str, Any],
    output_path: Optional[str] = None,
) -> str:
    """
    Generate theme coverage comparison chart.
    """
    _setup_style()

    if output_path is None:
        output_path = os.path.join(OUTPUT_DIR, "chart_theme_coverage.png")

    scenarios = _get_scenario_order(results)
    x = np.arange(len(scenarios))
    bar_width = 0.35

    baseline_vals = [
        results[s].get("ortools", {}).get("preference_alignment", {}).get("theme_coverage", 0) for s in scenarios
    ]
    system_vals = [
        results[s].get("acs", {}).get("preference_alignment", {}).get("theme_coverage", 0) for s in scenarios
    ]

    fig, ax = plt.subplots(figsize=(10, 6))

    bars1 = ax.bar(
        x - bar_width / 2,
        baseline_vals,
        bar_width,
        label=BASELINE_LABEL,
        color=BASELINE_COLOR,
        edgecolor="white",
        linewidth=0.7,
    )
    bars2 = ax.bar(
        x + bar_width / 2,
        system_vals,
        bar_width,
        label=SYSTEM_LABEL,
        color=SYSTEM_COLOR,
        edgecolor="white",
        linewidth=0.7,
    )

    ax.set_xlabel("Scenario", fontsize=11)
    ax.set_ylabel("Theme Coverage (%)", fontsize=11)
    ax.set_title("Theme Coverage Comparison", fontsize=13, pad=15)
    ax.set_xticks(x)
    ax.set_xticklabels([SCENARIO_LABELS.get(s, s) for s in scenarios], fontsize=9)
    ax.legend(loc="lower right", framealpha=0.9)
    ax.set_ylim(0, 110)

    ax.axhline(y=100, color="gray", linestyle="--", linewidth=0.8, alpha=0.7)

    _add_value_labels(ax, bars1, fmt=".1f")
    _add_value_labels(ax, bars2, fmt=".1f")

    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)

    plt.tight_layout()
    plt.savefig(output_path)
    plt.close(fig)

    return output_path


def generate_all_charts(results: Dict[str, Any]) -> List[str]:
    """
    Generate all comparison charts.

    Args:
        results: Dictionary of scenario results

    Returns:
        List of paths to saved chart images
    """
    paths = []

    path1 = generate_execution_time_chart(results)
    paths.append(path1)

    path2 = generate_meal_compliance_chart(results)
    paths.append(path2)

    path3 = generate_total_distance_chart(results)
    paths.append(path3)

    path4 = generate_theme_coverage_chart(results)
    paths.append(path4)

    return paths
