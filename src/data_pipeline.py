import pandas as pd
import sqlite3
from datetime import datetime
import sys
from pathlib import Path
import requests
import os
from dotenv import load_dotenv
import time

start_time = time.time()

# Per-execution request budgets; list pages and individual detail lookups count separately.
ACTIVITIES_API_RATE_LIMIT = 100
ACTIVITY_DETAILS_API_RATE_LIMIT = 150
activities_api_requests = 0
activity_details_api_requests = 0
strava_rate_limit_hit = False
ACTIVITIES_SCAN_RESUMED = False
ACTIVITIES_SCAN_INCOMPLETE = False
 
# Ensure local `src/` modules (like `tcx_to_csv.py`) are importable even when
# running this script from a different working directory.
_THIS_DIR = Path(__file__).resolve().parent
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))

from tcx_to_csv import process_tcx_files
from strava_client import StravaRateLimitError, StravaRateTracker

strava_api = StravaRateTracker(
    state_file=str(_THIS_DIR.parent / "data" / "processed" / "strava_rate_state.json")
)
 
PROJECT_ROOT = _THIS_DIR.parent  # run_performance_analysis/

load_dotenv(PROJECT_ROOT / "data" / ".env.txt")
env_path = PROJECT_ROOT / "data" / ".env.txt"
print("env file exists:", env_path.exists())
print("client id loaded:", os.getenv("STRAVA_CLIENT_ID") is not None)
print("client secret loaded:", os.getenv("STRAVA_CLIENT_SECRET") is not None)
print("refresh token loaded:", os.getenv("STRAVA_REFRESH_TOKEN") is not None)   

db_path = str(PROJECT_ROOT / "data" / "runs_summary.db")
conn = sqlite3.connect(db_path)

cursor = conn.cursor()
 
cursor.execute(
    "SELECT MAX(timestamp) FROM clean_strava_data"
)

row = cursor.fetchone()
last_sync = row[0] if row else None

MODE = "batch" # "incremental" or "batch"
NIKE_MODE = "incremental" # "incremental" or "batch"; keep aligned with tcx_to_csv.MODE

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
    payload = response.json()
    if 'access_token' not in payload:
        print("Strava token error:", response.status_code, payload)
    return payload['access_token']

def get_activities(access_token, after_timestamp, conn):
    global activities_api_requests
    global activity_details_api_requests
    global strava_rate_limit_hit
    global ACTIVITIES_SCAN_RESUMED
    global ACTIVITIES_SCAN_INCOMPLETE

    if any(
        not isinstance(limit, int) or isinstance(limit, bool) or limit < 0
        for limit in (ACTIVITIES_API_RATE_LIMIT, ACTIVITY_DETAILS_API_RATE_LIMIT)
    ):
        raise ValueError("Strava endpoint request limits must be non-negative integers.")

    activities = []
    activity_details_list = []
    processed_activity_details_list = []
    current_prs_df = pd.DataFrame()
    all_best_efforts = []
    missing = set()
    unexpected = set()

    conn.execute(
        "CREATE TABLE IF NOT EXISTS strava_pending_activity_details "
        "(activity_id TEXT PRIMARY KEY)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS strava_pipeline_state "
        "(key TEXT PRIMARY KEY, value TEXT NOT NULL)"
    )

    processed_table_exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' "
        "AND name='processed_activity_details'"
    ).fetchone()
    if processed_table_exists:
        processed_activity_details = pd.read_sql(
            "SELECT id, type FROM processed_activity_details", conn
        )
        processed_activity_ids = set(
            processed_activity_details["id"].dropna().astype(str)
        )
    else:
        processed_activity_ids = set()

    if processed_activity_ids:
        conn.executemany(
            "DELETE FROM strava_pending_activity_details WHERE activity_id = ?",
            [(activity_id,) for activity_id in processed_activity_ids],
        )

    saved_cursor = conn.execute(
        "SELECT value FROM strava_pipeline_state "
        "WHERE key = 'activities_next_page'"
    ).fetchone()
    page = int(saved_cursor[0]) if saved_cursor else 1
    saved_before = conn.execute(
        "SELECT value FROM strava_pipeline_state "
        "WHERE key = 'activities_scan_before'"
    ).fetchone()
    scan_before = (
        int(saved_before[0])
        if saved_cursor and saved_before
        else int(time.time())
    )
    ACTIVITIES_SCAN_RESUMED = page > 1
    ACTIVITIES_SCAN_INCOMPLETE = ACTIVITIES_SCAN_RESUMED

    def save_activities_cursor(next_page):
        conn.executemany(
            "INSERT OR REPLACE INTO strava_pipeline_state (key, value) VALUES (?, ?)",
            [
                ("activities_next_page", str(next_page)),
                ("activities_scan_before", str(scan_before)),
            ],
        )
        conn.commit()

    def process_pending_details():
        global activity_details_api_requests
        global strava_rate_limit_hit
        global ACTIVITIES_SCAN_INCOMPLETE

        pending_ids = conn.execute(
            "SELECT activity_id FROM strava_pending_activity_details ORDER BY rowid"
        ).fetchall()
        for (activity_id,) in pending_ids:
            if str(activity_id) in processed_activity_ids:
                continue
            if activity_details_api_requests >= ACTIVITY_DETAILS_API_RATE_LIMIT:
                print(
                    "Activity-details request limit reached "
                    f"({ACTIVITY_DETAILS_API_RATE_LIMIT}); pending IDs are saved for the next run."
                )
                return

            activity_details_api_requests += 1
            try:
                activity_response = strava_api.get(
                    f"https://www.strava.com/api/v3/activities/{activity_id}",
                    headers={"Authorization": f"Bearer {access_token}"},
                )
            except StravaRateLimitError:
                strava_rate_limit_hit = True
                ACTIVITIES_SCAN_INCOMPLETE = True
                print(
                    "Strava rate limit returned HTTP 429; stopping API requests "
                    "for this execution. Pending activity details remain queued."
                )
                return

            activity_detail = activity_response.json()
            if not isinstance(activity_detail, dict) or "id" not in activity_detail:
                raise ValueError(
                    f"Unexpected activity-details response for activity {activity_id}."
                )
            if str(activity_detail["id"]) != str(activity_id):
                raise ValueError(
                    f"Activity-details response ID did not match requested ID {activity_id}."
                )

            print(f"Processing activity {activity_id}")
            processed_activity_details_list.append(activity_detail)
            processed_activity_ids.add(str(activity_detail["id"]))
            if activity_detail.get("best_efforts"):
                best_efforts_df = pd.DataFrame(activity_detail["best_efforts"])
                best_efforts_df["activity_id"] = activity_detail["id"]
                all_best_efforts.append(best_efforts_df)
                print("best efforts logged")
            else:
                print("best_efforts is empty")

    print("FILTER:", after_timestamp)

    # Retry previously queued detail lookups before requesting more activity pages.
    process_pending_details()

    while True:
        if strava_rate_limit_hit:
            break
        if activities_api_requests >= ACTIVITIES_API_RATE_LIMIT:
            ACTIVITIES_SCAN_INCOMPLETE = True
            save_activities_cursor(page)
            print(
                "Activities request limit reached "
                f"({ACTIVITIES_API_RATE_LIMIT}); page {page} is saved for the next run."
            )
            break

        activities_api_requests += 1
        try:
            response = strava_api.get(
                'https://www.strava.com/api/v3/athlete/activities',
                headers={'Authorization': f'Bearer {access_token}'},
                params={'per_page': 200, 'page': page, 'before': scan_before}
            )
        except StravaRateLimitError:
            strava_rate_limit_hit = True
            ACTIVITIES_SCAN_INCOMPLETE = True
            save_activities_cursor(page)
            print(
                "Strava rate limit returned HTTP 429; stopping API requests "
                "for this execution."
            )
            break
        
        print(f"RESPONSE STATUS CODE: {response.status_code}")

        data = response.json()

        if not isinstance(data, list):
            raise ValueError(f"Unexpected Strava activities response: {data}")

        if not data:
            conn.execute(
                "DELETE FROM strava_pipeline_state "
                "WHERE key IN ('activities_next_page', 'activities_scan_before')"
            )
            conn.commit()
            break
        
        print(f"PAGE {page} LENGTH:", len(data))

# expected strava_raw db schema
        strava_raw_schema = {
            "resource_state",
            "athlete",
            "name",
            "distance",
            "moving_time",
            "elapsed_time",
            "total_elevation_gain",
            "type",
            "sport_type",
            "workout_type",
            "device_name",
            "tags",
            "id",
            "id_str",
            "start_date",
            "start_date_local",
            "timezone",
            "utc_offset",
            "location_city",
            "location_state",
            "location_country",
            "achievement_count",
            "kudos_count",
            "comment_count",
            "athlete_count",
            "photo_count",
            "map",
            "trainer",
            "commute",
            "manual",
            "private",
            "visibility",
            "flagged",
            "gear_id",
            "start_latlng",
            "end_latlng",
            "average_speed",
            "max_speed",
            "average_cadence",
            "average_watts",
            "max_watts",
            "weighted_average_watts",
            "device_watts",
            "kilojoules",
            "has_heartrate",
            "average_heartrate",
            "max_heartrate",
            "heartrate_opt_out",
            "display_hide_heartrate_option",
            "elev_high",
            "elev_low",
            "upload_id",
            "upload_id_str",
            "external_id",
            "from_accepted_tag",
            "pr_count",
            "total_photo_count",
            "has_kudoed",
            "suffer_score"   
        }

        # turn raw strava data into a set
        strava_raw_column_names = set()

        for activity in data:
            strava_raw_column_names.update(activity.keys())

        missing = strava_raw_schema - strava_raw_column_names
        unexpected = strava_raw_column_names - strava_raw_schema

        if missing:
            print("Expected columns missing:", sorted(missing))
        if unexpected:
            print("Unexpected columns:", sorted(unexpected))

        queued_ids = []
        for act in data:
            if act.get("type") == "Run" and str(act["id"]) not in processed_activity_ids:
                queued_ids.append((str(act["id"]),))
        if queued_ids:
            conn.executemany(
                "INSERT OR IGNORE INTO strava_pending_activity_details (activity_id) "
                "VALUES (?)",
                queued_ids,
            )
            conn.commit()
        process_pending_details()

        print(f"activity_detail data type: {type(activity_details_list)}")
        print(f"tuple length: {len(activity_details_list)}")
        
        # filters data after the last sync date
        if (
            MODE == "incremental"
            and after_timestamp is not None
            and not ACTIVITIES_SCAN_RESUMED
        ):
            filtered_data = []

            for act in data:
                act_time = datetime.fromisoformat(
                    act['start_date_local'].replace('Z', '+00:00')
                ).replace(tzinfo=None)
                if act_time > after_timestamp:
                    filtered_data.append(act)

            activities.extend(filtered_data)
        else:
            activities.extend(data)

        if strava_rate_limit_hit:
            ACTIVITIES_SCAN_INCOMPLETE = True
            save_activities_cursor(page + 1)
            break

        page += 1

    print(
        "Per-execution Strava API requests: "
        f"{activities_api_requests}/{ACTIVITIES_API_RATE_LIMIT} activities pages, "
        f"{activity_details_api_requests}/{ACTIVITY_DETAILS_API_RATE_LIMIT} activity details"
    )

    if all_best_efforts:
        all_best_efforts = pd.concat(all_best_efforts, ignore_index=True)
        if not all_best_efforts.empty:
            current_prs_df = (
                all_best_efforts
                .sort_values("elapsed_time")
                .groupby("distance", as_index=False)
                .first()
            )

    return activities, activity_details_list, processed_activity_details_list, current_prs_df, missing, unexpected

def process_strava(activities, processed_activity_details_list, current_prs_df):
    strava_raw = pd.DataFrame(activities)

    if processed_activity_details_list:
        processed_activity_details_df = pd.DataFrame(processed_activity_details_list)
        processed_activity_details_df = processed_activity_details_df[[
        'id', 'type'
        ]]
    else:
        processed_activity_details_df = pd.DataFrame(columns=['id', 'type'])
        print("processed_activity_details_list is empty! dataframe cannot be made")

    if len(current_prs_df) > 0:
        current_prs_df = current_prs_df[[
        'id', 'name', 'elapsed_time', 'distance' 
        ]]
        current_prs_df["elapsed_time_minutes"] = current_prs_df["elapsed_time"] / 60
        current_prs_df["distance_miles"] = current_prs_df["distance"] / 1609.34
        current_prs_df["pace"] = current_prs_df["elapsed_time_minutes"] / current_prs_df["distance_miles"]
    else:
        print("current_prs_df is empty!")
        current_prs_df = pd.DataFrame(columns=[
            'id', 'name', 'elapsed_time', 'distance',
            'elapsed_time_minutes', 'distance_miles', 'pace'
        ])

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

    strava_raw = strava_raw.astype(str)

    return strava_raw, clean_strava_data, processed_activity_details_df, current_prs_df

def write_strava_data(strava_raw, clean_strava_data, processed_activity_details_df, current_prs_df, conn):
    full_batch = MODE == "batch" and not ACTIVITIES_SCAN_INCOMPLETE
    if full_batch:
        clean_strava_data.to_sql("clean_strava_data", conn, if_exists="replace", index=False)
        strava_raw.to_sql("strava_raw", conn, if_exists="replace", index=False)
    else:
        _append_unseen_rows(clean_strava_data, "clean_strava_data", "activity_id", conn)
        _append_unseen_rows(strava_raw, "strava_raw", "id", conn)

    write_processed_activity_details(processed_activity_details_df, conn)

    if MODE == "batch":
        if full_batch:
            current_prs_df.to_sql("current_prs_raw", conn, if_exists="replace", index=False)
        else:
            _append_unseen_rows(current_prs_df, "current_prs_raw", "id", conn)
    else:
        clean_strava_data.to_csv(
            PROJECT_ROOT / "data" / "processed" / "clean_strava_data.csv",
            index=False,)
        if not current_prs_df.empty:
            _append_unseen_rows(current_prs_df, "current_prs_raw", "id", conn)


def _append_unseen_rows(frame, table_name, id_column, conn):
    if frame.empty:
        return
    incoming = frame.drop_duplicates(subset=[id_column]).copy()
    table_exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,),
    ).fetchone()
    if table_exists:
        existing_ids = pd.read_sql(
            f'SELECT "{id_column}" FROM "{table_name}"', conn
        )[id_column].dropna().astype(str)
        incoming = incoming[~incoming[id_column].astype(str).isin(set(existing_ids))]
    if not incoming.empty:
        incoming.to_sql(table_name, conn, if_exists="append", index=False)


def write_processed_activity_details(processed_activity_details_df, conn):
    if not processed_activity_details_df.empty:
        details = processed_activity_details_df[["id", "type"]].drop_duplicates(
            subset=["id"]
        )
        _append_unseen_rows(
            details, "processed_activity_details", "id", conn
        )

    table_exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' "
        "AND name='processed_activity_details'"
    ).fetchone()
    if table_exists:
        processed_ids = pd.read_sql(
            "SELECT id FROM processed_activity_details", conn
        )["id"].dropna().astype(str)
        if not processed_ids.empty:
            conn.executemany(
                "DELETE FROM strava_pending_activity_details WHERE activity_id = ?",
                [(activity_id,) for activity_id in set(processed_ids)],
            )
    conn.commit()

t0 = time.time()
access_token = get_access_token()
t1 = time.time()
activities, activity_details_list, processed_activity_details_list, current_prs_list, missing, unexpected = get_activities(access_token, after_timestamp, conn)

if missing:
    print(f"=====================\n WARNING: Incoming data from the activity details endpoint is missing expected columns: \n ===================== \n{sorted(missing)}")
if unexpected:
    print(f"=====================\n INFO: Ignoring unexpected columns in incoming data from the activity details endpoint: \n ===================== \n columns: {sorted(unexpected)}")

print(f"Auth: {time.time() - t0}")
print(f"Strava API: {time.time() - t1} seconds")
now = pd.Timestamp.now()
print("TOTAL ACTIVITIES:", len(activities))

# check if there are any activities to process
if not activities:
    print("No new Strava activities. Skipping Strava processing.")
    clean_strava_data = None  # empty placeholder
    strava_raw = None
    if processed_activity_details_list:
        pending_details_df = pd.DataFrame(processed_activity_details_list)[["id", "type"]]
        write_processed_activity_details(pending_details_df, conn)
    if not current_prs_list.empty:
        pending_prs_df = current_prs_list[
            ["id", "name", "elapsed_time", "distance"]
        ].copy()
        pending_prs_df["elapsed_time_minutes"] = pending_prs_df["elapsed_time"] / 60
        pending_prs_df["distance_miles"] = pending_prs_df["distance"] / 1609.34
        pending_prs_df["pace"] = (
            pending_prs_df["elapsed_time_minutes"] / pending_prs_df["distance_miles"]
        )
        _append_unseen_rows(pending_prs_df, "current_prs_raw", "id", conn)
else:
    t3 = time.time()
    strava_raw, clean_strava_data, processed_activity_details_df, current_prs_df = process_strava(activities, processed_activity_details_list, current_prs_list)
    print(f"Strava processing: {time.time() - t3} seconds")
    write_strava_data(strava_raw, clean_strava_data, processed_activity_details_df, current_prs_df, conn)

input_dir = str(PROJECT_ROOT / "data" / "raw" / "tcx")

t4 = time.time()
nike_raw = process_tcx_files(input_dir, conn)
print(f"TCX processing: {time.time() - t4:.2f} seconds")

if nike_raw.empty:
    print("Nike data is empty. Skipping Nike cleaning and storage.")
else:
    # standardized nike run club data
    clean_nike_data = nike_raw.copy().rename(columns={
        'start_time_utc':'timestamp',
        'avg_cadence_rpm':'average_cadence',
        'sport':'activity_type',
        'distance_m':'distance_miles',
        'total_time_s':'total_time_min'
        })
    clean_nike_data['timestamp'] = pd.to_datetime(
        clean_nike_data['timestamp']
    )
    clean_nike_data['source'] = 'Nike'

    # converted timestamp column from: timezone-aware -> timezone-native
    clean_nike_data['timestamp'] = (
        pd.to_datetime(clean_nike_data['timestamp'], utc=True)
          .dt.tz_convert('America/Los_Angeles')
          .dt.tz_localize(None)
    )

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

    clean_nike_data = clean_nike_data.drop_duplicates(subset=['file_path'])

    nike_write_mode = "replace" if NIKE_MODE == "batch" else "append"
    nike_raw.to_sql('nike_raw', conn, if_exists=nike_write_mode, index=False)
    clean_nike_data.to_sql(
        'clean_nike_data', conn, if_exists=nike_write_mode, index=False
    )

nike_clean_table_exists = conn.execute(
    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='clean_nike_data'"
).fetchone()
nike_union = """
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
""" if nike_clean_table_exists else ""

combined_datasets = pd.read_sql(f"""
    SELECT
        activity_id, 
        timestamp, 
        distance_miles, 
        total_time_min, 
        activity_type, 
        total_time_min/distance_miles AS pace, 
        source
    FROM clean_strava_data
    {nike_union}
""", conn)
combined_datasets.to_sql('combined_datasets', conn, if_exists='replace', index=False)

def match_cross_source_activities(
    activities,
    timestamp_threshold_minutes=2.0,
    distance_threshold_miles=0.10,
    elapsed_threshold_seconds=60.0,
):
    """Match one Strava/NRC pair at a time and preserve the raw activity_id values."""
    matched = activities.copy()
    if "source" not in matched.columns:
        raise ValueError("Matching requires a 'source' column.")

    matched["_match_source"] = matched["source"].astype(str).str.strip().str.lower()
    valid_sources = {"strava", "nike"}
    unknown_sources = set(matched["_match_source"]) - valid_sources
    if unknown_sources:
        raise ValueError(f"Unexpected activity sources: {sorted(unknown_sources)}")

    for required_column in ("timestamp", "distance_miles", "total_time_min"):
        if required_column not in matched.columns:
            raise ValueError(f"Matching requires a '{required_column}' column.")

    timestamps = pd.to_datetime(matched["timestamp"], utc=True, errors="coerce")
    if timestamps.isna().any():
        raise ValueError("Matching found invalid or missing timestamp values.")

    distance = pd.to_numeric(matched["distance_miles"], errors="coerce")
    elapsed_minutes = pd.to_numeric(matched["total_time_min"], errors="coerce")
    elapsed_seconds = elapsed_minutes * 60.0
    if distance.isna().any() or elapsed_minutes.isna().any():
        raise ValueError("Matching found invalid or missing distance/elapsed-time values.")

    strava_indexes = list(matched.index[matched["_match_source"] == "strava"])
    nike_indexes = list(matched.index[matched["_match_source"] == "nike"])

    candidates = []
    for strava_index in strava_indexes:
        for nike_index in nike_indexes:
            timestamp_difference = abs(
                (timestamps.loc[strava_index] - timestamps.loc[nike_index]).total_seconds()
            )
            distance_difference = abs(
                distance.loc[strava_index] - distance.loc[nike_index]
            )
            elapsed_difference = abs(
                elapsed_seconds.loc[strava_index] - elapsed_seconds.loc[nike_index]
            )

            # Cross-source matching uses all three thresholds; no same-source candidates are considered.
            if (
                timestamp_difference <= timestamp_threshold_minutes * 60.0
                and distance_difference <= distance_threshold_miles
                and elapsed_difference <= elapsed_threshold_seconds
            ):
                candidates.append(
                    (
                        timestamp_difference,
                        str(matched.loc[strava_index, "activity_id"]),
                        str(matched.loc[nike_index, "activity_id"]),
                        strava_index,
                        nike_index,
                    )
                )

    # A globally shortest-candidate-first pass makes matching mutually one-to-one.
    candidates.sort(key=lambda candidate: (candidate[0], candidate[1], candidate[2]))
    paired_indexes = set()
    pairs = []
    for _, _, _, strava_index, nike_index in candidates:
        if strava_index not in paired_indexes and nike_index not in paired_indexes:
            paired_indexes.add(strava_index)
            paired_indexes.add(nike_index)
            pairs.append((strava_index, nike_index))

    matched["dedupe_group_id"] = [
        f"unique_{position}" for position in range(len(matched))
    ]
    for pair_number, (strava_index, nike_index) in enumerate(pairs, start=1):
        group_id = f"cross_source_{pair_number}"
        matched.loc[strava_index, "dedupe_group_id"] = group_id
        matched.loc[nike_index, "dedupe_group_id"] = group_id

    matched = matched.drop(columns=["_match_source"])
    return matched


def validate_cross_source_deduplication_groups(matched_activities):
    """Fail loudly if a group could contain more than one row from either source."""
    if "dedupe_group_id" not in matched_activities.columns:
        raise ValueError("Validation requires a 'dedupe_group_id' column.")

    source_counts = (
        matched_activities.groupby(
            ["dedupe_group_id", "source"], sort=False
        )
        .size()
        .rename("source_count")
    )
    group_sizes = matched_activities.groupby(
        "dedupe_group_id", sort=False
    ).size()
    offending_ids = set(group_sizes[group_sizes > 2].index.astype(str))
    offending_ids.update(source_counts[source_counts > 1].index.get_level_values(0).astype(str))

    if offending_ids:
        raise ValueError(
            "Cross-source deduplication validation failed for group IDs: "
            + ", ".join(sorted(offending_ids))
        )


def deduplicate_by_group(matched_activities, conn):
    """Keep one row per group using the existing Strava-first window ordering."""
    validate_cross_source_deduplication_groups(matched_activities)

    matched_activities.to_sql(
        "combined_datasets_matched", conn, if_exists="replace", index=False
    )

    deduplicated = pd.read_sql(
        """
        SELECT
            timestamp,
            activity_id,
            distance_miles,
            total_time_min,
            activity_type,
            pace,
            source,
            row_number,
            dedupe_group_id,
            distance_bucket
        FROM (
            SELECT
                timestamp,
                activity_id,
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
                dedupe_group_id,
                ROW_NUMBER() OVER(
                    PARTITION BY dedupe_group_id
                    ORDER BY
                        CASE WHEN source = 'Strava' THEN 0 ELSE 1 END
                ) AS row_number
            FROM combined_datasets_matched
        )
        WHERE row_number = 1
        """,
        conn,
    )
    return deduplicated


combined_datasets_matched = match_cross_source_activities(combined_datasets)
combined_datasets_final = deduplicate_by_group(combined_datasets_matched, conn)

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
combined_datasets_final.to_csv(
    PROJECT_ROOT / "data" / "processed" / "combined_datasets_final.csv",
    index=False,
)

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

current_prs_raw = pd.read_sql("""
    SELECT * 
    FROM current_prs_raw 
""", conn)

# current_prs table transformations will be here for now
current_prs_clean = current_prs_raw.copy()
current_prs_clean["total_time_min"] = current_prs_clean["elapsed_time"] / 60
current_prs_clean["distance_miles"] = current_prs_clean["distance"] / 1609.34
current_prs_clean["pace"] = current_prs_clean["total_time_min"] / current_prs_clean["distance_miles"]

current_prs_clean.to_sql("current_prs_clean", conn, if_exists='replace', index=False)

current_prs_clean = pd.read_sql("""
    SELECT
        name,
        elapsed_time,
        pace
    FROM (
        SELECT 
            name, 
            elapsed_time, 
            distance,
            pace,
        ROW_NUMBER() OVER(
            PARTITION BY(distance)
            ORDER BY elapsed_time ASC
            ) AS distance_count
        FROM current_prs_clean
        )
    WHERE distance_count = 1
""", conn)
current_prs_clean.to_sql("current_prs_clean", conn, if_exists='replace', index=False)

print(current_prs_raw[["name","elapsed_time","pace"]].head(5))

conn.commit()
conn.close()