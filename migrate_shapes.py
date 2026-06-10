# migrate_shapes.py — one-time migration to add GeoJSON shape points to stop_pairs
# adds the road geometry between each stop pair so progress can be calculated offline
# run once while your GCP OSRM VM is running, then shut it down
# safe to rerun — skips pairs that already have shape_points

import sqlite3
import json
import time
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed

# ── CONFIG ────────────────────────────────────────────────────────────────────

OSRM_SERVER = "http://34.24.229.249:5000"  # update to your GCP VM IP
DB_PATH     = "routegraph.db"
MAX_WORKERS = 20  # same as precompute.py

session = requests.Session()

# ── DB SETUP ──────────────────────────────────────────────────────────────────

conn   = sqlite3.connect(DB_PATH, timeout=30.0)
cursor = conn.cursor()

# add shape_points column if it doesn't exist yet
try:
    cursor.execute("ALTER TABLE stop_pairs ADD COLUMN shape_points TEXT")
    print("Added column: shape_points")
except Exception:
    print("Column shape_points already exists, skipping")

conn.commit()

# ── FETCH PAIRS THAT NEED SHAPES ──────────────────────────────────────────────

cursor.execute("""
    SELECT sp.id,
           s1.lon, s1.lat,
           s2.lon, s2.lat
    FROM stop_pairs sp
    JOIN route_graphs rg ON sp.route_graph_id = rg.id
    JOIN stops s1 ON sp.origin_stop_id = s1.stop_id AND s1.system_id = rg.system_id
    JOIN stops s2 ON sp.dest_stop_id   = s2.stop_id AND s2.system_id = rg.system_id
    WHERE sp.shape_points IS NULL
      AND sp.road_distance_m IS NOT NULL
""")
pairs = cursor.fetchall()
print(f"Found {len(pairs)} stop pairs to fetch shapes for")

# ── OSRM FETCH ────────────────────────────────────────────────────────────────

def fetch_shape(pair):
    pair_id, lon1, lat1, lon2, lat2 = pair

    # sanity check coords
    if not all([lon1, lat1, lon2, lat2]):
        return (pair_id, None)
    if abs(float(lat1)) < 1.0 or abs(float(lon1)) < 1.0:
        return (pair_id, None)

    try:
        url = (
            f"{OSRM_SERVER}/route/v1/driving/"
            f"{lon1},{lat1};{lon2},{lat2}"
            f"?geometries=geojson&overview=full"
        )
        response = session.get(url, timeout=5)
        data = response.json()

        if "routes" not in data or not data["routes"]:
            return (pair_id, None)

        # GeoJSON coordinates — list of [lon, lat] pairs
        coords = data["routes"][0]["geometry"]["coordinates"]
        return (pair_id, json.dumps(coords))

    except Exception as e:
        print(f"  OSRM error for pair {pair_id}: {e}")
        return (pair_id, None)

# ── RUN IN PARALLEL ───────────────────────────────────────────────────────────

print(f"Firing {len(pairs)} OSRM requests in parallel (workers={MAX_WORKERS})...")
start = time.time()

results = []
with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
    futures = {executor.submit(fetch_shape, pair): pair for pair in pairs}
    for future in as_completed(futures):
        try:
            results.append(future.result())
        except Exception as e:
            print(f"  Thread error: {e}")

# ── WRITE TO DB ───────────────────────────────────────────────────────────────

updated = 0
skipped = 0

for pair_id, shape_json in results:
    if shape_json is None:
        skipped += 1
        continue
    cursor.execute("""
        UPDATE stop_pairs
        SET shape_points = ?
        WHERE id = ?
    """, (shape_json, pair_id))
    updated += 1

conn.commit()
conn.close()

elapsed = time.time() - start
print(f"\nDone in {elapsed:.1f}s — Updated: {updated}, Skipped: {skipped}")
print("You can now safely shut down your GCP VM.")