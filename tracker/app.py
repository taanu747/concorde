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

                AIRLINE_MAP = {
                    'DAL': 'Delta Air Lines',
                    'UAL': 'United Airlines',
                    'AAL': 'American Airlines',
                    'SWA': 'Southwest Airlines',
                    'JBU': 'JetBlue Airways',
                    'FDX': 'FedEx Express',
                    'UPS': 'UPS Airlines',
                    'SKW': 'SkyWest Airlines',
                    'EJA': 'NetJets',
                    'KAP': 'Cape Air',
                    'RCH': 'US Air Force (Reach)',
                    'PAT': 'US Army (PAT)',
                    'AFR': 'Air France',
                    'BAW': 'British Airways',
                    'DLH': 'Lufthansa'
                }

                for plane in payload['aircraft']:
                    seen = plane.get('seen', 0)
                    if seen < 15:
                        hex_code = plane.get('hex', '').lower()
                        callsign = plane.get('flight', '').strip()
                        lat = plane.get('lat')
                        lon = plane.get('lon')
                        altitude = plane.get('alt_baro') if plane.get('alt_baro') is not None else plane.get('alt_geom')
                        
                        # Ground track angle
                        track = plane.get('track') if plane.get('track') is not None else plane.get('r_dir')
                        
                        # Aircraft nose heading (check mag_heading, true_heading, heading, nav_heading)
                        heading = plane.get('mag_heading')
                        if heading is None: heading = plane.get('true_heading')
                        if heading is None: heading = plane.get('heading')
                        if heading is None: heading = plane.get('nav_heading')
                        
                        # Ground speed
                        speed = plane.get('gs') if plane.get('gs') is not None else (plane.get('spd') if plane.get('spd') is not None else plane.get('speed'))
                        
                        meta = metadata_map.get(hex_code, {})
                        
                        # Operator resolution
                        operator = plane.get('operator') or meta.get('operator')
                        if not operator and callsign:
                            prefix = callsign[:3].upper()
                            operator = AIRLINE_MAP.get(prefix)
                            
                        # Model resolution
                        model = plane.get('model') or meta.get('model') or meta.get('typecode') or plane.get('category')
                        squawk = str(plane.get('squawk', ''))
                        
                        # Calculate exact crosswind offset track_diff ONLY when both track & nose heading exist
                        track_diff = None
                        if track is not None and heading is not None:
                            diff = abs(float(track) - float(heading))
                            if diff > 180:
                                diff = 360.0 - diff
                            track_diff = round(diff, 1)

                        is_mili = 0
                        call_upper = callsign.upper()
                        op_upper = (operator or '').upper()
                        if squawk in ['7500', '7600', '7700'] or call_upper.startswith(('RCH', 'PAT', 'AF')) or any(kw in op_upper for kw in ['AIR FORCE', 'NAVY', 'ARMY', 'COAST GUARD', 'MARINE', 'MILITARY']):
                            is_mili = 1

                        if lat is not None and lon is not None:
                            cursor.execute(hist_query, (hex_code, callsign, lat, lon, altitude, heading, speed, track, track_diff, operator, model, is_mili))
                
                # Cleanup old data (> 14 days / 2 weeks)
                cutoff = time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(time.time() - 14 * 86400))
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
    """Return aggregated stats for the local analytics dashboard (past 14 days / 2 weeks)."""
    try:
        with get_db_connection() as conn:
            # 14 days / 2 weeks cutoff timestamp
            cutoff = time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(time.time() - 14 * 86400))

            # 1. Lowest aircraft flown in past 2 weeks
            q_lowest = '''
                SELECT hex, callsign, altitude, speed, model, operator, timestamp
                FROM aircraft_history
                WHERE altitude IS NOT NULL AND altitude > 0 AND timestamp >= ?
                ORDER BY altitude ASC
                LIMIT 1
            '''
            if DB_TYPE == "postgres": q_lowest = q_lowest.replace("?", "%s")
            res_lowest = execute_query(conn, q_lowest, (cutoff,))
            lowest = res_lowest[0] if res_lowest else None

            # 2. Fastest aircraft flown in past 2 weeks
            q_fastest = '''
                SELECT hex, callsign, altitude, speed, model, operator, timestamp
                FROM aircraft_history
                WHERE speed IS NOT NULL AND speed > 0 AND timestamp >= ?
                ORDER BY speed DESC
                LIMIT 1
            '''
            if DB_TYPE == "postgres": q_fastest = q_fastest.replace("?", "%s")
            res_fastest = execute_query(conn, q_fastest, (cutoff,))
            fastest = res_fastest[0] if res_fastest else None

            # 3. Busiest hour of the day (UTC) in past 2 weeks
            if DB_TYPE == "postgres":
                q_busiest = '''
                    SELECT EXTRACT(HOUR FROM timestamp)::text as hour_utc, COUNT(DISTINCT hex) as flight_count
                    FROM aircraft_history
                    WHERE timestamp >= %s
                    GROUP BY hour_utc
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
            res_busiest = execute_query(conn, q_busiest, (cutoff,))
            busiest = res_busiest[0] if res_busiest else {"hour_utc": "14", "flight_count": 0}

            # 4. Average Crosswind Drift & Average Altitude
            # Dynamic calculation for track vs heading difference when track_diff is null
            q_avg = '''
                SELECT 
                    AVG(
                        CASE 
                            WHEN track_diff IS NOT NULL AND track_diff > 0 THEN track_diff
                            WHEN track IS NOT NULL AND heading IS NOT NULL AND track != heading THEN
                                CASE WHEN ABS(track - heading) > 180 THEN 360 - ABS(track - heading) ELSE ABS(track - heading) END
                            ELSE NULL
                        END
                    ) as avg_drift,
                    AVG(altitude) as avg_alt
                FROM aircraft_history
                WHERE altitude IS NOT NULL AND altitude > 0 AND timestamp >= ?
            '''
            if DB_TYPE == "postgres": q_avg = q_avg.replace("?", "%s")
            res_avg = execute_query(conn, q_avg, (cutoff,))
            avg_drift = round(float(res_avg[0]['avg_drift']), 1) if res_avg and res_avg[0]['avg_drift'] is not None else 8.4
            avg_alt = round(float(res_avg[0]['avg_alt'])) if res_avg and res_avg[0]['avg_alt'] is not None else 0

            # 5. Top 5 Operators / Airlines in past 2 weeks
            q_top_airlines = '''
                SELECT COALESCE(NULLIF(operator, ''), 'General Aviation / Private') as name, COUNT(DISTINCT hex) as flight_count
                FROM aircraft_history
                WHERE operator IS NOT NULL AND operator != '' AND timestamp >= ?
                GROUP BY name
                ORDER BY flight_count DESC
                LIMIT 5
            '''
            if DB_TYPE == "postgres": q_top_airlines = q_top_airlines.replace("?", "%s")
            top_airlines = execute_query(conn, q_top_airlines, (cutoff,)) or []

            # 6. Top 5 Aircraft Models in past 2 weeks
            q_top_models = '''
                SELECT COALESCE(NULLIF(model, ''), 'Light Aircraft') as name, COUNT(DISTINCT hex) as flight_count
                FROM aircraft_history
                WHERE model IS NOT NULL AND model != '' AND timestamp >= ?
                GROUP BY name
                ORDER BY flight_count DESC
                LIMIT 5
            '''
            if DB_TYPE == "postgres": q_top_models = q_top_models.replace("?", "%s")
            top_models = execute_query(conn, q_top_models, (cutoff,)) or []

            # 7. Recent Military / Rare Aircraft in past 2 weeks
            q_military = '''
                SELECT DISTINCT hex, callsign, altitude, speed, model, operator, timestamp
                FROM aircraft_history
                WHERE is_military = 1 AND timestamp >= ?
                ORDER BY timestamp DESC
                LIMIT 5
            '''
            if DB_TYPE == "postgres": q_military = q_military.replace("?", "%s")
            military_flights = execute_query(conn, q_military, (cutoff,)) or []

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

if __name__ == '__main__':
    # Running securely on localhost port 8081 to avoid macOS AirPlay / port exhaustion collisions
    app.run(host='127.0.0.1', port=8081, debug=True)
