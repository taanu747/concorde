import time
import json
import requests
import argparse
import os

def main():
    parser = argparse.ArgumentParser(description="Push local dump1090 data to Cloud backend.")
    parser.add_argument("--target-url", default="https://flight-tracker-8qg2.onrender.com", help="Cloud backend URL")
    parser.add_argument("--secret", default="changeme", help="Secret token for authentication")
    parser.add_argument("--source-url", default="http://localhost:8080/data/aircraft.json", help="Local dump1090 JSON URL")
    parser.add_argument("--source-file", help="Local dump1090 JSON file path (e.g., history/aircraft.json)")
    parser.add_argument("--interval", type=float, default=1.5, help="Polling interval in seconds")
    args = parser.parse_args()

    endpoint = f"{args.target_url.rstrip('/')}/api/update"
    
    print(f"Starting feeder...")
    print(f"Target: {endpoint}")
    if args.source_file:
        print(f"Source: Local file {args.source_file}")
    else:
        print(f"Source: URL {args.source_url}")
    
    while True:
        try:
            data = None
            if args.source_file:
                if os.path.exists(args.source_file):
                    with open(args.source_file, "r") as f:
                        data = json.load(f)
                else:
                    print(f"[{time.strftime('%H:%M:%S')}] Waiting for file {args.source_file} to be created...")
                    time.sleep(args.interval)
                    continue
            else:
                try:
                    source_response = requests.get(args.source_url, timeout=2)
                    source_response.raise_for_status()
                    data = source_response.json()
                except requests.RequestException as e:
                    print(f"[{time.strftime('%H:%M:%S')}] Waiting for dump1090 at {args.source_url} (Error: {e})")
                    time.sleep(args.interval)
                    continue

            plane_count = len(data.get("aircraft", []))
                
            # Push data to cloud backend
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
            print(f"[{time.strftime('%H:%M:%S')}] Invalid JSON received from source.")
        except requests.RequestException as e:
            print(f"[{time.strftime('%H:%M:%S')}] Network error when pushing to target: {e}")
        except Exception as e:
            print(f"[{time.strftime('%H:%M:%S')}] Error: {e}")
            
        time.sleep(args.interval)

if __name__ == "__main__":
    main()
