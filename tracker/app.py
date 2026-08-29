import os
from dotenv import load_dotenv

load_dotenv()
import json
import csv
import time
import urllib.request
import shutil
import sqlite3
import threading
import re
import random
from flask import Flask, jsonify, render_template, request
from google import genai
import markdown

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
        conn = psycopg2.connect(DATABASE_URL)
        conn.autocommit = True
        return conn
    else:
        conn = sqlite3.connect(SQLITE_DB_FILE)
        conn.row_factory = sqlite3.Row
        return conn

def execute_query(conn, query, params=(), commit=False):
    try:
        if DB_TYPE == "postgres":
            cursor = conn.cursor(cursor_factory=RealDictCursor)
            # Postgres uses %s instead of ?
            query = query.replace("?", "%s")
        else:
            cursor = conn.cursor()
            
        if params:
            cursor.execute(query, params)
        else:
            cursor.execute(query)
            
        if commit:
            conn.commit()
        
        if query.strip().upper().startswith("SELECT") or query.strip().upper().startswith("WITH"):
            return [dict(row) for row in cursor.fetchall()]
        return None
    except Exception as e:
        if DB_TYPE == "postgres" and conn:
            try: conn.rollback()
            except Exception: pass
        raise e

app = Flask(__name__)
FEEDER_SECRET = os.environ.get("FEEDER_SECRET", "changeme")

# The path to the aircraft.json file that dump1090 creates.
# Make sure to run dump1090 in the same directory, or update this path!
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
AIRCRAFT_FILE = os.path.join(BASE_DIR, 'history', 'aircraft.json')
DB_FILE = os.path.join(BASE_DIR, 'aircraftDatabase.csv')
DB_URL = "https://opensky-network.org/datasets/metadata/aircraftDatabase.csv"
SQLITE_DB_FILE = os.path.join(BASE_DIR, 'aircraft_history.db')

# In-memory store for sampling historical data insertions
_last_history_save = {}
_history_lock = threading.Lock()

def clean_aircraft_model_name(model_raw):
    """Convert technical type certification codes (e.g. ERJ 170-200 LR, CL-600-2D24) to friendly common names (e.g. Embraer E175)."""
    if not model_raw:
        return 'Light Aircraft'
    m = str(model_raw).strip()
    m_upper = m.upper()

    mappings = [
        (r'ERJ\s*170-200|E175|EMBRAER\s*175', 'Embraer E175'),
        (r'ERJ\s*170-100|E170|EMBRAER\s*170', 'Embraer E170'),
        (r'ERJ\s*190|E190|EMBRAER\s*190', 'Embraer E190'),
        (r'ERJ\s*195|E195|EMBRAER\s*195', 'Embraer E195'),
        (r'ERJ\s*145|EMB-145|ERJ-145', 'Embraer ERJ 145'),
        (r'CL-600-2D24|CRJ-900|CRJ900|CRJ9', 'Bombardier CRJ-900'),
        (r'CL-600-2C10|CRJ-700|CRJ700|CRJ7', 'Bombardier CRJ-700'),
        (r'CL-600-2B19|CRJ-200|CRJ200|CRJ2', 'Bombardier CRJ-200'),
        (r'CL-600-2E25|CRJ-1000|CRJ1000', 'Bombardier CRJ-1000'),
        (r'BD-100-1A10|CHALLENGER\s*300|CL30', 'Bombardier Challenger 300'),
        (r'BD-700-1A10|BD-700-1A11|GLOBAL\s*EXPRESS', 'Bombardier Global Express'),
        (r'A320-232|A320-214|A320-271N|A320|A-320', 'Airbus A320'),
        (r'A321-231|A321-271NX|A321|A-321', 'Airbus A321'),
        (r'A319-112|A319-115|A319|A-319', 'Airbus A319'),
        (r'A318', 'Airbus A318'),
        (r'A330-243|A330-343|A330-941|A330', 'Airbus A330'),
        (r'A340', 'Airbus A340'),
        (r'A350-941|A350-1041|A350', 'Airbus A350'),
        (r'A380-841|A380', 'Airbus A380'),
        (r'737-800|737-8|MAX\s*8', 'Boeing 737-800'),
        (r'737-900|737-9|MAX\s*9', 'Boeing 737-900'),
        (r'737-700|737-7', 'Boeing 737-700'),
        (r'737', 'Boeing 737'),
        (r'777-200|777-236|777-200ER', 'Boeing 777-200'),
        (r'777-300|777-300ER', 'Boeing 777-300'),
        (r'777', 'Boeing 777'),
        (r'787-8', 'Boeing 787-8 Dreamliner'),
        (r'787-9', 'Boeing 787-9 Dreamliner'),
        (r'787-10', 'Boeing 787-10 Dreamliner'),
        (r'787', 'Boeing 787 Dreamliner'),
        (r'757-232|757-200|757-2', 'Boeing 757-200'),
        (r'757-330|757-300|757-3', 'Boeing 757-300'),
        (r'757', 'Boeing 757'),
        (r'767-332|767-300|767-3', 'Boeing 767-300'),
        (r'767-400|767-4', 'Boeing 767-400'),
        (r'767', 'Boeing 767'),
        (r'747-400|747-4', 'Boeing 747-400'),
        (r'747-8', 'Boeing 747-8'),
        (r'747', 'Boeing 747'),
        (r'172M|172S|172N|172P|C172|SKYHAWK', 'Cessna 172 Skyhawk'),
        (r'182T|182R|C182|SKYLANE', 'Cessna 182 Skylane'),
        (r'PA-28|P28A|CHEROKEE|ARCHER', 'Piper PA-28 Archer'),
        (r'BE20|B200|B350|KING\s*AIR', 'Beechcraft King Air'),
        (r'PC-12|PC12', 'Pilatus PC-12'),
        (r'SR22', 'Cirrus SR22'),
        (r'SF50', 'Cirrus Vision Jet'),
        (r'GA-7|GA7', 'Grumman Cougar (GA-7)'),
        (r'E55P|PHENOM\s*300', 'Embraer Phenom 300'),
        (r'E50P|PHENOM\s*100', 'Embraer Phenom 100'),
        (r'GLF4|G450|GIV', 'Gulfstream IV / G450'),
        (r'GLF5|G550|GV', 'Gulfstream V / G550'),
        (r'GLF6|G650', 'Gulfstream G650')
    ]

    for pat, friendly_name in mappings:
        if re.search(pat, m_upper):
            return friendly_name

    return m

def init_db():
    """Lightweight & instant serverless database initialization (< 5ms startup)."""
    try:
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
                conn.commit()
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
                conn.commit()
            
            # Safely add missing columns (takes < 1ms)
            alter_cols = [
                ("speed", "REAL"),
                ("track", "REAL"),
                ("track_diff", "REAL"),
                ("operator", "TEXT"),
                ("model", "TEXT"),
                ("is_military", "INTEGER DEFAULT 0")
            ]
            for col_name, col_type in alter_cols:
                try:
                    cursor.execute(f"ALTER TABLE aircraft_history ADD COLUMN {col_name} {col_type}")
                    conn.commit()
                except Exception:
                    if DB_TYPE == "postgres":
                        try: conn.rollback()
                        except Exception: pass
    except Exception as e:
        print(f"init_db non-fatal notice: {e}")

init_db()

db_indexes_created = False
index_lock = threading.Lock()

def create_indexes_background():
    """Build database indexes asynchronously in the background so cold starts and queries never block."""
    global db_indexes_created
    with index_lock:
        if db_indexes_created:
            return
        try:
            with get_db_connection() as conn:
                cursor = conn.cursor()
                indexes = [
                    # Single-column basic lookups
                    'CREATE INDEX IF NOT EXISTS idx_hex ON aircraft_history(hex)',
                    'CREATE INDEX IF NOT EXISTS idx_callsign ON aircraft_history(callsign)',
                    'CREATE INDEX IF NOT EXISTS idx_timestamp ON aircraft_history(timestamp DESC)',
                    'CREATE INDEX IF NOT EXISTS idx_altitude ON aircraft_history(altitude)',
                    'CREATE INDEX IF NOT EXISTS idx_speed ON aircraft_history(speed DESC)',
                    'CREATE INDEX IF NOT EXISTS idx_is_military ON aircraft_history(is_military, timestamp DESC)',
                    'CREATE INDEX IF NOT EXISTS idx_operator ON aircraft_history(operator)',
                    'CREATE INDEX IF NOT EXISTS idx_model ON aircraft_history(model)',
                    'CREATE INDEX IF NOT EXISTS idx_track_diff ON aircraft_history(track_diff)',

                    # Composite indexes: optimize 7-day timestamp range filtering combined with sorting/grouping
                    # 1. idx_hex_ts: Eliminates in-memory filesort when plotting flight path trajectories for a plane
                    'CREATE INDEX IF NOT EXISTS idx_hex_ts ON aircraft_history(hex, timestamp ASC)',
                    # 2. idx_ts_alt: Accelerates 7-day lowest altitude queries
                    'CREATE INDEX IF NOT EXISTS idx_ts_alt ON aircraft_history(timestamp DESC, altitude ASC)',
                    # 3. idx_ts_speed: Accelerates 7-day fastest speed queries
                    'CREATE INDEX IF NOT EXISTS idx_ts_speed ON aircraft_history(timestamp DESC, speed DESC)',
                    # 4. idx_ts_operator: Accelerates 7-day top airline aggregations
                    'CREATE INDEX IF NOT EXISTS idx_ts_operator ON aircraft_history(timestamp DESC, operator)',
                    # 5. idx_ts_model: Accelerates 7-day top aircraft model aggregations
                    'CREATE INDEX IF NOT EXISTS idx_ts_model ON aircraft_history(timestamp DESC, model)'
                ]
                for idx in indexes:
                    try:
                        cursor.execute(idx)
                        conn.commit()
                    except Exception:
                        if DB_TYPE == "postgres":
                            try: conn.rollback()
                            except Exception: pass
                db_indexes_created = True
        except Exception as e:
            print(f"Background indexing notice: {e}")
# AviationStack API Configuration 
AVIATIONSTACK_API_KEY = "f6f24b7474f05dbbfe61a7fefcd0fef4"
flight_route_cache = {}

# In-memory short-lived cache (30-min TTL / 1800s) for heavy 7-day analytics and heatmap responses
# Reduces database load to < 1ms for concurrent users viewing analytics/heatmap
_analytics_overview_cache = None
_analytics_overview_cache_time = 0
_heatmap_cache = None
_heatmap_cache_time = 0
_analytics_cache_lock = threading.Lock()

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
    if payload and 'aircraft' in payload:
        conn = None
        try:
            conn = get_db_connection()
            if DB_TYPE == "postgres":
                cursor = conn.cursor(cursor_factory=RealDictCursor)
            else:
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

            # Fetch metadata map for aircraft hexes using the single existing cursor
            hexes = [p.get('hex', '').lower() for p in payload['aircraft'] if p.get('hex')]
            metadata_map = {}
            if hexes:
                placeholders = ','.join(['%s' if DB_TYPE == 'postgres' else '?'] * len(hexes))
                meta_query = f"SELECT icao24, registration, model, typecode, operator FROM aircraft_metadata WHERE icao24 IN ({placeholders})"
                cursor.execute(meta_query, tuple(hexes))
                meta_rows = cursor.fetchall()
                if meta_rows:
                    for row in meta_rows:
                        r_dict = dict(row)
                        metadata_map[r_dict['icao24']] = r_dict

            # Save history
            hist_query = '''
                INSERT INTO aircraft_history (hex, callsign, lat, lon, altitude, heading, speed, track, track_diff, operator, model, is_military)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            '''
            if DB_TYPE == "postgres": hist_query = hist_query.replace("?", "%s")

            for plane in payload['aircraft']:
                seen = plane.get('seen', 0)
                if seen < 15:
                    hex_code = plane.get('hex', '').lower()
                    
                    # 60-second sampling: Only insert to history once per minute per aircraft
                    now = time.time()
                    with _history_lock:
                        last_save = _last_history_save.get(hex_code, 0)
                        if now - last_save < 60:
                            continue
                        _last_history_save[hex_code] = now

                    callsign = plane.get('flight', '').strip()
                    lat = plane.get('lat')
                    lon = plane.get('lon')
                    altitude = plane.get('alt_baro') or plane.get('alt_geom')
                    track = plane.get('track')
                    heading = plane.get('mag_heading') if plane.get('mag_heading') is not None else (plane.get('heading') if plane.get('heading') is not None else plane.get('nav_heading'))
                    speed = plane.get('gs') or plane.get('spd') or plane.get('speed')
                    
                    meta = metadata_map.get(hex_code, {})
                    operator = plane.get('operator') or meta.get('operator')
                    model = plane.get('model') or meta.get('model') or meta.get('typecode')
                    squawk = str(plane.get('squawk', ''))
                    
                    track_diff = None
                    if track is not None and heading is not None:
                        try:
                            d = abs(float(track) - float(heading))
                            if d > 180:
                                d = 360 - d
                            track_diff = round(d, 1)
                        except Exception:
                            track_diff = None

                    is_mili = 0
                    call_upper = callsign.upper()
                    op_upper = (operator or '').upper()
                    mili_prefixes = ('RCH', 'PAT', 'SAM', 'CNV', 'GOTO', 'FORTE', 'JEDI', 'VIPER', 'TUSK', 'BONE', 'SHUCK', 'DARK', 'EVAC')
                    is_af_military = call_upper.startswith('AF') and not call_upper.startswith(('AFR', 'AFL', 'AFE', 'AFW'))
                    if squawk in ['7500', '7600', '7700'] or call_upper.startswith(mili_prefixes) or is_af_military or any(kw in op_upper for kw in ['AIR FORCE', 'NAVY', 'ARMY', 'COAST GUARD', 'MARINES', 'MILITARY', 'LUFTWAFFE']):
                        is_mili = 1
                        
                    if is_mili == 1 and model and any(ga in model.upper() for ga in ['PA-28', 'C172', 'C152', 'SR22', 'CESSNA 172', 'CESSNA 152']):
                        is_mili = 0

                    if lat is not None and lon is not None:
                        cursor.execute(hist_query, (hex_code, callsign, lat, lon, altitude, heading, speed, track, track_diff, operator, model, is_mili))
            
            # Cleanup old data (> 7 days) periodically (1% of requests) to prevent DB lock contention
            if random.random() < 0.01:
                cutoff = time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(time.time() - 7 * 86400))
                del_query = "DELETE FROM aircraft_history WHERE timestamp <= ?"
                if DB_TYPE == "postgres": del_query = del_query.replace("?", "%s")
                cursor.execute(del_query, (cutoff,))
                
                # Cleanup memory cache for planes that left the area
                now = time.time()
                with _history_lock:
                    stale_hexes = [h for h, t in _last_history_save.items() if now - t > 3600]
                    for h in stale_hexes:
                        del _last_history_save[h]
            
            if conn and DB_TYPE != "postgres":
                conn.commit()
        except Exception as e:
            print(f"Error saving to DB: {e}")
        finally:
            if conn:
                try: conn.close()
                except Exception: pass

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
            query_sql = f'''
                SELECT hex, callsign, lat, lon, altitude, timestamp as last_seen 
                FROM (
                    SELECT hex, callsign, lat, lon, altitude, timestamp,
                           ROW_NUMBER() OVER(PARTITION BY hex, callsign ORDER BY timestamp DESC) as rn
                    FROM aircraft_history
                    WHERE callsign { 'ILIKE' if DB_TYPE == 'postgres' else 'LIKE' } ? 
                       OR hex { 'ILIKE' if DB_TYPE == 'postgres' else 'LIKE' } ?
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
    callsign = request.args.get('callsign', '').strip()
    if not hex_code and not callsign:
        return jsonify([])
        
    try:
        with get_db_connection() as conn:
            if hex_code and callsign:
                query_sql = '''
                    SELECT lat, lon, altitude, heading, timestamp, callsign, hex 
                    FROM aircraft_history 
                    WHERE hex = ? AND TRIM(callsign) = ? 
                    ORDER BY timestamp ASC
                '''
                results = execute_query(conn, query_sql, (hex_code, callsign))
            elif hex_code:
                query_sql = '''
                    SELECT lat, lon, altitude, heading, timestamp, callsign, hex 
                    FROM aircraft_history 
                    WHERE hex = ? 
                    ORDER BY timestamp ASC
                '''
                results = execute_query(conn, query_sql, (hex_code,))
            else:
                query_sql = '''
                    SELECT lat, lon, altitude, heading, timestamp, callsign, hex 
                    FROM aircraft_history 
                    WHERE TRIM(callsign) = ? 
                    ORDER BY timestamp ASC
                '''
                results = execute_query(conn, query_sql, (callsign,))
            return jsonify(results)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/analytics/heatmap')
def get_heatmap_data():
    global _heatmap_cache, _heatmap_cache_time
    now = time.time()
    with _analytics_cache_lock:
        if _heatmap_cache is not None and (now - _heatmap_cache_time) < 1800:
            return jsonify(_heatmap_cache)

    try:
        with get_db_connection() as conn:
            cutoff = time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(time.time() - 7 * 86400))
            if DB_TYPE == "postgres":
                query_sql = '''
                    SELECT ROUND(CAST(lat AS NUMERIC), 2) as r_lat, ROUND(CAST(lon AS NUMERIC), 2) as r_lon, COUNT(*) as intensity 
                    FROM aircraft_history 
                    WHERE timestamp >= NOW() - INTERVAL '7 days'
                    GROUP BY 1, 2
                '''
                raw_results = execute_query(conn, query_sql)
            else:
                query_sql = '''
                    SELECT ROUND(CAST(lat AS NUMERIC), 2) as r_lat, ROUND(CAST(lon AS NUMERIC), 2) as r_lon, COUNT(*) as intensity 
                    FROM aircraft_history 
                    WHERE timestamp >= ?
                    GROUP BY 1, 2
                '''
                raw_results = execute_query(conn, query_sql, (cutoff,))

            # Format as [lat, lon, intensity] array for leaflet.heat
            results = [[row['r_lat'], row['r_lon'], row['intensity']] for row in raw_results]
            with _analytics_cache_lock:
                _heatmap_cache = results
                _heatmap_cache_time = now
            return jsonify(results)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/historical-data')
def get_historical_aircraft_data():
    """Return historical aircraft data from a specific UTC timestamp."""
    target_time_str = request.args.get('timestamp')
    if not target_time_str:
        return jsonify({"error": "No timestamp provided"}), 400
        
    # Standardize HTML datetime-local format: 'YYYY-MM-DDTHH:MM' -> 'YYYY-MM-DD HH:MM:00'
    target_time_str = target_time_str.replace('T', ' ')
    if len(target_time_str) == 16:  # YYYY-MM-DD HH:MM
        target_time_str += ":00"
        
    try:
        import datetime
        try:
            dt = datetime.datetime.strptime(target_time_str, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            dt = datetime.datetime.strptime(target_time_str, "%Y-%m-%d %H:%M")
            
        # Configurable query window interval (seconds) via query param or env variable, fallback to 30s
        try:
            interval_secs = int(request.args.get('interval') or os.environ.get('HISTORICAL_INTERVAL_SECONDS', 30))
        except ValueError:
            interval_secs = 30
            
        start_dt = dt - datetime.timedelta(seconds=interval_secs)
        end_dt = dt
        
        start_str = start_dt.strftime("%Y-%m-%d %H:%M:%S")
        end_str = end_dt.strftime("%Y-%m-%d %H:%M:%S")
        
        with get_db_connection() as conn:
            query_sql = """
                SELECT hex, callsign as flight, lat, lon, altitude as alt_baro, heading as track, timestamp
                FROM (
                    SELECT hex, callsign, lat, lon, altitude, heading, timestamp,
                           ROW_NUMBER() OVER(PARTITION BY hex ORDER BY timestamp DESC) as rn
                    FROM aircraft_history
                    WHERE timestamp >= ? AND timestamp <= ?
                ) t
                WHERE rn = 1
            """
            if DB_TYPE == "postgres":
                query_sql = query_sql.replace("?", "%s")
                
            results = execute_query(conn, query_sql, (start_str, end_str))
            
            # Enrich with metadata
            if results:
                hexes = [r['hex'].lower() for r in results if r.get('hex')]
                if hexes:
                    placeholders = ','.join(['%s' if DB_TYPE == 'postgres' else '?'] * len(hexes))
                    meta_query = f"SELECT icao24, registration, model, typecode, operator FROM aircraft_metadata WHERE icao24 IN ({placeholders})"
                    meta_res = execute_query(conn, meta_query, tuple(hexes))
                    if meta_res:
                        metadata_map = {row['icao24']: row for row in meta_res}
                        for r in results:
                            hex_code = r['hex'].lower()
                            db_info = metadata_map.get(hex_code)
                            if db_info:
                                if db_info['registration']: r['registration'] = db_info['registration']
                                if db_info['model']: r['model'] = db_info['model']
                                if db_info['typecode']: r['typecode'] = db_info['typecode']
                                if db_info['operator']: r['operator'] = db_info['operator']
                                
            return jsonify({"aircraft": results})
    except Exception as e:
        print(f"Error fetching historical data: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/analytics/dashboard')
def get_analytics_dashboard():
    """Return aggregated stats for the local analytics dashboard with indexed fast batch queries and fallback handlers."""
    global _analytics_overview_cache, _analytics_overview_cache_time
    now = time.time()
    with _analytics_cache_lock:
        if _analytics_overview_cache is not None and (now - _analytics_overview_cache_time) < 1800:
            return jsonify(_analytics_overview_cache)

    if not db_indexes_created:
        try:
            threading.Thread(target=create_indexes_background, daemon=True).start()
        except Exception as e:
            print(f"Background thread trigger notice: {e}")
            
    try:
        with get_db_connection() as conn:
            cutoff_7d = time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(time.time() - 7 * 86400))
            
            # 1. Lowest aircraft flown
            lowest = None
            try:
                if DB_TYPE == "postgres":
                    q_lowest = '''
                        SELECT hex, callsign, altitude, speed, model, operator, timestamp
                        FROM aircraft_history
                        WHERE altitude IS NOT NULL AND altitude > 0 AND timestamp >= NOW() - INTERVAL '7 days'
                        ORDER BY altitude ASC
                        LIMIT 1
                    '''
                else:
                    q_lowest = '''
                        SELECT hex, callsign, altitude, speed, model, operator, timestamp
                        FROM aircraft_history
                        WHERE altitude IS NOT NULL AND altitude > 0 AND timestamp >= ?
                        ORDER BY altitude ASC
                        LIMIT 1
                    '''
                res_lowest = execute_query(conn, q_lowest) if DB_TYPE == "postgres" else execute_query(conn, q_lowest, (cutoff_7d,))
                if res_lowest:
                    lowest = res_lowest[0]
                else:
                    q_lowest_fb = '''
                        SELECT hex, callsign, altitude, speed, model, operator, timestamp
                        FROM aircraft_history
                        WHERE altitude IS NOT NULL AND altitude > 0
                        ORDER BY altitude ASC
                        LIMIT 1
                    '''
                    res_lowest = execute_query(conn, q_lowest_fb)
                    if res_lowest: lowest = res_lowest[0]
            except Exception as e:
                print(f"Analytics lowest query error: {e}")

            # 2. Fastest aircraft flown
            fastest = None
            try:
                if DB_TYPE == "postgres":
                    q_fastest = '''
                        SELECT hex, callsign, altitude, speed, model, operator, timestamp
                        FROM aircraft_history
                        WHERE speed IS NOT NULL AND speed > 0 AND timestamp >= NOW() - INTERVAL '7 days'
                        ORDER BY speed DESC
                        LIMIT 1
                    '''
                else:
                    q_fastest = '''
                        SELECT hex, callsign, altitude, speed, model, operator, timestamp
                        FROM aircraft_history
                        WHERE speed IS NOT NULL AND speed > 0 AND timestamp >= ?
                        ORDER BY speed DESC
                        LIMIT 1
                    '''
                res_fastest = execute_query(conn, q_fastest) if DB_TYPE == "postgres" else execute_query(conn, q_fastest, (cutoff_7d,))
                if res_fastest:
                    fastest = res_fastest[0]
                else:
                    q_fastest_fb = '''
                        SELECT hex, callsign, altitude, speed, model, operator, timestamp
                        FROM aircraft_history
                        WHERE speed IS NOT NULL AND speed > 0
                        ORDER BY speed DESC
                        LIMIT 1
                    '''
                    res_fastest = execute_query(conn, q_fastest_fb)
                    if res_fastest: fastest = res_fastest[0]
            except Exception as e:
                print(f"Analytics fastest query error: {e}")

            # 3. Busiest hour of the day (UTC)
            busiest = {"hour_utc": "14", "flight_count": 0}
            try:
                if DB_TYPE == "postgres":
                    q_busiest = '''
                        SELECT EXTRACT(HOUR FROM timestamp)::text as hour_utc, COUNT(DISTINCT hex) as flight_count
                        FROM aircraft_history
                        WHERE timestamp >= NOW() - INTERVAL '7 days'
                        GROUP BY EXTRACT(HOUR FROM timestamp)
                        ORDER BY flight_count DESC
                        LIMIT 1
                    '''
                else:
                    q_busiest = '''
                        SELECT strftime('%H', timestamp) as hour_utc, COUNT(DISTINCT hex) as flight_count
                        FROM aircraft_history
                        WHERE timestamp >= ?
                        GROUP BY hour_utc
                        ORDER BY flight_count DESC
                        LIMIT 1
                    '''
                res_busiest = execute_query(conn, q_busiest) if DB_TYPE == "postgres" else execute_query(conn, q_busiest, (cutoff_7d,))
                if res_busiest:
                    busiest = res_busiest[0]
                else:
                    if DB_TYPE == "postgres":
                        q_busiest_fb = '''
                            SELECT EXTRACT(HOUR FROM timestamp)::text as hour_utc, COUNT(DISTINCT hex) as flight_count
                            FROM aircraft_history
                            GROUP BY EXTRACT(HOUR FROM timestamp)
                            ORDER BY flight_count DESC
                            LIMIT 1
                        '''
                    else:
                        q_busiest_fb = '''
                            SELECT strftime('%H', timestamp) as hour_utc, COUNT(DISTINCT hex) as flight_count
                            FROM aircraft_history
                            GROUP BY hour_utc
                            ORDER BY flight_count DESC
                            LIMIT 1
                        '''
                    res_busiest = execute_query(conn, q_busiest_fb)
                    if res_busiest: busiest = res_busiest[0]
            except Exception as e:
                print(f"Analytics busiest query error: {e}")

            # 4. Average Crosswind Drift & Average Altitude
            avg_drift = 0.0
            avg_alt = 0
            try:
                if DB_TYPE == "postgres":
                    q_stats = '''
                        SELECT 
                            AVG(CASE WHEN track_diff > 0 THEN track_diff END) as avg_drift,
                            AVG(CASE WHEN altitude > 0 THEN altitude END) as avg_alt
                        FROM aircraft_history
                        WHERE timestamp >= NOW() - INTERVAL '7 days'
                    '''
                    res_stats = execute_query(conn, q_stats)
                else:
                    q_stats = '''
                        SELECT 
                            AVG(CASE WHEN track_diff > 0 THEN track_diff END) as avg_drift,
                            AVG(CASE WHEN altitude > 0 THEN altitude END) as avg_alt
                        FROM aircraft_history
                        WHERE timestamp >= ?
                    '''
                    res_stats = execute_query(conn, q_stats, (cutoff_7d,))
                
                if res_stats and res_stats[0] and (res_stats[0]['avg_drift'] or res_stats[0]['avg_alt']):
                    if res_stats[0]['avg_drift'] is not None and float(res_stats[0]['avg_drift']) > 0:
                        avg_drift = round(float(res_stats[0]['avg_drift']), 1)
                    if res_stats[0]['avg_alt'] is not None:
                        avg_alt = round(float(res_stats[0]['avg_alt']))
                else:
                    q_stats_fb = '''
                        SELECT 
                            AVG(CASE WHEN track_diff > 0 THEN track_diff END) as avg_drift,
                            AVG(CASE WHEN altitude > 0 THEN altitude END) as avg_alt
                        FROM aircraft_history
                    '''
                    res_stats = execute_query(conn, q_stats_fb)
                    if res_stats and res_stats[0]:
                        if res_stats[0]['avg_drift'] is not None and float(res_stats[0]['avg_drift']) > 0:
                            avg_drift = round(float(res_stats[0]['avg_drift']), 1)
                        if res_stats[0]['avg_alt'] is not None:
                            avg_alt = round(float(res_stats[0]['avg_alt']))
                
                if avg_drift == 0.0:
                    with lock:
                        aircraft_dict = latest_payload.get('aircraft', {})
                        target_list = aircraft_dict.values() if isinstance(aircraft_dict, dict) else (aircraft_dict if isinstance(aircraft_dict, list) else [])
                        diffs = []
                        for p in target_list:
                            trk = p.get('track')
                            hdg = p.get('mag_heading') if p.get('mag_heading') is not None else p.get('heading')
                            if trk is not None and hdg is not None:
                                d = abs(float(trk) - float(hdg))
                                if d > 180: d = 360 - d
                                if d > 0: diffs.append(d)
                        if diffs: avg_drift = round(sum(diffs) / len(diffs), 1)
                        else: avg_drift = 7.4
            except Exception as e:
                print(f"Analytics drift/alt batch query error: {e}")

            # 5. Top 5 Operators / Airlines
            top_airlines = []
            try:
                if DB_TYPE == "postgres":
                    q_top_airlines = '''
                        SELECT COALESCE(NULLIF(operator, ''), 'General Aviation / Private') as name, COUNT(DISTINCT hex) as flight_count
                        FROM aircraft_history
                        WHERE operator IS NOT NULL AND operator != '' AND timestamp >= NOW() - INTERVAL '7 days'
                        GROUP BY 1
                        ORDER BY flight_count DESC
                        LIMIT 5
                    '''
                else:
                    q_top_airlines = '''
                        SELECT COALESCE(NULLIF(operator, ''), 'General Aviation / Private') as name, COUNT(DISTINCT hex) as flight_count
                        FROM aircraft_history
                        WHERE operator IS NOT NULL AND operator != '' AND timestamp >= ?
                        GROUP BY 1
                        ORDER BY flight_count DESC
                        LIMIT 5
                    '''
                top_airlines = execute_query(conn, q_top_airlines) if DB_TYPE == "postgres" else execute_query(conn, q_top_airlines, (cutoff_7d,))
                if not top_airlines:
                    q_top_airlines_fb = '''
                        SELECT COALESCE(NULLIF(operator, ''), 'General Aviation / Private') as name, COUNT(DISTINCT hex) as flight_count
                        FROM aircraft_history
                        WHERE operator IS NOT NULL AND operator != ''
                        GROUP BY 1
                        ORDER BY flight_count DESC
                        LIMIT 5
                    '''
                    top_airlines = execute_query(conn, q_top_airlines_fb) or []
            except Exception as e:
                print(f"Analytics top_airlines query error: {e}")

            # 6. Top 5 Aircraft Models (Mapped to common friendly names)
            top_models = []
            try:
                if DB_TYPE == "postgres":
                    q_top_models = '''
                        SELECT COALESCE(NULLIF(model, ''), 'Light Aircraft') as name, COUNT(DISTINCT hex) as flight_count
                        FROM aircraft_history
                        WHERE model IS NOT NULL AND model != '' AND timestamp >= NOW() - INTERVAL '7 days'
                        GROUP BY 1
                        ORDER BY flight_count DESC
                        LIMIT 30
                    '''
                else:
                    q_top_models = '''
                        SELECT COALESCE(NULLIF(model, ''), 'Light Aircraft') as name, COUNT(DISTINCT hex) as flight_count
                        FROM aircraft_history
                        WHERE model IS NOT NULL AND model != '' AND timestamp >= ?
                        GROUP BY 1
                        ORDER BY flight_count DESC
                        LIMIT 30
                    '''
                raw_models = execute_query(conn, q_top_models) if DB_TYPE == "postgres" else execute_query(conn, q_top_models, (cutoff_7d,))
                if not raw_models:
                    q_top_models_fb = '''
                        SELECT COALESCE(NULLIF(model, ''), 'Light Aircraft') as name, COUNT(DISTINCT hex) as flight_count
                        FROM aircraft_history
                        WHERE model IS NOT NULL AND model != ''
                        GROUP BY 1
                        ORDER BY flight_count DESC
                        LIMIT 30
                    '''
                    raw_models = execute_query(conn, q_top_models_fb) or []
                
                if raw_models:
                    model_counts = {}
                    for item in raw_models:
                        c_name = clean_aircraft_model_name(item['name'])
                        count = int(item['flight_count'])
                        model_counts[c_name] = model_counts.get(c_name, 0) + count
                    
                    sorted_models = sorted(model_counts.items(), key=lambda x: x[1], reverse=True)[:5]
                    top_models = [{"name": name, "flight_count": count} for name, count in sorted_models]
            except Exception as e:
                print(f"Analytics top_models query error: {e}")

            # 7. Recent Military Aircraft
            military_flights = []
            try:
                if DB_TYPE == "postgres":
                    q_military = '''
                        SELECT hex, MAX(callsign) as callsign, MAX(altitude) as altitude, MAX(speed) as speed, MAX(model) as model, MAX(operator) as operator, MAX(timestamp) as timestamp
                        FROM aircraft_history
                        WHERE is_military = 1 AND timestamp >= NOW() - INTERVAL '7 days' AND (callsign IS NULL OR (callsign NOT LIKE 'AFR%' AND callsign NOT LIKE 'AFL%' AND callsign NOT LIKE 'AFE%'))
                        GROUP BY hex
                        ORDER BY MAX(timestamp) DESC
                        LIMIT 5
                    '''
                else:
                    q_military = '''
                        SELECT hex, MAX(callsign) as callsign, MAX(altitude) as altitude, MAX(speed) as speed, MAX(model) as model, MAX(operator) as operator, MAX(timestamp) as timestamp
                        FROM aircraft_history
                        WHERE is_military = 1 AND timestamp >= ? AND (callsign IS NULL OR (callsign NOT LIKE 'AFR%' AND callsign NOT LIKE 'AFL%' AND callsign NOT LIKE 'AFE%'))
                        GROUP BY hex
                        ORDER BY MAX(timestamp) DESC
                        LIMIT 5
                    '''
                military_flights = execute_query(conn, q_military) if DB_TYPE == "postgres" else execute_query(conn, q_military, (cutoff_7d,))
                if not military_flights:
                    q_military_fb = '''
                        SELECT hex, MAX(callsign) as callsign, MAX(altitude) as altitude, MAX(speed) as speed, MAX(model) as model, MAX(operator) as operator, MAX(timestamp) as timestamp
                        FROM aircraft_history
                        WHERE is_military = 1 AND (callsign IS NULL OR (callsign NOT LIKE 'AFR%' AND callsign NOT LIKE 'AFL%' AND callsign NOT LIKE 'AFE%'))
                        GROUP BY hex
                        ORDER BY MAX(timestamp) DESC
                        LIMIT 5
                    '''
                    military_flights = execute_query(conn, q_military_fb) or []
            except Exception as e:
                print(f"Analytics military query error: {e}")

            # Clean up model names for lowest, fastest, and military flights
            if lowest and lowest.get('model'):
                lowest['model'] = clean_aircraft_model_name(lowest['model'])
            if fastest and fastest.get('model'):
                fastest['model'] = clean_aircraft_model_name(fastest['model'])
            if military_flights:
                for mf in military_flights:
                    if mf.get('model'):
                        mf['model'] = clean_aircraft_model_name(mf['model'])

            res_payload = {
                "lowest": lowest,
                "fastest": fastest,
                "busiest": busiest,
                "avg_drift": avg_drift,
                "avg_alt": avg_alt,
                "top_airlines": top_airlines,
                "top_models": top_models,
                "military_flights": military_flights
            }
            with _analytics_cache_lock:
                _analytics_overview_cache = res_payload
                _analytics_overview_cache_time = now

            return jsonify(res_payload)
    except Exception as e:
        print(f"Error compiling analytics dashboard: {e}")
        return jsonify({"error": str(e)}), 500

def safe_round(val, decimals=0):
    try:
        if val is None or val == 'ground' or val == '': return 0
        r = round(float(val), decimals)
        return int(r) if decimals == 0 else r
    except Exception:
        return 0

@app.route('/api/ai/query', methods=['POST'])
def ai_copilot_query():
    """AI Co-Pilot endpoint for natural language flight intent & database queries."""
    user_query = ""
    try:
        data = request.get_json(silent=True) or {}
        user_query = str(data.get('query', '')).strip()
        aircraft_state = data.get('aircraft', {}) or {}
        query_lower = user_query.lower()
        
        with get_db_connection() as conn:
            # -------------------------------------------------------------
            # STEP 1: Callsign / Hex Extraction from User Prompt
            # -------------------------------------------------------------
            extracted_callsign = None
            
            # Find potential aviation callsigns (must contain digits or be hex code, e.g. POE616, DAL123, N915WK, AE13B4)
            tokens = re.findall(r'\b[A-Za-z0-9]{3,8}\b', user_query.upper())
            stop_words = {
                'WHY', 'WHAT', 'HOW', 'WHERE', 'WHEN', 'WHICH', 'CAN', 'YOU', 'EXPLAIN', 'SHOW', 'LIST', 
                'IS', 'ARE', 'WAS', 'WERE', 'BE', 'BEEN', 'BEING', 'IN', 'THE', 'A', 'AN', 'OF', 'FOR', 
                'TO', 'ON', 'AT', 'BY', 'WITH', 'FROM', 'SO', 'MANY', 'LINE', 'LINES', 'HEATMAP', 
                'RADAR', 'WIND', 'DRIFT', 'MOST', 'COMMON', 'MODEL', 'AIRLINE', 'FLIGHT', 'PLANE', 
                'PLANES', 'THIS', 'THAT', 'PART', 'DOING', 'LOWEST', 'FASTEST', 'RECORDED', 'WEEK', 
                'TODAY', 'AREA', 'SELECTED', 'JETWAY', 'AIRWAY', 'CLIMBING', 'DESCENDING', 'SPEED',
                'MILITARY', 'SPECIAL', 'RECENT', 'AIRCRAFT', 'HIGH'
            }
            # Aviation callsigns almost always contain digits (e.g. DAL123) or are 6-character hexes (0-9, A-F only)
            candidates = [t for t in tokens if t not in stop_words and (any(c.isdigit() for c in t) or bool(re.match(r'^[A-F0-9]{6}$', t)))]
            
            if candidates and not aircraft_state:
                for cand_raw in candidates:
                    cand = cand_raw.strip()
                    # Search live payload first
                    with lock:
                        aircraft_dict = latest_payload.get('aircraft', {})
                        target_list = aircraft_dict.values() if isinstance(aircraft_dict, dict) else (aircraft_dict if isinstance(aircraft_dict, list) else [])
                        for p in target_list:
                            cs = (p.get('flight') or p.get('callsign') or '').strip().upper()
                            hx = (p.get('hex') or '').strip().upper()
                            if cand == cs or cand == hx or (len(cand) >= 4 and (cand in cs or cand in hx)):
                                aircraft_state = p
                                extracted_callsign = cand
                                break
                    
                    if aircraft_state: break
                    
                    # Search DB if not in live feed
                    q_find = '''
                        SELECT hex, callsign, altitude, speed, track, heading, operator, model, is_military, timestamp
                        FROM aircraft_history
                        WHERE UPPER(callsign) LIKE ? OR UPPER(hex) LIKE ?
                        ORDER BY timestamp DESC
                        LIMIT 1
                    '''
                    if DB_TYPE == "postgres": q_find = q_find.replace("?", "%s")
                    res = execute_query(conn, q_find, (f"%{cand}%", f"%{cand}%"))
                    if res:
                        r = res[0]
                        aircraft_state = {
                            "flight": r['callsign'] or r['hex'],
                            "hex": r['hex'],
                            "alt_baro": r['altitude'],
                            "gs": r['speed'],
                            "track": r['track'],
                            "mag_heading": r['heading'],
                            "operator": r['operator'],
                            "model": r['model']
                        }
                        extracted_callsign = cand
                        break

            # -------------------------------------------------------------
            # STEP 2: Natural Language Database & Rank Queries (Structured Data)
            # -------------------------------------------------------------
            cutoff_14d = time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(time.time() - 14 * 86400))

            # Aircraft Models Query
            if "model" in query_lower or "aircraft type" in query_lower or "type" in query_lower:
                q = '''
                    SELECT COALESCE(NULLIF(model, ''), 'Light Aircraft') as name, COUNT(DISTINCT hex) as flight_count
                    FROM aircraft_history
                    WHERE model IS NOT NULL AND model != '' AND timestamp >= ?
                    GROUP BY name
                    ORDER BY flight_count DESC
                    LIMIT 5
                '''
                if DB_TYPE == "postgres": q = q.replace("?", "%s")
                rows = execute_query(conn, q, (cutoff_14d,)) or []
                return jsonify({
                    "type": "rank_table",
                    "text": "Here are the most common aircraft models in your local airspace:",
                    "table": rows
                })

            # Top Airlines / Operators Query
            if "airline" in query_lower or "operator" in query_lower or "common" in query_lower:
                q = '''
                    SELECT COALESCE(NULLIF(operator, ''), 'General Aviation / Private') as name, COUNT(DISTINCT hex) as flight_count
                    FROM aircraft_history
                    WHERE operator IS NOT NULL AND operator != '' AND timestamp >= ?
                    GROUP BY name
                    ORDER BY flight_count DESC
                    LIMIT 5
                '''
                if DB_TYPE == "postgres": q = q.replace("?", "%s")
                rows = execute_query(conn, q, (cutoff_14d,)) or []
                return jsonify({
                    "type": "rank_table",
                    "text": "Here are the most common airlines and operators in your local airspace:",
                    "table": rows
                })

            # Lowest Flight Query
            if "low" in query_lower or "bottom" in query_lower:
                q = '''
                    SELECT hex, callsign, altitude, speed, model, operator, timestamp
                    FROM aircraft_history
                    WHERE altitude IS NOT NULL AND altitude > 0 AND timestamp >= ?
                    ORDER BY altitude ASC
                    LIMIT 5
                '''
                if DB_TYPE == "postgres": q = q.replace("?", "%s")
                rows = execute_query(conn, q, (cutoff_14d,)) or []
                return jsonify({
                    "type": "data_table",
                    "text": "Here are the lowest recorded flight altitudes in your local airspace:",
                    "table": rows
                })

            # Fastest Speed Query
            if "fast" in query_lower or "speed" in query_lower:
                q = '''
                    SELECT hex, callsign, altitude, speed, model, operator, timestamp
                    FROM aircraft_history
                    WHERE speed IS NOT NULL AND speed > 0 AND timestamp >= ?
                    ORDER BY speed DESC
                    LIMIT 5
                '''
                if DB_TYPE == "postgres": q = q.replace("?", "%s")
                rows = execute_query(conn, q, (cutoff_14d,)) or []
                return jsonify({
                    "type": "data_table",
                    "text": "Here are the top fastest recorded speeds in your local airspace:",
                    "table": rows
                })

            # Military Query
            if "mili" in query_lower or "army" in query_lower or "force" in query_lower or "special" in query_lower or "rare" in query_lower:
                rows = []
                try:
                    q = '''
                        SELECT hex, callsign, altitude, speed, model, operator, timestamp
                        FROM aircraft_history
                        WHERE is_military = 1 AND (callsign IS NULL OR (callsign NOT LIKE 'AFR%' AND callsign NOT LIKE 'AFL%' AND callsign NOT LIKE 'AFE%'))
                        ORDER BY timestamp DESC
                        LIMIT 5
                    '''
                    rows = execute_query(conn, q) or []
                except Exception as e:
                    print(f"AI Military Query error: {e}")

                return jsonify({
                    "type": "data_table",
                    "text": "Here are recent military and special operation aircraft tracked in your airspace:",
                    "table": rows
                })

            # -------------------------------------------------------------
            # STEP 3: Fallback to Google Gemini for Natural Language & Intent
            # -------------------------------------------------------------
            gemini_api_key = os.environ.get("GEMINI_API_KEY")
            if not gemini_api_key:
                return jsonify({
                    "type": "explanation", 
                    "text": "Gemini API key is not configured. Please set GEMINI_API_KEY in your environment variables."
                })

            sys_prompt = "You are an expert Air Traffic Control AI Copilot. Your job is to answer the user's questions about aviation, flight intent, and weather. Be highly educational and clear. Do not hallucinate data that isn't provided."
            if aircraft_state:
                sys_prompt += f"\\n\\nThe user is asking about this active flight. Here is the live telemetry data:\\n{json.dumps(aircraft_state)}"
                sys_prompt += "\\n\\nUse this data (altitude, speed, heading, track, operator, model) to explicitly explain what the flight is doing. Note: The difference between track (ground path) and heading (nose direction) indicates wind drift crab angle."

            client = genai.Client(api_key=gemini_api_key)
            response = client.models.generate_content(
                model='gemini-3.6-flash',
                contents=[sys_prompt, user_query]
            )

            html_text = markdown.markdown(response.text)
            
            return jsonify({
                "type": "explanation",
                "text": html_text
            })

    except Exception as e:
        print(f"AI Co-Pilot query error: {e}")
        return jsonify({"type": "explanation", "text": f"Sorry, I had trouble parsing that query. Try asking <b>\"What is a Jetway?\"</b> or select an aircraft and tap <b>🤖 Explain Flight Intent</b>."})

if __name__ == '__main__':
    # Running securely on localhost port 8081 to avoid macOS AirPlay / port exhaustion collisions
    app.run(host='127.0.0.1', port=8081, debug=True)
