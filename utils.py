import math
from functools import lru_cache

def get_distance_m(lat1, lon1, lat2, lon2):
    a, b = math.radians(lat1), math.radians(lat2)
    dlat, dlon = b - a, math.radians(lon2 - lon1)
    h = math.sin(dlat / 2) ** 2 + math.cos(a) * math.cos(b) * math.sin(dlon / 2) ** 2
    return 6371000.0 * 2 * math.asin(min(1.0, math.sqrt(h)))

def angle_diff(a, b):
    diff = abs(a - b) % 360
    return diff if diff <= 180 else 360 - diff

@lru_cache(maxsize=4096)
def _shape_lengths(points):
    cumulative = [0.0]
    for (lon1, lat1), (lon2, lat2) in zip(points, points[1:]):
        cumulative.append(cumulative[-1] + get_distance_m(lat1, lon1, lat2, lon2))
    return cumulative


def project_onto_shape(lat, lon, shape_points):
    shape_points = tuple(tuple(point) for point in shape_points)
    lengths = _shape_lengths(shape_points)
    if len(shape_points) < 2:
        error = get_distance_m(lat, lon, shape_points[0][1], shape_points[0][0]) if shape_points else float('inf')
        return error, 0.0, 0.0
    if lengths[-1] == 0:
        return get_distance_m(lat, lon, shape_points[0][1], shape_points[0][0]), 0.0, 0.0
    
    best_error_m=float('inf')
    best_segment_idx=0
    best_t=0
    for i in range(len(shape_points)-1):
        A=shape_points[i]
        B=shape_points[i+1]
        lon_A, lat_A = A
        lon_B, lat_B = B
        scale = math.cos(math.radians((lat_A + lat_B) / 2))
        dx = (lon_B - lon_A) * scale
        dy = lat_B - lat_A
        vx = (lon - lon_A) * scale
        vy=(lat - lat_A)

        denom = dx*dx + dy*dy
        if denom == 0:
            continue

        t = (vx*dx + vy*dy) / denom
        t = max(0.0, min(1.0, t))
        closest_lat = lat_A + t * (lat_B - lat_A)
        closest_lon = lon_A + t * (lon_B - lon_A)

        error_m = get_distance_m(lat, lon, closest_lat, closest_lon)

        if error_m < best_error_m:
            best_error_m = error_m
            best_segment_idx = i
            best_t = t
    progress_pct = (lengths[best_segment_idx] + best_t * (lengths[best_segment_idx + 1] - lengths[best_segment_idx])) / lengths[-1]

    lon_A, lat_A = shape_points[best_segment_idx]
    lon_B, lat_B = shape_points[best_segment_idx + 1]
    local_bearing = compute_bearing(lat_A, lon_A, lat_B, lon_B)

    return best_error_m, progress_pct, local_bearing
    

def compute_bearing(lat1, lon1, lat2, lon2):
    lat1, lon1, lat2, lon2 = map(math.radians, [lat1, lon1, lat2, lon2])
    dlon = lon2 - lon1
    x = math.sin(dlon) * math.cos(lat2)
    y = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dlon)
    return (math.degrees(math.atan2(x, y)) + 360) % 360

# 1. find t          → WHERE on the segment is the closest point
# 2. find the point  → WHAT are its coordinates (using t)
# 3. measure error   → HOW FAR is the bus from that point
