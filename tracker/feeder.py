import time
import json
import requests
import os
import argparse

def main():
    parser = argparse.ArgumentParser(description="Push local dump1090 data to Render backend.")
    parser.add_argument("--url", default="https://flight-tracker-8qg2.onrender.com", help="Render URL")
    parser.add_argument("--secret", default="changeme", help="Secret token for authentication")
    parser.add_argument("--file", default="history/aircraft.json", help="Path to aircraft.json")
    parser.add_argument("--interval", type=float, default=1.5, help="Polling interval in seconds")
    args = parser.parse_args()

    endpoint = f"{args.url.rstrip('/')}/api/update"
    
    print(f"Starting feeder...")
    print(f"Target: {endpoint}")
    print(f"File:   {args.file}")
    
    while True:
        try:
            if not os.path.exists(args.file):
                print(f"Waiting for {args.file} to be created by dump1090...")
                time.sleep(args.interval)
                continue
                
            with open(args.file, 'r') as f:
                data = json.load(f)
                
            plane_count = len(data.get("aircraft", []))
                
            response = requests.post(
                endpoint,
                json=data,
                headers={"Authorization": f"Bearer {args.secret}"},
                timeout=5
            )
            
            if response.status_code == 200:
                print(f"[{time.strftime('%H:%M:%S')}] Pushed {plane_count} planes -> OK")
            else:
                print(f"[{time.strftime('%H:%M:%S')}] Failed: HTTP {response.status_code} - {response.text.strip()}")
                
        except json.JSONDecodeError:
            # File might be mid-write by dump1090
            pass
        except requests.RequestException as e:
            print(f"[{time.strftime('%H:%M:%S')}] Network error: {e}")
        except Exception as e:
            print(f"[{time.strftime('%H:%M:%S')}] Error: {e}")
            
        time.sleep(args.interval)

if __name__ == "__main__":
    main()
