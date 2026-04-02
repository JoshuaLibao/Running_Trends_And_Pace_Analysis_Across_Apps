-----=====Dashboard Overview=====-----
-- Overview of all the runs. Provides the total amount of runs, miles and minutes that you ran.
-- Also, it provides the average, fastest and slowest pace that you ran.
-- Lastly, it tells you when you first and last run was.
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

-----======Weekly Summary=====-----
-- Summary of all the runs during the week. Displaying the total amount of runs, total distance and average pace.
SELECT 
    year, 
    week, 
    COUNT(activity_type) AS total_runs, 
    SUM(distance_miles) AS total_distance, 
    AVG(pace) AS average_pace
FROM combined_datasets_final
GROUP BY year, week
ORDER BY year, week

-----=====Monthly Summary=====-----
-- Summary of all the runs during the month. Displaying the total amount of runs, total distance and average pace.
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
    COUNT(activity_type) AS total_runs, 
    SUM(distance_miles) AS total_distance, 
    AVG(pace) AS average_pace
FROM combined_datasets_final
GROUP BY year, month
ORDER BY year, month

-----=====Source Comparison Table=====-----
-- Compare nike and strava data quality and overlap.
-- Also validates that my deduplication and data standardization worked.
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

-----=====Performance Analysis=====-----
-- Allows you to see performance metrics for all your runs per distance group.
-- The distance groups being, 0-3 miles, 4-6 miles, 7-9 miles and 10+ miles.
SELECT
    distance_bucket,  
    AVG(pace) AS average_pace, 
    MIN(pace) AS fastest_pace, 
    MAX(pace) AS slowest_pace, 
    COUNT(timestamp) AS run_amount
FROM combined_datasets_final
GROUP BY distance_bucket