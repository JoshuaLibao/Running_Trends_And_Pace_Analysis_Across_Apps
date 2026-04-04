# 🏃‍♂️ Running Trends & Pace Analysis Across Apps

---

## 1️⃣ Project Overview

This is a project that analyzes a database of tracked runs, which consists of different tracking sources. The primary goal is to display performance trends through tables that provide meaningful metrics. Such as, a performance analysis table, source comparison table, weekly/monthly summary and a dashboard overview.

---

## 2️⃣ Skills / Technologies

**Tools & Libraries Used:**

- Python, Pandas  
- SQL, Window Functions  
- Git & Version Control  

---

## 3️⃣ Project Structure

**Brief description of repo structure:**
```
run_performance_analysis/ # Main project folder
│
├── README.md # Project overview and documentation
│
├── data/ # All raw and processed data
│ ├── raw/ # Original input files
│ │ ├── raw_nike.csv
│ │ ├── raw_strava.csv
│ │ └── tcx_files/ # Original 
│ │
│ └── processed/ # Cleaned and standardized datasets (auto-updated by script)
│ ├── clean_nike.csv
│ └── clean_strava.csv
│
├── src/ # Source code for the project
│ └── data_pipeline.py # Python script for cleaning, standardizing, combining, and exporting data
│
├── sql/ # SQL scripts and table schemas
│ ├── queries.sql
│ └── table_schemas.sql
│
└── runs_summary.db # SQLite database of combined runs (output from script)
```

---

## 4️⃣ Workflow / Methodology

To begin the project I imported two datasets of logged activities from separate sources, each having their own csv. Both datasets went through the same standardization, cleaning and validating process using Pandas. To analyze all the runs together, they were combined using UNION ALL and the aggregation and feature engineering was performed in SQL. 

---

## 5️⃣ Next Steps / Future Work (Optional)

- Implementing API ingestion  
- Scaling to multiple sources  
- Visualization dashboards  
- Implement machine learning  

---

## 6️⃣ Setup / Instructions

**Run the project safely with example or dummy data:**
```bash
1. Clone the repository:  

git clone <your-repo-url>
cd strava_nike_project

2. Install dependencies:

pip install -r requirements.txt

3. Place your CSV files in the
Place your CSV files in the data/raw/ folder.

4. Run the Python script to clean, standardize, combine, and export processed data:

python src/data_processing.py


5. Check the data/processed/ folder for cleaned datasets and runs_summary.db for the combined database.

```
