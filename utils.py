import math
from geopy.distance import geodesic

def get_distance_m(lat1, lon1, lat2, lon2):
    return geodesic((lat1, lon1), (lat2, lon2)).meters

def angle_diff(a, b):
    diff = abs(a - b) % 360
    return diff if diff <= 180 else 360 - diff

def project_onto_shape(lat, lon, shape_points):
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
    progress_pct = (best_segment_idx + best_t) / (len(shape_points) - 1)

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