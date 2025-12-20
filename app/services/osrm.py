import requests
from math import radians, sin, cos, sqrt, atan2
from typing import Optional, List, Tuple
from app.core.config import settings
from app.utils.logger import get_logger
from math import ceil

logger = get_logger(__name__)

MAX_OSRM_NODES = 1900
OSRM_TIMEOUT = 5


def tiered_round(minutes: float) -> int:
    """
    Round travel time conservatively for UI / solver.

    10-30 min: ceil to nearest 5
    30-60 min: ceil to nearest 10
    >60 min: ceil to nearest 15
    """
    if minutes < 30:
        return int(ceil(minutes / 5) * 5)
    elif minutes < 60:
        return int(ceil(minutes / 10) * 10)
    else:
        return int(ceil(minutes / 15) * 15)


def haversine_distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in km."""
    R = 6371.0
    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
    return R * 2 * atan2(sqrt(a), sqrt(1 - a))


def haversine_time_seconds(lat1: float, lon1: float, lat2: float, lon2: float, speed_kmh: float = 30.0) -> float:
    """Travel time in seconds assuming constant speed."""
    distance_km = haversine_distance_km(lat1, lon1, lat2, lon2)
    return (distance_km / speed_kmh) * 3600.0


def haversine_matrix(
    coords: List[Tuple[float, float]],
    fallback_speed_kmh: float = 25.0,
) -> List[List[int]]:
    """
    Haversine-based travel time matrix in whole minutes for CVRPTW.
    coords: [(lat, lon), ...] ordered as nodes[0..N-1]
    """
    n = len(coords)
    matrix: List[List[int]] = [[0] * n for _ in range(n)]
    for i in range(n):
        lat1, lon1 = coords[i]
        for j in range(n):
            if i == j:
                continue
            lat2, lon2 = coords[j]
            sec = haversine_time_seconds(lat1, lon1, lat2, lon2, speed_kmh=fallback_speed_kmh)
            minutes = tiered_round(sec / 60.0)
            matrix[i][j] = minutes

    return matrix


class OSRMClient:
    def __init__(self, base_url: Optional[str] = None):
        self.base_url = (base_url or settings.OSRM_URL).rstrip("/")
        self.timeout = OSRM_TIMEOUT
        self._osrm_available: Optional[bool] = None

    # internal

    def _check_osrm_available(self) -> bool:
        """Lightweight health check with caching."""
        if self._osrm_available is not None:
            return self._osrm_available

        try:
            url = f"{self.base_url}/route/v1/driving/0,0;0,0?overview=false"
            resp = requests.get(url, timeout=2)
            self._osrm_available = resp.ok
        except Exception:
            self._osrm_available = False

        if not self._osrm_available:
            logger.warning("OSRM probe failed, will use Haversine fallback")

        return self._osrm_available

    # pairwise API

    def route(
        self,
        lat1: float,
        lon1: float,
        lat2: float,
        lon2: float,
    ) -> float:
        """
        Travel time in seconds between two points.
        """
        if self._check_osrm_available():
            try:
                url = f"{self.base_url}/route/v1/driving/{lon1},{lat1};{lon2},{lat2}?overview=false"
                resp = requests.get(url, timeout=self.timeout)
                resp.raise_for_status()
                data = resp.json()
                duration = float(data["routes"][0]["duration"])
                logger.debug("OSRM route duration: %.1fs", duration)
                return duration
            except requests.exceptions.Timeout:
                logger.warning(
                    "OSRM route timeout after %ss, falling back to Haversine",
                    self.timeout,
                )
            except requests.exceptions.ConnectionError:
                logger.warning("OSRM route connection error, falling back to Haversine")
                self._osrm_available = False
            except Exception as e:
                logger.warning("OSRM route error: %s, falling back to Haversine", e)

        # Fallback
        duration = haversine_time_seconds(lat1, lon1, lat2, lon2)
        logger.debug("Haversine route duration: %.1fs", duration)
        return duration

    def distance(
        self,
        lat1: float,
        lon1: float,
        lat2: float,
        lon2: float,
    ) -> float:
        """
        Travel distance in km between two points.
        """
        if self._check_osrm_available():
            try:
                url = f"{self.base_url}/route/v1/driving/{lon1},{lat1};{lon2},{lat2}?overview=false"
                resp = requests.get(url, timeout=self.timeout)
                resp.raise_for_status()
                data = resp.json()
                distance = float(data["routes"][0]["distance"]) / 1000.0
                logger.debug("OSRM route distance: %.2fkm", distance)
                return distance
            except requests.exceptions.Timeout:
                logger.warning(
                    "OSRM distance timeout after %ss, falling back to Haversine",
                    self.timeout,
                )
            except requests.exceptions.ConnectionError:
                logger.warning("OSRM distance connection error, falling back to Haversine")
                self._osrm_available = False
            except Exception as e:
                logger.warning("OSRM distance error: %s, falling back to Haversine", e)

        # Fallback
        distance = haversine_distance_km(lat1, lon1, lat2, lon2)
        logger.debug("Haversine distance: %.2fkm", distance)
        return distance

    # matrix API for CVRPTW

    def matrix_minutes(
        self,
        coords: List[Tuple[float, float]],
        fallback_speed_kmh: float = 25.0,
    ) -> List[List[int]]:
        """
        NxN travel time matrix in whole minutes for CVRPTW.
        coords: [(lat, lon), ...] ordered as nodes[0..N-1]
        """
        n = len(coords)
        if n == 0:
            return []
        if n == 1:
            return [[0]]

        if n > MAX_OSRM_NODES:
            logger.info(
                "Skipping OSRM matrix for %d nodes > %d max, using Haversine fallback",
                n,
                MAX_OSRM_NODES,
            )
            return haversine_matrix(coords, fallback_speed_kmh)

        # OSRM
        if self._check_osrm_available():
            try:
                coord_str = ";".join(f"{lon},{lat}" for (lat, lon) in coords)
                url = f"{self.base_url}/table/v1/driving/{coord_str}?annotations=duration"
                resp = requests.get(url, timeout=self.timeout)
                resp.raise_for_status()
                data = resp.json()
                durations = data.get("durations")
                if not durations:
                    raise ValueError("OSRM /table missing 'durations'")

                # OSRM returns seconds; convert to int minutes
                matrix: List[List[int]] = [[0] * n for _ in range(n)]
                for i in range(n):
                    row = durations[i]
                    for j in range(n):
                        sec = row[j] if row[j] is not None else 0.0
                        minutes = tiered_round(float(sec) / 60.0)
                        matrix[i][j] = minutes

                logger.info("OSRM matrix computed: %d nodes", n)
                return matrix

            except requests.exceptions.Timeout:
                logger.warning(
                    "OSRM /table timeout after %ss, falling back to Haversine matrix",
                    self.timeout,
                )
            except requests.exceptions.ConnectionError:
                logger.warning("OSRM /table connection error, falling back to Haversine matrix")
                self._osrm_available = False
            except Exception as e:
                logger.warning("OSRM /table error: %s, falling back to Haversine", e)

        # Fallback: Haversine-based matrix in minutes
        logger.info("Using Haversine fallback matrix for %d nodes", n)
        matrix: List[List[int]] = [[0] * n for _ in range(n)]
        for i in range(n):
            lat1, lon1 = coords[i]
            for j in range(n):
                if i == j:
                    continue
                lat2, lon2 = coords[j]
                sec = haversine_time_seconds(lat1, lon1, lat2, lon2, speed_kmh=fallback_speed_kmh)
                minutes = tiered_round(sec / 60.0)
                matrix[i][j] = minutes

        return matrix


osrm_client = OSRMClient()
