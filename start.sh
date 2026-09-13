#!/bin/bash
wget -q https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 -O cloudflared
chmod +x cloudflared

# Tunnel ကို background မှာ run ပြီး URL ကို log file ထဲ ရေးမယ်
./cloudflared tunnel --url http://localhost:3095 --logfile tunnel.log &

python app.py
