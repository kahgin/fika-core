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

    assert best_path is not None
