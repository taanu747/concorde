#!/bin/bash

echo "=========================================="
echo " Concorde Flight Tracker - Pi Setup Script"
echo "=========================================="
echo ""

echo "[1/4] Installing dump1090-fa (Antenna Decoder)..."
sudo apt update
sudo apt install -y dump1090-fa

echo ""
echo "[2/4] Installing Python dependencies..."
sudo apt install -y python3-pip python3-requests python3-venv

echo ""
echo "[3/4] Creating Python Virtual Environment (Best Practice)..."
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

echo ""
echo "[4/4] Starting FlightRadar24 installation..."
echo "You will now be guided through the official FR24 setup."
echo "When asked for the receiver type, select '1 - DVB-T stick (dump1090)'"
echo "If asked if you want to start dump1090 automatically, select 'yes'"
echo ""
sleep 3
wget -qO- https://fr24.com/install.sh | sudo bash -s

echo ""
echo "=========================================="
echo " SETUP COMPLETE! "
echo "=========================================="
echo "Option A) Run manually in terminal:"
echo "  source venv/bin/activate"
echo "  python3 feeder.py --target-url https://[YOUR-VERCEL-URL].vercel.app --secret [YOUR-SECRET]"
echo ""
echo "Option B) Run as background service (auto-starts on boot):"
echo "  1. Edit concorde-feeder.service to set your VERCEL_URL and FEEDER_SECRET"
echo "  2. sudo cp concorde-feeder.service /etc/systemd/system/"
echo "  3. sudo systemctl daemon-reload"
echo "  4. sudo systemctl enable --now concorde-feeder"
echo ""
