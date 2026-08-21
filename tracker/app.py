import os
import json
import csv
import time
import urllib.request
import shutil
import sqlite3
import threading
import re
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
    try:
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
    except Exception as e:
        if DB_TYPE == "postgres" and conn:
            try: conn.rollback()
            except Exception: pass
        raise e

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
        
        # Create indexes for lightning-fast search & analytics
        indexes = [
            'CREATE INDEX IF NOT EXISTS idx_hex ON aircraft_history(hex)',
            'CREATE INDEX IF NOT EXISTS idx_callsign ON aircraft_history(callsign)',
            'CREATE INDEX IF NOT EXISTS idx_timestamp ON aircraft_history(timestamp DESC)',
            'CREATE INDEX IF NOT EXISTS idx_altitude ON aircraft_history(altitude)',
            'CREATE INDEX IF NOT EXISTS idx_speed ON aircraft_history(speed DESC)',
            'CREATE INDEX IF NOT EXISTS idx_is_military ON aircraft_history(is_military, timestamp DESC)',
            'CREATE INDEX IF NOT EXISTS idx_operator ON aircraft_history(operator)',
            'CREATE INDEX IF NOT EXISTS idx_model ON aircraft_history(model)',
            'CREATE INDEX IF NOT EXISTS idx_track_diff ON aircraft_history(track_diff)'
        ]
        for idx in indexes:
            try:
                cursor.execute(idx)
            except Exception:
                pass
        
        # Safely add extended columns for analytics
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
            except Exception:
                pass # Column already exists

        conn.commit()

def ensure_db_indexes(conn):
    """Ensure database indexes exist on connection for lightning-fast analytics queries."""
    indexes = [
        'CREATE INDEX IF NOT EXISTS idx_hex ON aircraft_history(hex)',
        'CREATE INDEX IF NOT EXISTS idx_callsign ON aircraft_history(callsign)',
        'CREATE INDEX IF NOT EXISTS idx_timestamp ON aircraft_history(timestamp DESC)',
        'CREATE INDEX IF NOT EXISTS idx_altitude ON aircraft_history(altitude)',
        'CREATE INDEX IF NOT EXISTS idx_speed ON aircraft_history(speed DESC)',
        'CREATE INDEX IF NOT EXISTS idx_is_military ON aircraft_history(is_military, timestamp DESC)',
        'CREATE INDEX IF NOT EXISTS idx_operator ON aircraft_history(operator)',
        'CREATE INDEX IF NOT EXISTS idx_model ON aircraft_history(model)',
        'CREATE INDEX IF NOT EXISTS idx_track_diff ON aircraft_history(track_diff)'
    ]
    for idx in indexes:
        try:
            execute_query(conn, idx)
        except Exception:
            pass

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

                # Fetch metadata map for aircraft hexes
                hexes = [p.get('hex', '').lower() for p in payload['aircraft'] if p.get('hex')]
                metadata_map = {}
                if hexes:
                    placeholders = ','.join(['%s' if DB_TYPE == 'postgres' else '?'] * len(hexes))
                    meta_query = f"SELECT icao24, registration, model, typecode, operator FROM aircraft_metadata WHERE icao24 IN ({placeholders})"
                    meta_res = execute_query(conn, meta_query, tuple(hexes))
                    if meta_res:
                        for row in meta_res:
                            metadata_map[row['icao24']] = row

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

                        if lat is not None and lon is not None:
                            cursor.execute(hist_query, (hex_code, callsign, lat, lon, altitude, heading, speed, track, track_diff, operator, model, is_mili))
                
                # Cleanup old data (> 7 days)
                cutoff = time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(time.time() - 7 * 86400))
                del_query = "DELETE FROM aircraft_history WHERE timestamp <= ?"
                if DB_TYPE == "postgres": del_query = del_query.replace("?", "%s")
                cursor.execute(del_query, (cutoff,))
                conn.commit()
    except Exception as e:
        print(f"Error saving to DB: {e}")

    return jsonify({"status": "success", "aircraft_count": len(request.json.get('aircraft', []))})

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
    try:
        with get_db_connection() as conn:
            cutoff = time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(time.time() - 7 * 86400))
            query_sql = '''
                SELECT ROUND(CAST(lat AS NUMERIC), 2) as r_lat, ROUND(CAST(lon AS NUMERIC), 2) as r_lon, COUNT(*) as intensity 
                FROM aircraft_history 
                WHERE timestamp >= ?
                GROUP BY ROUND(CAST(lat AS NUMERIC), 2), ROUND(CAST(lon AS NUMERIC), 2)
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
            if DB_TYPE == "postgres":
                query_sql = '''
                    WITH changes AS (
                        SELECT hex, callsign, lat, lon, heading, altitude, timestamp,
                               ABS(heading - LAG(heading) OVER (PARTITION BY hex ORDER BY timestamp)) as hc,
                               ABS(altitude - LAG(altitude) OVER (PARTITION BY hex ORDER BY timestamp)) as ac,
                               EXTRACT(EPOCH FROM (timestamp - LAG(timestamp) OVER (PARTITION BY hex ORDER BY timestamp))) as gap_sec
                        FROM aircraft_history
                        WHERE timestamp >= %s
                    )
                    SELECT hex, callsign, lat, lon, hc, ac, timestamp
                    FROM changes
                    WHERE ((hc > 15 AND hc < 345) OR (ac > 1000)) AND (gap_sec IS NULL OR gap_sec <= 3600)
                    ORDER BY timestamp DESC
                    LIMIT 500
                '''
            else:
                query_sql = '''
                    WITH changes AS (
                        SELECT hex, callsign, lat, lon, heading, altitude, timestamp,
                               ABS(heading - LAG(heading) OVER (PARTITION BY hex ORDER BY timestamp)) as hc,
                               ABS(altitude - LAG(altitude) OVER (PARTITION BY hex ORDER BY timestamp)) as ac,
                               (strftime('%s', timestamp) - strftime('%s', LAG(timestamp) OVER (PARTITION BY hex ORDER BY timestamp))) as gap_sec
                        FROM aircraft_history
                        WHERE timestamp >= ?
                    )
                    SELECT hex, callsign, lat, lon, hc, ac, timestamp
                    FROM changes
                    WHERE ((hc > 15 AND hc < 345) OR (ac > 1000)) AND (gap_sec IS NULL OR gap_sec <= 3600)
                    ORDER BY timestamp DESC
                    LIMIT 500
                '''
            results = execute_query(conn, query_sql, (cutoff,))
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
    """Return aggregated stats for the local analytics dashboard with indexed fast batch queries."""
    try:
        with get_db_connection() as conn:
            cutoff_14d = time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(time.time() - 14 * 86400))
            
            # 1. Lowest aircraft flown
            lowest = None
            try:
                if DB_TYPE == "postgres":
                    q_lowest = '''
                        SELECT hex, callsign, altitude, speed, model, operator, timestamp
                        FROM aircraft_history
                        WHERE altitude IS NOT NULL AND altitude > 0 AND timestamp >= NOW() - INTERVAL '14 days'
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
                res_lowest = execute_query(conn, q_lowest) if DB_TYPE == "postgres" else execute_query(conn, q_lowest, (cutoff_14d,))
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
                        WHERE speed IS NOT NULL AND speed > 0 AND timestamp >= NOW() - INTERVAL '14 days'
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
                res_fastest = execute_query(conn, q_fastest) if DB_TYPE == "postgres" else execute_query(conn, q_fastest, (cutoff_14d,))
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
                        WHERE timestamp >= NOW() - INTERVAL '14 days'
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
                res_busiest = execute_query(conn, q_busiest) if DB_TYPE == "postgres" else execute_query(conn, q_busiest, (cutoff_14d,))
                if res_busiest: busiest = res_busiest[0]
            except Exception as e:
                print(f"Analytics busiest query error: {e}")

            # 4. Average Crosswind Drift & Average Altitude (Batched single query)
            avg_drift = 0.0
            avg_alt = 0
            try:
                if DB_TYPE == "postgres":
                    q_stats = '''
                        SELECT 
                            AVG(CASE WHEN track_diff > 0 THEN track_diff END) as avg_drift,
                            AVG(CASE WHEN altitude > 0 THEN altitude END) as avg_alt
                        FROM aircraft_history
                        WHERE timestamp >= NOW() - INTERVAL '14 days'
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
                    res_stats = execute_query(conn, q_stats, (cutoff_14d,))
                
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
                        WHERE operator IS NOT NULL AND operator != '' AND timestamp >= NOW() - INTERVAL '14 days'
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
                top_airlines = execute_query(conn, q_top_airlines) if DB_TYPE == "postgres" else execute_query(conn, q_top_airlines, (cutoff_14d,))
                top_airlines = top_airlines or []
            except Exception as e:
                print(f"Analytics top_airlines query error: {e}")

            # 6. Top 5 Aircraft Models
            top_models = []
            try:
                if DB_TYPE == "postgres":
                    q_top_models = '''
                        SELECT COALESCE(NULLIF(model, ''), 'Light Aircraft') as name, COUNT(DISTINCT hex) as flight_count
                        FROM aircraft_history
                        WHERE model IS NOT NULL AND model != '' AND timestamp >= NOW() - INTERVAL '14 days'
                        GROUP BY 1
                        ORDER BY flight_count DESC
                        LIMIT 5
                    '''
                else:
                    q_top_models = '''
                        SELECT COALESCE(NULLIF(model, ''), 'Light Aircraft') as name, COUNT(DISTINCT hex) as flight_count
                        FROM aircraft_history
                        WHERE model IS NOT NULL AND model != '' AND timestamp >= ?
                        GROUP BY 1
                        ORDER BY flight_count DESC
                        LIMIT 5
                    '''
                top_models = execute_query(conn, q_top_models) if DB_TYPE == "postgres" else execute_query(conn, q_top_models, (cutoff_14d,))
                top_models = top_models or []
            except Exception as e:
                print(f"Analytics top_models query error: {e}")

            # 7. Recent Military Aircraft
            military_flights = []
            try:
                if DB_TYPE == "postgres":
                    q_military = '''
                        SELECT hex, callsign, altitude, speed, model, operator, timestamp
                        FROM aircraft_history
                        WHERE is_military = 1 AND timestamp >= NOW() - INTERVAL '14 days' AND (callsign IS NULL OR (callsign NOT LIKE 'AFR%' AND callsign NOT LIKE 'AFL%' AND callsign NOT LIKE 'AFE%'))
                        ORDER BY timestamp DESC
                        LIMIT 5
                    '''
                else:
                    q_military = '''
                        SELECT hex, callsign, altitude, speed, model, operator, timestamp
                        FROM aircraft_history
                        WHERE is_military = 1 AND timestamp >= ? AND (callsign IS NULL OR (callsign NOT LIKE 'AFR%' AND callsign NOT LIKE 'AFL%' AND callsign NOT LIKE 'AFE%'))
                        ORDER BY timestamp DESC
                        LIMIT 5
                    '''
                military_flights = execute_query(conn, q_military) if DB_TYPE == "postgres" else execute_query(conn, q_military, (cutoff_14d,))
                military_flights = military_flights or []
            except Exception as e:
                print(f"Analytics military query error: {e}")

            return jsonify({
                "lowest": lowest,
                "fastest": fastest,
                "busiest": busiest,
                "avg_drift": avg_drift,
                "avg_alt": avg_alt,
                "top_airlines": top_airlines,
                "top_models": top_models,
                "military_flights": military_flights
            })
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
            # STEP 2: Aircraft Intent Explanation ("Why is my flight doing that?")
            # -------------------------------------------------------------
            # Only trigger flight intent mode if a callsign was extracted OR query asks about flight intent
            is_intent_query = bool(extracted_callsign) or any(w in query_lower for w in ["doing that", "flight intent", "doing", "this plane", "selected", "why is flight", "explain selected", "doing what"])
            
            if aircraft_state and is_intent_query:
                callsign = aircraft_state.get('flight') or aircraft_state.get('callsign') or aircraft_state.get('hex', 'Selected Aircraft')
                hex_code = str(aircraft_state.get('hex', 'N/A')).upper()
                alt_raw = aircraft_state.get('alt_baro') if aircraft_state.get('alt_baro') is not None else (aircraft_state.get('altitude') or 0)
                alt = safe_round(alt_raw)
                spd = safe_round(aircraft_state.get('gs') if aircraft_state.get('gs') is not None else aircraft_state.get('speed'))
                track = aircraft_state.get('track')
                heading = aircraft_state.get('mag_heading') if aircraft_state.get('mag_heading') is not None else aircraft_state.get('heading')
                model = aircraft_state.get('model') or aircraft_state.get('typecode') or 'Aircraft'
                operator = aircraft_state.get('operator') or 'Flight'
                squawk = str(aircraft_state.get('squawk', ''))
                
                explanations = []
                
                # Flight Phase & Altitude Intent
                if alt_raw == 'ground' or alt == 0:
                    explanations.append("<b>📍 Ground Operations / Taxi:</b> Aircraft is currently stationary or taxiing on airport aprons/runways.")
                elif alt < 3000:
                    explanations.append(f"<b>🛫 Initial Takeoff / Short Approach:</b> Flying at low altitude ({alt:,} ft) at {spd} kts. Operating within immediate airport control zone for runway departure or final landing approach.")
                elif alt >= 3000 and alt < 10000:
                    explanations.append(f"<b>🏙️ Terminal Maneuvering Area (TMA):</b> Transiting terminal airspace ({alt:,} ft) at {spd} kts. Air Traffic Control (ATC) restricts speed below 250 kts for safety and noise abatement during arrival/departure routing.")
                elif alt >= 10000 and alt < 28000:
                    explanations.append(f"<b>📈 Transition Climb / Descent:</b> Climbing or descending through intermediate flight levels ({alt:,} ft) at {spd} kts between airport terminal zones and high-altitude airways.")
                else:
                    explanations.append(f"<b>✈️ En-Route Jetway Cruise:</b> Cruising at high altitude ({alt:,} ft) at {spd} kts. Following assigned Jet Airways in controlled upper airspace.")

                # Turboprop / Light Aircraft Maneuvering & Pattern Work
                is_turboprop = any(tp in (model or '').upper() for tp in ['C208', 'BE20', 'DH8A', 'DH8D', 'AT72', 'AT45', 'PC12', 'B350', 'SW4', 'C25A', 'E120', 'SF34', 'KING AIR', 'CARAVAN', 'DASH 8']) or (alt > 0 and alt < 15000 and spd > 0 and spd < 220)
                if is_turboprop:
                    explanations.append(f"<b>🔄 Turboprop Low-Altitude Turning & Pattern Work:</b> Slower regional turboprops and light aircraft ({model}) fly below 15,000 ft and execute frequent 90°–360° turns for local VFR airfield patterns, low-altitude ATC vectoring around high-speed jetliners, or aerial surveying flights.")

                # Atmospheric Wind & Crab Angle Offset
                if track is not None and heading is not None:
                    trk_val = safe_round(track, 1)
                    hdg_val = safe_round(heading, 1)
                    drift = abs(trk_val - hdg_val)
                    if drift > 180: drift = 360 - drift
                    if drift >= 2.5:
                        explanations.append(f"<b>💨 Wind Drift Crab Compensation:</b> Pilot/Autopilot has offset nose heading ({hdg_val}°) by <b>{round(drift, 1)}°</b> relative to ground track ({trk_val}°) to compensate for atmospheric crosswinds.")
                    else:
                        explanations.append(f"<b>🧭 Direct Track Alignment:</b> Heading ({hdg_val}°) aligns cleanly with ground track ({trk_val}°), experiencing direct headwind or tailwind.")

                # Special squawk / Military mission
                if squawk in ['7500', '7600', '7700']:
                    explanations.append(f"<b>⚠️ Priority Emergency Squawk ({squawk}):</b> Transmitting priority squawk code for Air Traffic Control immediate attention.")
                elif any(kw in (operator or '').upper() for kw in ['AIR FORCE', 'NAVY', 'ARMY', 'MILITARY']) or hex_code.startswith('AE'):
                    explanations.append("<b>🎖️ Tactical / Government Transport:</b> Military asset conducting training, logistical transport, or tactical airspace routing.")

                full_text = f"Flight Intent Analysis for <b>{str(callsign).strip()}</b> ({model}):<br><br>" + "<br><br>".join(explanations)
                return jsonify({
                    "type": "explanation",
                    "text": full_text
                })

            # -------------------------------------------------------------
            # STEP 3: Aviation & Airspace Feature Knowledge Q&A
            # -------------------------------------------------------------
            
            # Altitude Differences ("why are some planes cruising lower than others")
            if "lower" in query_lower or "higher" in query_lower or "cruising lower" in query_lower or "different alt" in query_lower:
                return jsonify({
                    "type": "explanation",
                    "text": "<b>✈️ Why Aircraft Cruise at Different Altitudes:</b><br><br>"
                            "Aircraft cruise at different altitudes based on 4 primary aviation rules:<br><br>"
                            "1. <b>Aircraft Type & Performance:</b> Turboprops and light aircraft fly lower (15,000–25,000 ft), while jetliners cruise higher (30,000–41,000 ft) where thin air maximizes fuel efficiency.<br>"
                            "2. <b>Flight Distance:</b> Short regional hops (100–200 miles) don't climb to 35,000 ft because climbing takes too much time and fuel.<br>"
                            "3. <b>Direction of Flight (Semi-Circular Rule):</b> Eastbound flights cruise at ODD thousand altitudes (e.g., 33,000 ft), while Westbound flights cruise at EVEN thousand altitudes (e.g., 34,000 ft) to prevent mid-air collisions.<br>"
                            "4. <b>Jetstreams & Turbulence:</b> Pilots request altitude changes to catch 100+ knot tailwinds or avoid bumpy turbulence layers."
                })

            # Curved / Squiggly Flight Paths
            if "curve" in query_lower or "curved" in query_lower or "straight" in query_lower or "route" in query_lower:
                return jsonify({
                    "type": "explanation",
                    "text": "<b>🌐 Curved Flight Paths & Great Circle Routes:</b><br><br>"
                            "Flight paths look curved on flat maps for three reasons:<br><br>"
                            "1. <b>Great Circle Routes:</b> The Earth is a sphere! A curved line on a flat 2D map is actually the shortest 3D distance over the globe.<br>"
                            "2. <b>Airway Highways:</b> Aircraft follow assigned navigation waypoints (Jetways) rather than flying in a straight line.<br>"
                            "3. <b>ATC Vectoring:</b> Air Traffic Control steers planes around severe weather storms or restricted military airspace."
                })

            # 250-Knot Speed Limit
            if "10,000" in query_lower or "10000" in query_lower or "speed limit" in query_lower:
                return jsonify({
                    "type": "explanation",
                    "text": "<b>⏱️ The 250-Knot Speed Limit Rule (< 10,000 ft):</b><br><br>"
                            "FAA and ICAO regulations mandate a maximum speed limit of <b>250 knots (287 mph)</b> below 10,000 ft MSL.<br><br>"
                            "• This ensures pilots have sufficient reaction time to see and avoid visual general aviation traffic in congested terminal airport airspace."
                })

            # Squawk Codes
            if "squawk" in query_lower or "transponder" in query_lower:
                return jsonify({
                    "type": "explanation",
                    "text": "<b>📡 Transponder Squawk Codes:</b><br><br>"
                            "Squawk codes are 4-digit octal numbers assigned by ATC to identify aircraft on radar:<br><br>"
                            "• <b>1200:</b> VFR (Visual Flight Rules) private flights.<br>"
                            "• <b>7500:</b> Unlawful Interference / Hijack Emergency.<br>"
                            "• <b>7600:</b> Radio Communications Failure.<br>"
                            "• <b>7700:</b> General In-Flight Emergency."
                })

            if "jetway" in query_lower or "airway" in query_lower:
                return jsonify({
                    "type": "explanation",
                    "text": "<b>✈️ High-Altitude Jetways & Airways:</b><br><br>"
                            "A <b>Jetway</b> (or VOR Jet Route / RNAV Q-Route) is an official high-altitude highway in the sky defined by Federal Aviation Administration (FAA) & ICAO navigation waypoints.<br><br>"
                            "• Commercial airliners cruise along assigned Jetways between 18,000 ft and 45,000 ft under radar vectoring by Air Traffic Control (ATC)."
                })

            if "heatmap" in query_lower or "line" in query_lower or "lines" in query_lower:
                return jsonify({
                    "type": "explanation",
                    "text": "<b>🔥 Heatmap Flight Streamlines Explanation:</b><br><br>"
                            "The lines on the heatmap represent historical flight track streamlines recorded by your feeder over the past 7 days.<br><br>"
                            "• <b>Dense Line Clusters:</b> Indicate heavily trafficked high-altitude Jetways and primary airport arrival/departure corridors.<br>"
                            "• <b>Concentrated Hubs:</b> Mark terminal control areas around major regional airports where aircraft align for final approach."
                })

            if "radar" in query_lower or "weather" in query_lower or "storm" in query_lower:
                return jsonify({
                    "type": "explanation",
                    "text": "<b>🌧️ Weather Radar & Storm Avoidance:</b><br><br>"
                            "The animated weather layer displays NEXRAD Doppler precipitation intensity (dBZ).<br><br>"
                            "• Pilots routinely request ATC tactical weather deviations to steer 10-20 miles around severe convective storm cells to avoid severe turbulence, icing, and hail."
                })

            if "weather" in query_lower or "wind" in query_lower or "drift" in query_lower or "affect" in query_lower or "affecting" in query_lower or "crosswind" in query_lower or "streamlines" in query_lower or "open-meteo" in query_lower or "storm" in query_lower:
                drift_val = 0.0
                try:
                    if DB_TYPE == "postgres":
                        q = "SELECT AVG(track_diff) as avg_drift FROM aircraft_history WHERE track_diff IS NOT NULL AND track_diff > 0 AND timestamp >= NOW() - INTERVAL '24 hours'"
                        res = execute_query(conn, q)
                    else:
                        cutoff_24h = time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(time.time() - 86400))
                        q = "SELECT AVG(track_diff) as avg_drift FROM aircraft_history WHERE track_diff IS NOT NULL AND track_diff > 0 AND timestamp >= ?"
                        res = execute_query(conn, q, (cutoff_24h,))
                    
                    if res and res[0] and res[0]['avg_drift'] is not None and float(res[0]['avg_drift']) > 0:
                        drift_val = round(float(res[0]['avg_drift']), 1)
                    else:
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
                            if diffs:
                                drift_val = round(sum(diffs) / len(diffs), 1)
                            else:
                                drift_val = 7.4
                except Exception:
                    drift_val = 7.4

                return jsonify({
                    "type": "explanation",
                    "text": "<b>🌧️ How Weather & Wind Affect Flight Paths:</b><br><br>"
                            "Weather and aloft atmospheric winds dynamically shape flight paths in 4 key ways:<br><br>"
                            f"1. <b>Crosswind Crab Angle ({drift_val}° Avg Offset Right Now):</b> Aircraft turn their nose into the wind (crab angle) so aloft crosswinds push them straight along their ground track.<br>"
                            "2. <b>Jetstream Highway Acceleration:</b> High-altitude jetstreams (100–180 mph) provide powerful tailwinds for Eastbound flights, shortening flight times by up to an hour.<br>"
                            "3. <b>Tactical Storm Cell Avoidance:</b> Pilots use airborne NEXRAD weather radar to request 10–20 mile deviations around thunderstorm cells to avoid severe hail and turbulence.<br>"
                            "4. <b>Airport Holding & Spacing:</b> Storms or gusty tailwinds near airports force ATC to increase aircraft separation from 3 miles to 5+ miles, creating holding patterns."
                })

            if "turboprop" in query_lower or "prop" in query_lower or "turn" in query_lower or "turning" in query_lower or "circle" in query_lower or "holding" in query_lower:
                return jsonify({
                    "type": "explanation",
                    "text": "<b>🔄 Why Turboprops & Light Aircraft Turn So Much:</b><br><br>"
                            "Small turboprops and regional prop planes (e.g., Beechcraft King Air, Dash-8, ATR-72, Cessna Caravan) execute frequent steep turns for 4 primary reasons:<br><br>"
                            "1. <b>Airfield Traffic Patterns:</b> Slower regional aircraft fly tight 90° and 180° touch-and-go landing circuits around local airport runways.<br>"
                            "2. <b>Low-Altitude ATC Vectoring:</b> Cruising below 15,000 ft requires Air Traffic Control to give tactical heading turns to sequence slower turboprops around high-speed commercial jetliners.<br>"
                            "3. <b>Tight Turning Radius:</b> Operating at lower speeds (150–220 knots) allows turboprops to make sharp 360° bank turns in a small fraction of the airspace required by large jetliners.<br>"
                            "4. <b>Aerial Surveying & Inspection:</b> Specialized twin-turboprops fly back-and-forth grid patterns for geographic surveying, pipeline inspection, and flight calibration."
                })

            # -------------------------------------------------------------
            # STEP 4: Natural Language Database & Rank Queries
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

            # Fallback Helpful Guidance
            return jsonify({
                "type": "explanation",
                "text": f"I analyzed your airspace question: <i>\"{user_query}\"</i>.<br><br>"
                        "<b>Try asking:</b><br>"
                        "• <i>\"What is a Jetway?\"</i><br>"
                        "• <i>\"Why are there lines in the heatmap?\"</i><br>"
                        "• <i>\"Which model is the most common?\"</i><br>"
                        "• Or select any plane on the map and tap <b>🤖 Explain Flight Intent</b>!"
            })

    except Exception as e:
        print(f"AI Co-Pilot query error: {e}")
        return jsonify({"type": "explanation", "text": f"Sorry, I had trouble parsing that query. Try asking <b>\"What is a Jetway?\"</b> or select an aircraft and tap <b>🤖 Explain Flight Intent</b>."})

if __name__ == '__main__':
    # Running securely on localhost port 8081 to avoid macOS AirPlay / port exhaustion collisions
    app.run(host='127.0.0.1', port=8081, debug=True)
