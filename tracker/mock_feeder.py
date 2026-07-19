import time
import json
import requests
import argparse
import random

# Real aircraft hex codes from your database so models show up!
HEX_CODES = ["aa3487", "a4fa61", "a7a809", "a00001", "a00002"]

def generate_mock_data():
    # Base location (New York City area)
    base_lat = 40.7128
    base_lon = -74.0060
    
    aircraft = []
    
    for i, hex_code in enumerate(HEX_CODES):
        # Move them slightly based on time to simulate flight
        offset = (time.time() % 1000) / 10000.0
        lat = base_lat + (random.random() * 0.5 - 0.25) + offset
        lon = base_lon + (random.random() * 0.5 - 0.25) + offset
        
        aircraft.append({
            "hex": hex_code,
            "flight": f"MOCK{i+1}",
            "lat": lat,
            "lon": lon,
            "alt_baro": 10000 + (i * 1500),
            "track": (i * 72 + offset * 1000) % 360,
            "speed": 400 + i * 20,
            "seen": 1
        })
        
    return {
        "now": time.time(),
        "messages": 100,
        "aircraft": aircraft
    }

def main():
    parser = argparse.ArgumentParser(description="Mock Feeder for Vercel/Supabase")
    parser.add_argument("--target-url", required=True, help="Your Vercel URL (e.g. https://your-project.vercel.app)")
    parser.add_argument("--secret", required=True, help="Your FEEDER_SECRET")
    args = parser.parse_args()

    print(f"Sending mock data to {args.target_url}/api/update...")
    
    while True:
        data = generate_mock_data()
        try:
            headers = {
                "Content-Type": "application/json", 
                "Authorization": f"Bearer {args.secret}"
            }
            # Ensure URL doesn't have a trailing slash
            url = f"{args.target_url.rstrip('/')}/api/update"
            response = requests.post(url, json=data, headers=headers, timeout=5)
            
            if response.status_code == 200:
                print(f"✅ Successfully sent 5 mock planes to Vercel (which saves to Supabase)")
            else:
                print(f"❌ Failed! Status {response.status_code}: {response.text}")
                
        except Exception as e:
            print(f"⚠️ Network error: {e}")
            
        # Send data every 3 seconds
        time.sleep(3)

if __name__ == "__main__":
    main()
