from dataclasses import dataclass
import numpy as np
import pandas as pd
import argparse

parser = argparse.ArgumentParser(description="Parse HRP instructed profile.")
parser.add_argument("--use_imc", action="store_true", help="Use IMC counters")
parser.add_argument("--use_offcore", action="store_true", help="Use offcore counters")
parser.add_argument("--use_write_est", action="store_true", help="Use write estimate counter")
parser.add_argument("--tsc_freq", type=float, required=True, help="TSC frequency in cycles per microsecond.")
parser.add_argument("--cpu_store_imc", type=int, default=0, help="CPU ID to store IMC data (default: 0)")
parser.add_argument("--cpu_id", type=int, default=10, help="The CPU ID to calculate the diff (default: 10). Use -1 to aggregate over all cores.")
parser.add_argument("--bin_path", type=str, default="/hrperf_log.bin", help="Path to the HRP instructed profile binary file (default: /hrperf_log.bin)")
args = parser.parse_args()

c1_name = None
c2_name = None

@dataclass
class TimeRangeData:
    duration_ms: float
    stall_mem_diff: int
    inst_retire_diff: int
    cpu_unhalt_diff: int
    c1_diff: int
    c2_diff: int
    imc_read_diff: int
    imc_write_diff: int
    start_ts: int
    end_ts: int

def prepare_core_name():
    global c1_name, c2_name
    if args.use_offcore:
        c1_name = 'offcore_read'
        c2_name = 'offcore_write_est' if args.use_write_est else 'offcore_write'
    else:
        c1_name = 'llc_misses'
        c2_name = 'sw_prefetch'

def read_logs_to_numpy(file_path: str) -> np.ndarray:
    global c1_name, c2_name, args
    if args.use_imc:
        dt = np.dtype([
            ('cpu_id', np.int32),
            ('timestamp', np.uint64),
            ('stall_mem', np.uint64),
            ('inst_retire', np.uint64),
            ('cpu_unhalt', np.uint64),
            (f'{c1_name}', np.uint64),
            (f'{c2_name}', np.uint64),
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
            (f'{c1_name}', np.uint64),
            (f'{c2_name}', np.uint64),
        ])
    try:
        data = np.fromfile(file_path, dtype=dt)
        return data
    except Exception as e:
        print(f"Error reading file: {e}")
        return np.array([])

def read_into_df(file_path: str) -> pd.DataFrame:
    data = read_logs_to_numpy(file_path)
    return pd.DataFrame(data)

def get_all_time_ranges(df: pd.DataFrame) -> list[tuple[int, int]]:
    timestamps = df['timestamp'].unique()
    timestamps.sort()
    return [(timestamps[i], timestamps[i+1]) for i in range(0, len(timestamps)-1, 2)]

def calc_data_in_range(time_range: tuple[int, int], df: pd.DataFrame) -> TimeRangeData:
    global c1_name, c2_name, args
    c1_diff_name = f'{c1_name}_diff'
    c2_diff_name = f'{c2_name}_diff'
    start, end = time_range

    df_range = df[(df['timestamp'] >= start) & (df['timestamp'] <= end)]
    assert len(df_range["timestamp"].unique()) == 2, "Must have two timestamps per range"

    t_min, t_max = df_range['timestamp'].min(), df_range['timestamp'].max()
    cols = ['cpu_id', 'stall_mem', 'inst_retire', 'cpu_unhalt', f'{c1_name}', f'{c2_name}']
    df_start = df[df['timestamp'] == t_min][cols]
    df_end = df[df['timestamp'] == t_max][cols]

    merged = pd.merge(df_end, df_start, on='cpu_id', suffixes=('_end', '_start'))
    diff = pd.DataFrame({
        'cpu_id': merged['cpu_id'],
        'stall_mem_diff': merged['stall_mem_end'] - merged['stall_mem_start'],
        'inst_retire_diff': merged['inst_retire_end'] - merged['inst_retire_start'],
        'cpu_unhalt_diff': merged['cpu_unhalt_end'] - merged['cpu_unhalt_start'],
        f'{c1_diff_name}': merged[f'{c1_name}_end'] - merged[f'{c1_name}_start'],
        f'{c2_diff_name}': merged[f'{c2_name}_end'] - merged[f'{c2_name}_start'],
    })

    if args.cpu_id != -1:
        diff = diff[diff['cpu_id'] == args.cpu_id]

    result = TimeRangeData(
        duration_ms=(t_max - t_min) / args.tsc_freq / 1e3,
        stall_mem_diff=int(diff['stall_mem_diff'].sum()),
        inst_retire_diff=int(diff['inst_retire_diff'].sum()),
        cpu_unhalt_diff=int(diff['cpu_unhalt_diff'].sum()),
        c1_diff=int(diff[c1_diff_name].sum()),
        c2_diff=int(diff[c2_diff_name].sum()),
        imc_read_diff=0,
        imc_write_diff=0,
        start_ts=int(t_min),
        end_ts=int(t_max)
    )

    if args.use_imc:
        imc_cols = ['imc_read', 'imc_write']
        imc_start = df[(df['timestamp'] == t_min) & (df['cpu_id'] == args.cpu_store_imc)][imc_cols]
        imc_end   = df[(df['timestamp'] == t_max) & (df['cpu_id'] == args.cpu_store_imc)][imc_cols]
        if not imc_start.empty and not imc_end.empty:
            result.imc_read_diff  = int(imc_end['imc_read'].values[0])  - int(imc_start['imc_read'].values[0])
            result.imc_write_diff = int(imc_end['imc_write'].values[0]) - int(imc_start['imc_write'].values[0])
    return result

def parse_hrp_instructed_profile(file_path: str) -> list[TimeRangeData]:
    df = read_into_df(file_path)
    if df.empty:
        print("No data to parse.")
        return []
    ranges = get_all_time_ranges(df)
    return [calc_data_in_range(r, df) for r in ranges]

def filter_outliers_95(data_list: list[TimeRangeData], use_imc: bool) -> list[TimeRangeData]:
    """Keep central 95% (2.5–97.5 pct). Drop full loop if any checked var is an outlier."""
    if not data_list:
        return data_list

    # Build DataFrame for percentile calc
    rows = []
    for d in data_list:
        rows.append({
            "duration_ms": d.duration_ms,
            "stall_mem_diff": d.stall_mem_diff,
            "inst_retire_diff": d.inst_retire_diff,
            "cpu_unhalt_diff": d.cpu_unhalt_diff,
            "c1_diff": d.c1_diff,
            "c2_diff": d.c2_diff,
            "imc_read_diff": d.imc_read_diff,
            "imc_write_diff": d.imc_write_diff,
            "start_ts": d.start_ts,
            "end_ts": d.end_ts,
        })
    df = pd.DataFrame(rows)

    # Variables to filter on (diffs only)
    vars_to_check = ["stall_mem_diff", "inst_retire_diff", "cpu_unhalt_diff", "c1_diff", "c2_diff"]
    if use_imc:
        vars_to_check += ["imc_read_diff", "imc_write_diff"]

    # Compute 2.5 and 97.5 percentiles per variable
    bounds = {}
    for v in vars_to_check:
        # guard against constant columns
        if df[v].nunique(dropna=False) <= 1:
            lo, hi = df[v].min(), df[v].max()
        else:
            lo, hi = np.percentile(df[v], [2.5, 97.5])
        bounds[v] = (lo, hi)

    # Combined mask: inside bounds for ALL checked variables
    mask = pd.Series(True, index=df.index)
    for v, (lo, hi) in bounds.items():
        mask &= (df[v] >= lo) & (df[v] <= hi)

    kept = df[mask]
    dropped = len(df) - len(kept)
    print(f"Outlier filtering (central 95% per variable): kept {len(kept)} / {len(df)} loops, dropped {dropped}.")

    # Rebuild TimeRangeData list from kept rows
    kept_list = []
    for _, r in kept.iterrows():
        kept_list.append(TimeRangeData(
            duration_ms=float(r["duration_ms"]),
            stall_mem_diff=int(r["stall_mem_diff"]),
            inst_retire_diff=int(r["inst_retire_diff"]),
            cpu_unhalt_diff=int(r["cpu_unhalt_diff"]),
            c1_diff=int(r["c1_diff"]),
            c2_diff=int(r["c2_diff"]),
            imc_read_diff=int(r["imc_read_diff"]),
            imc_write_diff=int(r["imc_write_diff"]),
            start_ts=int(r["start_ts"]),
            end_ts=int(r["end_ts"]),
        ))
    return kept_list

def print_stats(data_list: list[TimeRangeData]):
    if not data_list:
        print("No data to report after filtering.")
        return

    def mean_sd(attr):
        vals = np.array([getattr(d, attr) for d in data_list], dtype=float)
        avg = float(np.mean(vals))
        sd = float(np.std(vals, ddof=1)) if len(vals) > 1 else 0.0
        return avg, sd

    def pr(name, attr, fmt="{:.2f}"):
        avg, sd = mean_sd(attr)
        if isinstance(avg, float):
            print(f"{name}: avg={fmt.format(avg)}, sd={fmt.format(sd)}")
        else:
            print(f"{name}: avg={avg}, sd={sd}")

    print(f"Total loops (after filtering): {len(data_list)}")
    pr("Duration (ms)", "duration_ms", "{:.2f}")
    pr("stall_mem diff", "stall_mem_diff", "{:.2f}")
    pr("inst_retire diff", "inst_retire_diff", "{:.2f}")
    pr("cpu_unhalt diff", "cpu_unhalt_diff", "{:.2f}")
    pr(f"{c1_name} diff", "c1_diff", "{:.2f}")
    pr(f"{c2_name} diff", "c2_diff", "{:.2f}")

    # transferred counters
    avg_c, sd_c = mean_sd("c1_diff")
    avg_w, sd_w = mean_sd("c2_diff")
    # Propagate SD for sum via independence assumption (rough), else compute directly from series:
    total_vals = np.array([d.c1_diff + d.c2_diff for d in data_list], dtype=float)
    total_avg = float(np.mean(total_vals))
    total_sd = float(np.std(total_vals, ddof=1)) if len(total_vals) > 1 else 0.0
    print(f"Total transferred ((c1+c2)*64) MB: avg={(total_avg*64/1e6):.2f}, sd={(total_sd*64/1e6):.2f}")

    # IMC if available
    if args.use_imc:
        pr("IMC read diff", "imc_read_diff", "{:.2f}")
        pr("IMC write diff", "imc_write_diff", "{:.2f}")
        imc_total = np.array([d.imc_read_diff + d.imc_write_diff for d in data_list], dtype=float)
        imc_avg = float(np.mean(imc_total))
        imc_sd = float(np.std(imc_total, ddof=1)) if len(imc_total) > 1 else 0.0
        print(f"IMC total transferred ((read+write)*64) MB: avg={(imc_avg*64/1e6):.2f}, sd={(imc_sd*64/1e6):.2f}")

def main():
    if args.use_imc: print("Using IMC")
    if args.use_offcore: print("Using offcore")
    if args.use_write_est: print("Using write estimate")
    file_path = args.bin_path

    raw = parse_hrp_instructed_profile(file_path)
    print(f"Total loops (raw): {len(raw)}")
    filtered = filter_outliers_95(raw, args.use_imc)
    print_stats(filtered)

if __name__ == "__main__":
    prepare_core_name()
    main()
