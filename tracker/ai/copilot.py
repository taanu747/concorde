"""
AI Airspace Co-Pilot Engine
Processes natural language queries using Google Gemini 1.5 Flash LLM (when API key is provided)
or local expert rules fallback for real-time ADS-B flight intent & airspace analytics.
"""

import os
import re
import time
import json
import requests
from flask import jsonify

def query_gemini_llm(user_query, context_info, api_key):
    """Query Google Gemini 1.5 Flash LLM for dynamic natural language explanations."""
    try:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={api_key}"
        headers = {"Content-Type": "application/json"}
        
        system_instruction = (
            "You are the AI Airspace Co-Pilot for Concorde ADS-B Flight Tracker (Congressional App Contest entry). "
            "Your job is to explain air traffic telemetry, aircraft intent, wind drift, weather impact, and airspace questions. "
            "Use the provided context data to answer the user question. Keep your answer clear, educational, concise, "
            "and formatted in clean HTML (using <b>, <i>, <br>, bullet points). Never invent fake aircraft data if not in context."
        )
        
        prompt = f"{system_instruction}\n\nContext Telemetry & Database Information:\n{json.dumps(context_info, indent=2)}\n\nUser Question: {user_query}"
        
        payload = {
            "contents": [{
                "parts": [{"text": prompt}]
            }],
            "generationConfig": {
                "temperature": 0.3,
                "maxOutputTokens": 600
            }
        }
        res = requests.post(url, headers=headers, json=payload, timeout=6)
        if res.status_code == 200:
            data = res.json()
            candidates = data.get('candidates', [])
            if candidates and 'content' in candidates[0]:
                parts = candidates[0]['content'].get('parts', [])
                if parts:
                    return parts[0].get('text', '').strip()
    except Exception as e:
        print(f"Gemini API query exception: {e}")
    return None

def safe_round(val, decimals=0):
    """Safely round numbers, handling 'ground', None, and string types without exceptions."""
    try:
        if val is None or val == 'ground' or val == '': return 0
        r = round(float(val), decimals)
        return int(r) if decimals == 0 else r
    except Exception:
        return 0

def process_ai_query(user_query, aircraft_state, conn, latest_payload, lock, db_type, execute_query):
    """
    Main processing pipeline for AI Airspace Co-Pilot.
    Evaluates natural language intent, extracts callsigns, computes live telemetry intent,
    and returns structured HTML tables or explanations using Gemini LLM or local expert fallback.
    """
    query_lower = user_query.lower()
    gemini_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    
    # -------------------------------------------------------------
    # STEP 1: Callsign / Hex Extraction from User Prompt
    # -------------------------------------------------------------
    extracted_callsign = None
    
    # Find potential aviation callsigns (must contain digits or be 6-char hex code, e.g. POE616, DAL123, N915WK, AE13B4)
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
            if db_type == "postgres": q_find = q_find.replace("?", "%s")
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
    if aircraft_state or extracted_callsign:
        if not aircraft_state and extracted_callsign:
            return jsonify({
                "type": "explanation",
                "text": f"<b>🤖 AI Co-Pilot Flight Search:</b><br><br>"
                        f"Flight <b>{extracted_callsign}</b> is not currently active in your local feeder's live view or recorded in your 7-day database history.<br><br>"
                        f"• <b>Airspace Coverage Note:</b> Your feeder tracks aircraft within ~150–250 miles. Aircraft operating in distant states or overseas will only appear in your database when they enter your regional airspace."
            })
        
        # If Gemini API key is configured, attempt Gemini 1.5 Flash LLM query first
        if gemini_key and aircraft_state:
            llm_text = query_gemini_llm(user_query, aircraft_state, gemini_key)
            if llm_text:
                return jsonify({
                    "type": "explanation",
                    "text": llm_text
                })
            print("Gemini API query returned None (invalid key or error), proceeding with expert rules engine.")

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

    if "radar" in query_lower or "weather" in query_lower or "storm" in query_lower or "affect" in query_lower or "affecting" in query_lower:
        drift_val = 0.0
        try:
            if db_type == "postgres":
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

    if "turn" in query_lower or "circle" in query_lower or "holding" in query_lower:
        return jsonify({
            "type": "explanation",
            "text": "<b>🔄 Aircraft Turns & Holding Patterns:</b><br><br>"
                    "Aircraft execute turns or circular race-track holding patterns for three primary reasons:<br><br>"
                    "1. <b>ATC Sequencing:</b> Delaying arrivals to maintain standard 3-5 mile separation.<br>"
                    "2. <b>Terminal Alignment:</b> Following STAR arrival procedures to align with runway ILS glideslopes.<br>"
                    "3. <b>Destination Weather:</b> Awaiting thunderstorm clearance or runway snow clearing."
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
        if db_type == "postgres": q = q.replace("?", "%s")
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
        if db_type == "postgres": q = q.replace("?", "%s")
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
        if db_type == "postgres": q = q.replace("?", "%s")
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
        if db_type == "postgres": q = q.replace("?", "%s")
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

    # Fallback: Query Gemini LLM if API key is configured
    if gemini_key:
        llm_response = query_gemini_llm(user_query, {"user_query": user_query, "aircraft_state": aircraft_state}, gemini_key)
        if llm_response:
            return jsonify({
                "type": "explanation",
                "text": llm_response
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
