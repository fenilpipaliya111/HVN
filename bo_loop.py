"""Modular vanillin BO: same experiment logic as vanillin_all_in_one.py.

Requires lv.py, uvvis.py and run_all.py in this folder.
Preview: python bo_loop.py
Operate: python bo_loop.py --live
Stop: python bo_loop.py --stop (or python stop.py)
"""
import os
import sys
import threading
import argparse
import asyncio
import csv
import time
import warnings
from datetime import datetime
from pathlib import Path

import numpy as np
from sklearn.exceptions import ConvergenceWarning
from skopt import Optimizer
from skopt.space import Real

import lv
import uvvis
import run_all

warnings.filterwarnings("ignore", category=ConvergenceWarning)

DRY_RUN = True

ELECTRODE_AREA_CM2 = 100.0

I_MIN, I_MAX = (5.0, 20.0)

J_MIN, J_MAX = (1000 * I_MIN / ELECTRODE_AREA_CM2, 1000 * I_MAX / ELECTRODE_AREA_CM2)

Q_MIN, Q_MAX = (2.8666666666666667, 98.47333333333334)

FEED_CONC_M = 0.5

NAOH_CONC_M = 1.5

C_FLOOR_M = 0.0005

OBJECTIVE = 'weighted'

CONVERSION_TARGET = 0.95

W_PENALTY = 5.0

W_CONVERSION = 0.5

W_PRODUCTIVITY = 0.5

MAX_VOLTAGE_V = 25.0  # User-approved ceiling; never command above this.
VOLTAGE_LIMIT_V = 25.0  # CC compliance limit; PSU performs CC regulation.

N_INITIAL = 6

N_ITER = 14

EI_XI = 0.01

RANDOM_SEED = 1

RESUME_CSV = None

MAX_TOTAL_H = 14.0

CATHODE_VOLUME_ML = 70.0

FIRST_EXPERIMENT_VOLUMES = 1.5

LATER_EXPERIMENT_VOLUMES = 2.5

MEASURE_SPECTRA = 15



MAX_MEASURE_S = 600

MAX_FAILED_IN_A_ROW = 3



POLL_S = 2.0
PROGRESS_LOG_S = 15.0
EXPECTED_SPECTRUM_POINTS = 2048

SPECTRA_FOLDER = 'C:\\Users\\fen36728\\Desktop\\UV VIS'

SPECTRA_GLOB = '*OnlineUVVisHVN*.txt'

OUTPUT_FOLDER = Path(__file__).resolve().parent

FARADAY = 96485.33

K_AMP = FARADAY * (FEED_CONC_M or 0) / 60000.0

HIST_HEADER = ['time_iso', 'iteration', 'phase', 'Q_mL_min', 'charge_F_mol', 'I_A', 'conversion', 'conversion_std', 'steady', 'n_spectra', 'productivity_mmol_h', 'productivity_mmol_h_cm2', 'current_density_mA_cm2', 'faradaic_eff_not_measured', 'target_met', 'objective']
HIST_HEADER += ["voltage_setpoint_V"]

def current_for(q, charge):
    return charge * K_AMP * q

def charge_for(q, current):
    return current / (K_AMP * q)

def pump_flow_limits():
    lo = max((lv.rpm_to_flow(lv.CAL[p]['rpm_min'], p) for p in ('cathode', 'anode')))
    hi = min((lv.rpm_to_flow(lv.CAL[p]['rpm_max'], p) for p in ('cathode', 'anode')))
    return (lo, hi)

def flow_limits():
    lo, hi = pump_flow_limits()
    return (max(Q_MIN, lo), min(Q_MAX, hi))

def create_optimizer(q_lo, q_hi):
    """scikit-optimize minimises; tell it negative experimental scores."""
    return Optimizer(dimensions=[Real(q_lo, q_hi, name='flow_mL_min'), Real(I_MIN, I_MAX, name='current_A')], base_estimator='GP', n_initial_points=N_INITIAL, initial_point_generator='lhs', acq_func='EI', acq_optimizer='sampling', acq_func_kwargs={'xi': EI_XI}, acq_optimizer_kwargs={'n_points': 20000}, random_state=RANDOM_SEED)

class Spectra:
    def __init__(self, folder, pattern):
        self.folder, self.pattern = Path(folder), pattern
        self.seen, self.tries = set(), {}
        self.last_scan = {}

    def poll(self, since):
        out = []
        counts = dict(matched=0, seen_or_ignored=0, old=0, writing=0,
                      unreadable=0, invalid=0, ready=0)
        for p in self.folder.glob(self.pattern):
            counts["matched"] += 1
            if p.name in self.seen:
                counts["seen_or_ignored"] += 1
                continue
            try:
                mt = p.stat().st_mtime
            except OSError:
                counts["unreadable"] += 1
                continue
            if mt < since:
                self.seen.add(p.name)
                counts["old"] += 1
                continue
            if time.time() - mt < 0.5:
                counts["writing"] += 1
                continue
            try:
                wl, ab = uvvis.load_txt(p)
            except OSError as exc:
                counts["unreadable"] += 1
                self.tries[p.name] = self.tries.get(p.name, 0) + 1
                if self.tries[p.name] in (1, 5):
                    run_all.log.warning("Cannot read spectrum %s yet: %s; will retry", p.name, exc)
                continue
            reason = None
            if len(wl) != EXPECTED_SPECTRUM_POINTS:
                reason = f"{len(wl)} valid rows; expected {EXPECTED_SPECTRUM_POINTS}"
            elif not np.isfinite(wl).all() or not np.isfinite(ab).all():
                reason = "non-finite wavelength or absorbance"
            elif not np.all(np.diff(wl) > 0):
                reason = "wavelength axis is not strictly increasing"
            if reason is not None:
                counts["invalid"] += 1
                self.tries[p.name] = self.tries.get(p.name, 0) + 1
                if self.tries[p.name] in (1, 5):
                    run_all.log.warning("Spectrum %s not ready/invalid: %s; will retry", p.name, reason)
                continue
            self.seen.add(p.name)
            self.tries.pop(p.name, None)
            counts["ready"] += 1
            out.append((mt, p.name, uvvis.analyse(wl, ab, None)))
        self.last_scan = counts
        return sorted(out, key=lambda row: row[0])

    def diagnostic_summary(self):
        summary = ", ".join(f"{key}={value}" for key, value in self.last_scan.items())
        if not self.last_scan.get("matched", 0):
            examples = [p.name for _, p in zip(range(3), (p for p in self.folder.iterdir() if p.is_file()))]
            summary += "; filename examples=" + (", ".join(examples) if examples else "none")
        return summary


def conversion_of(res):
    """Conversion from one analysed spectrum, or None if the spectrum is unusable."""
    st = res['status']
    if st in ('calibrated', 'extrapolated') and res['conc_M'] is not None:
        c = max(res['conc_M'], C_FLOOR_M)
        return min(max(1.0 - c / FEED_CONC_M, 0.0), 1.0)
    return None

def mean(x):
    return sum(x) / len(x)

def std(x):
    m = mean(x)
    return (sum(((v - m) ** 2 for v in x)) / max(1, len(x) - 1)) ** 0.5

async def apply_setpoints(link, rpm1, rpm2, current, state):
    await run_all.start_together(link, rpm1, rpm2, current, VOLTAGE_LIMIT_V)
    state['condition_started'], state['I'] = (True, current)

def flush_seconds(q, first_experiment):
    volumes = FIRST_EXPERIMENT_VOLUMES if first_experiment else LATER_EXPERIMENT_VOLUMES
    return volumes * CATHODE_VOLUME_ML / q * 60.0


class ExperimentSkipped(Exception):
    """Operator requested a new condition; do not train on this experiment."""


class SkipControl:
    """Daemon console reader; requests apply only to the active experiment."""
    def __init__(self):
        self.lock = threading.Lock()
        self.active = False
        self.requested = False
        self.closed = False

    def arm(self):
        with self.lock:
            self.requested = False
            self.active = True

    def disarm(self):
        with self.lock:
            self.active = False
            self.requested = False

    def request(self):
        with self.lock:
            if self.active and not self.closed:
                self.requested = True

    def check(self):
        with self.lock:
            if self.requested:
                self.requested = False
                raise ExperimentSkipped()

    def close(self):
        with self.lock:
            self.closed = True
            self.active = False

    def start(self):
        threading.Thread(target=self._read_console, daemon=True).start()

    def _read_console(self):
        try:
            if os.name == "nt":
                import msvcrt
                while not self.closed:
                    if msvcrt.kbhit():
                        key = msvcrt.getwch()
                        if key in ("\x00", "\xe0"):
                            msvcrt.getwch()
                        elif key.lower() == "s":
                            self.request()
                    time.sleep(0.05)
            else:
                while not self.closed:
                    line = sys.stdin.readline()
                    if not line:
                        return
                    if line.strip().lower() == "s":
                        self.request()
        except (OSError, ValueError):
            return


def next_unskipped_condition(opt, skipped, rng):
    """Avoid repeating skipped conditions without inventing BO observations."""
    candidate = list(map(float, opt.ask()))
    for _ in range(1000):
        if not any(np.allclose(candidate, old, rtol=0, atol=1e-6) for old in skipped):
            return candidate
        candidate = list(map(float, opt.space.rvs(n_samples=1, random_state=rng)[0]))
    raise RuntimeError("Could not select a different condition after skipping")


async def run_experiment(link, spectra, q, charge, current, state, spec_writer, it):
    rpm1, rpm2 = (lv.flow_to_rpm(q, 'cathode'), lv.flow_to_rpm(q, 'anode'))
    control = state.get("skip_control")
    if control is not None:
        control.arm()
    t_change = time.time()
    first_experiment = not state['condition_started']
    await apply_setpoints(link, rpm1, rpm2, current, state)
    flush_s = flush_seconds(q, first_experiment)
    t_measure = time.time() + flush_s
    deadline = t_measure + MAX_MEASURE_S
    run_all.log.info('experiment %d: Q %.5f mL/min, I %.5f A, charge %.5f F/mol; flush %.1f s', it, q, current, charge, flush_s)
    run_all.log.info("Spectra folder: %s | pattern: %s", spectra.folder, spectra.pattern)
    records = []
    flush_announced = False
    next_progress = 0.0
    while time.time() < deadline:
        if control is not None:
            control.check()
        now = time.time()
        if now < t_measure:
            if now >= next_progress:
                run_all.log.info("Flushing: %.1f s remaining; spectrum reading paused", t_measure-now)
                next_progress = now + PROGRESS_LOG_S
            await asyncio.sleep(min(POLL_S, t_measure-now))
            continue
        if not flush_announced:
            run_all.log.info("Flush complete. Collecting %d usable spectra; no steadiness wait. Measurement timeout %.0f s.",
                         MEASURE_SPECTRA, MAX_MEASURE_S)
            flush_announced = True
            next_progress = 0.0
        for mt, name, res in spectra.poll(since=t_measure):
            x = conversion_of(res)
            eligible = time.time() >= t_measure and mt >= t_measure
            spec_writer.writerow([datetime.now().astimezone().isoformat(), it, name, res['status'], res['wavelength_nm'], res['conc_M'], x, 'measurement' if eligible else 'flushing', q, current, charge, VOLTAGE_LIMIT_V])
            if eligible and x is not None and len(records) < MEASURE_SPECTRA:
                records.append((mt, x))
            concentration = "n/a" if res["conc_M"] is None else f"{res['conc_M']:.6g}"
            conversion = "n/a" if x is None else f"{100*x:.2f}%"
            message = run_all.log.warning if eligible and x is None else run_all.log.info
            message("Spectrum %s [%s]: status=%s, c=%s M, conversion=%s, usable post-flush=%d/%d",
                    name, "measurement" if eligible else "flushing", res["status"],
                    concentration, conversion, min(len(records), MEASURE_SPECTRA), MEASURE_SPECTRA)
        if control is not None:
            control.check()
        if len(records) >= MEASURE_SPECTRA:
            xs = [x for _, x in records[:MEASURE_SPECTRA]]
            x_mean, x_std = (mean(xs), std(xs))
            prod = x_mean * FEED_CONC_M * q * 60.0
            p_ref = FEED_CONC_M * flow_limits()[1] * 60.0
            j = W_CONVERSION * x_mean + W_PRODUCTIVITY * prod / p_ref if OBJECTIVE == 'weighted' else prod / p_ref - W_PENALTY * max(0.0, CONVERSION_TARGET - x_mean)
            return {'X': x_mean, 'X_std': x_std, 'steady': None, 'n': len(xs), 'P': prod, 'FE': '', 'J': j, 't_start': t_change, 'target_met': x_mean >= CONVERSION_TARGET}
        now = time.time()
        if now >= next_progress:
            if now < t_measure:
                run_all.log.info("Flushing: %.1f s remaining; %s", t_measure-now, spectra.diagnostic_summary())
            else:
                xs = [x for _, x in records[-MEASURE_SPECTRA:]]
                spread = "n/a" if len(xs) < 2 else f"{std(xs):.4f}"
                run_all.log.info("Measurement: usable=%d/%d, conversion std=%s (diagnostic only), %.0f s remaining; %s",
                             min(len(records), MEASURE_SPECTRA), MEASURE_SPECTRA, spread,
                             max(0., deadline-now), spectra.diagnostic_summary())
                if spectra.last_scan.get("matched", 0) == 0:
                    run_all.log.warning("No spectrum files match %s in %s. Check the export folder, filename pattern and TXT saving.",
                                   spectra.pattern, spectra.folder)
            next_progress = now + PROGRESS_LOG_S
        await asyncio.sleep(POLL_S)
    run_all.log.warning("experiment %d rejected: %d usable post-flush spectra; need %d usable spectra. %s",
                    it, len(records), MEASURE_SPECTRA, spectra.diagnostic_summary())
    return None

def load_history(path):
    pts = []
    with open(path, newline='') as f:
        for row in csv.DictReader(f):
            pts.append(([float(row['Q_mL_min']), float(row['I_A'])], float(row['objective'])))
    return pts

async def run():
    stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    run_all.setup_logging(stamp)
    if VOLTAGE_LIMIT_V is not None or not DRY_RUN:
        run_all.validate_voltage(VOLTAGE_LIMIT_V)
    run_all.log.info("Voltage limit: %s V", VOLTAGE_LIMIT_V if VOLTAGE_LIMIT_V is not None else "not configured")
    if not np.isfinite(FEED_CONC_M) or FEED_CONC_M <= 0:
        raise ValueError('set FEED_CONC_M (vanillin feed concentration, mol/L)')
    if not DRY_RUN and (not Path(SPECTRA_FOLDER).is_dir()):
        raise FileNotFoundError(f'SPECTRA_FOLDER does not exist: {SPECTRA_FOLDER}')
    if not (0 < I_MIN < I_MAX and I_MIN >= 5 and (I_MAX <= 20)):
        raise ValueError('Current bounds must remain inside 5-20 A')
    if OBJECTIVE not in ('weighted', 'constrained'):
        raise ValueError('Unknown objective')
    if MEASURE_SPECTRA < 2 or FIRST_EXPERIMENT_VOLUMES < 1 or LATER_EXPERIMENT_VOLUMES < 1 or (CATHODE_VOLUME_ML <= 0):
        raise ValueError('Invalid measurement/flush settings')
    q_lo, q_hi = flow_limits()
    if q_lo >= q_hi:
        raise ValueError('no flow rate satisfies the current and charge limits at this feed concentration')
    p_lo, p_hi = pump_flow_limits()
    run_all.log.info('Pump calibrations allow %.2f - %.2f mL/min (cathode and anode)', p_lo, p_hi)
    run_all.log.info('Electrode %.0f cm2, current density %.0f-%.0f mA/cm2 = %.1f-%.1f A; vanillin %.2f M, NaOH %.2f M (both fixed)', ELECTRODE_AREA_CM2, J_MIN, J_MAX, I_MIN, I_MAX, FEED_CONC_M, NAOH_CONC_M)
    run_all.log.info('c0 = %.3f M: charge 1 F/mol at 1 mL/min = %.3f A', FEED_CONC_M, K_AMP)
    run_all.log.info('Search: Q %.5f-%.5f mL/min; I %.1f-%.1f A', q_lo, q_hi, I_MIN, I_MAX)
    run_all.log.info('Derived charge range %.5f-%.5f F/mol (no independent charge restriction)', charge_for(q_hi, I_MIN), charge_for(q_lo, I_MAX))
    opt = create_optimizer(q_lo, q_hi)
    done = 0
    if RESUME_CSV:
        for x, y in load_history(RESUME_CSV):
            if not (q_lo <= x[0] <= q_hi and I_MIN <= x[1] <= I_MAX and np.isfinite(y)):
                raise ValueError('Resume history has invalid or out-of-range observations')
            opt.tell(x, -y)
            done += 1
        run_all.log.info('resumed with %d earlier experiments from %s', done, RESUME_CSV)
    total = N_INITIAL + N_ITER
    run_all.log.info('Plan: %d experiments (%d start points, then %d chosen by the optimiser)', total, N_INITIAL, N_ITER)
    run_all.log.info('Optimiser: skopt.Optimizer, GP, expected improvement; initial design LHS')
    if DRY_RUN:
        q, current = map(float, opt.ask())
        run_all.log.info('First proposed condition: Q %.5f mL/min, I %.5f A, charge %.5f F/mol', q, current, charge_for(q, current))
        run_all.log.info('DRY RUN: nothing was started.')
        return
    hist_path = OUTPUT_FOLDER / f'bo_history_{stamp}.csv'
    spec_path = OUTPUT_FOLDER / f'bo_spectra_{stamp}.csv'
    hist_f = open(hist_path, 'w', newline='')
    spec_f = open(spec_path, 'w', newline='')
    hist_w = csv.writer(hist_f)

    class FlushingWriter:

        def writerow(self, row):
            csv.writer(spec_f).writerow(row)
            spec_f.flush()
    spec_w = FlushingWriter()
    hist_w.writerow(HIST_HEADER)
    spec_w.writerow(['time_iso', 'iteration', 'file', 'status', 'edge_nm', 'concentration_M', 'conversion', 'phase', 'Q_mL_min', 'I_A', 'charge_F_mol', 'voltage_setpoint_V'])
    run_all.log.info('History: %s', hist_path)
    link = run_all.Link()
    spectra = Spectra(SPECTRA_FOLDER, SPECTRA_GLOB)
    control = SkipControl()
    state = {'condition_started': False, 'I': 0.0, 'skip_control': control}
    skipped = []
    skip_rng = np.random.RandomState(RANDOM_SEED + 1000)
    reason, normal = ('finished', False)
    campaign_mono = time.monotonic()
    failed = 0
    run_all.keep_awake(True)
    try:
        await link.connect()
        if abs(float(await link.read(lv.PUMP1_SET))) > 0.05 or abs(float(await link.read(lv.PUMP2_SET))) > 0.05:
            raise RuntimeError('a pump has a nonzero setpoint - stop it first (python bo_loop.py --stop)')
        control.start()
        run_all.log.info("SKIP: press S in this terminal (Enter also works). Ctrl+C stops the campaign.")
        for it in range(done + 1, total + 1):
            if time.monotonic() - campaign_mono >= MAX_TOTAL_H * 3600:
                reason, normal = ('maximum campaign time reached', True)
                break
            phase = 'initial' if it <= N_INITIAL else 'bayes'
            q, current = next_unskipped_condition(opt, skipped, skip_rng)
            ch = charge_for(q, current)
            remaining = MAX_TOTAL_H * 3600 - (time.monotonic() - campaign_mono)
            try:
                res = await asyncio.wait_for(run_experiment(link, spectra, q, ch, current, state, spec_w, it), remaining)
            except ExperimentSkipped:
                control.disarm()
                skipped.append([q, current])
                spec_f.flush()
                run_all.log.warning("experiment %d SKIPPED by operator; no BO score recorded. Selecting a different condition; next flush is 2.5 volumes.", it)
                continue
            except asyncio.TimeoutError:
                reason, normal = ('maximum campaign time reached', True)
                break
            control.disarm()
            spec_f.flush()
            if res is None:
                failed += 1
                if failed >= MAX_FAILED_IN_A_ROW:
                    raise RuntimeError(f'{failed} experiments in a row without usable spectra - is OceanView saving?')
                continue
            failed = 0
            opt.tell([q, current], -res['J'])
            hist_w.writerow([datetime.now().isoformat(timespec='seconds'), it, phase, round(q, 3), round(ch, 3), round(current, 3), round(res['X'], 4), round(res['X_std'], 4), res['steady'], res['n'], round(res['P'], 3), round(res['P'] / ELECTRODE_AREA_CM2, 5), round(1000.0 * current / ELECTRODE_AREA_CM2, 1), res['FE'], res['target_met'], round(res['J'], 4), VOLTAGE_LIMIT_V])
            hist_f.flush()
            run_all.log.info('experiment %d done: conversion %.1f %% (target %.0f %%: %s), productivity %.2f mmol/h consumed vanillin, objective %.3f', it, 100 * res['X'], 100 * CONVERSION_TARGET, 'met' if res['target_met'] else 'NOT met', res['P'], res['J'])
        else:
            reason, normal = ('all experiments finished', True)
    except asyncio.CancelledError:
        reason = 'interrupted (Ctrl+C)'
        raise
    except Exception as e:
        reason = f'error: {e!r}'
        run_all.log.exception('campaign stopped by an error')
        raise
    finally:
        control.close()
        await run_all.safe_shutdown(link, reason, normal)
        await link.close()
        hist_f.close()
        spec_f.close()
        run_all.keep_awake(False)
    if opt.yi:
        i = int(np.argmin(opt.yi))
        q, current = opt.Xi[i]
        run_all.log.info('BEST MEASURED Q %.5f mL/min, I %.5f A, charge %.5f F/mol; objective %.5f', q, current, charge_for(q, current), -opt.yi[i])

async def stop_equipment():
    """Connect, request PSU off/current zero and stop both pumps."""
    run_all.setup_logging(datetime.now().strftime('%Y%m%d_%H%M%S'))
    link = run_all.Link()
    try:
        await link.connect()
        complete = await run_all.safe_shutdown(link, 'manual stop requested', normal=True)
        if not complete:
            raise RuntimeError('Shutdown incomplete; inspect the log and equipment')
    finally:
        await link.close()

def check_spectra_exports():
    """Inspect the newest export without connecting to or operating hardware."""
    run_all.setup_logging(datetime.now().strftime("%Y%m%d_%H%M%S"))
    folder = Path(SPECTRA_FOLDER)
    run_all.log.info("Spectrum check only: folder=%s | pattern=%s", folder, SPECTRA_GLOB)
    if not folder.is_dir():
        run_all.log.error("Spectrum folder does not exist: %s", folder)
        return 1
    files = []
    for p in folder.glob(SPECTRA_GLOB):
        try:
            files.append((p.stat().st_mtime, p))
        except OSError:
            continue
    watcher = Spectra(folder, SPECTRA_GLOB)
    if not files:
        watcher.poll(since=0)
        run_all.log.error("No matching spectra. %s", watcher.diagnostic_summary())
        return 1
    mature = [item for item in files if time.time() - item[0] >= 0.5]
    latest_time, latest = max(mature or files, key=lambda item: item[0])
    run_all.log.info("Matching files=%d; newest eligible candidate=%s; file age=%.1f s", len(files), latest.name, time.time()-latest_time)
    # Validate only the newest file; do not parse the entire historical folder.
    watcher.seen = {p.name for _, p in files if p.name != latest.name}
    records = watcher.poll(since=0)
    if not records:
        run_all.log.error("Newest export is not ready or invalid. %s", watcher.diagnostic_summary())
        return 1
    _, name, result = records[-1]
    x = conversion_of(result)
    run_all.log.info("Newest spectrum %s: status=%s, wavelength=%s nm, concentration=%s M, conversion=%s",
                name, result["status"], result["wavelength_nm"], result["conc_M"], x)
    return 0 if x is not None else 1


def main():
    global DRY_RUN, VOLTAGE_LIMIT_V
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--live', action='store_true', help='operate the setup')
    mode.add_argument('--stop', action='store_true', help='request PSU off and both pumps stopped')
    mode.add_argument("--check-spectra", action="store_true", help="check the newest spectrum export without hardware operation")
    parser.add_argument("--voltage-limit", "--voltage", dest="voltage_limit", type=float, default=None, help="PSU voltage ceiling in volts, maximum 25 V")
    args = parser.parse_args()
    if args.voltage_limit is not None:
        VOLTAGE_LIMIT_V = args.voltage_limit
    DRY_RUN = not args.live
    if args.check_spectra:
        raise SystemExit(check_spectra_exports())
    try:
        asyncio.run(stop_equipment() if args.stop else run())
    except KeyboardInterrupt:
        print('Interrupted; shutdown was requested. Check equipment/log for completion.')

if __name__ == "__main__":
    main()
