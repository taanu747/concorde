import time
import json
import requests
import argparse
import os

def main():
    parser = argparse.ArgumentParser(description="Push Raspberry Pi dump1090 data to Cloud backend.")
    parser.add_argument("--target-url", required=True, help="Your Vercel backend URL (e.g. https://your-app.vercel.app)")
    parser.add_argument("--secret", required=True, help="Secret token for authentication")
    # By default, dump1090-fa writes its live JSON to the Raspberry Pi's RAM disk
    parser.add_argument("--source-file", default="/run/dump1090-fa/aircraft.json", help="Local dump1090 JSON file path")
    parser.add_argument("--interval", type=float, default=1.5, help="Polling interval in seconds")
    args = parser.parse_args()

    endpoint = f"{args.target_url.rstrip('/')}/api/update"
    
    print(f"Starting Raspberry Pi feeder...")
    print(f"Target: {endpoint}")
    print(f"Source: Local file {args.source_file}")
    
    while True:
        try:
            data = None
            if os.path.exists(args.source_file):
                with open(args.source_file, "r") as f:
                    data = json.load(f)
            else:
                print(f"[{time.strftime('%H:%M:%S')}] Waiting for file {args.source_file} to be created by dump1090-fa, make sure it's on")
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
            pass # File might be written to concurrently, ignore and try again next loop
        except requests.RequestException as e:
            print(f"[{time.strftime('%H:%M:%S')}] Network error when pushing to target: {e}")
        except Exception as e:
            print(f"[{time.strftime('%H:%M:%S')}] Error: {e}")
            
        time.sleep(args.interval)

if __name__ == "__main__":
    main()
