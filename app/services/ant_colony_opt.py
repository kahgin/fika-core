from __future__ import annotations

import numpy as np
from dataclasses import dataclass
from typing import Protocol, Callable
import numba


@dataclass(frozen=True, slots=True)
class ACOConfig:
    """Immutable configuration for ACO algorithm."""

    n_ants: int = 50
    n_iterations: int = 100
    alpha: float = 1.0  # pheromone importance
    beta: float = 2.0  # heuristic importance
    evaporation_rate: float = 0.5
    q: float = 100.0  # pheromone deposit factor
    n_best: int = 5  # elite ants


class DistanceMatrix(Protocol):
    """Protocol for distance calculation."""

    def __call__(self, i: int, j: int) -> float: ...


@numba.njit(cache=True, fastmath=True)
def _calculate_probabilities(
    pheromones: np.ndarray,
    heuristic: np.ndarray,
    visited: np.ndarray,
    current: int,
    alpha: float,
    beta: float,
) -> np.ndarray:
    """Numba-optimized probability calculation."""
    n = len(pheromones)
    probs = np.zeros(n, dtype=np.float64)

    for j in range(n):
        if not visited[j]:
            probs[j] = (pheromones[current, j] ** alpha) * (
                heuristic[current, j] ** beta
            )

    total = np.sum(probs)
    if total > 0:
        probs /= total

    return probs


@numba.njit(cache=True, fastmath=True)
def _construct_solution(
    pheromones: np.ndarray,
    heuristic: np.ndarray,
    start: int,
    alpha: float,
    beta: float,
    seed: int,
) -> tuple[np.ndarray, float]:
    """Numba-optimized ant solution construction."""
    np.random.seed(seed)
    n = len(pheromones)
    visited = np.zeros(n, dtype=np.bool_)
    path = np.zeros(n, dtype=np.int32)

    current = start
    path[0] = current
    visited[current] = True
    total_distance = 0.0

    for step in range(1, n):
        probs = _calculate_probabilities(
            pheromones, heuristic, visited, current, alpha, beta
        )

        # Roulette wheel selection
        cumsum = np.cumsum(probs)
        r = np.random.random()
        next_city = np.searchsorted(cumsum, r)

        path[step] = next_city
        total_distance += (
            1.0 / heuristic[current, next_city]
            if heuristic[current, next_city] > 0
            else 1e10
        )
        visited[next_city] = True
        current = next_city

    # Return to start
    total_distance += (
        1.0 / heuristic[current, start] if heuristic[current, start] > 0 else 1e10
    )

    return path, total_distance


class AntColonyOptimizer:
    """High-performance ACO for TSP with modern Python practices."""

    __slots__ = (
        "config",
        "distance_matrix",
        "n_cities",
        "pheromones",
        "heuristic",
        "best_path",
        "best_distance",
        "history",
    )

    def __init__(self, distance_matrix: np.ndarray, config: ACOConfig | None = None):
        self.config = config or ACOConfig()
        self.distance_matrix = distance_matrix
        self.n_cities = len(distance_matrix)

        # Initialize pheromones and heuristic
        self.pheromones = np.ones((self.n_cities, self.n_cities), dtype=np.float64)

        # Heuristic: inverse of distance (avoid division by zero)
        with np.errstate(divide="ignore", invalid="ignore"):
            self.heuristic = np.where(distance_matrix > 0, 1.0 / distance_matrix, 0.0)

        self.best_path: np.ndarray | None = None
        self.best_distance = np.inf
        self.history: list[float] = []

    def _construct_solutions(self) -> list[tuple[np.ndarray, float]]:
        """Construct solutions for all ants."""
        solutions = []

        for ant in range(self.config.n_ants):
            start = np.random.randint(0, self.n_cities)
            seed = np.random.randint(0, 2**31)

            path, distance = _construct_solution(
                self.pheromones,
                self.heuristic,
                start,
                self.config.alpha,
                self.config.beta,
                seed,
            )
            solutions.append((path, distance))

        return solutions

    def _update_pheromones(self, solutions: list[tuple[np.ndarray, float]]) -> None:
        """Update pheromone trails using elitist strategy."""
        # Evaporation
        self.pheromones *= 1 - self.config.evaporation_rate

        # Sort solutions by distance (best first)
        solutions.sort(key=lambda x: x[1])

        # Deposit pheromones from elite ants
        for path, distance in solutions[: self.config.n_best]:
            deposit = self.config.q / distance

            for i in range(len(path) - 1):
                self.pheromones[path[i], path[i + 1]] += deposit
                self.pheromones[path[i + 1], path[i]] += deposit

            # Close the loop
            self.pheromones[path[-1], path[0]] += deposit
            self.pheromones[path[0], path[-1]] += deposit

        # Additional boost for global best
        if self.best_path is not None:
            deposit = self.config.q / self.best_distance
            for i in range(len(self.best_path) - 1):
                self.pheromones[self.best_path[i], self.best_path[i + 1]] += deposit * 2
                self.pheromones[self.best_path[i + 1], self.best_path[i]] += deposit * 2

    def optimize(
        self, callback: Callable[[int, float], None] | None = None
    ) -> tuple[np.ndarray, float]:
        """Run ACO optimization."""
        for iteration in range(self.config.n_iterations):
            solutions = self._construct_solutions()

            # Update best solution
            for path, distance in solutions:
                if distance < self.best_distance:
                    self.best_distance = distance
                    self.best_path = path.copy()

            self._update_pheromones(solutions)
            self.history.append(self.best_distance)

            if callback:
                callback(iteration, self.best_distance)

        return self.best_path, self.best_distance


def create_distance_matrix(coordinates: np.ndarray) -> np.ndarray:
    """Create Euclidean distance matrix from coordinates."""
    n = len(coordinates)
    distances = np.zeros((n, n), dtype=np.float64)

    for i in range(n):
        for j in range(i + 1, n):
            dist = np.linalg.norm(coordinates[i] - coordinates[j])
            distances[i, j] = dist
            distances[j, i] = dist

    return distances
