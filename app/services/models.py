from dataclasses import dataclass
import datetime as dt
from typing import List, Dict, Tuple, Optional


@dataclass
class DaySpec:
    day_index: int
    date: dt.date
    start_min: int
    end_min: int
    depot_id: str


@dataclass
class Node:
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
