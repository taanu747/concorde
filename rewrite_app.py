import re

with open("tracker/app.py", "r") as f:
    code = f.read()

header = """import os
import json
import csv
import time
import urllib.request
import shutil
import sqlite3
import threading
from flask import Flask, jsonify, render_template, request

# Database Configuration
DATABASE_URL = os.environ.get("DATABASE_URL")
if DATABASE_URL and DATABASE_URL.startswith("postgres"):
    import psycopg2
    from psycopg2.extras import RealDictCursor
    DB_TYPE = "postgres"
else:
    DB_TYPE = "sqlite"

def get_db_connection():
    if DB_TYPE == "postgres":
        return psycopg2.connect(DATABASE_URL)
    else:
        conn = sqlite3.connect(SQLITE_DB_FILE)
        conn.row_factory = sqlite3.Row
        return conn

def execute_query(conn, query, params=(), commit=False):
    if DB_TYPE == "postgres":
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        # Postgres uses %s instead of ?
        query = query.replace("?", "%s")
    else:
        cursor = conn.cursor()
        
    cursor.execute(query, params)
    if commit:
        conn.commit()
    
    if query.strip().upper().startswith("SELECT") or query.strip().upper().startswith("WITH"):
        return [dict(row) for row in cursor.fetchall()]
    return None
"""
# 1. Imports
code = re.sub(r'import os\n.*from flask import Flask, jsonify, render_template, request\n', header, code, flags=re.DOTALL)

# 2. init_db
old_init_db = """def init_db():
    with sqlite3.connect(SQLITE_DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS aircraft_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                hex TEXT,
                callsign TEXT,
                lat REAL,
                lon REAL,
                altitude REAL,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                heading REAL
            )
        ''')
        try:
            cursor.execute('ALTER TABLE aircraft_history ADD COLUMN heading REAL')
        except sqlite3.OperationalError:
            pass
        # Create indexes for faster search
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_hex ON aircraft_history(hex)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_callsign ON aircraft_history(callsign)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_timestamp ON aircraft_history(timestamp)')
        conn.commit()"""

new_init_db = """def init_db():
    with get_db_connection() as conn:
        cursor = conn.cursor()
        if DB_TYPE == "postgres":
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS aircraft_history (
                    id SERIAL PRIMARY KEY,
                    hex TEXT,
                    callsign TEXT,
                    lat REAL,
                    lon REAL,
                    altitude REAL,
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    heading REAL
                )
            ''')
        else:
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS aircraft_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    hex TEXT,
                    callsign TEXT,
                    lat REAL,
                    lon REAL,
                    altitude REAL,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                    heading REAL
                )
            ''')
        try:
            cursor.execute('ALTER TABLE aircraft_history ADD COLUMN heading REAL')
        except Exception:
            pass
        # Create indexes for faster search
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_hex ON aircraft_history(hex)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_callsign ON aircraft_history(callsign)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_timestamp ON aircraft_history(timestamp)')
        conn.commit()"""

code = code.replace(old_init_db, new_init_db)

# 3. update_aircraft_data
old_update = """                with sqlite3.connect(SQLITE_DB_FILE) as conn:
                    cursor = conn.cursor()
                    for plane in latest_aircraft_data['aircraft']:
                        seen = plane.get('seen', 0)
                        if seen < 15:
                            hex_code = plane.get('hex', '').lower()
                            callsign = plane.get('flight', '').strip()
                            lat = plane.get('lat')
                            lon = plane.get('lon')
                            altitude = plane.get('alt_baro') or plane.get('alt_geom')
                            heading = plane.get('track')
                            
                            if lat is not None and lon is not None:
                                cursor.execute('''
                                    INSERT INTO aircraft_history (hex, callsign, lat, lon, altitude, heading)
                                    VALUES (?, ?, ?, ?, ?, ?)
                                ''', (hex_code, callsign, lat, lon, altitude, heading))
                    
                    # Cleanup old data (> 7 days)
                    cursor.execute("DELETE FROM aircraft_history WHERE timestamp <= datetime('now', '-7 days')")
                    conn.commit()"""
new_update = """                with get_db_connection() as conn:
                    cursor = conn.cursor()
                    query = '''
                        INSERT INTO aircraft_history (hex, callsign, lat, lon, altitude, heading)
                        VALUES (?, ?, ?, ?, ?, ?)
                    '''
                    if DB_TYPE == "postgres": query = query.replace("?", "%s")

                    for plane in latest_aircraft_data['aircraft']:
                        seen = plane.get('seen', 0)
                        if seen < 15:
                            hex_code = plane.get('hex', '').lower()
                            callsign = plane.get('flight', '').strip()
                            lat = plane.get('lat')
                            lon = plane.get('lon')
                            altitude = plane.get('alt_baro') or plane.get('alt_geom')
                            heading = plane.get('track')
                            
                            if lat is not None and lon is not None:
                                cursor.execute(query, (hex_code, callsign, lat, lon, altitude, heading))
                    
                    # Cleanup old data (> 7 days)
                    cutoff = time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(time.time() - 7 * 86400))
                    del_query = "DELETE FROM aircraft_history WHERE timestamp <= ?"
                    if DB_TYPE == "postgres": del_query = del_query.replace("?", "%s")
                    cursor.execute(del_query, (cutoff,))
                    conn.commit()"""
code = code.replace(old_update, new_update)

# 4. search
old_search = """        with sqlite3.connect(SQLITE_DB_FILE) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            search_term = f"%{query}%"
            # Get latest position for each matching plane
            cursor.execute('''
                SELECT hex, callsign, lat, lon, altitude, max(timestamp) as last_seen 
                FROM aircraft_history 
                WHERE callsign LIKE ? OR hex LIKE ? 
                GROUP BY hex 
                ORDER BY last_seen DESC 
                LIMIT 50
            ''', (search_term, search_term))
            
            results = [dict(row) for row in cursor.fetchall()]"""
new_search = """        with get_db_connection() as conn:
            search_term = f"%{query}%"
            query_sql = '''
                SELECT hex, callsign, lat, lon, altitude, timestamp as last_seen 
                FROM (
                    SELECT hex, callsign, lat, lon, altitude, timestamp,
                           ROW_NUMBER() OVER(PARTITION BY hex ORDER BY timestamp DESC) as rn
                    FROM aircraft_history
                    WHERE callsign LIKE ? OR hex LIKE ?
                ) t
                WHERE rn = 1
                ORDER BY last_seen DESC 
                LIMIT 50
            '''
            results = execute_query(conn, query_sql, (search_term, search_term))"""
code = code.replace(old_search, new_search)

# 5. history
old_history = """        with sqlite3.connect(SQLITE_DB_FILE) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute('''
                SELECT lat, lon, altitude, heading, timestamp 
                FROM aircraft_history 
                WHERE hex = ? 
                ORDER BY timestamp ASC
            ''', (hex_code,))
            
            results = [dict(row) for row in cursor.fetchall()]"""
new_history = """        with get_db_connection() as conn:
            query_sql = '''
                SELECT lat, lon, altitude, heading, timestamp 
                FROM aircraft_history 
                WHERE hex = ? 
                ORDER BY timestamp ASC
            '''
            results = execute_query(conn, query_sql, (hex_code,))"""
code = code.replace(old_history, new_history)

# 6. heatmap
old_heatmap = """        with sqlite3.connect(SQLITE_DB_FILE) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            # 7 day heatmap to ensure good performance, rounding to 3 decimals clusters the points at ~110m resolution
            cursor.execute('''
                SELECT ROUND(lat, 3) as r_lat, ROUND(lon, 3) as r_lon, COUNT(*) as intensity 
                FROM aircraft_history 
                WHERE timestamp >= datetime('now', '-7 days')
                GROUP BY r_lat, r_lon
            ''')
            
            # Format as [lat, lon, intensity] array for leaflet.heat
            results = [[row['r_lat'], row['r_lon'], row['intensity']] for row in cursor.fetchall()]"""
new_heatmap = """        with get_db_connection() as conn:
            cutoff = time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(time.time() - 7 * 86400))
            query_sql = '''
                SELECT ROUND(CAST(lat AS NUMERIC), 3) as r_lat, ROUND(CAST(lon AS NUMERIC), 3) as r_lon, COUNT(*) as intensity 
                FROM aircraft_history 
                WHERE timestamp >= ?
                GROUP BY ROUND(CAST(lat AS NUMERIC), 3), ROUND(CAST(lon AS NUMERIC), 3)
            '''
            raw_results = execute_query(conn, query_sql, (cutoff,))
            # Format as [lat, lon, intensity] array for leaflet.heat
            results = [[row['r_lat'], row['r_lon'], row['intensity']] for row in raw_results]"""
code = code.replace(old_heatmap, new_heatmap)

# 7. weather deviations
old_weather = """        with sqlite3.connect(SQLITE_DB_FILE) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            # Scan last 3 hours for abrupt heading (>15 deg) or altitude (>1000 ft) changes
            cursor.execute('''
                WITH changes AS (
                    SELECT hex, callsign, lat, lon, heading, altitude, timestamp,
                           ABS(heading - LAG(heading) OVER (PARTITION BY hex ORDER BY timestamp)) as hc,
                           ABS(altitude - LAG(altitude) OVER (PARTITION BY hex ORDER BY timestamp)) as ac
                    FROM aircraft_history
                    WHERE timestamp >= datetime('now', '-3 hours')
                )
                SELECT hex, callsign, lat, lon, hc, ac, timestamp
                FROM changes
                WHERE (hc > 15 AND hc < 345) OR (ac > 1000)
                ORDER BY timestamp DESC
                LIMIT 500
            ''')
            
            results = [dict(row) for row in cursor.fetchall()]"""
new_weather = """        with get_db_connection() as conn:
            cutoff = time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(time.time() - 3 * 3600))
            query_sql = '''
                WITH changes AS (
                    SELECT hex, callsign, lat, lon, heading, altitude, timestamp,
                           ABS(heading - LAG(heading) OVER (PARTITION BY hex ORDER BY timestamp)) as hc,
                           ABS(altitude - LAG(altitude) OVER (PARTITION BY hex ORDER BY timestamp)) as ac
                    FROM aircraft_history
                    WHERE timestamp >= ?
                )
                SELECT hex, callsign, lat, lon, hc, ac, timestamp
                FROM changes
                WHERE (hc > 15 AND hc < 345) OR (ac > 1000)
                ORDER BY timestamp DESC
                LIMIT 500
            '''
            results = execute_query(conn, query_sql, (cutoff,))"""
code = code.replace(old_weather, new_weather)

with open("tracker/test_app.py", "w") as f:
    f.write(code)
