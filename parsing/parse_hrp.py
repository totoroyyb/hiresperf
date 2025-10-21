import sys
import os
import duckdb
import argparse
import polars as pl
import numpy as np


def read_logs_to_numpy(
    file_path: str,
    use_imc: bool = False,
    use_rdt: bool = False,
    use_rdt_local_bw: bool = False,
) -> np.ndarray:
    fields = [
        ("cpu_id", np.int32),
        ("timestamp", np.uint64),
        ("stall_mem", np.uint64),
        ("inst_retire", np.uint64),
        ("stalls_sb", np.uint64),
        ("cpu_unhalt", np.uint64),
        ("llc_misses", np.uint64),
        ("sw_prefetch", np.uint64),
    ]
    if use_imc:
        fields.extend([("imc_read", np.uint64), ("imc_write", np.uint64)])
    if use_rdt:
        fields.append(("total_bw", np.uint64))
        if use_rdt_local_bw:
            fields.append(("local_bw", np.uint64))
        fields.append(("occupancy", np.uint64))

    dt = np.dtype(fields)
    print("Reading binary file into NumPy array...")
    try:
        data = np.fromfile(file_path, dtype=dt)
        print(f"Read {len(data)} records to successfully.")
        return data
    except Exception as e:
        print(f"Error reading file with NumPy: {e}")
        return np.array([])


def create_tables_dynamic(
    con: duckdb.DuckDBPyConnection,
    rate_cols: list[str],
    include_mem_bw: bool,
    include_raw_cols: list[str],
    use_imc: bool,
    use_rdt: bool,
    use_rdt_local_bw: bool,
):
    cols_sql = []
    cols_sql.append('id BIGINT')
    cols_sql.append('cpu_id INTEGER')
    cols_sql.append('timestamp_ns UBIGINT')  # true nanoseconds now
    for c in rate_cols:
        cols_sql.append(f'"{c}" DOUBLE')
    if include_mem_bw:
        cols_sql.append('memory_bandwidth_bytes_per_us DOUBLE')
    cols_sql.append('time_delta_ns UBIGINT')
    for c in include_raw_cols:
        cols_sql.append(f'"{c}" UBIGINT')
    if use_imc:
        cols_sql.append('imc_read UBIGINT')
        cols_sql.append('imc_write UBIGINT')
    if use_rdt:
        cols_sql.append('total_bw UBIGINT')
        if use_rdt_local_bw:
            cols_sql.append('local_bw UBIGINT')
        cols_sql.append('occupancy UBIGINT')

    schema = ",\n    ".join(cols_sql)
    con.execute(f"CREATE TABLE IF NOT EXISTS performance_events ({schema})")
    con.execute("""
        CREATE TABLE IF NOT EXISTS node_memory_bandwidth (
            id BIGINT,
            start_time_ns UBIGINT,
            end_time_ns UBIGINT,
            memory_bandwidth_bytes_per_us DOUBLE
        )
    """)


def parse_hrperf_log_polars(
    perf_log_path: str,
    use_raw: bool,
    use_tsc_ts: bool,
    tsc_per_us: float,
    use_offcore: bool,
    use_imc: bool,
    db_path: str,
    use_write_est: bool,
    use_rdt: bool,
    use_rdt_local_bw: bool,
    rdt_scaling: int | None,
    use_counter_combination: bool,
    use_stall_total: bool,
    use_bound_on_loads: bool,
    use_bound_on_stores: bool,
    cpu_id: int,
):
    print("Reading all log entries into memory...")
    numpy_data = read_logs_to_numpy(perf_log_path, use_imc, use_rdt, use_rdt_local_bw)
    if numpy_data.size == 0:
        print("Log file is empty or could not be read.")
        return

    print("Converting to Polars DataFrame...")
    df = pl.from_numpy(numpy_data)

    # CPU filter first
    if cpu_id != -1:
        print(f"Filtering to cpu_id == {cpu_id}")
        df = df.filter(pl.col("cpu_id") == cpu_id)
        if df.height == 0:
            print(f"No rows for cpu_id={cpu_id}.")
            return

    # RDT scaling
    if use_rdt and rdt_scaling is not None:
        print(f"Applying RDT scaling factor: {rdt_scaling}")
        df = df.with_columns(
            (pl.col("total_bw") * rdt_scaling).alias("total_bw"),
            (pl.col("occupancy") * rdt_scaling).alias("occupancy"),
        )
        if use_rdt_local_bw:
            df = df.with_columns((pl.col("local_bw") * rdt_scaling).alias("local_bw"))

    print(f"Total log entries read: {df.height}")
    if df.height == 0:
        print("No data remaining after filtering.")
        return

    if use_tsc_ts:
        # timestamp is in cycles => ns = cycles * 1000 / (cycles/us)
        ts_ns = (pl.col("timestamp") * 1000.0) / tsc_per_us
        df = df.with_columns(pl.when(ts_ns.is_not_null())
                               .then(ts_ns.round(0).cast(pl.UInt64, strict=False))
                               .otherwise(None)
                               .alias("timestamp_ns"))
    else:
        # timestamp already in ns (ktime), ensure UInt64
        df = df.with_columns(pl.col("timestamp").cast(pl.UInt64, strict=False).alias("timestamp_ns"))

    # Prepare prev values (per CPU)
    df = df.sort(["cpu_id", "timestamp"])
    df = df.with_columns(
        pl.col("timestamp").shift(1).over("cpu_id").alias("prev_timestamp"),
        pl.col("stall_mem").shift(1).over("cpu_id").alias("prev_stall_mem"),
        pl.col("inst_retire").shift(1).over("cpu_id").alias("prev_inst_retire"),
        pl.col("stalls_sb").shift(1).over("cpu_id").alias("prev_stalls_sb"),
        pl.col("cpu_unhalt").shift(1).over("cpu_id").alias("prev_cpu_unhalt"),
        pl.col("llc_misses").shift(1).over("cpu_id").alias("prev_llc_misses"),
        pl.col("sw_prefetch").shift(1).over("cpu_id").alias("prev_sw_prefetch"),
    )

    # time delta (ns/us) — only for non-stall rates & cpu usage
    if use_tsc_ts:
        time_delta_ns = ((pl.col("timestamp") - pl.col("prev_timestamp")) * 1000.0) / tsc_per_us
    else:
        time_delta_ns = pl.col("timestamp") - pl.col("prev_timestamp")

    df = df.with_columns(
        time_delta_ns=time_delta_ns.round(0).cast(pl.UInt64, strict=False),
        time_delta_us=(time_delta_ns / 1000.0),
    ).filter(pl.col("time_delta_us") > 0)

    # diffs
    df = df.with_columns(
        unhalt_diff=(pl.col("cpu_unhalt") - pl.col("prev_cpu_unhalt")),
        inst_diff=(pl.col("inst_retire") - pl.col("prev_inst_retire")),
        stall_mem_diff=(pl.col("stall_mem") - pl.col("prev_stall_mem")),
        stalls_sb_diff=(pl.col("stalls_sb") - pl.col("prev_stalls_sb")),
        llc_miss_diff=(pl.col("llc_misses") - pl.col("prev_llc_misses")),
        sw_prefetch_diff=(pl.col("sw_prefetch") - pl.col("prev_sw_prefetch")),
    )

    # non-stall rates by duration
    df = df.with_columns(
        inst_retire_rate=(pl.col("inst_diff") / pl.col("time_delta_us")),
        cpu_usage=(pl.col("unhalt_diff") / (tsc_per_us * pl.col("time_delta_us"))),
        llc_misses_rate=(pl.col("llc_miss_diff") / pl.col("time_delta_us")),
        sw_prefetch_rate=(pl.col("sw_prefetch_diff") / pl.col("time_delta_us")),
    )

    # stall rates by unhalt cycles
    df = df.with_columns(
        stalls_per_us=pl.when(pl.col("unhalt_diff") > 0)
                        .then(pl.col("stall_mem_diff") / pl.col("unhalt_diff"))
                        .otherwise(None),
        stalls_sb_rate=pl.when(pl.col("unhalt_diff") > 0)
                         .then(pl.col("stalls_sb_diff") / pl.col("unhalt_diff"))
                         .otherwise(None),
    )

    # dynamic renaming
    rate_renames = {}
    raw_renames = {}
    if use_offcore:
        rate_renames["llc_misses_rate"] = "offcore_read_rate"
        rate_renames["sw_prefetch_rate"] = "write_estimate_rate" if use_write_est else "offcore_write_rate"
        raw_renames["llc_misses"] = "offcore_read"
        raw_renames["sw_prefetch"] = "write_estimate" if use_write_est else "offcore_write"
    else:
        if use_counter_combination:
            df = df.with_columns(
                stalls_total_rate=pl.when(pl.col("unhalt_diff") > 0)
                                    .then(pl.col("llc_miss_diff") / pl.col("unhalt_diff"))
                                    .otherwise(None),
                stalls_l1d_miss_rate=pl.when(pl.col("unhalt_diff") > 0)
                                       .then(pl.col("sw_prefetch_diff") / pl.col("unhalt_diff"))
                                       .otherwise(None),
                stalls_l2_miss_rate=pl.col("stalls_per_us"),
                stalls_l3_miss_rate=pl.col("stalls_sb_rate"),
            )
            raw_renames["llc_misses"] = "stalls_total"
            raw_renames["sw_prefetch"] = "stalls_l1d_miss"
            raw_renames["stall_mem"] = "stalls_l2_miss"
            raw_renames["stalls_sb"] = "stalls_l3_miss"
        else:
            if use_bound_on_loads:
                rate_renames["stalls_per_us"] = "bound_on_loads_rate"
                raw_renames["stall_mem"] = "bound_on_loads"
            if use_bound_on_stores:
                rate_renames["stalls_sb_rate"] = "bound_on_stores_rate"
                raw_renames["stalls_sb"] = "bound_on_stores"

    if rate_renames:
        df = df.rename({k: v for k, v in rate_renames.items() if k in df.columns})
    if raw_renames:
        df = df.rename({k: v for k, v in raw_renames.items() if k in df.columns})

    # pick rate cols
    rate_cols = []
    if use_counter_combination and not use_offcore:
        for c in ["stalls_total_rate", "stalls_l1d_miss_rate", "stalls_l2_miss_rate", "stalls_l3_miss_rate"]:
            if c in df.columns:
                rate_cols.append(c)
    else:
        for c in ["stalls_per_us", "stalls_sb_rate", "bound_on_loads_rate", "bound_on_stores_rate"]:
            if c in df.columns:
                rate_cols.append(c)
    for c in ["inst_retire_rate", "cpu_usage"]:
        if c in df.columns:
            rate_cols.append(c)

    # bandwidth (duration-based)
    rw_pair = None
    if use_offcore:
        cand = ("offcore_read_rate", "write_estimate_rate" if use_write_est else "offcore_write_rate")
        if all(c in df.columns for c in cand):
            rw_pair = cand
            rate_cols.extend(list(cand))
    else:
        if not use_counter_combination:
            cand = ("llc_misses_rate", "sw_prefetch_rate")
            if all(c in df.columns for c in cand):
                rw_pair = cand
                rate_cols.extend(list(cand))

    include_mem_bw = False
    if rw_pair:
        include_mem_bw = True
        df = df.with_columns((pl.col(rw_pair[0]) + pl.col(rw_pair[1])) * 64.0
                             .alias("memory_bandwidth_bytes_per_us"))

    # final cols
    final_cols = ["cpu_id", "timestamp_ns"]
    final_cols.extend(rate_cols)
    if include_mem_bw:
        final_cols.append("memory_bandwidth_bytes_per_us")
    final_cols.append("time_delta_ns")

    raw_cols = []
    if use_raw:
        base_raw = ["stall_mem", "inst_retire", "stalls_sb", "cpu_unhalt", "llc_misses", "sw_prefetch"]
        base_raw = [raw_renames.get(c, c) for c in base_raw]
        raw_cols = [c for c in base_raw if c in df.columns]
        final_cols.extend(raw_cols)

    if use_imc:
        for c in ["imc_read", "imc_write"]:
            if c in df.columns:
                final_cols.append(c)
    if use_rdt:
        if "total_bw" in df.columns:
            final_cols.append("total_bw")
        if use_rdt_local_bw and "local_bw" in df.columns:
            final_cols.append("local_bw")
        if "occupancy" in df.columns:
            final_cols.append("occupancy")

    perf_df = df.select([c for c in final_cols if c in df.columns]).with_row_index("id", offset=1)

    # node bandwidth
    print("Calculating node-wide memory bandwidth...")
    node_bw_df = None
    if include_mem_bw and "memory_bandwidth_bytes_per_us" in perf_df.columns:
        node_bw_df = (
            perf_df.select(["timestamp_ns", "memory_bandwidth_bytes_per_us"])
            .group_by("timestamp_ns", maintain_order=True)
            .agg(pl.sum("memory_bandwidth_bytes_per_us").alias("memory_bandwidth_bytes_per_us"))
            .sort("timestamp_ns")
        )
        node_bw_df = node_bw_df.with_columns(
            start_time_ns=pl.col("timestamp_ns"),
            end_time_ns=pl.col("timestamp_ns").shift(-1),
        ).drop_nulls()
        node_bw_df = node_bw_df.select(["start_time_ns", "end_time_ns", "memory_bandwidth_bytes_per_us"])
        node_bw_df = node_bw_df.with_row_index("id", offset=1)

    print("Writing data to DuckDB...")
    con = duckdb.connect(database=db_path)
    create_tables_dynamic(
        con=con,
        rate_cols=[c for c in rate_cols if c in perf_df.columns],
        include_mem_bw=include_mem_bw and ("memory_bandwidth_bytes_per_us" in perf_df.columns),
        include_raw_cols=raw_cols,
        use_imc=use_imc,
        use_rdt=use_rdt,
        use_rdt_local_bw=use_rdt_local_bw,
    )

    con.register("perf_df", perf_df.to_arrow())
    col_list = ", ".join(f'"{c}"' for c in perf_df.columns)
    con.execute(f'INSERT INTO performance_events ({col_list}) SELECT {col_list} FROM perf_df')

    if node_bw_df is not None:
        con.register("node_bw_df", node_bw_df.to_arrow())
        con.execute(
            'INSERT INTO node_memory_bandwidth (id, start_time_ns, end_time_ns, memory_bandwidth_bytes_per_us) '
            'SELECT id, start_time_ns, end_time_ns, memory_bandwidth_bytes_per_us FROM node_bw_df'
        )
    con.close()

    print(f"Processed performance data has been inserted into 'performance_events' table in '{db_path}'.")
    if node_bw_df is not None:
        print(f"Node memory bandwidth data has been inserted into 'node_memory_bandwidth' table in '{db_path}'.")


def main():
    parser = argparse.ArgumentParser(
        description="Parse hiresperf log files and store results in DuckDB (flag-driven, robust timestamps)."
    )
    parser.add_argument("perf_log_path", type=str, help="Path to the hiresperf log file.")
    parser.add_argument("--raw_counter", action="store_true", help="Include raw counter data in database.")
    parser.add_argument("--tsc_ts", action="store_true", help="Use TSC timestamps instead of ktime.")
    parser.add_argument("--tsc_freq", type=float, required=True, help="TSC frequency in cycles per microsecond.")
    parser.add_argument("--use_offcore", action="store_true", help="Binary uses offcore read/write counters.")
    parser.add_argument("--use_write_est", action="store_true", help="Offcore write channel is write-estimation.")
    parser.add_argument("--use_counter_combination", action="store_true",
                        help="Counter-combination: total/L1D/L2/L3 stall channels.")
    parser.add_argument("--use_stall_total", action="store_true",
                        help="When NOT in counter-combination: PMC0 uses total stall event.")
    parser.add_argument("--use_bound_on_loads", action="store_true",
                        help="When NOT in counter-combination: PMC2 is bound-on-loads (renamed and normalized).")
    parser.add_argument("--use_bound_on_stores", action="store_true",
                        help="When NOT in counter-combination: PMC3 is bound-on-stores (renamed and normalized).")
    parser.add_argument("--use_imc", action="store_true", help="Binary includes IMC fields.")
    parser.add_argument("--use_rdt", action="store_true", help="Binary includes RDT counters.")
    parser.add_argument("--use_rdt_local_bw", action="store_true", help="Binary includes RDT local bandwidth.")
    parser.add_argument("--rdt_scaling", type=int, help="RDT scaling factor (required if --use_rdt).")
    parser.add_argument("--cpu_id", type=int, default=10,
                        help="Filter to a single CPU ID (default: 10). Use -1 to aggregate all CPUs.")
    parser.add_argument("--db_path", type=str, default="analysis.duckdb",
                        help="Path to the DuckDB database file to store results.")
    args = parser.parse_args()

    if not os.path.isfile(args.perf_log_path):
        print(f"Error: File '{args.perf_log_path}' does not exist.")
        sys.exit(1)
    if args.use_rdt and args.rdt_scaling is None:
        print("Error: --rdt_scaling is required when --use_rdt is specified.")
        sys.exit(1)
    if not os.path.isabs(args.db_path):
        args.db_path = os.path.abspath(args.db_path)

    print(f"Using TSC timestamps: {args.tsc_ts}, TSC frequency: {args.tsc_freq} cycles/us")
    print(f"Add raw counters: {args.raw_counter}")
    print(f"Using offcore counters: {args.use_offcore}")
    print(f"Using write-estimation: {args.use_write_est}")
    print(f"Using counter-combination: {args.use_counter_combination}")
    print(f"Using stall_total (non-combination): {args.use_stall_total}")
    print(f"Using bound_on_loads (non-combination): {args.use_bound_on_loads}")
    print(f"Using bound_on_stores (non-combination): {args.use_bound_on_stores}")
    print(f"Using IMC counters: {args.use_imc}")
    print(f"Using RDT counters: {args.use_rdt}, local_bw={args.use_rdt_local_bw}, scaling={args.rdt_scaling}")
    print(f"CPU filter cpu_id: {args.cpu_id}  (-1 means aggregate all)")

    parse_hrperf_log_polars(
        perf_log_path=args.perf_log_path,
        use_raw=args.raw_counter,
        use_tsc_ts=args.tsc_ts,
        tsc_per_us=args.tsc_freq,
        use_offcore=args.use_offcore,
        use_imc=args.use_imc,
        db_path=args.db_path,
        use_write_est=args.use_write_est,
        use_rdt=args.use_rdt,
        use_rdt_local_bw=args.use_rdt_local_bw,
        rdt_scaling=args.rdt_scaling,
        use_counter_combination=args.use_counter_combination,
        use_stall_total=args.use_stall_total,
        use_bound_on_loads=args.use_bound_on_loads,
        use_bound_on_stores=args.use_bound_on_stores,
        cpu_id=args.cpu_id,
    )


if __name__ == "__main__":
    main()
