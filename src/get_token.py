import requests
from dotenv import load_dotenv
from pathlib import Path
import os

load_dotenv(Path(r'C:\Users\12063\Downloads\sqlite\run_performance_analysis\data\.env.txt'))

response = requests.post('https://www.strava.com/oauth/token', data={
    'client_id': os.getenv('STRAVA_CLIENT_ID'),
    'client_secret': os.getenv('STRAVA_CLIENT_SECRET'),
    'code': 'CODE_FROM_URL',
    'grant_type': 'authorization_code'
})
print(response.json())

