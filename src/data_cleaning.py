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
strava_raw = pd.read_csv(strava_path)

# confirm all values are whole numbers in duplicate column -> clear to delete column
elapsed_time_check = (strava_raw['Elapsed Time.1'] % 1 == 0).all()
strava_raw = strava_raw.drop(strava_raw.columns[15], axis=1)

# standardize columns
strava_raw.columns = (
    strava_raw.columns
        .str.strip()
        .str.lower()
        .str.replace(" ", "_")
        .str.replace("-", "_")
)

strava_raw['activity_date'] = pd.to_datetime(
    strava_raw['activity_date'],
    format= "%b %d, %Y, %I:%M:%S %p",
)
strava_raw = strava_raw.rename(columns={
    'activity_date':'timestamp', 
    'elapsed_time':'total_time_min',
    'distance':'distance_miles',
    'average_heart_rate':'avg_hr_bpm',
    'max_heart_rate.1':'max_hr_bpm'
    })
strava_raw['activity_type'] = strava_raw['activity_type'].replace('Run','Running')
strava_raw['source'] = 'Strava'

# standardize time units: seconds -> minutes
strava_raw['total_time_min'] = strava_raw['total_time_min'] / 60

# validate: date, time, distance and activity type
now = pd.Timestamp.now()
clean_strava_data = strava_raw[
    (strava_raw['timestamp'] <= now) &
    (strava_raw['total_time_min'] > 0) &
    (strava_raw['distance_miles'] > 0) &
    (strava_raw['activity_type'] == 'Running')
]
strava_raw.to_sql('strava_raw', conn, if_exists='replace', index=False)
clean_strava_data.to_sql('clean_strava_data', conn, if_exists='replace', index=False)

input_dir = r'C:\Users\12063\Downloads\sqlite\strava_nike_performance_analysis\data\raw\tcx'
output_csv = r'C:\Users\12063\Downloads\sqlite\strava_nike_performance_analysis\data\processed\nrc_tcx_summary.csv'

write_summary_csv(input_dir, output_csv)

nike_path = r'C:\Users\12063\Downloads\sqlite\strava_nike_performance_analysis\data\processed\nrc_tcx_summary.csv'
nike_raw = pd.read_csv(nike_path)

# standardize nike run club data
nike_raw = nike_raw.rename(columns={
    'start_time_utc':'timestamp',
    'average_cadence_rpm':'average_cadence',
    'sport':'activity_type',
    'distance_m':'distance_miles',
    'total_time_s':'total_time_min'
    })
nike_raw['timestamp'] = pd.to_datetime(
    nike_raw['timestamp']
)
nike_raw['source'] = 'Nike'

# convert timestamp column from: timezone-aware -> timezone-native
nike_raw['timestamp'] = nike_raw['timestamp'].dt.tz_localize(None)

# separate start date and time
nike_raw['start_date'] = nike_raw['timestamp'].dt.date
nike_raw['start_time'] = nike_raw['timestamp'].dt.time

# standardize distance units: meters -> miles
nike_raw['distance_miles'] = nike_raw['distance_miles'] / 1609.34

# standardize time units: seconds -> minutes
nike_raw['total_time_min'] = nike_raw['total_time_min'] / 60

# validate nike run club data
clean_nike_data = nike_raw[
    (nike_raw['timestamp'] <= now) &
    (nike_raw['total_time_min'] > 0) &
    (nike_raw['distance_miles'] > 0) &
    (nike_raw['activity_type'] == 'Running')
]

# standardize timestamp entry format
clean_nike_data['timestamp'] = clean_nike_data['timestamp'].dt.strftime("%Y-%m-%d %H:%M:%S")

nike_raw.to_sql('nike_raw', conn, if_exists='replace', index=False)
clean_nike_data.to_sql('clean_nike_data', conn, if_exists='replace', index=False)

combined_datasets = pd.read_sql("""
    SELECT timestamp, distance_miles, total_time_min, activity_type, total_time_min/distance_miles AS pace, source
    FROM clean_strava_data
    UNION ALL
    SELECT timestamp, distance_miles, total_time_min, activity_type, total_time_min/distance_miles AS pace, source
    FROM clean_nike_data
""", conn)
combined_datasets.to_sql('combined_datasets', conn, if_exists='replace', index=False)

combined_datasets_final = pd.read_sql("""
    SELECT timestamp, distance_miles, total_time_min, activity_type, pace
    FROM ( 
        SELECT timestamp, distance_miles, total_time_min, activity_type, total_time_min/distance_miles AS pace, source, 
        ROW_NUMBER() OVER(
            PARTITION BY(timestamp) 
            ORDER BY CASE 
                WHEN source = 'Strava' THEN 0
                ELSE 1
            END 
            ) AS row_number
        FROM combined_datasets 
        )
    WHERE row_number = 1
""", conn)
combined_datasets_final.to_sql('combined_datasets_final', conn, if_exists='replace', index=False)

dashboard_overview = pd.read_sql("""
    SELECT COUNT(activity_type) AS total_runs, 
        SUM(distance_miles) AS total_distance_miles, 
        SUM(total_time_min) AS total_duration_min,
        AVG(pace) AS average_pace, 
        MIN(pace) AS fastest_pace, 
        MAX(pace) AS slowest_pace,
        MIN(timestamp) AS first_run_date,
        MAX(timestamp) AS last_run_date
    FROM combined_datasets_final
""", conn)

dashboard_overview.to_sql('dashboard_overview', conn, if_exists='replace', index=False)
