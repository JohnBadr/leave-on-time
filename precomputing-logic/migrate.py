# migrate.py — one-time migration to add bearing and is_complex_zone to stop_pairs
# run once, never again
# written fully by CLAUDE

import sqlite3
from math import radians, sin, cos, sqrt, atan2, degrees

conn = sqlite3.connect("routegraph.db", timeout=30.0)
cursor = conn.cursor()

# ── ADD COLUMNS ───────────────────────────────────────────────────────────────

try:
    cursor.execute("ALTER TABLE stop_pairs ADD COLUMN bearing REAL")
    print("Added column: bearing")
except Exception:
    print("Column bearing already exists, skipping")

try:
    cursor.execute("ALTER TABLE stop_pairs ADD COLUMN is_complex_zone INTEGER")
    print("Added column: is_complex_zone")
except Exception:
    print("Column is_complex_zone already exists, skipping")

conn.commit()

# ── MATH ──────────────────────────────────────────────────────────────────────

def haversine(lat1, lon1, lat2, lon2):
    R = 6371000
    lat1, lon1, lat2, lon2 = map(radians, [float(lat1), float(lon1), float(lat2), float(lon2)])
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    a = sin(dlat/2)**2 + cos(lat1) * cos(lat2) * sin(dlon/2)**2
    return R * 2 * atan2(sqrt(a), sqrt(1-a))

def compute_bearing(lat1, lon1, lat2, lon2):
    lat1, lon1, lat2, lon2 = map(radians, [float(lat1), float(lon1), float(lat2), float(lon2)])
    dlon = lon2 - lon1
    x = sin(dlon) * cos(lat2)
    y = cos(lat1) * sin(lat2) - sin(lat1) * cos(lat2) * cos(dlon)
    return (degrees(atan2(x, y)) + 360) % 360

# ── FETCH ALL STOP PAIRS ──────────────────────────────────────────────────────

cursor.execute("""
    SELECT sp.id, sp.origin_stop_id, sp.dest_stop_id, sp.road_distance_m
    FROM stop_pairs sp
    WHERE sp.bearing IS NULL OR sp.is_complex_zone IS NULL
""")
pairs = cursor.fetchall()
print(f"Found {len(pairs)} stop pairs to migrate")

# ── PROCESS ───────────────────────────────────────────────────────────────────

updated = 0
skipped = 0

for pair_id, origin_id, dest_id, road_distance in pairs:

    # fetch origin stop coords
    cursor.execute("""
        SELECT lat, lon FROM stops WHERE stop_id = ?
    """, (origin_id,))
    origin = cursor.fetchone()

    # fetch dest stop coords
    cursor.execute("""
        SELECT lat, lon FROM stops WHERE stop_id = ?
    """, (dest_id,))
    dest = cursor.fetchone()

    if not origin or not dest:
        print(f"  Skipping pair {pair_id} — missing stop coords")
        skipped += 1
        continue

    if not road_distance:
        print(f"  Skipping pair {pair_id} — missing road_distance_m")
        skipped += 1
        continue

    lat1, lon1 = origin
    lat2, lon2 = dest

    straight_line = haversine(lat1, lon1, lat2, lon2)
    
    if straight_line == 0:
        print(f"  Skipping pair {pair_id} — zero straight line distance")
        skipped += 1
        continue

    bearing = compute_bearing(lat1, lon1, lat2, lon2)
    is_complex = 1 if (float(road_distance) / straight_line) > 1.4 else 0

    cursor.execute("""
        UPDATE stop_pairs
        SET bearing = ?, is_complex_zone = ?
        WHERE id = ?
    """, (bearing, is_complex, pair_id))

    updated += 1

conn.commit()

print(f"\nDone. Updated: {updated} pairs, Skipped: {skipped} pairs")
conn.close()