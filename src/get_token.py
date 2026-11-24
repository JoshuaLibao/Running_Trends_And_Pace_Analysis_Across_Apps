import requests
from dotenv import load_dotenv
from pathlib import Path
import os

_THIS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = _THIS_DIR.parent  # run_performance_analysis/

load_dotenv(PROJECT_ROOT / "data" / ".env.txt")

response = requests.post('https://www.strava.com/oauth/token', data={
    'client_id': os.getenv('STRAVA_CLIENT_ID'),
    'client_secret': os.getenv('STRAVA_CLIENT_SECRET'),
    'code': 'CODE_FROM_URL',
    'grant_type': 'authorization_code'
})
print(response.json())

