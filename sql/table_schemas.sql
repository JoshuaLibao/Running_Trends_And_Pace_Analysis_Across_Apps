--Runs Database Schema
--====================

BEGIN TRANSACTION;
CREATE TABLE IF NOT EXISTS "dashboard_overview" (
"total_runs" INTEGER,
  "total_distance_miles" REAL,
  "total_duration_min" REAL,
  "average_pace" REAL,
  "fastest_pace" REAL,
  "slowest_pace" REAL,
  "first_run_date" TEXT,
  "last_run_date" TEXT
);
CREATE TABLE IF NOT EXISTS "monthly_summary" (
"year" INTEGER,
  "month_name" TEXT,
  "total_runs" INTEGER,
  "total_distance" REAL,
  "average_pace" REAL
);
CREATE TABLE IF NOT EXISTS "performance_analysis" (
"distance_bucket" TEXT,
  "average_pace" REAL,
  "fastest_pace" REAL,
  "slowest_pace" REAL,
  "run_amount" INTEGER
);
CREATE TABLE IF NOT EXISTS "source_comparison_table" (
"total_runs_nike" INTEGER,
  "total_runs_strava" INTEGER,
  "duplicate_runs" INTEGER,
  "total_mismatched_distance" INTEGER,
  "total_mismatched_duration" INTEGER
);
CREATE TABLE IF NOT EXISTS "weekly_summary" (
"year" INTEGER,
  "week" INTEGER,
  "total_runs" INTEGER,
  "total_distance" REAL,
  "average_pace" REAL
);
COMMIT;
