import os
import warnings
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Dict, Iterable, Iterator, List, Optional, Tuple
from xml.etree import ElementTree as ET
import pandas as pd

MODE = "incremental"  # "incremental" or "batch"

_SUMMARY_COLUMNS = [
    "file_path",
    "activity_id",
    "sport",
    "start_time_utc",
    "total_time_s",
    "distance_m",
    "calories",
    "avg_hr_bpm",
    "max_hr_bpm",
    "avg_cadence_rpm",
]


def _strip_ns(tag: str) -> str:
    if "}" in tag:
        return tag.split("}", 1)[1]
    return tag


def _iter_elems(root: ET.Element, name: str) -> Iterator[ET.Element]:
    target = name
    for el in root.iter():
        if _strip_ns(el.tag) == target:
            yield el


def _first_text(root: ET.Element, path: List[str]) -> Optional[str]:
    """
    Namespace-agnostic: walks by local-name match.
    path is a list like ["Activities","Activity","Id"].
    """
    cur = root
    for part in path:
        nxt = None
        for child in list(cur):
            if _strip_ns(child.tag) == part:
                nxt = child
                break
        if nxt is None:
            return None
        cur = nxt
    return (cur.text or "").strip() or None


def _parse_float(s: Optional[str]) -> Optional[float]:
    if s is None:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _parse_int(s: Optional[str]) -> Optional[int]:
    if s is None:
        return None
    try:
        return int(float(s))
    except ValueError:
        return None


def _parse_time_utc(s: Optional[str]) -> Optional[str]:
    if not s:
        return None
    # TCX times are typically ISO8601 with 'Z'
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    except ValueError:
        return s


def _walk_tcx_files(input_dir: str) -> Iterator[str]:
    for root, _, files in os.walk(input_dir):
        for fn in files:
            if fn.lower().endswith(".tcx"):
                yield os.path.join(root, fn)


@dataclass
class ActivitySummary:
    file_path: str
    activity_id: Optional[str]
    sport: Optional[str]
    start_time_utc: Optional[str]
    total_time_s: Optional[float]
    distance_m: Optional[float]
    calories: Optional[int]
    avg_hr_bpm: Optional[int]
    max_hr_bpm: Optional[int]
    avg_cadence_rpm: Optional[int]


def _parse_activity_summary(root: ET.Element, file_path: str) -> List[ActivitySummary]:
    out: List[ActivitySummary] = []
    for act in _iter_elems(root, "Activity"):
        sport = act.attrib.get("Sport") or None
        activity_id = None
        for id_el in _iter_elems(act, "Id"):
            activity_id = (id_el.text or "").strip() or None
            break

        # Use first lap start time if present, else Id.
        start_time_utc = None
        first_lap = None
        for lap in _iter_elems(act, "Lap"):
            first_lap = lap
            start_time_utc = _parse_time_utc(lap.attrib.get("StartTime"))
            break
        if start_time_utc is None:
            start_time_utc = _parse_time_utc(activity_id)

        total_time_s = None
        distance_m = None
        calories = None
        avg_hr = None
        max_hr = None
        avg_cadence = None

        # Sum across laps when present.
        total_time_acc = 0.0
        distance_acc = 0.0
        calories_acc = 0
        saw_lap = False
        for lap in _iter_elems(act, "Lap"):
            saw_lap = True
            tt = _parse_float(_first_text(lap, ["TotalTimeSeconds"]))
            if tt is not None:
                total_time_acc += tt
            dist = _parse_float(_first_text(lap, ["DistanceMeters"]))
            if dist is not None:
                distance_acc += dist
            cal = _parse_int(_first_text(lap, ["Calories"]))
            if cal is not None:
                calories_acc += cal

            # Lap average/max HR can be nested in AverageHeartRateBpm/Value etc.
            lap_avg_hr = _parse_int(_first_text(lap, ["AverageHeartRateBpm", "Value"]))
            lap_max_hr = _parse_int(_first_text(lap, ["MaximumHeartRateBpm", "Value"]))
            if lap_avg_hr is not None and avg_hr is None:
                avg_hr = lap_avg_hr
            if lap_max_hr is not None:
                max_hr = max(max_hr or lap_max_hr, lap_max_hr)

            lap_cad = _parse_int(_first_text(lap, ["Cadence"]))
            if lap_cad is not None and avg_cadence is None:
                avg_cadence = lap_cad

        if saw_lap:
            total_time_s = total_time_acc if total_time_acc != 0.0 else None
            distance_m = distance_acc if distance_acc != 0.0 else None
            calories = calories_acc if calories_acc != 0 else None

        out.append(
            ActivitySummary(
                file_path=file_path,
                activity_id=activity_id,
                sport=sport,
                start_time_utc=start_time_utc,
                total_time_s=total_time_s,
                distance_m=distance_m,
                calories=calories,
                avg_hr_bpm=avg_hr,
                max_hr_bpm=max_hr,
                avg_cadence_rpm=avg_cadence,
            )
        )
    return out


def _parse_trackpoints(root: ET.Element, file_path: str) -> Iterator[Dict[str, object]]:
    # Pull some activity-level context if available
    activity_id = _first_text(root, ["Activities", "Activity", "Id"])
    sport = None
    for act in _iter_elems(root, "Activity"):
        sport = act.attrib.get("Sport") or None
        break

    # Trackpoints can appear under Track > Trackpoint
    for tp in _iter_elems(root, "Trackpoint"):
        time_utc = _parse_time_utc(_first_text(tp, ["Time"]))

        lat = _parse_float(_first_text(tp, ["Position", "LatitudeDegrees"]))
        lon = _parse_float(_first_text(tp, ["Position", "LongitudeDegrees"]))
        alt_m = _parse_float(_first_text(tp, ["AltitudeMeters"]))
        dist_m = _parse_float(_first_text(tp, ["DistanceMeters"]))

        hr = _parse_int(_first_text(tp, ["HeartRateBpm", "Value"]))
        cad = _parse_int(_first_text(tp, ["Cadence"]))

        yield {
            "file_path": file_path,
            "activity_id": activity_id,
            "sport": sport,
            "time_utc": time_utc,
            "latitude_deg": lat,
            "longitude_deg": lon,
            "altitude_m": alt_m,
            "distance_m": dist_m,
            "heart_rate_bpm": hr,
            "cadence_rpm": cad,
        }


def _read_tcx(file_path: str) -> Optional[ET.Element]:
    try:
        tree = ET.parse(file_path)
        return tree.getroot()
    except ET.ParseError:
        return None
    except OSError:
        return None


def _read_summary_dataframe(
    file_paths: Iterable[str],
) -> Tuple[pd.DataFrame, List[str]]:
    rows: List[ActivitySummary] = []
    parsed_file_paths: List[str] = []
    for fp in file_paths:
        root = _read_tcx(fp)
        if root is None:
            warnings.warn(
                f"Skipping malformed or unreadable TCX file: {fp}",
                RuntimeWarning,
                stacklevel=2,
            )
            continue

        parsed_file_paths.append(fp)
        rows.extend(_parse_activity_summary(root, fp))

    dataframe = pd.DataFrame.from_records(
        (asdict(row) for row in rows), columns=_SUMMARY_COLUMNS
    )
    return dataframe, parsed_file_paths


def read_summary_dataframe(input_dir: str) -> pd.DataFrame:
    dataframe, _ = _read_summary_dataframe(_walk_tcx_files(input_dir))
    return dataframe


def process_tcx_files(input_dir: str, conn, mode: Optional[str] = None) -> pd.DataFrame:
    processing_mode = MODE if mode is None else mode
    if processing_mode not in {"batch", "incremental"}:
        raise ValueError("mode must be either 'batch' or 'incremental'")

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS process_tcx_files (
            file_name TEXT PRIMARY KEY
        )
        """
    )

    if processing_mode == "batch":
        conn.execute("DELETE FROM process_tcx_files")
        file_paths = list(_walk_tcx_files(input_dir))
    else:
        processed_file_names = {
            row[0] for row in conn.execute("SELECT file_name FROM process_tcx_files")
        }
        file_paths = [
            file_path
            for file_path in _walk_tcx_files(input_dir)
            if os.path.basename(file_path) not in processed_file_names
        ]
        if not file_paths:
            print("No new TCX files. Skipping TCX parsing.")
            return pd.DataFrame(columns=_SUMMARY_COLUMNS)

    dataframe, parsed_file_paths = _read_summary_dataframe(file_paths)
    conn.executemany(
        "INSERT OR IGNORE INTO process_tcx_files (file_name) VALUES (?)",
        ((os.path.basename(file_path),) for file_path in parsed_file_paths),
    )
    return dataframe


def read_trackpoints_dataframe(input_dir: str) -> pd.DataFrame:
    rows: List[Dict[str, object]] = []
    for fp in _walk_tcx_files(input_dir):
        root = _read_tcx(fp)
        if root is None:
            warnings.warn(
                f"Skipping malformed or unreadable TCX file: {fp}",
                RuntimeWarning,
                stacklevel=2,
            )
            continue
        rows.extend(_parse_trackpoints(root, fp))

    columns = [
        "file_path",
        "activity_id",
        "sport",
        "time_utc",
        "latitude_deg",
        "longitude_deg",
        "altitude_m",
        "distance_m",
        "heart_rate_bpm",
        "cadence_rpm",
    ]
    return pd.DataFrame.from_records(rows, columns=columns)

