#this vers wont include walking times yet.
import sqlite3
from passiogo_fix import passiogo

conn = sqlite3.connect("routegraph.db")
cursor = conn.cursor()

USF_SYSTEM_ID = 4502

#everything will be changed from input to dropdown/search and select
system_input = input("Enter your system ID: ") or USF_SYSTEM_ID
system = passiogo.getSystemFromID(system_input)

cursor.execute("""
    SELECT id, route_name
    FROM route_graphs
    WHERE system_id = ?
    ORDER BY id
""", (system_input,))

route_options=cursor.fetchall()
for r in range(len(route_options)):
    print(f'{r+1}. {route_options[r]}')

route_id = input(("Enter one of the IDs above to select your route: ")) or route_options[0]
all_vehicles = system.getVehicles()

route_vehicles=[]

