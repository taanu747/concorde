import os
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

app = Flask(__name__)

# The path to the aircraft.json file that dump1090 creates.
# Make sure to run dump1090 in the same directory, or update this path!
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
AIRCRAFT_FILE = os.path.join(BASE_DIR, 'history', 'aircraft.json')
DB_FILE = os.path.join(BASE_DIR, 'aircraftDatabase.csv')
DB_URL = "https://opensky-network.org/datasets/metadata/aircraftDatabase.csv"
SQLITE_DB_FILE = os.path.join(BASE_DIR, 'aircraft_history.db')

FEEDER_SECRET = os.environ.get("FEEDER_SECRET", "changeme")



def init_db():
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
        # Create stateless tables
        if DB_TYPE == "postgres":
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS latest_payload (
                    id INTEGER PRIMARY KEY,
                    payload TEXT,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS aircraft_metadata (
                    icao24 TEXT PRIMARY KEY,
                    registration TEXT,
                    model TEXT,
                    typecode TEXT,
                    operator TEXT
                )
            ''')
        else:
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS latest_payload (
                    id INTEGER PRIMARY KEY,
                    payload TEXT,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS aircraft_metadata (
                    icao24 TEXT PRIMARY KEY,
                    registration TEXT,
                    model TEXT,
                    typecode TEXT,
                    operator TEXT
                )
            ''')
        
        # Create indexes for faster search
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_hex ON aircraft_history(hex)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_callsign ON aircraft_history(callsign)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_timestamp ON aircraft_history(timestamp)')
        conn.commit()

init_db()
# AviationStack API Configuration 
AVIATIONSTACK_API_KEY = "f6f24b7474f05dbbfe61a7fefcd0fef4"
flight_route_cache = {}

@app.route('/api/route/<flight_iata>')
def get_flight_route(flight_iata):
    flight_iata = flight_iata.strip().upper()
    if not flight_iata:
        return jsonify({"error": "No flight IATA provided"}), 400
        
    # Check memory cache first to prevent spamming the external API
    if flight_iata in flight_route_cache:
        return jsonify(flight_route_cache[flight_iata])
        
    if AVIATIONSTACK_API_KEY == "YOUR_API_KEY_HERE":
        return jsonify({"error": "API Key not configured"}), 503

    try:
        # AviationStack free tier only supports HTTP
        url = f"http://api.aviationstack.com/v1/flights?access_key={AVIATIONSTACK_API_KEY}&flight_iata={flight_iata}"
        
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        
        # Bypass Python's strict macOS SSL certificate verification
        import ssl
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        
        with urllib.request.urlopen(req, timeout=5, context=ctx) as response:
            data = json.loads(response.read().decode('utf-8'))
            
        if 'data' in data and len(data['data']) > 0:
            flight = data['data'][0]
            departure = flight.get('departure', {})
            arrival = flight.get('arrival', {})
            
            origin = departure.get('iata') or departure.get('airport', 'Unknown')
            destination = arrival.get('iata') or arrival.get('airport', 'Unknown')
            
            if origin and destination and origin.lower() != 'unknown' and destination.lower() != 'unknown':
                route_info = {
                    'origin': origin,
                    'destination': destination
                }
                flight_route_cache[flight_iata] = route_info
                return jsonify(route_info)
            
        return jsonify({"error": "No route found"}), 404
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/')
def index():
    """Serve the main map page."""
    # Pass a timestamp parameter to bust browser cache for static files
    return render_template('index.html', ts=int(time.time()))

@app.route('/api/update', methods=['POST'])
def update_aircraft_data():
    """Receive live data pushed from local feeder script."""
    secret = request.headers.get('Authorization') or request.args.get('secret')
    if secret != FEEDER_SECRET and secret != f"Bearer {FEEDER_SECRET}":
        return jsonify({"error": "Unauthorized"}), 401
        
    payload = request.json
    try:
        if payload and 'aircraft' in payload:
            with get_db_connection() as conn:
                cursor = conn.cursor()
                
                # Save raw payload for stateless retrieval
                payload_str = json.dumps(payload)
                upsert_q = "UPDATE latest_payload SET payload = ?, updated_at = CURRENT_TIMESTAMP WHERE id = 1"
                if DB_TYPE == "postgres": upsert_q = upsert_q.replace("?", "%s")
                cursor.execute(upsert_q, (payload_str,))
                if cursor.rowcount == 0:
                    insert_q = "INSERT INTO latest_payload (id, payload) VALUES (1, ?)"
                    if DB_TYPE == "postgres": insert_q = insert_q.replace("?", "%s")
                    cursor.execute(insert_q, (payload_str,))

                # Save history
                hist_query = '''
                    INSERT INTO aircraft_history (hex, callsign, lat, lon, altitude, heading)
                    VALUES (?, ?, ?, ?, ?, ?)
                '''
                if DB_TYPE == "postgres": hist_query = hist_query.replace("?", "%s")

                for plane in payload['aircraft']:
                    seen = plane.get('seen', 0)
                    if seen < 15:
                        hex_code = plane.get('hex', '').lower()
                        callsign = plane.get('flight', '').strip()
                        lat = plane.get('lat')
                        lon = plane.get('lon')
                        altitude = plane.get('alt_baro') or plane.get('alt_geom')
                        heading = plane.get('track')
                        
                        if lat is not None and lon is not None:
                            cursor.execute(hist_query, (hex_code, callsign, lat, lon, altitude, heading))
                
                # Cleanup old data (> 7 days)
                cutoff = time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(time.time() - 7 * 86400))
                del_query = "DELETE FROM aircraft_history WHERE timestamp <= ?"
                if DB_TYPE == "postgres": del_query = del_query.replace("?", "%s")
                cursor.execute(del_query, (cutoff,))
                conn.commit()
    except Exception as e:
        print(f"Error saving to DB: {e}")

    return jsonify({"status": "success", "aircraft_count": len(request.json.get('aircraft', []))})

@app.route('/api/data')
def get_aircraft_data():
    """Return live aircraft data from memory, enriched with database details."""
    try:
        with get_db_connection() as conn:
            query = "SELECT payload FROM latest_payload WHERE id = 1"
            res = execute_query(conn, query)
            if not res or not res[0]['payload']:
                return jsonify({"aircraft": []})
                
            data = json.loads(res[0]['payload'])
            active_aircraft = []
            
            if 'aircraft' in data:
                # Extract hex codes to fetch metadata
                hexes = [p.get('hex', '').lower() for p in data['aircraft'] if p.get('hex')]
                
                metadata_map = {}
                if hexes:
                    placeholders = ','.join(['%s' if DB_TYPE == 'postgres' else '?'] * len(hexes))
                    meta_query = f"SELECT icao24, registration, model, typecode, operator FROM aircraft_metadata WHERE icao24 IN ({placeholders})"
                    meta_res = execute_query(conn, meta_query, tuple(hexes))
                    if meta_res:
                        for row in meta_res:
                            metadata_map[row['icao24']] = row

                for plane in data['aircraft']:
                    seen = plane.get('seen', 0)
                    if seen < 15:
                        hex_code = plane.get('hex', '').lower()
                        db_info = metadata_map.get(hex_code)
                        if db_info:
                            if db_info['registration']: plane['registration'] = db_info['registration']
                            if db_info['model']: plane['model'] = db_info['model']
                            if db_info['typecode']: plane['typecode'] = db_info['typecode']
                            if db_info['operator']: plane['operator'] = db_info['operator']
                            
                        active_aircraft.append(plane)
                
                data['aircraft'] = active_aircraft
                
            return jsonify(data)
    except Exception as e:
        print(f"Error fetching data: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/search')
def search_aircraft():
    query = request.args.get('query', '').strip()
    if not query:
        return jsonify([])
    
    try:
        with get_db_connection() as conn:
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
            results = execute_query(conn, query_sql, (search_term, search_term))
            return jsonify(results)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/history')
def get_aircraft_history():
    hex_code = request.args.get('hex', '').strip().lower()
    if not hex_code:
        return jsonify([])
        
    try:
        with get_db_connection() as conn:
            query_sql = '''
                SELECT lat, lon, altitude, heading, timestamp 
                FROM aircraft_history 
                WHERE hex = ? 
                ORDER BY timestamp ASC
            '''
            results = execute_query(conn, query_sql, (hex_code,))
            return jsonify(results)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/analytics/heatmap')
def get_heatmap_data():
    try:
        with get_db_connection() as conn:
            cutoff = time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(time.time() - 7 * 86400))
            query_sql = '''
                SELECT ROUND(CAST(lat AS NUMERIC), 3) as r_lat, ROUND(CAST(lon AS NUMERIC), 3) as r_lon, COUNT(*) as intensity 
                FROM aircraft_history 
                WHERE timestamp >= ?
                GROUP BY ROUND(CAST(lat AS NUMERIC), 3), ROUND(CAST(lon AS NUMERIC), 3)
            '''
            raw_results = execute_query(conn, query_sql, (cutoff,))
            # Format as [lat, lon, intensity] array for leaflet.heat
            results = [[row['r_lat'], row['r_lon'], row['intensity']] for row in raw_results]
            return jsonify(results)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/analytics/weather-deviations')
def get_weather_deviations():
    try:
        with get_db_connection() as conn:
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
            results = execute_query(conn, query_sql, (cutoff,))
            return jsonify(results)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == '__main__':
    # Running securely on localhost port 8081 to avoid macOS AirPlay / port exhaustion collisions
    app.run(host='127.0.0.1', port=8081, debug=True)
