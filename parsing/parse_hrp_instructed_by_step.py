from dataclasses import dataclass, asdict
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
parser.add_argument("--out_csv", type=str, required=True, help="Path to output CSV file containing per-loop stats")
args = parser.parse_args()

c1_name = None
c2_name = None

@dataclass
class TimeRangeData:
    duration_ms: float
    stall_mem_diff: int
    inst_retire_diff: int
    stalls_sb_diff: int
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
            ('stalls_sb', np.uint64),
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
            ('stalls_sb', np.uint64),
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
    cols = ['cpu_id', 'stall_mem', 'inst_retire', 'stalls_sb', 'cpu_unhalt', f'{c1_name}', f'{c2_name}']
    df_start = df[df['timestamp'] == t_min][cols]
    df_end = df[df['timestamp'] == t_max][cols]

    merged = pd.merge(df_end, df_start, on='cpu_id', suffixes=('_end', '_start'))
    diff = pd.DataFrame({
        'cpu_id': merged['cpu_id'],
        'stall_mem_diff': merged['stall_mem_end'] - merged['stall_mem_start'],
        'inst_retire_diff': merged['inst_retire_end'] - merged['inst_retire_start'],
        'stalls_sb_diff': merged['stalls_sb_end'] - merged['stalls_sb_start'],
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
        stalls_sb_diff=int(diff['stalls_sb_diff'].sum()),
        cpu_unhalt_diff=int(diff['cpu_unhalt_diff'].sum()),
        c1_diff=int(diff[f'{c1_diff_name}'].sum()),
        c2_diff=int(diff[f'{c2_diff_name}'].sum()),
        imc_read_diff=0,
        imc_write_diff=0,
        start_ts=int(t_min),
        end_ts=int(t_max)
    )

    if args.use_imc:
        imc_cols = ['imc_read', 'imc_write']
        imc_start = df[(df['timestamp'] == t_min) & (df['cpu_id'] == args.cpu_store_imc)][imc_cols]
        imc_end = df[(df['timestamp'] == t_max) & (df['cpu_id'] == args.cpu_store_imc)][imc_cols]
        if not imc_start.empty and not imc_end.empty:
            result.imc_read_diff = int(imc_end['imc_read'].values[0]) - int(imc_start['imc_read'].values[0])
            result.imc_write_diff = int(imc_end['imc_write'].values[0]) - int(imc_start['imc_write'].values[0])
    return result

def parse_hrp_instructed_profile(file_path: str) -> list[TimeRangeData]:
    df = read_into_df(file_path)
    if df.empty:
        print("No data to parse.")
        return []
    ranges = get_all_time_ranges(df)
    return [calc_data_in_range(r, df) for r in ranges]

def results_to_dataframe(data_list: list[TimeRangeData]) -> pd.DataFrame:
    """Convert per-loop results to a DataFrame and add derived per-loop metrics."""
    if not data_list:
        return pd.DataFrame()

    rows = [asdict(d) for d in data_list]
    df = pd.DataFrame(rows)

    df[f'{c1_name}_diff'] = df['c1_diff']
    df[f'{c2_name}_diff'] = df['c2_diff']

    df['total_transferred_MB'] = (df['c1_diff'] + df['c2_diff']) * 64.0 / 1e6

    df['imc_total_transferred_MB'] = (df['imc_read_diff'] + df['imc_write_diff']) * 64.0 / 1e6

    eps = 1e-12
    denom = df['cpu_unhalt_diff'].astype(float).clip(lower=eps)

    df['load_mem_cycle_stall_ratio'] = df['stall_mem_diff'] / denom
    df['total_mem_cycle_stall'] = df['stall_mem_diff'] + df['stalls_sb_diff']
    df['total_mem_cycle_stall_ratio'] = df['total_mem_cycle_stall'] / denom
    df['total_instruction_ratio'] = df['inst_retire_diff'] / denom

    preferred_cols = [
        'start_ts', 'end_ts', 'duration_ms',
        'stall_mem_diff', 'stalls_sb_diff', 'cpu_unhalt_diff',
        'inst_retire_diff',
        'c1_diff', f'{c1_name}_diff',
        'c2_diff', f'{c2_name}_diff',
        'total_transferred_MB'
    ]
    if 'imc_read_diff' in df.columns and 'imc_write_diff' in df.columns:
        preferred_cols += ['imc_read_diff', 'imc_write_diff', 'imc_total_transferred_MB']

    preferred_cols += [
        'load_mem_cycle_stall_ratio',
        'total_mem_cycle_stall',
        'total_mem_cycle_stall_ratio',
        'total_instruction_ratio'
    ]

    seen, ordered_cols = set(), []
    for c in preferred_cols:
        if c in df.columns and c not in seen:
            ordered_cols.append(c); seen.add(c)

    for c in df.columns:
        if c not in seen:
            ordered_cols.append(c); seen.add(c)

    return df[ordered_cols]

def main():
    if args.use_imc: print("Using IMC")
    if args.use_offcore: print("Using offcore")
    if args.use_write_est: print("Using write estimate")

    file_path = args.bin_path
    results = parse_hrp_instructed_profile(file_path)
    if not results:
        return

    df_out = results_to_dataframe(results)
    df_out.to_csv(args.out_csv, index=False)
    print(f"Wrote {len(df_out)} loops to CSV: {args.out_csv}")

if __name__ == "__main__":
    prepare_core_name()
    main()
