#!/usr/bin/env python3
"""Generate MQOM3 benchmark tables (Markdown or LaTeX) from bench JSON data.

The script can compile the necessary binaries, run the bench, and format the
results — all in one step — or work from an existing JSON file.

Examples:
  # Compile + bench + format (all-in-one):
  python3 bench_table.py --compile --format md -n 100

  # Compile category 1 with parallel jobs (-j), then bench sequentially
  # (bench parallelism is a separate knob, -p, and defaults to sequential:
  # concurrent benches contend for CPU/cache and pollute timing measurements):
  python3 bench_table.py --compile -j -1 --format latex cat1

  # Skip compilation (binaries already in build/), run bench:
  python3 bench_table.py --format md -n 100

  # From an existing JSON file (skip compilation and bench):
  python3 bench_table.py -i stats/bench.json --format latex

  # Include key/signature size columns:
  python3 bench_table.py -i bench.json --sizes

  # Extrapolate ms figures from the machine they were measured on (2.4GHz) to
  # a hypothetical 3.5GHz target (ms columns only; cycles are left untouched):
  python3 bench_table.py -i bench.json --current-freq 2.4 --target-freq 3.5

  # No real cycle data (e.g. macOS bench.json): get a Mcycles column anyway,
  # projected from ms assuming a 3.5GHz target:
  python3 bench_table.py -i bench.json --projected-cycles --target-freq 3.5

  # Dump instance parameters (Table 6 + Table 7 style), no bench needed:
  python3 bench_table.py --instances-parameters --format md
  python3 bench_table.py --instances-parameters --format latex
"""
import argparse
import json
import re
import sys
import subprocess
import pathlib
import time

SCRIPT_DIR = pathlib.Path(__file__).parent.absolute()

# Canonical variant ordering matching the spec tables (L1→L3→L5, gf2-shorter first)
ORDERED_VARIANTS = [
    ('cat1', 'gf2',  'shorter', 'ct'),
    ('cat1', 'gf2',  'shorter', 'ot'),
    ('cat1', 'gf16', 'short',   'ct'),
    ('cat1', 'gf16', 'short',   'ot'),
    ('cat1', 'gf16', 'fast',    'ct'),
    ('cat1', 'gf16', 'fast',    'ot'),
    ('cat3', 'gf2',  'shorter', 'ct'),
    ('cat3', 'gf2',  'shorter', 'ot'),
    ('cat3', 'gf16', 'short',   'ct'),
    ('cat3', 'gf16', 'short',   'ot'),
    ('cat3', 'gf16', 'fast',    'ct'),
    ('cat3', 'gf16', 'fast',    'ot'),
    ('cat5', 'gf2',  'shorter', 'ct'),
    ('cat5', 'gf2',  'shorter', 'ot'),
    ('cat5', 'gf16', 'short',   'ct'),
    ('cat5', 'gf16', 'short',   'ot'),
    ('cat5', 'gf16', 'fast',    'ct'),
    ('cat5', 'gf16', 'fast',    'ot'),
]

# Security level of each category for the separator logic
CAT_LEVEL = {'cat1': 1, 'cat3': 3, 'cat5': 5}


def format_cycles(cycles):
    m = cycles / 1e6
    if m >= 100:
        return f'{m:.0f}M'
    elif m >= 10:
        return f'{m:.1f}M'
    else:
        return f'{m:.2f}M'


def format_ms(ms):
    return f'{ms:.2f}'


def load_results(json_file):
    """Load bench JSON and return dict keyed by 'path' (e.g. 'cat1_gf16_fast_ct')."""
    with open(json_file) as f:
        data = json.load(f)
    # Strip \r from string fields (bench binary may use \r\n line endings)
    for d in data:
        for k, v in d.items():
            if isinstance(v, str):
                d[k] = v.strip()
    return {d['path']: d for d in data}


def run_compile(schemes, compile_jobs, tmpdir, extra_cflags,
                with_cycles=False, with_detailed=False, with_mem_usage=False):
    """Invoke manage.py compile (bench only, no KAT) for the requested schemes.

    compile_jobs:   parallel scheme compilations (manage.py compile -p). Safe to
                    parallelize: this only affects build time, not measurements.
    with_cycles:    add -DBENCHMARK_CYCLES via EXTRA_CFLAGS — RDPMC cycle counts
                    for KeyGen/Sign/Verify only (no per-step overhead).
                    Requires perf_event_paranoid<=2 on Linux.
    with_detailed:  set BENCHMARK=1 — enables both -DBENCHMARK (per-step breakdown)
                    and -DBENCHMARK_CYCLES.  Implies with_cycles.
    with_mem_usage: set MEASURE_STACK=1 — enables per-operation stack + heap measurement.
    """
    cmd = [sys.executable, str(SCRIPT_DIR / 'manage.py'), 'compile'] + schemes
    cmd += ['--no-kat']
    if compile_jobs != 0:
        cmd += ['-p', str(compile_jobs)]
    if tmpdir:
        cmd += ['--tmpdir', tmpdir]
    import os
    env = os.environ.copy()
    if with_detailed:
        env['BENCHMARK'] = '1'          # adds -DBENCHMARK + -DBENCHMARK_CYCLES
    elif with_cycles:
        # Only RDPMC cycles, no per-step instrumentation overhead
        base = extra_cflags or ''
        extra_cflags = (base + ' -DBENCHMARK_CYCLES').strip()
    if with_mem_usage:
        env['MEASURE_STACK'] = '1'      # adds -DMEASURE_STACK + -lpthread
    if extra_cflags:
        env['EXTRA_CFLAGS'] = extra_cflags
    print(f'Running: {" ".join(cmd)}', file=sys.stderr)
    proc = subprocess.Popen(cmd, cwd=SCRIPT_DIR, env=env)
    try:
        proc.wait()
    except KeyboardInterrupt:
        proc.terminate()
        proc.wait()
        print('\nInterrupted.', file=sys.stderr)
        sys.exit(130)
    if proc.returncode != 0:
        raise subprocess.CalledProcessError(proc.returncode, cmd)


def run_bench(schemes, n, build_folder, parallel_jobs):
    """Invoke manage.py bench and write to a timestamped JSON, return its path.

    parallel_jobs: parallel scheme benchmarks (manage.py bench -p). Defaults to
                   sequential (0): concurrent benches contend for CPU/cache and
                   pollute timing/cycle measurements — keep at 0 unless you
                   explicitly accept noisier numbers in exchange for speed.
    """
    (SCRIPT_DIR / 'stats').mkdir(parents=True, exist_ok=True)
    out_file = SCRIPT_DIR / 'stats' / f'bench_table_{time.strftime("%Y%m%d_%H%M%S")}.json'
    cmd = [sys.executable, str(SCRIPT_DIR / 'manage.py'), 'bench'] + schemes
    cmd += ['-n', str(n), '-o', str(out_file)]
    if build_folder:
        cmd += ['-f', build_folder]
    if parallel_jobs != 0:
        cmd += ['-p', str(parallel_jobs)]
    print(f'Running: {" ".join(cmd)}', file=sys.stderr)
    proc = subprocess.Popen(cmd, cwd=SCRIPT_DIR)
    try:
        proc.wait()
    except KeyboardInterrupt:
        proc.terminate()
        proc.wait()
        print('\nInterrupted.', file=sys.stderr)
        sys.exit(130)
    if proc.returncode != 0:
        print(f'\nError: bench command failed (exit {proc.returncode}).', file=sys.stderr)
        print('Hint: compile schemes first with --compile, or run:', file=sys.stderr)
        print(f'  python3 manage.py compile {"all" if "all" in schemes else " ".join(schemes)}',
              file=sys.stderr)
        sys.exit(proc.returncode)
    return out_file


def build_rows(results, requested_cats):
    """Return ordered list of row dicts from the results dict."""
    rows = []
    for (cat, field, tradeoff, variant) in ORDERED_VARIANTS:
        # Filter to requested categories
        if requested_cats and cat not in requested_cats:
            continue
        path = f'{cat}_{field}_{tradeoff}_{variant}'
        if path not in results:
            continue
        d = results[path]
        name = d.get('name', path)
        keygen_ms = d['keygen'][0] if isinstance(d['keygen'], (list, tuple)) else d['keygen']
        sign_ms   = d['sign'][0]   if isinstance(d['sign'],   (list, tuple)) else d['sign']
        verif_ms  = d['verif'][0]  if isinstance(d['verif'],  (list, tuple)) else d['verif']
        pk  = d.get('pk_size', 0)
        sk  = d.get('sk_size', 0)
        # Prefer sig_size_max (worst-case); fall back to mean
        if 'sig_size_max' in d and d['sig_size_max']:
            sig = d['sig_size_max']
        elif isinstance(d.get('sig_size'), (list, tuple)):
            sig = d['sig_size'][0]
        else:
            sig = d.get('sig_size', 0)
        # Detailed per-step breakdown (present only when compiled with BENCHMARK=1)
        DETAIL_STEPS = [
            ('blc_commit',       'BLC.Commit'),
            ('piop_compute',     'PIOP.Compute'),
            ('sample_challenge', 'Sample Chall.'),
            ('blc_open',         'BLC.Open'),
        ]
        detail = {}
        for key, label in DETAIL_STEPS:
            raw = d.get(f'detailed_{key}_total')
            if raw is not None:
                ms_val  = raw[0] if isinstance(raw, (list, tuple)) else raw
                cyc_val = raw[1] if isinstance(raw, (list, tuple)) and len(raw) > 1 else None
                detail[key] = (label, ms_val, cyc_val)

        # Sub-step breakdowns within BLC.Commit and PIOP.Compute
        BLC_SUB_STEPS = [
            ('detailed_blc_commit_expand_trees',      'Expand Trees'),
            ('detailed_blc_commit_seed_commit',        'Seed Commit'),
            ('detailed_blc_commit_prg',                'PRG'),
            ('detailed_blc_commit_xof',                'XOF'),
            ('detailed_blc_commit_arithm',             'Arith'),
        ]
        PIOP_SUB_STEPS = [
            ('detailed_piop_compute_expand_mq',            'Expand MQ'),
            ('detailed_piop_compute_expand_batching_mat',  'Expand Bat.Mat'),
            ('detailed_piop_compute_matrix_mult_ext',      'Mat.Mult Ext'),
            ('detailed_piop_compute_compute_t1',           'Compute T1'),
            ('detailed_piop_compute_compute_p_zi',         'Compute Pzi'),
            ('detailed_piop_compute_batch_and_mask',       'Batch & Mask'),
        ]

        def _load_sub(sub_steps):
            sub = {}
            for json_key, label in sub_steps:
                raw = d.get(json_key)
                if raw is not None:
                    ms_val  = raw[0] if isinstance(raw, (list, tuple)) else raw
                    cyc_val = raw[1] if isinstance(raw, (list, tuple)) and len(raw) > 1 else None
                    sub[json_key] = (label, ms_val, cyc_val)
            return sub

        # Memory usage (present only when compiled with MEASURE_STACK=1)
        mem = d.get('memory', {})
        def _mem(op, kind):
            entry = mem.get(op, {})
            v = entry.get(kind)
            return int(v) if v is not None else None

        rows.append({
            'cat':        cat,
            'name':       name,
            'keygen_ms':  keygen_ms,
            'sign_ms':    sign_ms,
            'verif_ms':   verif_ms,
            'pk':         pk,
            'sk':         sk,
            'sig':        sig,
            'keygen_cyc': d.get('keygen_cycles'),
            'sign_cyc':   d.get('sign_cycles'),
            'verif_cyc':  d.get('verif_cycles'),
            'detail':     detail,          # empty dict when not compiled with BENCHMARK=1
            'blc_sub':    _load_sub(BLC_SUB_STEPS),
            'piop_sub':   _load_sub(PIOP_SUB_STEPS),
            '_blc_sub_keys':  [k for k, _ in BLC_SUB_STEPS],
            '_piop_sub_keys': [k for k, _ in PIOP_SUB_STEPS],
            # Stack/heap per operation (bytes); None when not measured
            'kg_stack':  _mem('keygen', 'stack'),
            'sg_stack':  _mem('sign',   'stack'),
            'vf_stack':  _mem('verify', 'stack'),
            'kg_heap':   _mem('keygen', 'heap'),
            'sg_heap':   _mem('sign',   'heap'),
            'vf_heap':   _mem('verify', 'heap'),
        })
    return rows


def scale_rows_ms_by_freq(rows, current_freq, target_freq):
    """Scale every millisecond figure in 'rows' in place by current_freq/target_freq.

    Extrapolates timings from the frequency they were actually measured at
    (current_freq) to a hypothetical target_freq, assuming the same number of
    cycles is executed: ms_scaled = ms_measured * current_freq / target_freq.
    Cycle counts (keygen_cyc/sign_cyc/verif_cyc and the cyc component of the
    detail/blc_sub/piop_sub tuples) are left untouched -- cycles are
    frequency-independent by construction, only wall-clock ms changes.

    This is only a valid approximation across two clock speeds of the *same*
    microarchitecture (e.g. base vs boost clock); it does not account for IPC
    differences between different CPUs.
    """
    factor = current_freq / target_freq
    for row in rows:
        row['keygen_ms'] *= factor
        row['sign_ms']   *= factor
        row['verif_ms']  *= factor
        for sub_dict_key in ('detail', 'blc_sub', 'piop_sub'):
            sub = row[sub_dict_key]
            for key, (label, ms_val, cyc_val) in sub.items():
                sub[key] = (label, ms_val * factor, cyc_val)
    return rows


def project_cycles_from_ms(rows, target_freq):
    """Overwrite every cycle figure in 'rows' in place with ms * target_freq * 1e6.

    Replaces whatever cycle data is present -- real RDPMC/perf_event-measured
    cycles, if any, are discarded -- with a projection computed directly from
    the millisecond figures currently in 'rows'. cycles = ms * target_freq_GHz
    * 1e6 (ms is 1e-3 s, target_freq is 1e9 Hz per GHz).

    Call this *after* scale_rows_ms_by_freq() if both are used together: the
    projection is then based on the already-scaled ms, so it stays consistent
    with whatever ms ends up being displayed (and, since ms_scaled already
    folds in current_freq/target_freq, the projected cycles end up equal to
    ms_measured * current_freq -- i.e. target_freq cancels out, as expected
    for a frequency-independent cycle count).
    """
    for row in rows:
        row['keygen_cyc'] = row['keygen_ms'] * target_freq * 1e6
        row['sign_cyc']   = row['sign_ms']   * target_freq * 1e6
        row['verif_cyc']  = row['verif_ms']  * target_freq * 1e6
        for sub_dict_key in ('detail', 'blc_sub', 'piop_sub'):
            sub = row[sub_dict_key]
            for key, (label, ms_val, _cyc_val) in sub.items():
                sub[key] = (label, ms_val, ms_val * target_freq * 1e6)
    return rows


# ---------------------------------------------------------------------------
# Markdown output
# ---------------------------------------------------------------------------

def _row_cells(row, sizes, cycles, ms=True):
    cells = [row['name']]
    if ms:
        cells.append(format_ms(row['keygen_ms']))
    if cycles:
        cells.append(format_cycles(row['keygen_cyc']) if row['keygen_cyc'] else 'N/A')
    if ms:
        cells.append(format_ms(row['sign_ms']))
    if cycles:
        cells.append(format_cycles(row['sign_cyc']) if row['sign_cyc'] else 'N/A')
    if ms:
        cells.append(format_ms(row['verif_ms']))
    if cycles:
        cells.append(format_cycles(row['verif_cyc']) if row['verif_cyc'] else 'N/A')
    if sizes:
        cells += [str(row['pk']), str(row['sk']), str(row['sig'])]
    return cells


def output_markdown(rows, sizes=False, cycles=True, ms=True):
    header = ['Instance']
    if ms:
        header.append('KeyGen (ms)')
    if cycles:
        header.append('KeyGen (Mcycles)')
    if ms:
        header.append('Sign (ms)')
    if cycles:
        header.append('Sign (Mcycles)')
    if ms:
        header.append('Verify (ms)')
    if cycles:
        header.append('Verify (Mcycles)')
    if sizes:
        header += ['pk (B)', 'sk (B)', 'sig (B)']

    # col 0 is left-aligned (name), the rest are right-aligned numbers
    RIGHT = set(range(1, len(header)))

    # Build all data rows + separator rows for category breaks
    data_rows = []
    prev_cat = None
    for row in rows:
        if prev_cat and row['cat'] != prev_cat:
            data_rows.append(None)  # category separator
        prev_cat = row['cat']
        data_rows.append(_row_cells(row, sizes, cycles, ms))

    # Compute column widths
    widths = [len(h) for h in header]
    for cells in data_rows:
        if cells is None:
            continue
        for i, c in enumerate(cells):
            widths[i] = max(widths[i], len(c))

    def fmt_row(cells, sep_char=' '):
        parts = []
        for i, (w, c) in enumerate(zip(widths, cells)):
            if i in RIGHT:
                parts.append(c.rjust(w))
            else:
                parts.append(c.ljust(w))
        return '| ' + ' | '.join(parts) + ' |'

    def separator(char='-'):
        return '|-' + '-|-'.join(char * w for w in widths) + '-|'

    lines = [fmt_row(header), separator()]
    for cells in data_rows:
        if cells is None:
            lines.append(separator())
        else:
            lines.append(fmt_row(cells))

    return '\n'.join(lines)


# ---------------------------------------------------------------------------
# Detailed breakdown table (terminal only)
# ---------------------------------------------------------------------------

DETAIL_STEP_KEYS = ['blc_commit', 'piop_compute', 'sample_challenge', 'blc_open']

def output_markdown_detailed(rows, ms=True):
    """Aligned table of per-step Sign breakdown (requires BENCHMARK=1 data)."""
    # Check if any row has detailed data
    if not any(rows[0]['detail']):
        return None

    has_cyc = any(
        v[2] is not None
        for row in rows
        for v in row['detail'].values()
    )

    # Column headers: Instance, then for each step: ms (and cycles if available)
    step_labels = []
    for key in DETAIL_STEP_KEYS:
        for row in rows:
            if key in row['detail']:
                step_labels.append(row['detail'][key][0])
                break

    header = ['Instance']
    for lbl in step_labels:
        if ms:
            header.append(f'{lbl} (ms)')
        if has_cyc:
            header.append(f'{lbl} (Mcyc)')

    RIGHT = set(range(1, len(header)))

    data_rows = []
    prev_cat = None
    for row in rows:
        if prev_cat and row['cat'] != prev_cat:
            data_rows.append(None)
        prev_cat = row['cat']
        cells = [row['name']]
        for key in DETAIL_STEP_KEYS:
            entry = row['detail'].get(key)
            if entry:
                if ms:
                    cells.append(format_ms(entry[1]))
                if has_cyc:
                    cells.append(format_cycles(entry[2]) if entry[2] else 'N/A')
            else:
                if ms:
                    cells.append('N/A')
                if has_cyc:
                    cells.append('N/A')
        data_rows.append(cells)

    widths = [len(h) for h in header]
    for cells in data_rows:
        if cells is None:
            continue
        for i, c in enumerate(cells):
            widths[i] = max(widths[i], len(c))

    def fmt_row(cells):
        parts = []
        for i, (w, c) in enumerate(zip(widths, cells)):
            parts.append(c.rjust(w) if i in RIGHT else c.ljust(w))
        return '| ' + ' | '.join(parts) + ' |'

    def separator():
        return '|-' + '-|-'.join('-' * w for w in widths) + '-|'

    lines = ['\nSign breakdown:', fmt_row(header), separator()]
    for cells in data_rows:
        lines.append(separator() if cells is None else fmt_row(cells))
    return '\n'.join(lines)


# ---------------------------------------------------------------------------
# Sub-step breakdown tables (BLC.Commit internals, PIOP internals)
# ---------------------------------------------------------------------------

def output_markdown_sub_detailed(rows, sub_key, ordered_keys, title, ms=True):
    """Table of sub-step timings within one Sign phase (BLC.Commit or PIOP.Compute)."""
    if not any(rows[0].get(sub_key)):
        return None

    has_cyc = any(
        v[2] is not None
        for row in rows
        for v in row.get(sub_key, {}).values()
    )

    # Collect labels in order
    step_labels = []
    for key in ordered_keys:
        for row in rows:
            if key in row.get(sub_key, {}):
                step_labels.append(row[sub_key][key][0])
                break

    header = ['Instance']
    for lbl in step_labels:
        if ms:
            header.append(f'{lbl} (ms)')
        if has_cyc:
            header.append(f'{lbl} (Mcyc)')

    RIGHT = set(range(1, len(header)))

    data_rows = []
    prev_cat = None
    for row in rows:
        if prev_cat and row['cat'] != prev_cat:
            data_rows.append(None)
        prev_cat = row['cat']
        cells = [row['name']]
        for key in ordered_keys:
            entry = row.get(sub_key, {}).get(key)
            if entry:
                if ms:
                    cells.append(format_ms(entry[1]))
                if has_cyc:
                    cells.append(format_cycles(entry[2]) if entry[2] else 'N/A')
            else:
                if ms:
                    cells.append('N/A')
                if has_cyc:
                    cells.append('N/A')
        data_rows.append(cells)

    widths = [len(h) for h in header]
    for cells in data_rows:
        if cells is None:
            continue
        for i, c in enumerate(cells):
            widths[i] = max(widths[i], len(c))

    def fmt_row(cells):
        parts = []
        for i, (w, c) in enumerate(zip(widths, cells)):
            parts.append(c.rjust(w) if i in RIGHT else c.ljust(w))
        return '| ' + ' | '.join(parts) + ' |'

    def separator():
        return '|-' + '-|-'.join('-' * w for w in widths) + '-|'

    lines = [f'\n{title}:', fmt_row(header), separator()]
    for cells in data_rows:
        lines.append(separator() if cells is None else fmt_row(cells))
    return '\n'.join(lines)


# ---------------------------------------------------------------------------
# Memory usage table (stack + heap per operation)
# ---------------------------------------------------------------------------

def _fmt_kib(b):
    """Format bytes as KiB with one decimal, e.g. '12.3 KiB'."""
    if b is None:
        return 'N/A'
    return f'{b / 1024:.1f}'


def output_markdown_memory(rows):
    """Aligned table of stack / heap / total usage per operation (MEASURE_STACK=1 data)."""
    if not any(r['kg_stack'] is not None for r in rows):
        return None

    OPS = [
        ('KeyGen', 'kg_stack', 'kg_heap'),
        ('Sign',   'sg_stack', 'sg_heap'),
        ('Verify', 'vf_stack', 'vf_heap'),
    ]

    header = ['Instance']
    for op_label, _, _ in OPS:
        header += [f'{op_label} stack (KiB)', f'{op_label} heap (KiB)', f'{op_label} total (KiB)']

    RIGHT = set(range(1, len(header)))

    data_rows = []
    prev_cat = None
    for row in rows:
        if prev_cat and row['cat'] != prev_cat:
            data_rows.append(None)
        prev_cat = row['cat']
        cells = [row['name']]
        for _, sk, hk in OPS:
            s = row.get(sk)
            h = row.get(hk)
            total = (s + h) if (s is not None and h is not None) else None
            cells += [_fmt_kib(s), _fmt_kib(h), _fmt_kib(total)]
        data_rows.append(cells)

    widths = [len(h) for h in header]
    for cells in data_rows:
        if cells is None:
            continue
        for i, c in enumerate(cells):
            widths[i] = max(widths[i], len(c))

    def fmt_row(cells):
        parts = []
        for i, (w, c) in enumerate(zip(widths, cells)):
            parts.append(c.rjust(w) if i in RIGHT else c.ljust(w))
        return '| ' + ' | '.join(parts) + ' |'

    def separator():
        return '|-' + '-|-'.join('-' * w for w in widths) + '-|'

    lines = ['\nMemory usage (stack + heap):', fmt_row(header), separator()]
    for cells in data_rows:
        lines.append(separator() if cells is None else fmt_row(cells))
    return '\n'.join(lines)


# ---------------------------------------------------------------------------
# LaTeX output
# ---------------------------------------------------------------------------

def output_latex(rows, sizes=False, cycles=True, ms=True):
    cols_per_op = (1 if ms else 0) + (1 if cycles else 0)
    n_timing = 3 * cols_per_op   # KeyGen + Sign + Verify columns
    n_size   = 3 if sizes else 0
    n_total  = 1 + n_timing + n_size

    # Column specification
    timing_group = 'c' * cols_per_op
    col_spec = 'c|' + timing_group + '|' + timing_group + '|' + timing_group
    if sizes:
        col_spec += '|ccc'

    lines = []
    lines.append(r'  \begin{tabular}{' + col_spec + r'}')

    # Header line 1
    def multicol(n, align, text):
        return r'\multicolumn{' + str(n) + r'}{' + align + r'}{' + text + r'}'

    ver_align = 'c' if not sizes else 'c|'

    h1 = (
        r'  \multirow{2}{*}{Instance}'
        + ' & ' + multicol(cols_per_op, 'c|',      r'\textbf{KeyGen}')
        + ' & ' + multicol(cols_per_op, 'c|',      r'\textbf{Sign}')
        + ' & ' + multicol(cols_per_op, ver_align,  r'\textbf{Verify}')
    )
    if sizes:
        h1 += ' & ' + multicol(3, 'c', r'\textbf{Sizes (B)}')
    cline_end = n_total
    lines.append(h1 + r' \\ \cline{2-' + str(cline_end) + r'}')

    # Header line 2
    h2_cells = ['']
    for _ in range(3):   # KeyGen, Sign, Verify
        if ms:
            h2_cells.append('ms')
        if cycles:
            h2_cells.append('cycles')
    if sizes:
        h2_cells += ['pk', 'sk', 'sig']
    lines.append('  ' + ' & '.join(h2_cells) + r' \\ \hline')

    prev_cat = None
    for row in rows:
        if prev_cat and row['cat'] != prev_cat:
            lines.append(r'  \midrule')
        prev_cat = row['cat']

        cells = [r'\textsf{' + row['name'] + r'}']
        for op_ms, op_cyc in [
            (row['keygen_ms'], row['keygen_cyc']),
            (row['sign_ms'],   row['sign_cyc']),
            (row['verif_ms'],  row['verif_cyc']),
        ]:
            if ms:
                cells.append(format_ms(op_ms))
            if cycles:
                cells.append(format_cycles(op_cyc) if op_cyc else '--')
        if sizes:
            cells += [str(row['pk']), str(row['sk']), str(row['sig'])]
        lines.append('  ' + ' & '.join(cells) + r' \\')

    lines.append(r'  \hline')
    lines.append(r'  \end{tabular}')
    return '\n'.join(lines)


# ---------------------------------------------------------------------------
# LaTeX detailed breakdown tables
# ---------------------------------------------------------------------------

def output_latex_detailed(rows, ms=True):
    """LaTeX table of per-step Sign breakdown (requires BENCHMARK=1 data)."""
    if not any(rows[0]['detail']):
        return None

    has_cyc = any(
        v[2] is not None
        for row in rows
        for v in row['detail'].values()
    )

    step_labels = []
    for key in DETAIL_STEP_KEYS:
        for row in rows:
            if key in row['detail']:
                step_labels.append(row['detail'][key][0])
                break

    cols_per_step = (1 if ms else 0) + (1 if has_cyc else 0)
    n_steps = len(step_labels)
    n_total = 1 + n_steps * cols_per_step

    col_spec = 'c|'
    for i in range(n_steps):
        col_spec += 'c' * cols_per_step
        if i < n_steps - 1:
            col_spec += '|'

    lines = []
    lines.append(r'  \begin{tabular}{' + col_spec + r'}')

    h1_cells = [r'  \multirow{2}{*}{Instance}']
    for i, lbl in enumerate(step_labels):
        align = 'c|' if i < n_steps - 1 else 'c'
        h1_cells.append(
            r'\multicolumn{' + str(cols_per_step) + r'}{' + align + r'}{\textbf{' + lbl + r'}}')
    lines.append(' & '.join(h1_cells) + r' \\ \cline{2-' + str(n_total) + r'}')

    h2_cells = ['']
    for _ in step_labels:
        if ms:
            h2_cells.append('ms')
        if has_cyc:
            h2_cells.append('cycles')
    lines.append('  ' + ' & '.join(h2_cells) + r' \\ \hline')

    prev_cat = None
    for row in rows:
        if prev_cat and row['cat'] != prev_cat:
            lines.append(r'  \midrule')
        prev_cat = row['cat']
        cells = [r'\textsf{' + row['name'] + r'}']
        for key in DETAIL_STEP_KEYS:
            entry = row['detail'].get(key)
            if entry:
                if ms:
                    cells.append(format_ms(entry[1]))
                if has_cyc:
                    cells.append(format_cycles(entry[2]) if entry[2] else '--')
            else:
                if ms:
                    cells.append('--')
                if has_cyc:
                    cells.append('--')
        lines.append('  ' + ' & '.join(cells) + r' \\')

    lines.append(r'  \hline')
    lines.append(r'  \end{tabular}')
    return '\n'.join(lines)


def output_latex_sub_detailed(rows, sub_key, ordered_keys, title, ms=True):
    """LaTeX table of sub-step timings within one Sign phase (BLC.Commit or PIOP.Compute)."""
    if not any(rows[0].get(sub_key)):
        return None

    has_cyc = any(
        v[2] is not None
        for row in rows
        for v in row.get(sub_key, {}).values()
    )

    step_labels = []
    for key in ordered_keys:
        for row in rows:
            if key in row.get(sub_key, {}):
                step_labels.append(row[sub_key][key][0])
                break

    cols_per_step = (1 if ms else 0) + (1 if has_cyc else 0)
    n_steps = len(step_labels)
    n_total = 1 + n_steps * cols_per_step

    col_spec = 'c|'
    for i in range(n_steps):
        col_spec += 'c' * cols_per_step
        if i < n_steps - 1:
            col_spec += '|'

    lines = []
    lines.append(r'  \begin{tabular}{' + col_spec + r'}')

    h1_cells = [r'  \multirow{2}{*}{Instance}']
    for i, lbl in enumerate(step_labels):
        align = 'c|' if i < n_steps - 1 else 'c'
        h1_cells.append(
            r'\multicolumn{' + str(cols_per_step) + r'}{' + align + r'}{\textbf{' + lbl + r'}}')
    lines.append(' & '.join(h1_cells) + r' \\ \cline{2-' + str(n_total) + r'}')

    h2_cells = ['']
    for _ in step_labels:
        if ms:
            h2_cells.append('ms')
        if has_cyc:
            h2_cells.append('cycles')
    lines.append('  ' + ' & '.join(h2_cells) + r' \\ \hline')

    prev_cat = None
    for row in rows:
        if prev_cat and row['cat'] != prev_cat:
            lines.append(r'  \midrule')
        prev_cat = row['cat']
        cells = [r'\textsf{' + row['name'] + r'}']
        for key in ordered_keys:
            entry = row.get(sub_key, {}).get(key)
            if entry:
                if ms:
                    cells.append(format_ms(entry[1]))
                if has_cyc:
                    cells.append(format_cycles(entry[2]) if entry[2] else '--')
            else:
                if ms:
                    cells.append('--')
                if has_cyc:
                    cells.append('--')
        lines.append('  ' + ' & '.join(cells) + r' \\')

    lines.append(r'  \hline')
    lines.append(r'  \end{tabular}')
    return '\n'.join(lines)


# ---------------------------------------------------------------------------
# Instance parameters (Table 6 + Table 7 style) -- no bench JSON required
# ---------------------------------------------------------------------------

_CAT_TO_LEVEL  = {'cat1': '1', 'cat3': '3', 'cat5': '5'}
_CAT_TO_NIST   = {'cat1': 'Cat. I', 'cat3': 'Cat. III', 'cat5': 'Cat. V'}
_CAT_TO_LAMBDA = {'cat1': 128, 'cat3': 192, 'cat5': 256}


def _parse_header_int(path, key):
    """Return the integer value of #define MQOM3_PARAM_<key> ... from a header file."""
    text = path.read_text()
    m = re.search(r'#define\s+MQOM3_PARAM_' + key + r'\s+(\S+)', text)
    if not m:
        return None
    raw = m.group(1).strip('()')
    # May be a compound like MQOM3_PARAM_SECURITY/MQOM3_PARAM_EXT_FIELD -- not needed here
    try:
        return int(raw)
    except ValueError:
        return None


def load_instance_params(param_dir=None):
    """Parse all 18 mqom3_parameters_*.h files and return a list of param dicts."""
    if param_dir is None:
        param_dir = SCRIPT_DIR / 'parameters'
    param_dir = pathlib.Path(param_dir)
    rows = []
    for (cat, field, tradeoff, blc) in ORDERED_VARIANTS:
        path = param_dir / f'mqom3_parameters_{cat}_{field}_{tradeoff}_{blc}.h'
        if not path.exists():
            continue
        text = path.read_text()

        def _get(key, default=None):
            m = re.search(r'#define\s+MQOM3_PARAM_' + key + r'\s+(\S.*)', text)
            if not m:
                return default
            raw = m.group(1).strip()
            # Handle simple parenthesised expressions like (A/B) using already-known values
            try:
                return int(raw.strip('()'))
            except ValueError:
                return raw  # leave as string for later resolution

        security  = int(_get('SECURITY', 128))
        base_f    = int(_get('BASE_FIELD', 4))    # log2(|F|): 1->GF(2), 4->GF(16)
        ext_f     = int(_get('EXT_FIELD', 8))     # log2(|K|): 8->GF(256), 16->GF(2^16)
        mq_n      = int(_get('MQ_N', 56))
        tau_val   = int(_get('TAU', 17))
        log2_n    = int(_get('NB_EVALS_LOG', 8))
        w_val     = int(_get('W', 9))
        topen     = _get('LARGE_GGM_T_OPEN')      # None for CT
        if topen is not None:
            topen = int(topen)

        mu        = ext_f // base_f
        n_evals   = 1 << log2_n
        eta       = security // ext_f

        # Size helpers (integer arithmetic, parameters always aligned)
        seed_size   = security // 8
        digest_size = (2 * security) // 8

        def bsb(n):   # BYTE_SIZE_FIELD_BASE
            return (n * base_f) // 8

        def bse(n):   # BYTE_SIZE_FIELD_EXT
            return (n * ext_f) // 8

        pk_size = 2 * seed_size + bse(mq_n // mu)
        sk_size = pk_size + bsb(mq_n)

        # CT signature
        ct_opening = tau_val * (bsb(mq_n) - seed_size + log2_n * seed_size + digest_size + bsb(eta * mu))
        ct_sig = 4 + seed_size + digest_size + ct_opening

        # OT signature (only meaningful when topen is present)
        if topen is not None:
            ot_opening = (topen * seed_size + tau_val * digest_size
                          + tau_val * bsb(mq_n) + tau_val * bsb(eta * mu))
            ot_sig = 4 + seed_size + digest_size + ot_opening
        else:
            ot_sig = None

        # Display name: MQOM3-L1-gf2-shorter-ct
        level = _CAT_TO_LEVEL[cat]
        name = f'MQOM3-L{level}-{field}-{tradeoff}-{blc}'

        rows.append({
            'cat':      cat,
            'blc':      blc,
            'name':     name,
            'nist':     _CAT_TO_NIST[cat],
            'base_f':   1 << base_f,       # actual cardinality: 2 or 16
            'mq_n':     mq_n,
            'tau':      tau_val,
            'n_evals':  n_evals,
            'mu':       mu,
            'eta':      eta,
            'w':        w_val,
            'topen':    topen,
            'pk':       pk_size,
            'sk':       sk_size,
            'ct_sig':   ct_sig,
            'ot_sig':   ot_sig,
        })
    return rows


# ---------------------------------------------------------------------------
# Table 6 equivalent: MQ + proof system parameters
# ---------------------------------------------------------------------------

def output_markdown_instance_params(rows):
    header = ['Instance', 'NIST', '|F|', 'm=n', 'tau', 'N', 'mu', 'eta', 'w', 'Topen']
    RIGHT  = set(range(2, len(header)))

    data_rows = []
    prev_cat = None
    for r in rows:
        if prev_cat and r['cat'] != prev_cat:
            data_rows.append(None)
        prev_cat = r['cat']
        topen_s = str(r['topen']) if r['topen'] is not None else '--'
        data_rows.append([
            r['name'], r['nist'],
            str(r['base_f']), str(r['mq_n']),
            str(r['tau']), str(r['n_evals']),
            str(r['mu']), str(r['eta']),
            str(r['w']), topen_s,
        ])

    widths = [len(h) for h in header]
    for cells in data_rows:
        if cells is None:
            continue
        for i, c in enumerate(cells):
            widths[i] = max(widths[i], len(c))

    def fmt_row(cells):
        parts = []
        for i, (w, c) in enumerate(zip(widths, cells)):
            parts.append(c.rjust(w) if i in RIGHT else c.ljust(w))
        return '| ' + ' | '.join(parts) + ' |'

    def separator():
        return '|-' + '-|-'.join('-' * w for w in widths) + '-|'

    lines = [fmt_row(header), separator()]
    for cells in data_rows:
        lines.append(separator() if cells is None else fmt_row(cells))
    return '\n'.join(lines)


def output_latex_instance_params(rows):
    # Column spec: name | NIST sec. || MQ (|F|, m=n) || Proof (tau, N, mu, eta, w, Topen)
    col_spec = r'l c cc ccccc c'
    lines = []
    lines.append(r'  \begin{tabular}{' + col_spec + r'}')
    lines.append(r'  \toprule')
    lines.append(
        r'  \multicolumn{1}{c}{\multirow{2}{*}{\textbf{Instance}}}'
        r' & \multirow{2}{*}{\textbf{NIST}}'
        r' & \multicolumn{2}{c}{\textbf{MQ Parameters}}'
        r' & \multicolumn{6}{c}{\textbf{Proof System Parameters}}'
        r' \\'
    )
    lines.append(r'  \cmidrule(lr){3-4} \cmidrule(lr){5-10}')
    lines.append(
        r'  & & $|\mathbb{F}|$ & $m\!=\!n$'
        r' & $\tau$ & $N$ & $\mu$ & $\eta$ & $w$ & $T_{\mathsf{open}}$'
        r' \\ \midrule'
    )
    prev_cat = None
    for r in rows:
        if prev_cat and r['cat'] != prev_cat:
            lines.append(r'  \midrule')
        prev_cat = r['cat']
        topen_s = str(r['topen']) if r['topen'] is not None else r'\text{--}'
        line = (
            r'  \textsf{' + r['name'] + r'}'
            r' & ' + r['nist']
            + r' & ' + str(r['base_f'])
            + r' & ' + str(r['mq_n'])
            + r' & ' + str(r['tau'])
            + r' & ' + str(r['n_evals'])
            + r' & ' + str(r['mu'])
            + r' & ' + str(r['eta'])
            + r' & ' + str(r['w'])
            + r' & ' + topen_s
            + r' \\'
        )
        lines.append(line)
    lines.append(r'  \bottomrule')
    lines.append(r'  \end{tabular}')
    return '\n'.join(lines)


# ---------------------------------------------------------------------------
# Table 7 equivalent: key and signature sizes
# ---------------------------------------------------------------------------

def output_markdown_instance_sizes(rows):
    header = ['Instance', 'pk (B)', 'sk (B)', 'sig (B)']
    RIGHT  = set(range(1, len(header)))

    data_rows = []
    prev_cat = None
    for r in rows:
        if prev_cat and r['cat'] != prev_cat:
            data_rows.append(None)
        prev_cat = r['cat']
        if r['blc'] == 'ct':
            sig_s = str(r['ct_sig'])
        else:
            sig_s = str(r['ot_sig']) if r['ot_sig'] is not None else '--'
        data_rows.append([
            r['name'],
            str(r['pk']), str(r['sk']),
            sig_s,
        ])

    widths = [len(h) for h in header]
    for cells in data_rows:
        if cells is None:
            continue
        for i, c in enumerate(cells):
            widths[i] = max(widths[i], len(c))

    def fmt_row(cells):
        parts = []
        for i, (w, c) in enumerate(zip(widths, cells)):
            parts.append(c.rjust(w) if i in RIGHT else c.ljust(w))
        return '| ' + ' | '.join(parts) + ' |'

    def separator():
        return '|-' + '-|-'.join('-' * w for w in widths) + '-|'

    lines = [fmt_row(header), separator()]
    for cells in data_rows:
        lines.append(separator() if cells is None else fmt_row(cells))
    return '\n'.join(lines)


def output_latex_instance_sizes(rows):
    col_spec = r'l ccc'
    lines = []
    lines.append(r'  \begin{tabular}{' + col_spec + r'}')
    lines.append(r'  \toprule')
    lines.append(
        r'  \multicolumn{1}{c}{\multirow{2}{*}{\textbf{Instance}}}'
        r' & \multicolumn{3}{c}{\textbf{Sizes (in bytes)}}'
        r' \\'
    )
    lines.append(r'  \cmidrule(lr){2-4}')
    lines.append(r'  & $pk$ & $sk$ & Sig. \\ \midrule')
    prev_cat = None
    for r in rows:
        if prev_cat and r['cat'] != prev_cat:
            lines.append(r'  \midrule')
        prev_cat = r['cat']
        # For CT rows show ct_sig; for OT rows show ot_sig.
        # Both pk and sk are the same within a (cat, field, tradeoff) pair.
        if r['blc'] == 'ct':
            sig_s = str(r['ct_sig'])
        else:
            sig_s = str(r['ot_sig']) if r['ot_sig'] is not None else r'\text{--}'
        line = (
            r'  \textsf{' + r['name'] + r'}'
            + r' & ' + str(r['pk'])
            + r' & ' + str(r['sk'])
            + r' & ' + sig_s
            + r' \\'
        )
        lines.append(line)
    lines.append(r'  \bottomrule')
    lines.append(r'  \end{tabular}')
    return '\n'.join(lines)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description='Generate MQOM3 benchmark tables (Markdown or LaTeX).',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        '-i', '--input', dest='input_file',
        help='Existing bench JSON (from "manage.py bench -o FILE"); skips compilation and bench',
    )
    parser.add_argument(
        '--compile', action='store_true',
        help='Compile bench binaries via manage.py before running bench',
    )
    parser.add_argument(
        '--format', choices=['md', 'latex', 'both'], default='md',
        help='Output format: md, latex, or both (default: md)',
    )
    parser.add_argument(
        '-n', '--nb-repetitions', dest='nb_repetitions', type=int, default=100,
        help='Bench repetitions (default: 100)',
    )
    parser.add_argument(
        '-j', '--jobs', dest='compile_jobs', type=int, default=0,
        help='Parallel scheme compilations (-1 = max, 0 = sequential, default). '
             'Only affects --compile; safe to parallelize, does not touch measurements.',
    )
    parser.add_argument(
        '-p', '--parallel-jobs', dest='parallel_jobs', type=int, default=0,
        help='Parallel scheme benchmarks (-1 = max, 0 = sequential, default). '
             'Keep at 0 for accurate timing: concurrent benches contend for '
             'CPU/cache and pollute cycle/ms measurements.',
    )
    parser.add_argument(
        '-f', '--build-folder', dest='build_folder', default=None,
        help='Build folder (default: build/)',
    )
    parser.add_argument(
        '--tmpdir', dest='tmpdir', default=None,
        help='Temporary build root for compilation (passed to manage.py compile --tmpdir)',
    )
    parser.add_argument(
        '--extra-cflags', dest='extra_cflags', default=None,
        help='Extra CFLAGS passed as EXTRA_CFLAGS env var during compilation',
    )
    parser.add_argument(
        '--sizes', action='store_true',
        help='Add pk/sk/sig size columns',
    )
    parser.add_argument(
        '--current-freq', dest='current_freq', type=float, default=None,
        help='CPU frequency (GHz) the ms figures were actually measured at. '
             'Combined with --target-freq, extrapolates every millisecond figure '
             'to ms_scaled = ms * current_freq / target_freq (cycle counts are '
             'left untouched -- they are frequency-independent). Valid only as an '
             'approximation across two clock speeds of the *same* microarchitecture '
             '(e.g. base vs boost clock); does not account for IPC differences '
             'between different CPUs.',
    )
    parser.add_argument(
        '--target-freq', dest='target_freq', type=float, default=None,
        help='Target CPU frequency (GHz). Shared by two independent features, at least '
             'one of which is required to use it: (1) with --current-freq, extrapolates '
             'ms figures to this frequency; (2) with --projected-cycles, is the frequency '
             'assumed when projecting Mcycles from ms.',
    )
    parser.add_argument(
        '--projected-cycles', action='store_true', dest='projected_cycles',
        help='Replace cycle figures (real RDPMC/perf_event data, if any, is discarded) with '
             'a projection computed from ms: cycles = ms * target_freq * 1e6. Requires '
             '--target-freq. Useful to get a Mcycles column on platforms with no real cycle '
             'counting (e.g. macOS); combine with --current-freq to keep the projection '
             'consistent with scaled ms figures.',
    )
    parser.add_argument(
        '--cycles', action='store_true', dest='with_cycles',
        help='Compile with -DBENCHMARK_CYCLES for RDPMC cycle counts (KeyGen/Sign/Verify only, '
             'no per-step overhead). Requires perf_event_paranoid<=2 on Linux.',
    )
    parser.add_argument(
        '--detailed', action='store_true', dest='with_detailed',
        help='Compile with BENCHMARK=1: cycle counts + per-step Sign breakdown table. '
             'Implies --cycles. Requires perf_event_paranoid<=2 on Linux.',
    )
    parser.add_argument(
        '--no-cycles', action='store_true', dest='no_cycles',
        help='Omit cycles columns (show ms only)',
    )
    parser.add_argument(
        '--no-ms', action='store_true', dest='no_ms',
        help='Omit millisecond columns (show cycles only); requires --cycles or --detailed',
    )
    parser.add_argument(
        '--mem-usage', action='store_true', dest='with_mem_usage',
        help='Compile with MEASURE_STACK=1: per-operation stack and heap measurement. '
             'Adds a memory table to the output. Requires --compile to take effect.',
    )
    parser.add_argument(
        '--instances-parameters', action='store_true', dest='instances_parameters',
        help='Output instance parameter tables (Table 6: MQ+proof params, Table 7: key+sig sizes). '
             'Does not require a bench JSON or compiled binaries.',
    )
    parser.add_argument(
        'schemes', nargs='*', default=['all'],
        help='Schemes: all, cat1, cat3, cat5, or specific variants like cat1_gf16_fast_ct',
    )
    args = parser.parse_args()

    schemes = args.schemes or ['all']

    # --instances-parameters: dump parameter + size tables, no bench required
    if args.instances_parameters:
        param_rows = load_instance_params()
        if not param_rows:
            print('No parameter headers found under parameters/.', file=sys.stderr)
            sys.exit(1)
        # Filter to requested categories if the user passed explicit ones
        all_cats = {'cat1', 'cat3', 'cat5'}
        if 'all' not in schemes:
            req_cats = {s for s in schemes if s in all_cats}
            if not req_cats:
                req_cats = all_cats
            param_rows = [r for r in param_rows if r['cat'] in req_cats]
        want_md    = args.format in ('md', 'both')
        want_latex = args.format in ('latex', 'both')
        if want_latex and want_md:
            print('=' * 60)
            print('LaTeX')
            print('=' * 60)
        if want_latex:
            print('% Table 6: MQ and proof system parameters')
            print(output_latex_instance_params(param_rows))
            print()
            print('% Table 7: Key and signature sizes')
            print(output_latex_instance_sizes(param_rows))
        if want_latex and want_md:
            print()
            print('=' * 60)
            print('Markdown')
            print('=' * 60)
        if want_md:
            print('## MQ and proof system parameters\n')
            print(output_markdown_instance_params(param_rows))
            print()
            print('## Key and signature sizes\n')
            print(output_markdown_instance_sizes(param_rows))
        return

    # Determine which categories to include in the output table
    all_cats = {'cat1', 'cat3', 'cat5'}
    if 'all' in schemes:
        requested_cats = all_cats
    else:
        requested_cats = set()
        for s in schemes:
            for c in all_cats:
                if s.startswith(c):
                    requested_cats.add(c)
        if not requested_cats:
            requested_cats = all_cats

    if args.input_file:
        json_file = args.input_file
    else:
        if args.compile:
            run_compile(schemes, args.compile_jobs, args.tmpdir, args.extra_cflags,
                        with_cycles=args.with_cycles, with_detailed=args.with_detailed,
                        with_mem_usage=args.with_mem_usage)
        json_file = run_bench(schemes, args.nb_repetitions, args.build_folder, args.parallel_jobs)

    results = load_results(json_file)
    rows = build_rows(results, requested_cats)

    if not rows:
        print('No matching results found in the JSON file.', file=sys.stderr)
        sys.exit(1)

    if args.current_freq is not None and args.target_freq is None:
        print('Error: --current-freq requires --target-freq.', file=sys.stderr)
        sys.exit(1)
    if args.projected_cycles and args.target_freq is None:
        print('Error: --projected-cycles requires --target-freq.', file=sys.stderr)
        sys.exit(1)
    if args.target_freq is not None and args.current_freq is None and not args.projected_cycles:
        print('Error: --target-freq has no effect without --current-freq and/or '
              '--projected-cycles.', file=sys.stderr)
        sys.exit(1)
    if args.current_freq is not None and args.current_freq <= 0:
        print('Error: --current-freq must be positive.', file=sys.stderr)
        sys.exit(1)
    if args.target_freq is not None and args.target_freq <= 0:
        print('Error: --target-freq must be positive.', file=sys.stderr)
        sys.exit(1)

    scale_note = None
    if args.current_freq is not None:
        factor = args.current_freq / args.target_freq
        scale_rows_ms_by_freq(rows, args.current_freq, args.target_freq)
        scale_note = (
            f'ms figures scaled from {args.current_freq:g} GHz to {args.target_freq:g} GHz '
            f'(x{factor:.3f}); cycle counts are unaffected. Linear extrapolation assuming '
            'identical IPC -- valid only across clock speeds of the same microarchitecture, '
            'not across different CPUs.'
        )
        print(f'Note: {scale_note}', file=sys.stderr)

    cycles_note = None
    if args.projected_cycles:
        project_cycles_from_ms(rows, args.target_freq)
        cycles_note = (
            f'Mcycles figures are PROJECTED as ms * {args.target_freq:g} GHz, not measured '
            '-- any real cycle data has been overridden.'
        )
        print(f'Note: {cycles_note}', file=sys.stderr)

    # Auto-hide cycles columns if no row has cycle data
    has_cycles = any(r['keygen_cyc'] is not None for r in rows)
    cycles = has_cycles and not args.no_cycles
    show_ms = not args.no_ms
    if not cycles and not args.no_cycles:
        print('Note: no cycle data — recompile with --cycles or --detailed '
              '(requires perf_event_paranoid<=2).', file=sys.stderr)
    if args.no_ms and not cycles:
        print('Warning: --no-ms with no cycle data — timing columns will be empty.',
              file=sys.stderr)

    has_detail = any(r['detail'] for r in rows)
    has_memory = any(r['kg_stack'] is not None for r in rows)
    want_md    = args.format in ('md', 'both')
    want_latex = args.format in ('latex', 'both')

    if want_latex and want_md:
        print('=' * 60)
        print('LaTeX')
        print('=' * 60)
    if want_latex:
        if scale_note:
            print(f'% Note: {scale_note}')
        if cycles_note:
            print(f'% Note: {cycles_note}')
        print(output_latex(rows, sizes=args.sizes, cycles=cycles, ms=show_ms))
        if has_detail:
            detail_out = output_latex_detailed(rows, ms=show_ms)
            if detail_out:
                print()
                print('% Sign breakdown')
                print(detail_out)
            blc_keys  = rows[0].get('_blc_sub_keys', [])
            piop_keys = rows[0].get('_piop_sub_keys', [])
            blc_sub_out = output_latex_sub_detailed(
                rows, 'blc_sub', blc_keys, 'BLC.Commit breakdown', ms=show_ms)
            if blc_sub_out:
                print()
                print('% BLC.Commit breakdown')
                print(blc_sub_out)
            piop_sub_out = output_latex_sub_detailed(
                rows, 'piop_sub', piop_keys, 'PIOP.Compute breakdown', ms=show_ms)
            if piop_sub_out:
                print()
                print('% PIOP.Compute breakdown')
                print(piop_sub_out)
    if want_latex and want_md:
        print()
        print('=' * 60)
        print('Markdown')
        print('=' * 60)
    if want_md:
        if scale_note:
            print(f'> **Note:** {scale_note}\n')
        if cycles_note:
            print(f'> **Note:** {cycles_note}\n')
        print(output_markdown(rows, sizes=args.sizes, cycles=cycles, ms=show_ms))
        if has_detail:
            detail_out = output_markdown_detailed(rows, ms=show_ms)
            if detail_out:
                print(detail_out)
            blc_keys  = rows[0].get('_blc_sub_keys', [])
            piop_keys = rows[0].get('_piop_sub_keys', [])
            blc_sub_out = output_markdown_sub_detailed(
                rows, 'blc_sub', blc_keys, 'BLC.Commit breakdown', ms=show_ms)
            if blc_sub_out:
                print(blc_sub_out)
            piop_sub_out = output_markdown_sub_detailed(
                rows, 'piop_sub', piop_keys, 'PIOP.Compute breakdown', ms=show_ms)
            if piop_sub_out:
                print(piop_sub_out)
        if has_memory:
            mem_out = output_markdown_memory(rows)
            if mem_out:
                print(mem_out)


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print('\nInterrupted.', file=sys.stderr)
        sys.exit(130)
