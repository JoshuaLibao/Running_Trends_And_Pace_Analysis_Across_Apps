from multiprocessing import current_process
from typing import Any
import pandas as pd
import sqlite3
from datetime import datetime, timedelta
import sys
from pathlib import Path
import requests
import os
from dotenv import load_dotenv
import time

start_time = time.time()
 
# Ensure local `src/` modules (like `tcx_to_csv.py`) are importable even when
# running this script from a different working directory.
_THIS_DIR = Path(__file__).resolve().parent
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))
 
from tcx_to_csv import write_summary_csv
 
load_dotenv(Path(r'C:\Users\12063\Downloads\sqlite\run_performance_analysis\data\.env.txt'))
 
db_path = r'C:\Users\12063\Downloads\sqlite\run_performance_analysis\data\runs_summary.db'
conn = sqlite3.connect(db_path)

cursor = conn.cursor()
 
cursor.execute(
    "SELECT MAX(timestamp) FROM clean_strava_data"
)

row = cursor.fetchone()
last_sync = row[0] if row else None

MODE = "incremental" # "incremental" or "batch"

if MODE == "incremental":
    after_timestamp = last_sync
    after_timestamp = pd.to_datetime(after_timestamp)
else:
    after_timestamp = None

if last_sync:
    last_sync = pd.to_datetime(last_sync)

print("LAST SYNC:", last_sync, type(last_sync))
 
def get_access_token():
    response = requests.post('https://www.strava.com/oauth/token', data={
        'client_id': os.getenv('STRAVA_CLIENT_ID'),
        'client_secret': os.getenv('STRAVA_CLIENT_SECRET'),
        'refresh_token': os.getenv('STRAVA_REFRESH_TOKEN'),
        'grant_type': 'refresh_token'
    })
    return response.json()['access_token']

def get_activities(access_token, after_timestamp, conn):
    activities = []
    all_best_efforts = []
    activity_details_list = []
    processed_activity_details_list = []
    current_prs_list = pd.DataFrame()
    page = 1

    print("FILTER:", after_timestamp)

    while True:
        response = requests.get(
            'https://www.strava.com/api/v3/athlete/activities',
            headers={'Authorization': f'Bearer {access_token}'},
            params={'per_page': 200, 'page': page}
        )
        
        print(f"RESPONSE STATUS CODE: {response.status_code}")

        data = response.json()

        if not data:
            break
        
        print(f"PAGE {page} LENGTH:", len(data))

        processed_activity_details = pd.read_sql("""
            SELECT id, type
            FROM processed_activity_details
        """, conn)

        # turn processed_activity_ids into a set
        processed_activity_ids = set[Any](processed_activity_details["id"])

        read_rate = 0
        max_reads = 90       
        for act in data:
            if read_rate <= max_reads:
                activity_id = act["id"]
                activity_type = act["type"]

                if activity_id in processed_activity_ids:
                    print("activity already logged! moving onto next activity")
                    continue
                else:        
                    activity_response = requests.get(
                        f"https://www.strava.com/api/v3/activities/{activity_id}",
                        headers={"Authorization": f"Bearer {access_token}"}
                    )

                    print(f"ACTIVITY_RESPONSE STATUS CODE: {activity_response.status_code}")
                    print(f"Processing activity {activity_id}")
                    
                    activity_detail = activity_response.json()

                    processed_activity_details_list.append(activity_detail)
                    read_rate+=1
                    print(activity_response.headers.get("X-RateLimit-Usage"))
                    print(f"read rate limit: {read_rate}/{max_reads}")

                    if activity_detail.get("best_efforts"):
                        best_efforts_df = pd.DataFrame(activity_detail["best_efforts"])
                        best_efforts_df["activity_id"] = activity_detail["id"]
                        all_best_efforts.append(best_efforts_df)
                        print("best efforts logged")
                    else:
                        print("best_efforts is empty")
                        continue
            else:
                print("read rate limit reached! wait 15 minutes")
                print(f"read rate limit was hit on this day/time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
                break

        if all_best_efforts:
            all_best_efforts = pd.concat(all_best_efforts, ignore_index=True)
            current_prs_list = (
                all_best_efforts[all_best_efforts["pr_rank"] == 1]
                .sort_values("start_date")
                .groupby("distance", as_index=False)
                .first()
            )
        
        print(f"activity_type: {activity_type}")
        print(f"activity_detail data type: {type(activity_details_list)}")
        print(f"tuple length: {len(activity_details_list)}")
        
        # filters data after the last sync date
        if after_timestamp is not None:
            filtered_data = []

            if not isinstance(data, list):
                print("Strava error payload:", data)
                break   # or raise

            for act in data:
                
                act_time = datetime.fromisoformat(
                    act['start_date_local'].replace('Z', '+00:00')
                ).replace(tzinfo=None)

                print("----")
                print("ACTIVITY:", act_time)
                print("LAST_SYNC", after_timestamp)
                print("COMPARE:", act_time > after_timestamp if after_timestamp else "NO FILTER")

                if act_time > after_timestamp:
                    filtered_data.append(act)

            activities.extend(filtered_data)

        else:
            activities.extend(data)

        page += 1

    return activities, activity_details_list, processed_activity_details_list, current_prs_list

def process_strava(activities, processed_activity_details_list, current_prs_list):
    strava_raw = pd.DataFrame(activities)
    processed_activity_details_df = pd.DataFrame(processed_activity_details_list)
    current_prs_df = pd.DataFrame(current_prs_list)

    clean_strava_data = strava_raw.copy()

    # standardize columns
    clean_strava_data.columns = clean_strava_data.columns.astype(str)
    print(f"clean_strava_data columns dtypes: {clean_strava_data.columns.dtype}")

    clean_strava_data.columns = (
        clean_strava_data.columns
            .str.strip()
            .str.lower()
            .str.replace(" ", "_")
            .str.replace("-", "_")
    )

    # stable identifier for de-duping and DB joins
    if "id" not in clean_strava_data.columns:
        raise KeyError(f"Expected Strava 'id' field. Columns received: {list(clean_strava_data.columns)}")
    clean_strava_data["activity_id"] = clean_strava_data["id"]

    clean_strava_data['start_date_local'] = pd.to_datetime(
        clean_strava_data['start_date_local'],
        format= 'ISO8601',
    )
    clean_strava_data = clean_strava_data.rename(columns={
        'start_date_local':'timestamp', 
        'elapsed_time':'total_time_min',
        'distance':'distance_miles',
        'average_heartrate':'avg_hr_bpm',
        'max_heartrate':'max_hr_bpm',
        'sport_type':'activity_type'
        })
    clean_strava_data['activity_type'] = clean_strava_data['activity_type'].replace('Run','Running')
    clean_strava_data['source'] = 'Strava'

    # converted timestamp column from: timezone-aware -> timezone-native
    clean_strava_data['timestamp'] = clean_strava_data['timestamp'].dt.tz_localize(None)

    # standardized time units: seconds -> minutes
    clean_strava_data['total_time_min'] = clean_strava_data['total_time_min'] / 60

    # validate: date, time, distance and activity type
    clean_strava_data = clean_strava_data[
        (clean_strava_data['timestamp'] <= now) &
        (clean_strava_data['total_time_min'] > 0) &
        (clean_strava_data['distance_miles'] > 0) &
        (clean_strava_data['activity_type'] == 'Running')
    ]

    expected_cols = ['avg_hr_bpm', 'max_hr_bpm']

    for col in expected_cols:
        if col not in clean_strava_data.columns:
            clean_strava_data[col] = None

    clean_strava_data = clean_strava_data[[
        'activity_id',
        'timestamp', 'total_time_min', 'distance_miles',
        'activity_type', 'avg_hr_bpm', 'max_hr_bpm', 'source'
    ]]

    # standardized distance units: meters -> miles
    clean_strava_data['distance_miles'] = clean_strava_data['distance_miles'] / 1609.34

    processed_activity_details_df = processed_activity_details_df[[
        'id', 'type'
    ]]

    current_prs_df = current_prs_df[[
        'id', 'name', 'elapsed_time', 'distance' 
    ]]

    current_prs_df["elapsed_time_minutes"] = current_prs_df["elapsed_time"] / 60
    current_prs_df["distance_miles"] = current_prs_df["distance"] / 1609.34
    current_prs_df["pace"] = current_prs_df["elapsed_time_minutes"] / current_prs_df["distance_miles"]

    strava_raw = strava_raw.astype(str)

    return strava_raw, clean_strava_data, processed_activity_details_df, current_prs_df

def write_strava_data(strava_raw, clean_strava_data, processed_activity_details_df, current_prs_df, conn):
    if MODE == "batch":
        clean_strava_data.to_sql("clean_strava_data", conn, if_exists="replace", index=False)
        strava_raw.to_sql("strava_raw", conn, if_exists="replace", index=False)
        processed_activity_details_df.to_sql("processed_activity_details", conn, if_exists="replace", index=False)
        current_prs_df.to_sql("current_prs", conn, if_exists="replace",index=False)
    else:  # incremental
        clean_strava_data.to_sql("clean_strava_data", conn, if_exists="append", index=False)
        strava_raw.to_sql("strava_raw", conn, if_exists="append", index=False)
        clean_strava_data = clean_strava_data.drop_duplicates(subset=["activity_id"])
        existing_ids = pd.read_sql("SELECT activity_id FROM clean_strava_data", conn)
        clean_strava_data = clean_strava_data[
            ~clean_strava_data["activity_id"].isin(existing_ids["activity_id"])
        ]
        clean_strava_data.to_csv(
            r"C:\Users\12063\Downloads\sqlite\run_performance_analysis\data\processed\clean_strava_data.csv",
            index=False,)
        processed_activity_details_df.to_sql("processed_activity_details", conn, if_exists="append", index=False)
        current_prs_df.to_sql("current_prs", conn, if_exists="append",index=False)

t0 = time.time()
access_token = get_access_token()
t1 = time.time()
activities, activity_details_list, processed_activity_details_list, current_prs_list = get_activities(access_token, after_timestamp, conn)

print(f"Auth: {time.time() - t0}")
print(f"Strava API: {time.time() - t1} seconds")
now = pd.Timestamp.now()
print("TOTAL ACTIVITIES:", len(activities))

# check if there are any activities to process
if not activities:
    print("No new Strava activities. Skipping Strava processing.")
    clean_strava_data = None  # empty placeholder
    strava_raw = None
else:
    t3 = time.time()
    strava_raw, clean_strava_data, processed_activity_details_df, current_prs_df = process_strava(activities, processed_activity_details_list, current_prs_list)
    print(f"Strava processing: {time.time() - t3} seconds")
    write_strava_data(strava_raw, clean_strava_data, processed_activity_details_df, current_prs_df, conn)

input_dir = r'C:\Users\12063\Downloads\sqlite\run_performance_analysis\data\raw\tcx'
output_csv = r'C:\Users\12063\Downloads\sqlite\run_performance_analysis\data\raw\raw_nike.csv'

t4 = time.time()
write_summary_csv(input_dir, output_csv, conn)
print(f"TCX processing: {time.time() - t4:.2f} seconds")

nike_path = r'C:\Users\12063\Downloads\sqlite\run_performance_analysis\data\raw\raw_nike.csv'
nike_raw = pd.read_csv(nike_path)
clean_nike_data = nike_raw.copy()

# standardized nike run club data
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

# converted timestamp column from: timezone-aware -> timezone-native
clean_nike_data['timestamp'] = clean_nike_data['timestamp'].dt.tz_localize(None)

# separated start date and time
clean_nike_data['start_date'] = clean_nike_data['timestamp'].dt.date
clean_nike_data['start_time'] = clean_nike_data['timestamp'].dt.time

# standardized distance units: meters -> miles
clean_nike_data['distance_miles'] = clean_nike_data['distance_miles'] / 1609.34

# standardized time units: seconds -> minutes
clean_nike_data['total_time_min'] = clean_nike_data['total_time_min'] / 60

# validated nike run club data
clean_nike_data = clean_nike_data[
    (clean_nike_data['timestamp'] <= now) &
    (clean_nike_data['total_time_min'] > 0) &
    (clean_nike_data['distance_miles'] > 0) &
    (clean_nike_data['activity_type'] == 'Running')
]

# standardize timestamp entry format
clean_nike_data['timestamp'] = clean_nike_data['timestamp'].dt.strftime("%Y-%m-%d %H:%M:%S")

clean_nike_data = clean_nike_data.drop_duplicates(subset=['timestamp', 'distance_miles'])

nike_raw.to_sql('nike_raw', conn, if_exists='replace', index=False)
clean_nike_data.to_sql('clean_nike_data', conn, if_exists='append', index=False)
clean_nike_data.to_csv(r'C:\Users\12063\Downloads\sqlite\run_performance_analysis\data\processed\clean_nike_data.csv', index=False)

combined_datasets = pd.read_sql("""
    SELECT
        activity_id, 
        timestamp, 
        distance_miles, 
        total_time_min, 
        activity_type, 
        total_time_min/distance_miles AS pace, 
        source
    FROM clean_strava_data
    UNION ALL
    SELECT 
        activity_id,
        timestamp, 
        distance_miles, 
        total_time_min, 
        activity_type, 
        total_time_min/distance_miles AS pace, 
        source
    FROM clean_nike_data
""", conn)
combined_datasets.to_sql('combined_datasets', conn, if_exists='replace', index=False)

combined_datasets_final = pd.read_sql("""
    SELECT 
        timestamp, 
        distance_miles, 
        total_time_min, 
        activity_type, 
        pace, 
        source, 
        row_number,
        distance_bucket
    FROM ( 
        SELECT
            timestamp, 
            distance_miles, 
            total_time_min, 
            activity_type, 
            total_time_min/distance_miles AS pace, 
            source,
        CASE 
            WHEN 0 <= distance_miles AND distance_miles < 3 THEN '0-3'
            WHEN 3 <= distance_miles AND distance_miles < 6 THEN '3-6'
            WHEN 6 <= distance_miles AND distance_miles < 10 THEN '6-10'
            ELSE '10+'
        END AS distance_bucket,
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

# convert timestamp datatype to datetime and go from timezone native -> aware
combined_datasets_final['timestamp'] = pd.to_datetime(combined_datasets_final['timestamp'])
combined_datasets_final['timestamp'] = combined_datasets_final['timestamp'].dt.tz_localize(None)

# assigned week number to run entries
combined_datasets_final['week'] = pd.to_datetime(combined_datasets_final['timestamp']).dt.isocalendar().week
combined_datasets_final['week'] = combined_datasets_final['week'].astype(int)
combined_datasets_final['month'] = pd.to_datetime(combined_datasets_final['timestamp']).dt.month.astype(int)
combined_datasets_final['year'] = pd.to_datetime(combined_datasets_final['timestamp']).dt.year
combined_datasets_final['year'] = combined_datasets_final['year'].astype(int)
combined_datasets_final.to_sql('combined_datasets_final', conn, if_exists='replace', index=False)
combined_datasets_final.to_csv(r'C:\Users\12063\Downloads\sqlite\run_performance_analysis\data\processed\combined_datasets_final.csv', index=False)

dashboard_overview = pd.read_sql("""
    SELECT 
        COUNT(activity_type) AS total_runs, 
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

source_main = pd.read_sql("""
    SELECT 
        source, 
        row_number
    FROM (
        SELECT 
        timestamp, 
        distance_miles, 
        total_time_min, 
        activity_type,
        total_time_min/distance_miles AS pace,
        source,
        ROW_NUMBER() OVER(
            PARTITION BY(timestamp) 
            ) AS row_number
        FROM combined_datasets 
        )
""", conn)
source_main.to_sql('source_main', conn, if_exists='replace', index=False)

# source comparison table with cte implementation
source_comparison_table = pd.read_sql("""
    WITH 
    mismatched_metrics AS (
        SELECT
            timestamp,
            MAX(CASE WHEN source = 'Strava' THEN distance_miles END) AS strava_distance,
            MAX(CASE WHEN source = 'Nike' THEN distance_miles END) AS nike_distance,
            MAX(CASE WHEN source = 'Strava' THEN total_time_min END) AS strava_duration,
            MAX(CASE WHEN source = 'Nike' THEN total_time_min END) AS nike_duration
        FROM combined_datasets
        GROUP BY timestamp
    ),
    total_and_dupes AS (
        SELECT 
            SUM(CASE WHEN source = 'Nike' THEN 1 ELSE 0 END) AS total_runs_nike,
            SUM(CASE WHEN source = 'Strava' THEN 1 ELSE 0 END) AS total_runs_strava,
            SUM(CASE WHEN row_number > 1 THEN 1 ELSE 0 END) AS duplicate_runs
        FROM source_main
    ),
    raw_source_comparison_table AS (
        SELECT
            *,
            (CASE WHEN strava_distance != nike_distance THEN 'yes' ELSE 'no' END) AS mismatched_distance,
            (CASE WHEN strava_duration != nike_duration THEN 'yes' ELSE 'no' END) AS mismatched_duration
        FROM mismatched_metrics
        CROSS JOIN total_and_dupes 
    )
    SELECT 
        total_runs_nike, 
        total_runs_strava, 
        duplicate_runs, 
        COUNT(mismatched_distance) FILTER (WHERE mismatched_distance = 'yes') AS total_mismatched_distance, 
        COUNT(mismatched_duration) FILTER (WHERE mismatched_duration = 'yes') AS total_mismatched_duration
    FROM raw_source_comparison_table
""", conn)
source_comparison_table.to_sql('source_comparison_table', conn, if_exists='replace', index=False)

performance_analysis = pd.read_sql("""
    SELECT
        distance_bucket,  
        AVG(pace) AS average_pace, 
        MIN(pace) AS fastest_pace, 
        MAX(pace) AS slowest_pace, 
        COUNT(timestamp) AS run_amount
    FROM combined_datasets_final
    GROUP BY distance_bucket
""", conn)
performance_analysis.to_sql('performance_analysis', conn, if_exists='replace',index=False)

end_time = time.time()
print(f"Total runtime: {end_time - start_time:.2f} seconds")

# # testing dynamic query, hardcode for now, later it will take user input 
first_timestamp = '2026-01-01 00:48:51'
last_timestamp = "2026-05-10 16:00:52"

# group by week, month or year
time_frame = "weekly"

# validation for groupings
allowed_groupings = {
    "weekly": "year, week",
    "monthly": "year, month",
    "yearly": "year"
}
grouping_sql = allowed_groupings[time_frame]

dynamic_query = pd.read_sql(f"""
    SELECT 
        year, 
        CASE strftime('%m', timestamp)
            WHEN '01' THEN 'January'
            WHEN '02' THEN 'February'
            WHEN '03' THEN 'March'
            WHEN '04' THEN 'April'
            WHEN '05' THEN 'May'
            WHEN '06' THEN 'June'
            WHEN '07' THEN 'July'
            WHEN '08' THEN 'August'
            WHEN '09' THEN 'September'
            WHEN '10' THEN 'October'
            WHEN '11' THEN 'November'
            WHEN '12' THEN 'December'
        END AS month_name,
        week, 
        COUNT(activity_type) AS total_runs, 
        SUM(distance_miles) AS total_distance, 
        AVG(pace) AS average_pace
    FROM combined_datasets_final
    WHERE timestamp >= ? AND timestamp <= ?
    GROUP BY {grouping_sql}
    ORDER BY {grouping_sql}
""", conn, params=(first_timestamp, last_timestamp))
dynamic_query.to_sql('dynamic_query', conn, if_exists='replace',index=False)

# current_prs = pd.read_sql("""
#     SELECT name, pace 
#     FROM (
#         elapsed_time,
#         distance,
#         (elapsed_time/60) AS elapsed_time_minutes,
#         (distance/1609.34) AS distance_miles,
#         elapsed_time_minutes/distance_miles AS pace
#         FROM current_prs
#         )
# """, conn)
# current_prs.to_sql("current_prs", conn, if_exists='replace',index=False)

conn.commit()
conn.close()