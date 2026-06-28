import os
import json
import csv
import time
import urllib.request
import shutil
import sqlite3
import threading
from flask import Flask, jsonify, render_template, request

app = Flask(__name__)

# The path to the aircraft.json file that dump1090 creates.
# Make sure to run dump1090 in the same directory, or update this path!
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
AIRCRAFT_FILE = os.path.join(BASE_DIR, 'history', 'aircraft.json')
DB_FILE = os.path.join(BASE_DIR, 'aircraftDatabase.csv')
DB_URL = "https://opensky-network.org/datasets/metadata/aircraftDatabase.csv"
SQLITE_DB_FILE = os.path.join(BASE_DIR, 'aircraft_history.db')

# Global in-memory dictionary for fast lookups
aircraft_db = {}
latest_aircraft_data = {}
last_db_write_time = 0
FEEDER_SECRET = os.environ.get("FEEDER_SECRET", "changeme")

def load_aircraft_db():
    global aircraft_db
    if not os.path.exists(DB_FILE):
        print("Downloading OpenSky aircraft database... (This may take a minute)")
        try:
            import ssl
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            
            req = urllib.request.Request(DB_URL, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, context=ctx) as response, open(DB_FILE, 'wb') as out_file:
                shutil.copyfileobj(response, out_file)
            print("Download complete!")
        except Exception as e:
            print(f"Failed to download database: {e}")
            return

    print("Loading OpenSky aircraft database into memory...")
    try:
        with open(DB_FILE, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                icao = row.get('icao24', '').strip().lower()
                if icao:
                    aircraft_db[icao] = {
                        'registration': row.get('registration', ''),
                        'model': row.get('model', ''),
                        'typecode': row.get('typecode', ''),
                        'operator': row.get('operator', '')
                    }
        print(f"Loaded {len(aircraft_db)} aircraft records.")
    except Exception as e:
        print(f"Error reading database: {e}")

# Load database on startup
load_aircraft_db()

def init_db():
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
        
    global latest_aircraft_data, last_db_write_time
    latest_aircraft_data = request.json
    
    current_time = time.time()
    if current_time - last_db_write_time >= 5:
        last_db_write_time = current_time
        try:
            if latest_aircraft_data and 'aircraft' in latest_aircraft_data:
                with sqlite3.connect(SQLITE_DB_FILE) as conn:
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
                    conn.commit()
        except Exception as e:
            print(f"Error saving to DB: {e}")

    return jsonify({"status": "success", "aircraft_count": len(request.json.get('aircraft', []))})

@app.route('/api/data')
def get_aircraft_data():
    """Return live aircraft data from memory, enriched with database details."""
    # Create a shallow copy to safely iterate
    data = dict(latest_aircraft_data) if latest_aircraft_data else {}
    if not data:
        return jsonify({"aircraft": []})

    try:
            
        # Filter out planes that haven't been seen recently.
        # dump1090 adds a 'seen' or 'seen_pos' field indicating seconds since last update.
        active_aircraft = []
        if 'aircraft' in data:
            for plane in data['aircraft']:
                seen = plane.get('seen', 0)
                # Keep if we've seen a message from this plane in the last 15 seconds
                if seen < 15:
                    # Enrich with database details
                    hex_code = plane.get('hex', '').lower()
                    if hex_code in aircraft_db:
                        db_info = aircraft_db[hex_code]
                        if db_info['registration']: plane['registration'] = db_info['registration']
                        if db_info['model']: plane['model'] = db_info['model']
                        if db_info['typecode']: plane['typecode'] = db_info['typecode']
                        if db_info['operator']: plane['operator'] = db_info['operator']
                        
                    active_aircraft.append(plane)
            
            data['aircraft'] = active_aircraft
            
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/search')
def search_aircraft():
    query = request.args.get('query', '').strip()
    if not query:
        return jsonify([])
    
    try:
        with sqlite3.connect(SQLITE_DB_FILE) as conn:
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
            
            results = [dict(row) for row in cursor.fetchall()]
            return jsonify(results)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/history')
def get_aircraft_history():
    hex_code = request.args.get('hex', '').strip().lower()
    if not hex_code:
        return jsonify([])
        
    try:
        with sqlite3.connect(SQLITE_DB_FILE) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute('''
                SELECT lat, lon, altitude, heading, timestamp 
                FROM aircraft_history 
                WHERE hex = ? 
                ORDER BY timestamp ASC
            ''', (hex_code,))
            
            results = [dict(row) for row in cursor.fetchall()]
            return jsonify(results)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/analytics/heatmap')
def get_heatmap_data():
    try:
        with sqlite3.connect(SQLITE_DB_FILE) as conn:
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
            results = [[row['r_lat'], row['r_lon'], row['intensity']] for row in cursor.fetchall()]
            return jsonify(results)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/analytics/weather-deviations')
def get_weather_deviations():
    try:
        with sqlite3.connect(SQLITE_DB_FILE) as conn:
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
            
            results = [dict(row) for row in cursor.fetchall()]
            return jsonify(results)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == '__main__':
    # Running securely on localhost port 8081 to avoid macOS AirPlay / port exhaustion collisions
    app.run(host='127.0.0.1', port=8081, debug=True)
