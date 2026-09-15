"""Geodesic distance calculation utilities."""

from __future__ import annotations

import math

EARTH_RADIUS_METERS: float = 6_371_000.0


def haversine_distance_meters(
    lat1: float,
    lon1: float,
    lat2: float,
    lon2: float,
) -> float:
    """Calculate the great-circle distance between two GPS points using the Haversine formula.

    Args:
        lat1: Latitude of point 1 in degrees (-90.0 to 90.0).
        lon1: Longitude of point 1 in degrees (-180.0 to 180.0).
        lat2: Latitude of point 2 in degrees (-90.0 to 90.0).
        lon2: Longitude of point 2 in degrees (-180.0 to 180.0).

    Returns:
        Distance in meters as a float.
    """
    if not (-90.0 <= lat1 <= 90.0 and -90.0 <= lat2 <= 90.0):
        raise ValueError("Latitude must be between -90.0 and 90.0 degrees.")
    if not (-180.0 <= lon1 <= 180.0 and -180.0 <= lon2 <= 180.0):
        raise ValueError("Longitude must be between -180.0 and 180.0 degrees.")

    lat1_r, lat2_r = math.radians(lat1), math.radians(lat2)
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)

    a = math.sin(dlat / 2.0) ** 2 + math.cos(lat1_r) * math.cos(lat2_r) * math.sin(dlon / 2.0) ** 2
    a = min(1.0, max(0.0, a))
    c = 2.0 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))

    return round(EARTH_RADIUS_METERS * c, 2)
