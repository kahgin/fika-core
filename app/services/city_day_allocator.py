"""
City Day Allocator - Handles intelligent day-to-city mapping with mandatory POI constraints.

This module implements contiguous block allocation that:
1. Honors explicit user day assignments (day → city or day → specific POI)
2. Respects mandatory POIs with fixed days
3. Uses proportional allocation for remaining days with weighted counts
4. Applies contiguity smoothing to minimize travel shuffles
5. Expands blocks adjacent to fixed days first

Examples (5-day trip):
A - User: day1 = SG POI, day2 = Johor POI, day3 = SG POI
    Fixed: 1→SG, 2→Johor, 3→SG. Remaining days: 4,5.

B - User: day1 = Johor, day3 = Singapore
    Fixed: 1→Johor, 3→Singapore. Remaining: 2,4,5.
    Contiguous: 1,2 → Johor, 3,4,5 → Singapore.

C - User: day1 = Johor, day2 = Singapore
    Fixed: 1→Johor, 2→Singapore. Remaining: 3,4,5.
    Contiguous: 1 → Johor, 2,3,4,5 → Singapore.

D - User: day1 = Johor, day2 = Singapore, day5 = Johor
    Fixed: 1→J, 2→S, 5→J. Remaining: 3,4.
    Balanced: 1→J, 2,3→S, 4,5→J (or similar balanced split).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Dict, List, Optional, Set, Tuple, Any

from app.utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class FixedDayAssignment:
    """Represents a fixed day-to-city assignment from user input or mandatory POI."""
    day: int  # 1-based day index
    city: str
    source: str  # 'user_day', 'user_poi', 'mandatory_poi'
    poi_id: Optional[str] = None


@dataclass
class CityDayAllocation:
    """Result of city day allocation."""
    day_to_city: Dict[int, str]  # 1-based day index → city name
    city_to_days: Dict[str, List[int]]  # city name → list of 1-based day indices
    city_order: List[str]  # Order of cities as they appear in the trip
    fixed_days: Dict[int, FixedDayAssignment]  # Days that cannot be moved
    city_switches: List[int]  # Day indices where city changes (for travel planning)


def _normalize_city_name(raw: Optional[str]) -> Optional[str]:
    """Normalize city name for matching."""
    if not raw:
        return None
    name = str(raw).strip()
    if "," in name:
        name = name.split(",")[0].strip()
    return name


def _parse_day_from_date(
    date_str: str,
    trip_start: date,
) -> Optional[int]:
    """Convert a date string to 1-based day index relative to trip start."""
    try:
        poi_date = date.fromisoformat(str(date_str).split("T")[0])
        day_index = (poi_date - trip_start).days + 1
        return day_index if day_index > 0 else None
    except (ValueError, TypeError):
        return None


def extract_fixed_assignments(
    total_days: int,
    mandatory: Optional[Dict[str, Dict]] = None,
    user_input: Optional[Dict[str, Any]] = None,
    poi_city_lookup: Optional[Dict[str, str]] = None,
    trip_start: Optional[date] = None,
) -> Dict[int, FixedDayAssignment]:
    """
    Extract all fixed day-to-city assignments from user input and mandatory POIs.
    
    Priority:
    1. User explicit day → city assignments (from user_input.day_assignments)
    2. User explicit day → POI assignments (POI's city forces that day)
    3. Mandatory POIs with fixed days
    
    Args:
        total_days: Total number of days in the trip
        mandatory: Mandatory POI constraints {poi_id: {day, window, time_type, poi_destination}}
        user_input: User preferences including day_assignments, days_per_city
        poi_city_lookup: Mapping of poi_id → city name
        trip_start: Trip start date for date-to-day conversion
        
    Returns:
        Dict mapping 1-based day index → FixedDayAssignment
    """
    fixed: Dict[int, FixedDayAssignment] = {}
    poi_city_lookup = poi_city_lookup or {}
    
    # 1. User explicit day → city assignments
    if user_input:
        day_assignments = user_input.get("day_assignments", {})
        for day_str, city in day_assignments.items():
            try:
                day = int(day_str)
                if 1 <= day <= total_days:
                    city_norm = _normalize_city_name(city)
                    if city_norm:
                        fixed[day] = FixedDayAssignment(
                            day=day,
                            city=city_norm,
                            source="user_day",
                        )
            except (ValueError, TypeError):
                continue
    
    # 2. Mandatory POIs with fixed days
    if mandatory:
        for poi_id, spec in mandatory.items():
            if not spec:
                continue
                
            # Get day from spec (1-based)
            day = spec.get("day")
            date_str = spec.get("date")
            
            # Convert date to day if needed
            if day is None and date_str and trip_start:
                day = _parse_day_from_date(date_str, trip_start)
            
            if day is None or not (1 <= day <= total_days):
                continue
            
            # Get city from spec or lookup
            city = spec.get("poi_destination")
            if not city:
                city = poi_city_lookup.get(poi_id)
            city_norm = _normalize_city_name(city)
            
            if not city_norm:
                continue
            
            # Don't override user explicit day assignments
            if day in fixed and fixed[day].source == "user_day":
                continue
            
            fixed[day] = FixedDayAssignment(
                day=day,
                city=city_norm,
                source="mandatory_poi",
                poi_id=poi_id,
            )
    
    return fixed


def _count_pois_per_city(
    cities: Dict[str, Dict[str, Any]],
    mandatory: Optional[Dict[str, Dict]] = None,
    poi_city_lookup: Optional[Dict[str, str]] = None,
) -> Dict[str, Tuple[int, int]]:
    """
    Count POIs per city with weighted counts (mandatory weight > optional weight).
    
    Also counts mandatory POIs from the mandatory dict that may not be in places.
    
    Returns:
        Dict mapping city → (mandatory_count, optional_count)
    """
    poi_city_lookup = poi_city_lookup or {}
    mandatory = mandatory or {}
    mandatory_ids: Set[str] = set(mandatory.keys())
    
    # Track which mandatory POIs we've already counted
    counted_mandatory: Set[str] = set()
    
    # Initialize counts for all cities
    counts: Dict[str, Tuple[int, int]] = {city: (0, 0) for city in cities}
    
    # Count POIs from places
    for city, city_data in cities.items():
        places = city_data.get("places", [])
        mandatory_count = 0
        optional_count = 0
        
        for poi in places:
            poi_id = poi.get("id", "")
            base_id = poi_id.rsplit("_day", 1)[0] if "_day" in poi_id else poi_id
            
            # Skip accommodations
            if "accommodation" in poi.get("roles", []):
                continue
            
            if base_id in mandatory_ids:
                mandatory_count += 1
                counted_mandatory.add(base_id)
            else:
                optional_count += 1
        
        counts[city] = (mandatory_count, optional_count)
    
    # Also count mandatory POIs from the mandatory dict by their destination
    # (for POIs not in places list)
    for poi_id, spec in mandatory.items():
        if poi_id in counted_mandatory:
            continue
        if not spec:
            continue
        
        # Get city from poi_destination or poi_city_lookup
        poi_dest = spec.get("poi_destination")
        if not poi_dest:
            poi_dest = poi_city_lookup.get(poi_id)
        
        if poi_dest:
            city_norm = _normalize_city_name(poi_dest)
            if city_norm:
                # Find matching city
                matched = False
                for city in cities:
                    city_lower = city.lower()
                    dest_lower = city_norm.lower()
                    if city_lower == dest_lower or city_lower in dest_lower or dest_lower in city_lower:
                        mand, opt = counts.get(city, (0, 0))
                        counts[city] = (mand + 1, opt)
                        counted_mandatory.add(poi_id)
                        matched = True
                        break
                
                # If no match found but we have the city in poi_city_lookup, use that
                if not matched and poi_id in poi_city_lookup:
                    lookup_city = poi_city_lookup[poi_id]
                    if lookup_city in cities:
                        mand, opt = counts.get(lookup_city, (0, 0))
                        counts[lookup_city] = (mand + 1, opt)
                        counted_mandatory.add(poi_id)
    
    logger.debug(f"POI counts per city: {counts}, mandatory_ids={mandatory_ids}, counted={counted_mandatory}")
    
    return counts


def _compute_weighted_days(
    poi_counts: Dict[str, Tuple[int, int]],
    total_days: int,
    fixed_days_per_city: Dict[str, int],
    city_order: List[str],
    mandatory_weight: float = 2.0,
    optional_weight: float = 1.0,
) -> Dict[str, int]:
    """
    Compute proportional day allocation using weighted POI counts.
    
    Args:
        poi_counts: Dict mapping city → (mandatory_count, optional_count)
        total_days: Total days to allocate
        fixed_days_per_city: Days already fixed per city
        city_order: Preferred order of cities
        mandatory_weight: Weight multiplier for mandatory POIs
        optional_weight: Weight multiplier for optional POIs
        
    Returns:
        Dict mapping city → total allocated days (including fixed)
    """
    # Calculate weighted scores
    weighted_scores: Dict[str, float] = {}
    for city, (mand, opt) in poi_counts.items():
        weighted_scores[city] = mand * mandatory_weight + opt * optional_weight
    
    total_weight = sum(weighted_scores.values())
    
    # Calculate remaining days to allocate
    total_fixed = sum(fixed_days_per_city.values())
    remaining_days = total_days - total_fixed
    
    if remaining_days <= 0:
        # Ensure all cities in fixed_days_per_city are included
        result = {city: 0 for city in poi_counts}
        result.update(fixed_days_per_city)
        return result
    
    # Initialize with fixed days
    allocated: Dict[str, int] = {city: 0 for city in poi_counts}
    for city, days in fixed_days_per_city.items():
        allocated[city] = days
    
    if total_weight == 0:
        # Equal distribution if no POIs - use city_order
        cities_to_fill = [c for c in city_order if c in poi_counts]
        if not cities_to_fill:
            cities_to_fill = list(poi_counts.keys())
        
        per_city = remaining_days // len(cities_to_fill) if cities_to_fill else 0
        extra = remaining_days % len(cities_to_fill) if cities_to_fill else 0
        
        for i, city in enumerate(cities_to_fill):
            allocated[city] += per_city + (1 if i < extra else 0)
        
        return allocated
    
    # Proportional allocation of remaining days
    remaining = remaining_days
    
    # First pass: allocate proportionally (floor)
    for city, score in weighted_scores.items():
        if score > 0:
            proportion = score / total_weight
            extra_days = int(proportion * remaining_days)
            allocated[city] += extra_days
            remaining -= extra_days
    
    # Second pass: distribute remaining days to cities with highest scores
    if remaining > 0:
        sorted_cities = sorted(
            weighted_scores.keys(),
            key=lambda c: weighted_scores[c],
            reverse=True,
        )
        for city in sorted_cities:
            if remaining <= 0:
                break
            allocated[city] += 1
            remaining -= 1
    
    # Ensure at least 1 day per city with POIs (if we have enough days)
    for city in poi_counts:
        if allocated.get(city, 0) == 0 and sum(poi_counts[city]) > 0:
            # Steal from city with most days
            max_city = max(allocated.keys(), key=lambda c: allocated[c])
            if allocated[max_city] > 1:
                allocated[max_city] -= 1
                allocated[city] = 1
    
    return allocated


def _build_contiguous_blocks(
    total_days: int,
    fixed: Dict[int, FixedDayAssignment],
    target_days_per_city: Dict[str, int],
    city_order: List[str],
) -> Dict[int, str]:
    """
    Build contiguous day blocks per city, minimizing travel shuffles.
    
    Algorithm:
    1. Start with fixed day assignments
    2. Identify segments between fixed days
    3. For each segment, expand from adjacent fixed days
    4. Minimize city switches while respecting target allocations
    
    Args:
        total_days: Total number of days
        fixed: Fixed day assignments
        target_days_per_city: Target days per city from weighted allocation
        city_order: Preferred order of cities
        
    Returns:
        Dict mapping 1-based day index → city name
    """
    day_to_city: Dict[int, str] = {}
    
    # Initialize with fixed assignments
    for day, assignment in fixed.items():
        day_to_city[day] = assignment.city
    
    # Track current allocation per city
    current_per_city: Dict[str, int] = {city: 0 for city in target_days_per_city}
    for day, city in day_to_city.items():
        current_per_city[city] = current_per_city.get(city, 0) + 1
    
    def needs_more_days(city: str) -> int:
        """Return how many more days city needs."""
        return max(0, target_days_per_city.get(city, 0) - current_per_city.get(city, 0))
    
    def assign_day(day: int, city: str):
        """Assign a day to a city."""
        day_to_city[day] = city
        current_per_city[city] = current_per_city.get(city, 0) + 1
    
    # Get unfixed days
    unfixed_days = set(d for d in range(1, total_days + 1) if d not in day_to_city)
    
    if not unfixed_days:
        return day_to_city
    
    # If no fixed days, use city_order to create contiguous blocks
    if not fixed:
        day = 1
        for city in city_order:
            days_needed = target_days_per_city.get(city, 0)
            for _ in range(days_needed):
                if day <= total_days:
                    assign_day(day, city)
                    unfixed_days.discard(day)
                    day += 1
        
        # Assign any remaining days to last city in order
        if unfixed_days and city_order:
            last_city = city_order[-1]
            for day in sorted(unfixed_days):
                assign_day(day, last_city)
        
        return day_to_city
    
    # With fixed days: identify segments and fill them
    sorted_fixed_days = sorted(fixed.keys())
    
    # Build segments: [(start, end, left_city, right_city)]
    # A segment is a range of unfixed days between fixed days (or boundaries)
    segments: List[Tuple[int, int, Optional[str], Optional[str]]] = []
    
    # Segment before first fixed day
    if sorted_fixed_days[0] > 1:
        segments.append((1, sorted_fixed_days[0] - 1, None, fixed[sorted_fixed_days[0]].city))
    
    # Segments between fixed days
    for i in range(len(sorted_fixed_days) - 1):
        left_day = sorted_fixed_days[i]
        right_day = sorted_fixed_days[i + 1]
        if right_day - left_day > 1:
            segments.append((
                left_day + 1,
                right_day - 1,
                fixed[left_day].city,
                fixed[right_day].city,
            ))
    
    # Segment after last fixed day
    if sorted_fixed_days[-1] < total_days:
        segments.append((
            sorted_fixed_days[-1] + 1,
            total_days,
            fixed[sorted_fixed_days[-1]].city,
            None,
        ))
    
    # Fill each segment
    for seg_start, seg_end, left_city, right_city in segments:
        seg_days = list(range(seg_start, seg_end + 1))
        seg_len = len(seg_days)
        
        if seg_len == 0:
            continue
        
        # Determine how to split this segment
        if left_city == right_city:
            # Same city on both sides - fill with that city
            for day in seg_days:
                if day in unfixed_days:
                    assign_day(day, left_city)
                    unfixed_days.discard(day)
        elif left_city and right_city:
            # Different cities on each side - split to create contiguous blocks
            # Strategy: Extend the earlier (left) city to minimize switches
            # This creates pattern: [left_city block] [right_city block]
            
            left_needs = needs_more_days(left_city)
            right_needs = needs_more_days(right_city)
            
            # Key insight: To minimize switches, we want to extend from one side
            # Prefer extending the left city (earlier in trip) to create larger contiguous blocks
            # This matches user expectation: day1=Johor, day3=Singapore → 1,2→Johor, 3,4,5→Singapore
            
            if left_needs >= seg_len:
                # Left city needs all or more than this segment - give all to left
                left_share = seg_len
            elif left_needs > 0:
                # Left city needs some days - give it what it needs
                left_share = left_needs
            elif right_needs > 0:
                # Left city doesn't need days, right city does - give all to right
                left_share = 0
            else:
                # Neither needs days - extend left to minimize switches
                left_share = seg_len
            
            # Assign days: left city gets first left_share days, right gets rest
            for i, day in enumerate(seg_days):
                if day in unfixed_days:
                    if i < left_share:
                        assign_day(day, left_city)
                    else:
                        assign_day(day, right_city)
                    unfixed_days.discard(day)
        elif left_city:
            # Only left city defined (segment at end of trip) - extend from left
            # This is the last segment, so ALWAYS extend the left city to minimize switches
            # Adding a switch at the end of the trip is expensive and should be avoided
            for day in seg_days:
                if day in unfixed_days:
                    assign_day(day, left_city)
                    unfixed_days.discard(day)
        elif right_city:
            # Only right city defined (segment at start of trip) - extend from right
            # Fill backwards so right_city expands toward the start
            for day in reversed(seg_days):
                if day in unfixed_days:
                    if needs_more_days(right_city) > 0:
                        assign_day(day, right_city)
                    else:
                        # Find another city that needs days
                        for city in city_order:
                            if needs_more_days(city) > 0:
                                assign_day(day, city)
                                break
                        else:
                            assign_day(day, right_city)
                    unfixed_days.discard(day)
    
    # Handle any remaining unfixed days (shouldn't happen normally)
    if unfixed_days:
        for day in sorted(unfixed_days):
            # Find city that needs most days
            best_city = None
            best_need = -1
            for city in city_order:
                need = needs_more_days(city)
                if need > best_need:
                    best_need = need
                    best_city = city
            
            if best_city is None:
                best_city = city_order[0] if city_order else list(target_days_per_city.keys())[0]
            
            assign_day(day, best_city)
    
    return day_to_city


def _smooth_contiguity(
    day_to_city: Dict[int, str],
    fixed: Dict[int, FixedDayAssignment],
    total_days: int,
    target_days_per_city: Dict[str, int],
) -> Dict[int, str]:
    """
    Apply contiguity smoothing to reduce unnecessary city switches.
    
    Only moves optional (non-fixed) days to create larger contiguous blocks.
    Respects target day counts per city.
    
    Args:
        day_to_city: Current day-to-city mapping
        fixed: Fixed day assignments that cannot be moved
        total_days: Total number of days
        target_days_per_city: Target days per city
        
    Returns:
        Smoothed day-to-city mapping
    """
    result = day_to_city.copy()
    
    def count_switches() -> int:
        switches = 0
        prev_city = None
        for day in range(1, total_days + 1):
            city = result.get(day)
            if prev_city and city != prev_city:
                switches += 1
            prev_city = city
        return switches
    
    def count_per_city() -> Dict[str, int]:
        counts: Dict[str, int] = {}
        for city in result.values():
            counts[city] = counts.get(city, 0) + 1
        return counts
    
    # Try to reduce switches by moving non-fixed days
    max_iterations = total_days * 2
    for _ in range(max_iterations):
        initial_switches = count_switches()
        if initial_switches <= 1:
            break
        
        improved = False
        for day in range(2, total_days):
            if day in fixed:
                continue
            
            current_city = result[day]
            prev_city = result.get(day - 1)
            next_city = result.get(day + 1)
            
            # If this day creates a switch, try to smooth it
            if prev_city and next_city and prev_city == next_city and current_city != prev_city:
                # Check if we can move this day to match neighbors
                # Only if it doesn't violate target counts too much
                counts = count_per_city()
                
                # Would this leave current_city with too few days?
                if counts.get(current_city, 0) <= 1:
                    continue
                
                result[day] = prev_city
                new_switches = count_switches()
                
                if new_switches < initial_switches:
                    improved = True
                else:
                    result[day] = current_city
        
        if not improved:
            break
    
    return result


def _extract_city_order(day_to_city: Dict[int, str], total_days: int) -> List[str]:
    """Extract the order of cities as they appear in the trip."""
    order: List[str] = []
    prev_city = None
    
    for day in range(1, total_days + 1):
        city = day_to_city.get(day)
        if city and city != prev_city:
            order.append(city)
            prev_city = city
    
    return order


def _find_city_switches(day_to_city: Dict[int, str], total_days: int) -> List[int]:
    """Find day indices where city changes (for travel planning)."""
    switches: List[int] = []
    prev_city = None
    
    for day in range(1, total_days + 1):
        city = day_to_city.get(day)
        if prev_city and city != prev_city:
            switches.append(day)
        prev_city = city
    
    return switches


def allocate_days_to_cities(
    cities: Dict[str, Dict[str, Any]],
    total_days: int,
    mandatory: Optional[Dict[str, Dict]] = None,
    user_input: Optional[Dict[str, Any]] = None,
    poi_city_lookup: Optional[Dict[str, str]] = None,
    trip_start: Optional[date] = None,
    request_id: Optional[str] = None,
) -> CityDayAllocation:
    """
    Allocate days to cities with intelligent contiguous block allocation.
    
    This is the main entry point for the city day allocator.
    
    Args:
        cities: Dict of city_name → maut_suboutput with places
        total_days: Total number of days in the trip
        mandatory: Mandatory POI constraints {poi_id: {day, window, time_type, poi_destination}}
        user_input: User preferences including day_assignments, days_per_city, city_order
        poi_city_lookup: Mapping of poi_id → city name
        trip_start: Trip start date for date-to-day conversion
        request_id: Optional request ID for logging
        
    Returns:
        CityDayAllocation with day-to-city mapping and metadata
    """
    if not cities or total_days <= 0:
        return CityDayAllocation(
            day_to_city={},
            city_to_days={},
            city_order=[],
            fixed_days={},
            city_switches=[],
        )
    
    # Build POI city lookup if not provided
    if poi_city_lookup is None:
        poi_city_lookup = {}
        for city, city_data in cities.items():
            for poi in city_data.get("places", []):
                poi_id = poi.get("id", "")
                base_id = poi_id.rsplit("_day", 1)[0] if "_day" in poi_id else poi_id
                poi_city_lookup[base_id] = city
    
    # Extract fixed day assignments
    fixed = extract_fixed_assignments(
        total_days=total_days,
        mandatory=mandatory,
        user_input=user_input,
        poi_city_lookup=poi_city_lookup,
        trip_start=trip_start,
    )
    
    # Determine city order FIRST (before computing target days)
    city_order: List[str] = []
    if user_input and user_input.get("city_order"):
        city_order = [c for c in user_input["city_order"] if c in cities]
        # Add any missing cities
        for city in cities:
            if city not in city_order:
                city_order.append(city)
    elif fixed:
        # Use order from fixed assignments, then alphabetical
        fixed_order = []
        for day in sorted(fixed.keys()):
            city = fixed[day].city
            if city not in fixed_order:
                fixed_order.append(city)
        city_order = fixed_order + [c for c in sorted(cities.keys()) if c not in fixed_order]
    else:
        # No fixed days, no user order - use alphabetical
        city_order = sorted(cities.keys())
    
    # Count fixed days per city
    fixed_days_per_city: Dict[str, int] = {}
    for assignment in fixed.values():
        fixed_days_per_city[assignment.city] = fixed_days_per_city.get(assignment.city, 0) + 1
    
    # Compute target days per city
    if user_input and user_input.get("days_per_city"):
        target_days = {}
        dpc = user_input["days_per_city"]
        
        for city in cities:
            days = None
            # Exact match
            if city in dpc:
                days = dpc[city]
            else:
                # Approximate matching
                city_lower = city.lower()
                for k, v in dpc.items():
                    k_lower = str(k).lower()
                    if k_lower in city_lower or city_lower in k_lower:
                        days = v
                        break
            
            if days is not None:
                target_days[city] = max(1, int(days))
            else:
                # Will be filled proportionally
                target_days[city] = fixed_days_per_city.get(city, 0)
        
        # Adjust to match total_days
        current_total = sum(target_days.values())
        if current_total != total_days:
            diff = total_days - current_total
            # Distribute difference to cities proportionally
            if diff > 0:
                for city in city_order:
                    if diff <= 0:
                        break
                    if city in target_days:
                        target_days[city] += 1
                        diff -= 1
            elif diff < 0:
                for city in sorted(city_order, key=lambda c: target_days.get(c, 0), reverse=True):
                    if diff >= 0:
                        break
                    if target_days.get(city, 0) > fixed_days_per_city.get(city, 0):
                        target_days[city] -= 1
                        diff += 1
    else:
        # Use weighted proportional allocation
        poi_counts = _count_pois_per_city(cities, mandatory, poi_city_lookup)
        target_days = _compute_weighted_days(
            poi_counts=poi_counts,
            total_days=total_days,
            fixed_days_per_city=fixed_days_per_city,
            city_order=city_order,
        )
    
    # Build contiguous blocks
    day_to_city = _build_contiguous_blocks(
        total_days=total_days,
        fixed=fixed,
        target_days_per_city=target_days,
        city_order=city_order,
    )
    
    # Apply contiguity smoothing
    day_to_city = _smooth_contiguity(day_to_city, fixed, total_days, target_days)
    
    # Build city_to_days mapping
    city_to_days: Dict[str, List[int]] = {}
    for day, city in day_to_city.items():
        if city not in city_to_days:
            city_to_days[city] = []
        city_to_days[city].append(day)
    
    # Sort days within each city
    for city in city_to_days:
        city_to_days[city].sort()
    
    # Extract final city order and switches
    final_city_order = _extract_city_order(day_to_city, total_days)
    city_switches = _find_city_switches(day_to_city, total_days)
    
    logger.info(
        f"City day allocation: total_days={total_days}, "
        f"cities={list(cities.keys())}, "
        f"fixed_days={list(fixed.keys())}, "
        f"city_order={final_city_order}, "
        f"switches={city_switches}"
    )
    
    return CityDayAllocation(
        day_to_city=day_to_city,
        city_to_days=city_to_days,
        city_order=final_city_order,
        fixed_days=fixed,
        city_switches=city_switches,
    )
