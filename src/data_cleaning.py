import pandas as pd
import sqlite3
from datetime import datetime
import sys
from pathlib import Path
from pandas.core.series import pd_array

# Ensure local `src/` modules (like `tcx_to_csv.py`) are importable even when
# running this script from a different working directory.
_THIS_DIR = Path(__file__).resolve().parent
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))

from tcx_to_csv import write_summary_csv

strava_path = r'C:\Users\12063\Downloads\sqlite\strava_nike_performance_analysis\data\raw\activities.csv'
db_path = r'C:\Users\12063\Downloads\sqlite\strava_nike_performance_analysis\data\strava_data.db'
conn = sqlite3.connect(db_path)

strava_raw = pd.read_csv(strava_path)
clean_strava_data = strava_raw.copy()

# confirm all values are whole numbers in duplicate column -> clear to delete column
elapsed_time_check = (clean_strava_data['Elapsed Time.1'] % 1 == 0).all()
clean_strava_data = clean_strava_data.drop(clean_strava_data.columns[15], axis=1)

# standardize columns
clean_strava_data.columns = (
    clean_strava_data.columns
        .str.strip()
        .str.lower()
        .str.replace(" ", "_")
        .str.replace("-", "_")
)

clean_strava_data['activity_date'] = pd.to_datetime(
    clean_strava_data['activity_date'],
    format= "%b %d, %Y, %I:%M:%S %p",
)
clean_strava_data = clean_strava_data.rename(columns={
    'activity_date':'timestamp', 
    'elapsed_time':'total_time_min',
    'distance':'distance_miles',
    'average_heart_rate':'avg_hr_bpm',
    'max_heart_rate.1':'max_hr_bpm'
    })
clean_strava_data['activity_type'] = clean_strava_data['activity_type'].replace('Run','Running')
clean_strava_data['source'] = 'Strava'

# standardize time units: seconds -> minutes
clean_strava_data['total_time_min'] = clean_strava_data['total_time_min'] / 60

# validate: date, time, distance and activity type
now = pd.Timestamp.now()
clean_strava_data = clean_strava_data[
    (clean_strava_data['timestamp'] <= now) &
    (clean_strava_data['total_time_min'] > 0) &
    (clean_strava_data['distance_miles'] > 0) &
    (clean_strava_data['activity_type'] == 'Running')
]
strava_raw.to_sql('strava_raw', conn, if_exists='replace', index=False)
clean_strava_data.to_sql('clean_strava_data', conn, if_exists='replace', index=False)

input_dir = r'C:\Users\12063\Downloads\sqlite\strava_nike_performance_analysis\data\raw\tcx'
output_csv = r'C:\Users\12063\Downloads\sqlite\strava_nike_performance_analysis\data\raw\nrc_tcx_summary.csv'

write_summary_csv(input_dir, output_csv)

nike_path = r'C:\Users\12063\Downloads\sqlite\strava_nike_performance_analysis\data\raw\nrc_tcx_summary.csv'
nike_raw = pd.read_csv(nike_path)
clean_nike_data = nike_raw.copy()

# standardize nike run club data
clean_nike_data = clean_nike_data.rename(columns={
    'start_time_utc':'timestamp',
    'average_cadence_rpm':'average_cadence',
    'sport':'activity_type',
    'distance_m':'distance_miles',
    'total_time_s':'total_time_min'
    })
clean_nike_data['timestamp'] = pd.to_datetime(
    clean_nike_data['timestamp']
)
clean_nike_data['source'] = 'Nike'

# convert timestamp column from: timezone-aware -> timezone-native
clean_nike_data['timestamp'] = clean_nike_data['timestamp'].dt.tz_localize(None)

# separate start date and time
clean_nike_data['start_date'] = clean_nike_data['timestamp'].dt.date
clean_nike_data['start_time'] = clean_nike_data['timestamp'].dt.time

# standardize distance units: meters -> miles
clean_nike_data['distance_miles'] = clean_nike_data['distance_miles'] / 1609.34

# standardize time units: seconds -> minutes
clean_nike_data['total_time_min'] = clean_nike_data['total_time_min'] / 60

# validate nike run club data
clean_nike_data = clean_nike_data[
    (clean_nike_data['timestamp'] <= now) &
    (clean_nike_data['total_time_min'] > 0) &
    (clean_nike_data['distance_miles'] > 0) &
    (clean_nike_data['activity_type'] == 'Running')
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

# assign week number to run entries
combined_datasets_final['week'] = pd.to_datetime(combined_datasets_final['timestamp']).dt.isocalendar().week
combined_datasets_final['week'] = combined_datasets_final['week'].astype(int)
combined_datasets_final['month'] = pd.to_datetime(combined_datasets_final['timestamp']).dt.month.astype(int)
combined_datasets_final['year'] = pd.to_datetime(combined_datasets_final['timestamp']).dt.year
combined_datasets_final['year'] = combined_datasets_final['year'].astype(int)
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

# !!!!!!!!!!reformat output later!!!!!!!!!!!!!!!
weekly_summary = pd.read_sql("""
    SELECT year, week, COUNT(activity_type) AS total_runs, SUM(distance_miles) AS total_distance, AVG(pace) AS average_pace
    FROM combined_datasets_final
    GROUP BY year, week
    ORDER BY year, week
""", conn)
weekly_summary.to_sql('weekly_summary', conn, if_exists='replace', index=False)

# !!!!!!!!!!reformat output later!!!!!!!!!!!!!!!!
monthly_summary = pd.read_sql("""
    SELECT year, month, COUNT(activity_type) AS total_runs, SUM(distance_miles) AS total_distance, AVG(pace) AS average_pace
    FROM combined_datasets_final
    GROUP BY year, month
    ORDER BY year, month
""", conn)
monthly_summary.to_sql('monthly_summary', conn, if_exists='replace', index=False)

# combined datasets table with row number column
source_main = pd.read_sql("""
    SELECT source, row_number
    FROM (
        SELECT timestamp, distance_miles, total_time_min, activity_type, total_time_min/distance_miles AS pace, source,
        ROW_NUMBER() OVER(
            PARTITION BY(timestamp) 
            ) AS row_number
        FROM combined_datasets 
        )
""", conn)
source_main.to_sql('source_main', conn, if_exists='replace', index=False)

source_comparison_table = pd.read_sql("""
    SELECT 
    SUM(CASE WHEN source = 'Nike' THEN 1 ELSE 0 END) AS total_runs_nike,
    SUM(CASE WHEN source = 'Strava' THEN 1 ELSE 0 END) AS total_runs_strava,
    SUM(CASE WHEN row_number > 1 THEN 1 ELSE 0 END) AS duplicate_runs
    FROM source_main
""", conn)
source_comparison_table.to_sql('source_comparison_table', conn, if_exists='replace', index=False)
