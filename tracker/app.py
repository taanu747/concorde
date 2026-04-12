import os
import json
import csv
import time
import urllib.request
import shutil
from flask import Flask, jsonify, render_template

app = Flask(__name__)

# The path to the aircraft.json file that dump1090 creates.
# Make sure to run dump1090 in the same directory, or update this path!
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
AIRCRAFT_FILE = os.path.join(BASE_DIR, 'history', 'aircraft.json')
DB_FILE = os.path.join(BASE_DIR, 'aircraftDatabase.csv')
DB_URL = "https://opensky-network.org/datasets/metadata/aircraftDatabase.csv"

# Global in-memory dictionary for fast lookups
aircraft_db = {}

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

@app.route('/api/data')
def get_aircraft_data():
    """Read aircraft.json, filter stale data, and return as JSON."""
    if not os.path.exists(AIRCRAFT_FILE):
        return jsonify({"error": f"File {AIRCRAFT_FILE} not found."}), 404

    try:
        with open(AIRCRAFT_FILE, 'r') as f:
            data = json.load(f)
            
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
    except json.JSONDecodeError:
        # File might be mid-write by dump1090
        return jsonify({"error": "Failed to parse JSON (file may be mid-update)"}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == '__main__':
    # Running securely on localhost port 8081 to avoid macOS AirPlay / port exhaustion collisions
    app.run(host='127.0.0.1', port=8081, debug=True)
