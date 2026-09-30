#!/usr/bin/env bash
pkg update -y && pkg upgrade -y
pkg install -y python python-pillow git clang make libffi openssl
pip install --upgrade pip
pip install -r requirements.txt
[ -f .env ] || echo "BOT_TOKEN=isi_token_bot" > .env
echo "Selesai. Edit .env lalu jalankan: python bot_dor_v2.py"
