# How to Contribute Flight Data to SkySync

You can help expand the coverage map of this flight tracker by setting up your own antenna and streaming live aircraft data to our server! It takes about 15 minutes to set up.

## What You Need
1. A **Raspberry Pi** (Any model, Zero W or newer works fine) running Raspberry Pi OS.
2. An **RTL-SDR USB Dongle** (Available on Amazon for ~$30).
3. A **1090 MHz Antenna** (Often comes bundled with the SDR dongle).
4. The **Server URL** and **Feeder Secret Key** (Ask the server admin for these!).

---

## Step 1: Clone the Repository
Open the terminal on your Raspberry Pi and clone this repository to get the feeder scripts:

```bash
git clone https://github.com/taanu747/concorde.git
cd concorde/pi_feeder
```

## Step 2: Run the Setup Script
We've included an automated setup script that will install the radio drivers (`dump1090-fa`) and set up the Python environment for you.

```bash
# Run the automated installer
sudo bash setup_pi.sh
```
*Note: This script will install the antenna drivers and guide you through the FlightRadar24 setup if you also want to feed them.*

## Step 3: Test the Feeder
Before setting it to run forever in the background, let's test it manually to make sure planes are appearing on the server!

You will need the `Server URL` (e.g., `https://your-app.onrender.com`) and the secret password to push data.

```bash
# Activate the python environment
source venv/bin/activate

# Start the feeder manually
python3 feeder.py --target-url https://[YOUR-SERVER-URL] --secret [YOUR-SECRET-KEY]
```
If successful, you will see output like `Pushed 42 planes -> OK` every 1.5 seconds. Check the live website to see your planes! Once confirmed, press `Ctrl+C` to stop the manual test.

---

## Step 4: Run Automatically in the Background (24/7)
To ensure the feeder runs automatically even if your Raspberry Pi reboots, you should set it up as a system service.

1. **Edit the Service File:**
   Open the `concorde-feeder.service` file in a text editor:
   ```bash
   nano concorde-feeder.service
   ```
   Find the line starting with `ExecStart=` and replace the placeholder URL and Secret with your actual values. Save and exit (Ctrl+X, then Y).

2. **Install and Enable the Service:**
   ```bash
   sudo cp concorde-feeder.service /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable --now concorde-feeder
   ```

3. **Check the Status:**
   You can verify it's running silently in the background with:
   ```bash
   sudo systemctl status concorde-feeder
   ```

That's it! You are now permanently contributing live airspace data to the map!
