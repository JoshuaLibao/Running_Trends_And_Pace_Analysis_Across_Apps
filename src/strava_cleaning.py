import pandas as pd
import sqlite3
from datetime import datetime
import sys
from pathlib import Path

# Ensure local `src/` modules (like `tcx_to_csv.py`) are importable even when
# running this script from a different working directory.
_THIS_DIR = Path(__file__).resolve().parent
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))

from tcx_to_csv import write_summary_csv

db_path = r'C:\Users\12063\Downloads\sqlite\strava_nike_performance_analysis\data\strava_data.db'
conn = sqlite3.connect(db_path)

strava_path = r'C:\Users\12063\Downloads\sqlite\strava_nike_performance_analysis\data\raw\activities.csv'
df = pd.read_csv(strava_path)

#change duplicate column name
cols = list(df.columns)

# confirm all values are whole numbers in duplicate column -> clear to delete column
elapsed_time_check = (df['Elapsed Time.1'] % 1 == 0).all()
df = df.drop(df.columns[15], axis=1)

# standardize columns to snake_case
df.columns = (
    df.columns
        .str.strip()
        .str.lower()
        .str.replace(" ", "_")
        .str.replace("-", "_")
)

# validate: date, time, distance and activity type

df['activity_date'] = pd.to_datetime(
    df['activity_date'],
    format= "%b %d, %Y, %I:%M:%S %p",
)

today = pd.Timestamp.today()
clean_strava_data = df[
    (df['activity_date'] <= today) &
    (df['elapsed_time'] > 0) &
    (df['distance'] > 0) &
    (df['activity_type'] == 'Run')
]
clean_strava_data.to_sql('clean_strava_data', conn, if_exists='replace', index=False)

input_dir = r'C:\Users\12063\Downloads\sqlite\strava_nike_performance_analysis\data\raw\tcx'
output_csv = r'C:\Users\12063\Downloads\sqlite\strava_nike_performance_analysis\data\processed\nrc_tcx_summary.csv'

write_summary_csv(input_dir, output_csv)