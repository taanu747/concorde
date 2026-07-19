import re

with open("tracker/app.py", "r") as f:
    code = f.read()

# 1. Remove aircraft_db, latest_aircraft_data, last_db_write_time, load_aircraft_db
code = re.sub(r'# Global in-memory dictionary.*?\nFEEDER_SECRET', 'FEEDER_SECRET', code, flags=re.DOTALL)
code = re.sub(r'def load_aircraft_db\(\):.*?\n# Load database on startup\nload_aircraft_db\(\)', '', code, flags=re.DOTALL)

# 2. Update init_db to add latest_payload and aircraft_metadata (for SQLite fallback/creation)
old_init_db = """        # Create indexes for faster search
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_hex ON aircraft_history(hex)')"""
new_init_db = """        # Create stateless tables
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
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_hex ON aircraft_history(hex)')"""
code = code.replace(old_init_db, new_init_db)

# 3. Update /api/update to be stateless
old_update = """    global latest_aircraft_data, last_db_write_time
    latest_aircraft_data = request.json
    
    current_time = time.time()
    if current_time - last_db_write_time >= 5:
        last_db_write_time = current_time
        try:
            if latest_aircraft_data and 'aircraft' in latest_aircraft_data:
                with get_db_connection() as conn:
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
                    conn.commit()
        except Exception as e:
            print(f"Error saving to DB: {e}")"""
new_update = """    payload = request.json
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
        print(f"Error saving to DB: {e}")"""
code = code.replace(old_update, new_update)

# 4. Update /api/data to fetch from DB
old_data = """    # Create a shallow copy to safely iterate
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
        return jsonify({"error": str(e)}), 500"""

new_data = """    try:
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
        return jsonify({"error": str(e)}), 500"""
code = code.replace(old_data, new_data)

with open("tracker/test_app2.py", "w") as f:
    f.write(code)

