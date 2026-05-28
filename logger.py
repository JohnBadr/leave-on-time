import passiogo
import sqlite3
import time
from datetime import datetime, date


def get_period_type():
    today = date.today()

    # --- HOLIDAYS ---
    holidays = {
        date(2026, 5, 25),   # Memorial Day
        date(2026, 6, 19),   # Juneteenth
        date(2026, 7, 3),    # Independence Day observed
        date(2026, 9, 7),    # Labor Day
        date(2026, 11, 11),  # Veterans Day
        date(2026, 11, 26),  # Thanksgiving
        date(2026, 11, 27),  # Thanksgiving
        date(2026, 12, 25),  # Christmas
        date(2027, 1, 1),    # New Year's Day
        date(2027, 1, 18),   # MLK Day
        date(2027, 5, 31),   # Memorial Day
        date(2027, 6, 19),   # Juneteenth
        date(2027, 7, 5),    # Independence Day observed
        date(2027, 9, 6),    # Labor Day
        date(2027, 11, 11),  # Veterans Day
        date(2027, 11, 25),  # Thanksgiving
        date(2027, 11, 26),  # Thanksgiving
        date(2027, 12, 25),  # Christmas
        date(2028, 1, 1),    # New Year's Day
        date(2028, 1, 17),   # MLK Day
    }
    if today in holidays:
        return "holiday"

    # --- SUMMER 2026 ---
    if date(2026, 5, 18) <= today <= date(2026, 8, 7):
        return "summer"

    # --- FALL 2026 ---
    if date(2026, 8, 24) <= today <= date(2026, 8, 28):
        return "first_week"
    if date(2026, 12, 5) <= today <= date(2026, 12, 10):
        return "finals"
    if date(2026, 8, 24) <= today <= date(2026, 12, 4):
        return "regular"

    # --- WINTER BREAK 2026-2027 ---
    if date(2026, 12, 11) <= today <= date(2027, 1, 10):
        return "break"

    # --- SPRING 2027 ---
    if date(2027, 1, 11) <= today <= date(2027, 1, 17):
        return "first_week"
    if date(2027, 3, 15) <= today <= date(2027, 3, 21):
        return "break"  # Spring break
    if date(2027, 4, 24) <= today <= date(2027, 4, 30):
        return "finals_week_prep"
    if date(2027, 5, 1) <= today <= date(2027, 5, 6):
        return "finals"
    if date(2027, 1, 11) <= today <= date(2027, 4, 30):
        return "regular"

    # --- SUMMER 2027 ---
    if date(2027, 5, 17) <= today <= date(2027, 8, 6):
        return "summer"

    # --- FALL 2027 (estimated) ---
    if date(2027, 8, 23) <= today <= date(2027, 8, 27):
        return "first_week"
    if date(2027, 12, 4) <= today <= date(2027, 12, 10):
        return "finals"
    if date(2027, 8, 23) <= today <= date(2027, 12, 3):
        return "regular"

    # --- WINTER BREAK 2027-2028 ---
    if date(2027, 12, 11) <= today <= date(2028, 1, 9):
        return "break"

    # --- SPRING 2028 (estimated) ---
    if date(2028, 1, 10) <= today <= date(2028, 1, 16):
        return "first_week"
    if date(2028, 3, 10) <= today <= date(2028, 3, 16):
        return "break"
    if date(2028, 4, 28) <= today <= date(2028, 5, 3):
        return "finals"
    if date(2028, 1, 10) <= today <= date(2028, 4, 27):
        return "regular"

    # --- SUMMER 2028 (estimated) ---
    if date(2028, 5, 15) <= today <= date(2028, 8, 7):
        return "summer"

    return "break"


# Setup database
conn = sqlite3.connect("bullrunner.db")
cursor = conn.cursor()

cursor.execute('''
    CREATE TABLE IF NOT EXISTS vehicle_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp TEXT,
        vehicle_id TEXT,
        vehicle_name TEXT,
        route_name TEXT,
        route_id TEXT,
        latitude TEXT,
        longitude TEXT,
        calculated_course TEXT,
        out_of_service INTEGER,
        period_type TEXT
    )
''')
conn.commit()

system = passiogo.getSystemFromID(2343)

print("Logger started. Logging every 60 seconds...")

while True:
    try:
        vehicles = system.getVehicles()
        timestamp = datetime.now().isoformat()

        if not vehicles:
            print(f"{timestamp} - No vehicles running")
        else:
            for v in vehicles:
                cursor.execute('''
                    INSERT INTO vehicle_logs 
                    (timestamp, vehicle_id, vehicle_name, route_name, route_id, latitude, longitude, calculated_course, out_of_service, period_type)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (
                    timestamp,
                    v.id,
                    v.name,
                    v.routeName,
                    v.routeId,
                    v.latitude,
                    v.longitude,
                    v.calculatedCourse,
                    v.outOfService,
                    get_period_type()
                ))
            conn.commit()
            print(f"{timestamp} - Logged {len(vehicles)} vehicles")

    except Exception as e:
        print(f"Error: {e}")

    time.sleep(60)