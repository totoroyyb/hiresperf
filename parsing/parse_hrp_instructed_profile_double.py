from dataclasses import dataclass
import numpy as np
import pandas as pd
import argparse

parser = argparse.ArgumentParser(description="Parse HRP instructed profile for two cores and compute overlap stats.")
parser.add_argument("--use_imc", action="store_true", help="Use IMC counters")
parser.add_argument("--use_offcore", action="store_true", help="Use offcore counters")
parser.add_argument("--use_write_est", action="store_true", help="Use write estimate counter")
parser.add_argument("--tsc_freq", type=float, required=True, help="TSC frequency in cycles per microsecond.")
parser.add_argument("--cpu_ids", type=int, nargs=2, required=True, help="Two CPU IDs to compare")
parser.add_argument("--bin_path", type=str, required=True, help="Path to the HRP instructed profile binary file")
args = parser.parse_args()

c1_name = None
c2_name = None

def prepare_core_name():
    global c1_name, c2_name
    if args.use_offcore:
        c1_name = 'offcore_read'
        c2_name = 'offcore_write_est' if args.use_write_est else 'offcore_write'
    else:
        c1_name = 'llc_misses'
        c2_name = 'sw_prefetch'

def read_logs_to_numpy(file_path: str) -> np.ndarray:
    global c1_name, c2_name
    if args.use_imc:
        dt = np.dtype([
            ('cpu_id', np.int32),
            ('timestamp', np.uint64),
            ('stall_mem', np.uint64),
            ('inst_retire', np.uint64),
            ('cpu_unhalt', np.uint64),
            (c1_name, np.uint64),
            (c2_name, np.uint64),
            ('imc_read', np.uint64),
            ('imc_write', np.uint64),
        ])
    else:
        dt = np.dtype([
            ('cpu_id', np.int32),
            ('timestamp', np.uint64),
            ('stall_mem', np.uint64),
            ('inst_retire', np.uint64),
            ('cpu_unhalt', np.uint64),
            (c1_name, np.uint64),
            (c2_name, np.uint64),
        ])
    try:
        return np.fromfile(file_path, dtype=dt)
    except Exception as e:
        print(f"Error reading file: {e}")
        return np.array([])

def read_into_df(file_path: str) -> pd.DataFrame:
    data = read_logs_to_numpy(file_path)
    return pd.DataFrame(data)

def pairwise_timestamps(timestamps: np.ndarray) -> list[tuple[int, int]]:
    timestamps.sort()
    return [(timestamps[i], timestamps[i + 1]) for i in range(0, len(timestamps) - 1, 2)]

def get_core_ranges(df: pd.DataFrame, cpu_id: int) -> list[tuple[int, int]]:
    df_core = df[df["cpu_id"] == cpu_id]
    timestamps = df_core["timestamp"].unique()
    return pairwise_timestamps(timestamps)

def get_overlapped_ranges(ranges1, ranges2) -> list[tuple[int, int]]:
    overlaps = []
    i, j = 0, 0
    while i < len(ranges1) and j < len(ranges2):
        s1, e1 = ranges1[i]
        s2, e2 = ranges2[j]
        start = max(s1, s2)
        end = min(e1, e2)
        if start < end:
            overlaps.append((start, end))
        if e1 < e2:
            i += 1
        else:
            j += 1
    return overlaps

def calc_diff_for_core(df: pd.DataFrame, cpu_id: int, t_start: int, t_end: int) -> dict:
    row_start = df[(df["cpu_id"] == cpu_id) & (df["timestamp"] == t_start)]
    row_end = df[(df["cpu_id"] == cpu_id) & (df["timestamp"] == t_end)]
    if row_start.empty or row_end.empty:
        return None

    return {
        "duration": (t_end - t_start) / args.tsc_freq / 1e3,
        "stall_mem": int(row_end["stall_mem"].values[0]) - int(row_start["stall_mem"].values[0]),
        "inst_retire": int(row_end["inst_retire"].values[0]) - int(row_start["inst_retire"].values[0]),
        "cpu_unhalt": int(row_end["cpu_unhalt"].values[0]) - int(row_start["cpu_unhalt"].values[0]),
        "c1": int(row_end[c1_name].values[0]) - int(row_start[c1_name].values[0]),
        "c2": int(row_end[c2_name].values[0]) - int(row_start[c2_name].values[0]),
    }

def average_rate_across_overlap(overlaps, df: pd.DataFrame) -> dict:
    totals = {"stall_mem": 0, "inst_retire": 0, "cpu_unhalt": 0, "c1": 0, "c2": 0}
    total_duration = 0

    for t_start, t_end in overlaps:
        d1 = calc_diff_for_core(df, args.cpu_ids[0], t_start, t_end)
        d2 = calc_diff_for_core(df, args.cpu_ids[1], t_start, t_end)
        if not d1 or not d2:
            continue
        duration = min(d1["duration"], d2["duration"])
        total_duration += duration
        for k in totals:
            totals[k] += (d1[k] + d2[k]) / 2

    if total_duration == 0:
        return {}

    return {k: v / total_duration for k, v in totals.items()}  # unit: per ms

def main():
    print(f"Analyzing overlap between CPU {args.cpu_ids[0]} and CPU {args.cpu_ids[1]}")
    if args.use_imc:
        print("IMC enabled (ignored in this version)")
    if args.use_offcore:
        print("Offcore counters used")
    if args.use_write_est:
        print("Using write estimate for offcore")

    prepare_core_name()
    df = read_into_df(args.bin_path)
    if df.empty:
        print("No data available.")
        return

    ranges1 = get_core_ranges(df, args.cpu_ids[0])
    ranges2 = get_core_ranges(df, args.cpu_ids[1])
    overlaps = get_overlapped_ranges(ranges1, ranges2)

    if not overlaps:
        print("No overlapping time ranges found.")
        return

    result = average_rate_across_overlap(overlaps, df)
    if not result:
        print("No valid data in overlap.")
        return

    print(f"Total Overlapping Periods: {len(overlaps)}")
    for k, v in result.items():
        print(f"Avg {k} rate: {v:.2f} /ms")
    print(f"Avg total transferred ((c1 + c2) * 64) rate: {(result['c1'] + result['c2']) * 64 / 1e6:.4f} MB/ms")

if __name__ == "__main__":
    main()
