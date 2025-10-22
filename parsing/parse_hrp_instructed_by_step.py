from dataclasses import dataclass, asdict
import numpy as np
import pandas as pd
import argparse

parser = argparse.ArgumentParser(description="Parse HRP instructed profile.")
parser.add_argument("--use_imc", action="store_true", help="Use IMC counters")
parser.add_argument("--use_offcore", action="store_true", help="Use offcore counters")
parser.add_argument("--use_write_est", action="store_true", help="Use write estimate counter")
parser.add_argument("--use_counter_combination", action="store_true", help="Use counter-combination (L1D/L2/L3 stall channels)")
parser.add_argument("--use_stall_total", action="store_true", help="PMC0 uses total stall event instead of LLC/offcore read")
parser.add_argument("--use_bound_on_loads", action="store_true", help="PMC2 uses bound-on-loads instead of mem stalls (when not in combination)")
parser.add_argument("--use_bound_on_stores", action="store_true", help="PMC3 uses bound-on-stores instead of stalls_sb (when not in combination)")

parser.add_argument("--tsc_ts", action="store_true", help="Timestamps are TSC cycles (else nanoseconds)")
parser.add_argument("--tsc_freq", type=float, required=True, help="TSC frequency in cycles per microsecond.")
parser.add_argument("--cpu_store_imc", type=int, default=0, help="CPU ID to store IMC data (default: 0)")
parser.add_argument("--cpu_id", type=int, default=10, help="The CPU ID to calculate the diff (default: 10). Use -1 to aggregate over all cores.")
parser.add_argument("--bin_path", type=str, default="/hrperf_log.bin", help="Path to the HRP instructed profile binary file (default: /hrperf_log.bin)")
parser.add_argument("--out_csv", type=str, required=True, help="Path to output CSV file containing per-loop stats")
args = parser.parse_args()

# Dynamic semantic names for PMC0/1 and aliases for PMC2/3
c1_name = None  # PMC0 semantic alias
c2_name = None  # PMC1 semantic alias
c3_name = None # PMC2 semantic alias based on flags (still read as 'stall_mem' in binary)
c4_name = None # PMC3 semantic alias based on flags (still read as 'stalls_sb' in binary)

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
    """Resolve semantic names following the kernel flag logic with minimal intrusion."""
    global c1_name, c2_name, c3_name, c4_name
    # PMC0 selection: stall_total overrides others, else offcore_read if offcore, else llc_misses
    if args.use_stall_total:
        c1_name = 'stall_total'
    elif args.use_offcore:
        c1_name = 'offcore_read'
    else:
        c1_name = 'llc_misses'

    # PMC1 selection: counter-combination overrides others -> L1D stall; else offcore write (or est) / sw_prefetch
    if args.use_counter_combination:
        c2_name = 'stall_l1d_miss'
    else:
        if args.use_offcore:
            c2_name = 'offcore_write_est' if args.use_write_est else 'offcore_write'
        else:
            c2_name = 'sw_prefetch'

    # PMC2 alias (binary field is still 'stall_mem')
    if args.use_counter_combination:
        c3_name = 'stall_l2_miss'
    else:
        c3_name = 'bound_on_loads' if args.use_bound_on_loads else 'stall_mem'

    # PMC3 alias (binary field is still 'stalls_sb')
    if args.use_counter_combination:
        c4_name = 'stall_l3_miss'
    else:
        c4_name = 'bound_on_stores' if args.use_bound_on_stores else 'stalls_sb'

def read_logs_to_numpy(file_path: str) -> np.ndarray:
    """Keep the binary field order; only the names for PMC0/1 are semantic aliases."""
    global c1_name, c2_name, args
    if args.use_imc:
        dt = np.dtype([
            ('cpu_id', np.int32),
            ('timestamp', np.uint64),
            ('stall_mem', np.uint64),   # PMC2 (semantics vary by flags)
            ('inst_retire', np.uint64),
            ('stalls_sb', np.uint64),   # PMC3 (semantics vary by flags)
            ('cpu_unhalt', np.uint64),
            (f'{c1_name}', np.uint64),  # PMC0 semantic alias
            (f'{c2_name}', np.uint64),  # PMC1 semantic alias
            ('imc_read', np.uint64),
            ('imc_write', np.uint64),
        ])
    else:
        dt = np.dtype([
            ('cpu_id', np.int32),
            ('timestamp', np.uint64),
            ('stall_mem', np.uint64),   # PMC2
            ('inst_retire', np.uint64),
            ('stalls_sb', np.uint64),   # PMC3
            ('cpu_unhalt', np.uint64),
            (f'{c1_name}', np.uint64),  # PMC0
            (f'{c2_name}', np.uint64),  # PMC1
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

    # Duration: TSC cycles -> ms if --tsc_ts; else nanoseconds -> ms
    if args.tsc_ts:
        duration_ms = (t_max - t_min) / args.tsc_freq / 1e3
    else:
        duration_ms = (t_max - t_min) / 1e6

    result = TimeRangeData(
        duration_ms=duration_ms,
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

    # Preserve channel-specific diffs from c1/c2 names
    df[f'{c1_name}_diff'] = df['c1_diff']
    df[f'{c2_name}_diff'] = df['c2_diff']

    # Also expose semantic aliases for PMC2/PMC3 based on flags
    if c3_name is not None:
        df[f'{c3_name}_diff'] = df['stall_mem_diff']
    if c4_name is not None:
        df[f'{c4_name}_diff'] = df['stalls_sb_diff']

    # Total transferred MB: only meaningful when using offcore and not in combination mode
    if args.use_offcore and not args.use_counter_combination:
        df['total_transferred_MB'] = (df['c1_diff'] + df['c2_diff']) * 64.0 / 1e6
    else:
        df['total_transferred_MB'] = np.nan

    # IMC total MB
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

    if c3_name is not None and f'{c3_name}_diff' in df.columns:
        preferred_cols.append(f'{c3_name}_diff')
    if c4_name is not None and f'{c4_name}_diff' in df.columns:
        preferred_cols.append(f'{c4_name}_diff')

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
    if args.use_counter_combination: print("Using counter-combination")
    if args.use_stall_total: print("PMC0 uses stall_total")
    if args.use_bound_on_loads: print("PMC2 uses bound_on_loads")
    if args.use_bound_on_stores: print("PMC3 uses bound_on_stores")
    if args.tsc_ts: print("Timestamps interpreted as TSC cycles")

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
