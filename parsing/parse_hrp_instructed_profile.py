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
        stall_mem_diff=diff['stall_mem_diff'].sum(),
        inst_retire_diff=diff['inst_retire_diff'].sum(),
        stalls_sb_diff=diff['stalls_sb_diff'].sum(),
        cpu_unhalt_diff=diff['cpu_unhalt_diff'].sum(),
        c1_diff=diff[c1_diff_name].sum(),
        c2_diff=diff[c2_diff_name].sum(),
        imc_read_diff=0,
        imc_write_diff=0,
        start_ts=t_min,
        end_ts=t_max
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

def print_avg_time_ranges_data(data_list: list[TimeRangeData]):
    if not data_list:
        return
    avg = lambda attr: np.mean([getattr(d, attr) for d in data_list])
    print(f"Total loops: {len(data_list)}")
    print(f"Avg Duration (ms): {avg('duration_ms'):.2f}")
    print(f"Avg stall_mem diff: {avg('stall_mem_diff')}")
    print(f"Avg inst_retire diff: {avg('inst_retire_diff')}")
    print(f"Avg stalls_sb diff: {avg('stalls_sb_diff')}")
    print(f"Avg cpu_unhalt diff: {avg('cpu_unhalt_diff')}")
    print(f"Avg {c1_name} diff: {avg('c1_diff')}")
    print(f"Avg {c2_name} diff: {avg('c2_diff')}")
    print(f"Avg total transferred ((c1+c2)*64): {(avg('c1_diff') + avg('c2_diff')) * 64 / 1e6:.2f} MB")
    print(f"Avg IMC read diff: {avg('imc_read_diff')}")
    print(f"Avg IMC write diff: {avg('imc_write_diff')}")
    print(f"Avg IMC total transferred ((read+write)*64): {(avg('imc_read_diff') + avg('imc_write_diff')) * 64 / 1e6:.2f} MB")

def main():
    if args.use_imc: print("Using IMC")
    if args.use_offcore: print("Using offcore")
    if args.use_write_est: print("Using write estimate")
    file_path = args.bin_path
    results = parse_hrp_instructed_profile(file_path)
    print_avg_time_ranges_data(results)

if __name__ == "__main__":
    prepare_core_name()
    main()
