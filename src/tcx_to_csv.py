import argparse
import csv
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, Iterable, Iterator, List, Optional, Tuple
from xml.etree import ElementTree as ET
import pandas as pd


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


def write_summary_csv(input_dir: str, output_csv: str, conn) -> Tuple[int, int]:
    rows: List[ActivitySummary] = []

    # get processed files
    existing_files = pd.read_sql(
        "SELECT file_path FROM processed_tcx_files",
        conn
    )
    processed_set = set(existing_files["file_path"])

    # filter new files
    all_files = list(_walk_tcx_files(input_dir))
    new_files = [str(fp) for fp in all_files if str(fp) not in processed_set]

    print(f"New TCX files detected: {len(new_files)}")

    ok = 0
    bad = 0

    for fp in new_files:
        root = _read_tcx(fp)
        if root is None:
            bad += 1
            continue

        ok += 1
        rows.extend(_parse_activity_summary(root, fp))

    fieldnames = [
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

    with open(output_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in rows:
            w.writerow({
                "file_path": r.file_path,
                "activity_id": r.activity_id,
                "sport": r.sport,
                "start_time_utc": r.start_time_utc,
                "total_time_s": r.total_time_s,
                "distance_m": r.distance_m,
                "calories": r.calories,
                "avg_hr_bpm": r.avg_hr_bpm,
                "max_hr_bpm": r.max_hr_bpm,
                "avg_cadence_rpm": r.avg_cadence_rpm,
            })

    # mark processed
    if new_files:
        pd.DataFrame({"file_path": new_files}).to_sql(
            "processed_tcx_files",
            conn,
            if_exists="append",
            index=False
        )

    return ok, bad

def write_trackpoints_csv(input_dir: str, output_csv: str) -> Tuple[int, int, int]:
    files = list(_walk_tcx_files(input_dir))
    ok = 0
    bad = 0
    tp_count = 0

    fieldnames = [
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

    with open(output_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for fp in files:
            root = _read_tcx(fp)
            if root is None:
                bad += 1
                continue
            ok += 1
            for row in _parse_trackpoints(root, fp):
                w.writerow(row)
                tp_count += 1

    return ok, bad, tp_count


def main() -> int:
    p = argparse.ArgumentParser(
        description="Merge Nike Run Club (TCX) files into a single CSV."
    )
    p.add_argument("input_dir", help="Folder containing .tcx files (recursively searched).")
    p.add_argument("output_csv", help="Path to write the merged CSV.")
    p.add_argument(
        "--mode",
        choices=["summary", "trackpoints"],
        default="summary",
        help="summary = one row per activity; trackpoints = one row per trackpoint.",
    )
    args = p.parse_args()

    input_dir = os.path.abspath(args.input_dir)
    output_csv = os.path.abspath(args.output_csv)

    if not os.path.isdir(input_dir):
        raise SystemExit(f"Input directory not found: {input_dir}")

    if args.mode == "summary":
        ok, bad = write_summary_csv(input_dir, output_csv)
        print(f"Wrote {output_csv}")
        print(f"Parsed: {ok} files; skipped (bad XML/unreadable): {bad} files")
    else:
        ok, bad, tp_count = write_trackpoints_csv(input_dir, output_csv)
        print(f"Wrote {output_csv}")
        print(f"Parsed: {ok} files; skipped (bad XML/unreadable): {bad} files")
        print(f"Trackpoints written: {tp_count}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

