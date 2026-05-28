import passiogo
import sqlite3
import requests
import time
from datetime import datetime

# Local OSRM server (running via Docker)
OSRM_LOCAL = "http://localhost:5000"

# DB setup
conn = sqlite3.connect("routegraph.db")
cursor = conn.cursor()

cursor.execute('''
    CREATE TABLE IF NOT EXISTS systems (
        system_id INTEGER PRIMARY KEY,
        name TEXT,
        precomputed INTEGER DEFAULT 0,
        last_updated TEXT
    )
''')

cursor.execute('''
    CREATE TABLE IF NOT EXISTS route_graphs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        system_id INTEGER,
        route_id TEXT,
        route_name TEXT,
        color TEXT
    )
''')

cursor.execute('''
    CREATE TABLE IF NOT EXISTS stops (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        system_id INTEGER,
        stop_id TEXT,
        name TEXT,
        lat REAL,
        lon REAL
    )
''')

cursor.execute('''
    CREATE TABLE IF NOT EXISTS stop_pairs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        route_graph_id INTEGER,
        origin_stop_id TEXT,
        dest_stop_id TEXT,
        road_distance_m REAL,
        road_duration_s REAL,
        UNIQUE(route_graph_id, origin_stop_id, dest_stop_id)
    )
''')

conn.commit()
print("Database tables created.")

#gets all systems
def fetch_and_store_systems():
    systems = passiogo.getSystems()
    for system in systems:
        cursor.execute('''
            INSERT OR IGNORE INTO systems(system_id, name)
            VALUES(?,?)
        ''', (system.id, system.name))
    conn.commit()

#gets all routes per system
def fetch_and_store_routes(system_id):
    system=passiogo.getSystemFromID(system_id)
    routes=system.getRoutes()
    for route in routes:
        cursor.execute('''
            INSERT OR IGNORE INTO route_graphs(system_id, route_id, route_name, color)
            VALUES(?, ?, ?, ?)
        ''', (system.id, route.myid, route.name, route.groupColor))
    conn.commit()

#gets all stops per system (will be filtered later so that each route has its stops assigned to it)
def fetch_and_store_stops(system_id):
    system=passiogo.getSystemFromID(system_id)
    stops=system.getStops()
    for stop in stops:
        cursor.execute('''
            INSERT OR IGNORE INTO stops(system_id, stop_id, name, lat, lon)
            VALUES(?, ?, ?, ?, ?)
        ''', (system.id, stop.id, stop.name, stop.latitude, stop.longitude))
    conn.commit()

#gets the road distance between 2 points using OSRM (free unlike google maps API)
def get_road_distance(lon1, lat1, lon2, lat2):
    try:
        response = requests.get(f"http://localhost:5000/route/v1/driving/{lon1},{lat1};{lon2},{lat2}?overview=false")
        data = response.json()
        distance = data['routes'][0]['distance']
        duration = data['routes'][0]['duration']
        return (distance, duration)
    except Exception as e:
        print(f"OSRM error between ({lat1},{lon1}) and ({lat2},{lon2}): {e}")
        return (None, None)

def compute_stop_pairs(system_id):
    system=passiogo.getSystemFromID(system_id)
    routes=system.getRoutes()
    stops=system.getStops()
    for route in routes:  # Get stops for this route in correct order
        route_stops=[]
        for stop in stops:
            if route.myid in stop.routesAndPositions:
                position = stop.routesAndPositions[route.myid][0]
                route_stops.append((position,stop))
        
        #sorts by position
        route_stops.sort(key=lambda x: x[0]) 

        #extract stops in order
        ordered_stops = []
        for position, stop in route_stops:
            ordered_stops.append(stop)

        cursor.execute('''
            SELECT id FROM route_graphs 
            WHERE system_id = ? AND route_id = ?
        ''', (system_id, route.myid))
        row = cursor.fetchone()
        if row is None:
            continue
        route_graph_id = row[0]

        for i in range(len(ordered_stops) - 1):
            stop1 = ordered_stops[i]
            stop2 = ordered_stops[i + 1]

            distance, duration=get_road_distance(stop1.longitude, stop1.latitude, stop2.longitude, stop2.latitude)
            if distance is None:
                continue

            cursor.execute('''
                INSERT OR IGNORE INTO stop_pairs (route_graph_id, origin_stop_id, dest_stop_id, road_distance_m, road_duration_s)
                VALUES (?, ?, ?, ?, ?)
            ''', (route_graph_id, stop1.id, stop2.id, distance, duration))
        conn.commit()

def precompute_system(system_id):
    fetch_and_store_routes(system_id)
    fetch_and_store_stops(system_id)
    compute_stop_pairs(system_id)
    cursor.execute('''
        UPDATE systems
        SET precomputed = 1, last_updated = ?
        WHERE system_id = ?''', (datetime.now().isoformat(), system_id))

    conn.commit()

def run():
    fetch_and_store_systems()
    cursor.execute('''
        SELECT system_id, name FROM systems
        WHERE precomputed = 0
    ''')
    rows = cursor.fetchall()
    for row in rows:
        system_id=row[0]
        name=row[1]
        print(f"Precomputing {name} (system {system_id})...")
        precompute_system(system_id)
        print(f"Done: {name}")

# run()

precompute_system(2343)
print("USF done")