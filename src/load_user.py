import sqlite3, requests
from dotenv import load_dotenv
from pathlib import Path
import os

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / "data" / ".env.txt")

# get your athlete ID
response = requests.post('https://www.strava.com/oauth/token', data={
    'client_id': os.getenv('STRAVA_CLIENT_ID'),
    'client_secret': os.getenv('STRAVA_CLIENT_SECRET'),
    'refresh_token': os.getenv('STRAVA_REFRESH_TOKEN'),
    'grant_type': 'refresh_token'
})

tokens = response.json()
athlete_id = 116622583
refresh_token = os.getenv('STRAVA_REFRESH_TOKEN')

# insert yourself into the DB
conn = sqlite3.connect(PROJECT_ROOT / "data" / "runs_summary.db")
conn.execute('INSERT INTO users (external_athlete_id, source, refresh_token) VALUES (?, ?, ?)',
             (athlete_id, 'Strava', refresh_token))
conn.commit()
conn.close()

print('Done! Athlete ID:', athlete_id)