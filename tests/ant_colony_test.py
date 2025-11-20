from app.services.ant_colony_opt import (
    AntColonyOptimizer,
    ACOConfig,
    create_distance_matrix,
)
import numpy as np


def test_ant_colony_optimization():
    # Sample cities (x, y coordinates)
    np.random.seed(42)
    cities = np.random.rand(20, 2) * 100

    # Create distance matrix
    dist_matrix = create_distance_matrix(cities)

    # Configure and run ACO
    config = ACOConfig(
        n_ants=30, n_iterations=100, alpha=1.0, beta=3.0, evaporation_rate=0.3, n_best=5
    )

    aco = AntColonyOptimizer(dist_matrix, config)

    def progress(iteration: int, distance: float) -> None:
        if iteration % 10 == 0:
            print(f"Iteration {iteration}: Best distance = {distance:.2f}")

    best_path, best_distance = aco.optimize(callback=progress)

    print(f"\nBest path: {best_path}")
    print(f"Best distance: {best_distance:.2f}")

    # Validate path structure
    n = len(cities)
    assert best_path is not None, "ACO returned None path"
    assert (
        len(best_path) == n
    ), f"ACO path length mismatch: expected {n}, got {len(best_path)}"
    assert len(set(best_path)) == n, "ACO path has duplicate nodes"
    assert all(0 <= idx < n for idx in best_path), "ACO path index out of bounds"

    # Validate distance sanity - ACO should not be worse than naive path
    def route_length(path, matrix):
        total = 0
        for i in range(len(path)):
            total += matrix[path[i]][path[(i + 1) % len(path)]]
        return total

    naive_path = list(range(n))
    naive_distance = route_length(naive_path, dist_matrix)
    assert (
        best_distance <= naive_distance * 1.05
    ), f"ACO distance {best_distance:.2f} worse than naive {naive_distance:.2f}"

    print(f"✅ ACO path valid: {n} unique cities")
    print(
        f"✅ Distance improvement over naive: {((naive_distance - best_distance) / naive_distance * 100):.1f}%"
    )
