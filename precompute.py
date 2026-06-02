from datetime import datetime
import sqlite3
import time
from passiogo_fix import passiogo
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed  # CLAUDE: ThreadPoolExecutor runs multiple OSRM requests at the same time instead of one by one; as_completed lets us collect results as they finish

# OSRM server (running via GCP)
# Update this to your GCP IP once your VM is running
OSRM_SERVER = "http://146.148.70.30:5000"

# Set up a persistent session for connection pooling (vital for cloud speed)
session = requests.Session()

# DB setup with expanded timeout to prevent lock crashes
conn = sqlite3.connect("routegraph.db", timeout=30.0)
cursor = conn.cursor()

cursor.execute("""
    CREATE TABLE IF NOT EXISTS systems (
        system_id INTEGER PRIMARY KEY,
        name TEXT,
        precomputed INTEGER DEFAULT 0,
        last_updated TEXT
    )
""")

cursor.execute("""
    CREATE TABLE IF NOT EXISTS route_graphs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        system_id INTEGER,
        route_name TEXT,
        color TEXT,
        UNIQUE(system_id, route_name)
    )
""")
# CLAUDE: route_graphs now deduplicates by (system_id, route_name) instead of storing every PassioGo
# route variant separately. PassioGo has multiple myid variants per named route (e.g. 4 "Green" IDs)
# which caused every stop pair to be inserted 4x. Now we store one row per unique route name.

cursor.execute("""
    CREATE TABLE IF NOT EXISTS stops (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        system_id INTEGER,
        stop_id TEXT,
        name TEXT,
        lat REAL,
        lon REAL
    )
""")

cursor.execute("""
    CREATE TABLE IF NOT EXISTS stop_pairs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        route_graph_id INTEGER,
        position INTEGER,
        origin_stop_id TEXT,
        dest_stop_id TEXT,
        road_distance_m REAL,
        road_duration_s REAL,
        UNIQUE(route_graph_id, origin_stop_id, dest_stop_id)
    )
""")
# CLAUDE: added a position column to stop_pairs so we can ORDER BY position when querying,
# giving correct stop sequence regardless of insert order (threading inserts in random order)

conn.commit()
print("Database tables created.")


# gets all systems
def fetch_and_store_systems():
    try:
        systems = passiogo.getSystems()
        for system in systems:
            cursor.execute(
                "INSERT OR IGNORE INTO systems(system_id, name) VALUES(?, ?)",
                (system.id, system.name),
            )
        conn.commit()
    except Exception as e:
        print(f"Error fetching systems master list: {e}")


# gets all routes per system; deduplicates by name so we only store one row per unique route name;
# returns the list so precompute_system can reuse it without re-fetching
def fetch_and_store_routes(system):
    routes = system.getRoutes()

    # CLAUDE: PassioGo returns multiple route objects with the same name but different myid values
    # — the reason for this is unclear, possibly direction or schedule differences internal to PassioGo.
    # We only want one DB row per name, so we pick one representative per unique name — the one with
    # the most stops wins since it's the most complete variant and best represents the full route path.
    stops = system.getStops()
    name_to_best_route = {}
    for route in routes:
        route_key = str(route.myid)
        stop_count = sum(
            1 for stop in stops
            if route_key in {str(k) for k in getattr(stop, "routesAndPositions", {}).keys()}
        )
        existing = name_to_best_route.get(route.name)                           #note: they will be stores like this in the dict basically (str name): tuple(route_obj, stop_count):
        if existing is None or stop_count > existing[1]:                        # { "Green": (route_obj, 28), "Blue": (route_obj, 15), "Red": (route_obj, 30) }
            name_to_best_route[route.name] = (route, stop_count)

    best_routes = [route for route, _ in name_to_best_route.values()] #keeps only the route objects, discarding the stop counts since we don't need them anymore

    for route in best_routes:
        cursor.execute(
            "INSERT OR IGNORE INTO route_graphs(system_id, route_name, color) VALUES(?, ?, ?)",
            (system.id, route.name, route.groupColor),
        )
    conn.commit()
    return best_routes  # CLAUDE: returning routes here means compute_stop_pairs can receive them as a parameter instead of calling getRoutes() again


# gets all stops per system; returns the list so compute_stop_pairs can reuse it without re-fetching
def fetch_and_store_stops(system):
    stops = system.getStops()
    for stop in stops:
        cursor.execute(
            "INSERT OR IGNORE INTO stops(system_id, stop_id, name, lat, lon) VALUES(?, ?, ?, ?, ?)",
            (system.id, stop.id, stop.name, stop.latitude, stop.longitude),
        )
    conn.commit()
    return stops  # CLAUDE: same idea as fetch_and_store_routes — return once, reuse everywhere


# gets the road distance between 2 points using OSRM (free unlike google maps API)
def get_road_distance(lon1, lat1, lon2, lat2):
    # Sanity check to instantly drop missing coordinates or placeholder (0.0, 0.0) data
    if not lon1 or not lat1 or abs(float(lat1)) < 1.0 or abs(float(lon1)) < 1.0:
        return (None, None)
    try:
        response = session.get(
            f"{OSRM_SERVER}/route/v1/driving/{lon1},{lat1};{lon2},{lat2}?overview=false",
            timeout=5,
        )
        data = response.json()
        # Check if route list exists and isn't empty before accessing indexes
        if "routes" in data and len(data["routes"]) > 0:
            return (data["routes"][0]["distance"], data["routes"][0]["duration"])
        return (None, None)
    except Exception as e:
        print(f"OSRM error between ({lat1},{lon1}) and ({lat2},{lon2}): {e}")
        return (None, None)


def compute_stop_pairs(system_id, routes, stops):
    # CLAUDE: instead of firing OSRM requests one at a time inside the loop, we first collect every
    # stop pair that needs a distance lookup into a tasks list, then fire them all at once in parallel
    tasks = []

    for route in routes:
        route_stops = []
        for stop in stops:
            # Type-casting safety: standardizes dictionary keys to strings to prevent data-type mismatches
            routes_str_keys = {str(k): v for k, v in getattr(stop, "routesAndPositions", {}).items()}
            target_key = str(route.myid)
            if target_key in routes_str_keys:
                position = routes_str_keys[target_key][0]
                route_stops.append((position, stop))

        # sorts by position so stop pairs are in correct route order
        route_stops.sort(key=lambda x: x[0])
        ordered_stops = [stop for _, stop in route_stops]

        if len(ordered_stops) < 2:
            continue

        # look up the single deduplicated route_graph row by name
        cursor.execute(
            "SELECT id FROM route_graphs WHERE system_id = ? AND route_name = ?",
            (system_id, route.name),
        )
        row = cursor.fetchone()
        if row is None:
            continue
        route_graph_id = row[0]

        for i in range(len(ordered_stops) - 1):
            tasks.append((route_graph_id, i, ordered_stops[i], ordered_stops[i + 1]))
            # CLAUDE: i is the position index — stored in the DB so we can ORDER BY position later
            # regardless of what order the threaded OSRM responses come back in
        tasks.append((route_graph_id, len(ordered_stops) - 1, ordered_stops[-1], ordered_stops[0]))

    print(f"  Firing {len(tasks)} OSRM requests in parallel...")

    # CLAUDE: this inner function is what each thread will run — it takes one task (a stop pair)
    # and returns the distance/duration result along with the IDs needed to insert into the DB
    def fetch_pair(task):
        route_graph_id, position, stop1, stop2 = task
        distance, duration = get_road_distance(
            stop1.longitude, stop1.latitude, stop2.longitude, stop2.latitude
        )
        return (route_graph_id, position, stop1.id, stop2.id, distance, duration)

    # CLAUDE: ThreadPoolExecutor spins up 20 threads and hands each one a task from the list;
    # as_completed yields each future as it finishes so we don't have to wait for the slowest one
    # before starting to collect results; 20 workers is a safe ceiling that won't hammer the server
    results = []
    with ThreadPoolExecutor(max_workers=20) as executor:
        futures = {executor.submit(fetch_pair, task): task for task in tasks}
        for future in as_completed(futures):
            try:
                results.append(future.result())
            except Exception as e:
                print(f"Thread error: {e}")

    # Write all results to DB in one batch commit instead of committing after every single insert
    for route_graph_id, position, origin_id, dest_id, distance, duration in results:
        if distance is None:
            continue
        cursor.execute(
            """INSERT OR IGNORE INTO stop_pairs
               (route_graph_id, position, origin_stop_id, dest_stop_id, road_distance_m, road_duration_s)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (route_graph_id, position, origin_id, dest_id, distance, duration),
        )
    conn.commit()


def precompute_system(system_id):
    try:
        system = passiogo.getSystemFromID(system_id)  # CLAUDE: fetch the system object once here and pass it into every function below — previously each function called getSystemFromID separately which was 3 redundant API calls
        routes = fetch_and_store_routes(system)
        stops = fetch_and_store_stops(system)
        compute_stop_pairs(system_id, routes, stops)
        cursor.execute(
            "UPDATE systems SET precomputed = 1, last_updated = ? WHERE system_id = ?",
            (datetime.now().isoformat(), system_id),
        )
        conn.commit()
    except Exception as e:
        print(f"Failed to process system {system_id}: {e}")


def run_all_systems():
    fetch_and_store_systems()
    cursor.execute("SELECT system_id, name FROM systems WHERE precomputed = 0")
    rows = cursor.fetchall()

    if not rows:
        print("All systems are already precomputed!")
        return

    print(f"Found {len(rows)} systems left to precompute.")

    for row in rows:
        system_id, name = row
        print(f"Precomputing {name} (system {system_id})...")
        start_time = time.time()
        precompute_system(system_id)
        elapsed = time.time() - start_time
        print(f"Done: {name} (took {elapsed:.2f} seconds)")

    print("\n======================================================================")
    print(" SUCCESS: All North America systems processed and saved to database.")
    print(" REMINDER: You can now safely KILL your GCP VM!")
    print("======================================================================")


if __name__ == "__main__":
    run_all_systems()
    # precompute_system(2343)
    # print("Precomputation complete for system 2343. Check the database for results.")
    conn.close()

#REMINDER: NEED TO ADD LOGIC THAT PRECOMPUTES ROUTES THAT HAVE NOT BEEN PRECOMPUTED AND THEY NEED TO BE USED BY THE USER