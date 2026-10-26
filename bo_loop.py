"""Modular vanillin BO: same experiment logic as vanillin_all_in_one.py.

Requires lv.py, uvvis.py and run_all.py in this folder.
Preview: python bo_loop.py
Operate: python bo_loop.py --live
Stop: python bo_loop.py --stop (or python stop.py)
"""
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
VOLTAGE_LIMIT_V = 25.0  # CC compliance limit; PSU adjusts actual voltage.

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

STEADY_TOL_X = 0.02

MAX_MEASURE_S = 600

MAX_FAILED_IN_A_ROW = 3

PUMP_TOL_RPM = 0.3

PUMP_FAULT_S = 30

POLL_S = 2.0

SPECTRA_FOLDER = 'C:\\Users\\fen36728\\Desktop\\UV VIS'

SPECTRA_GLOB = 'OnlineUVVisHVN*.txt'

OUTPUT_FOLDER = Path(__file__).resolve().parent

FARADAY = 96485.33

K_AMP = FARADAY * (FEED_CONC_M or 0) / 60000.0

HIST_HEADER = ['time_iso', 'iteration', 'phase', 'Q_mL_min', 'charge_F_mol', 'I_A', 'conversion', 'conversion_std', 'steady', 'n_spectra', 'productivity_mmol_h', 'productivity_mmol_h_cm2', 'current_density_mA_cm2', 'faradaic_eff_not_measured', 'target_met', 'objective']
HIST_HEADER += ["voltage_setpoint_V", "voltage_actual_mean_V"]

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
        self.folder, self.pattern = (Path(folder), pattern)
        self.seen, self.tries = (set(), {})

    def poll(self, since):
        out = []
        for p in self.folder.glob(self.pattern):
            if p.name in self.seen:
                continue
            try:
                mt = p.stat().st_mtime
            except OSError:
                continue
            if mt < since:
                self.seen.add(p.name)
                continue
            if time.time() - mt < 0.5:
                continue
            try:
                wl, ab = uvvis.load_txt(p)
            except OSError:
                continue
            if len(wl) != 2048 or not np.isfinite(wl).all() or (not np.isfinite(ab).all()) or (not np.all(np.diff(wl) > 0)):
                self.tries[p.name] = self.tries.get(p.name, 0) + 1
                if self.tries[p.name] == 5:
                    run_all.log.warning('still incomplete/invalid %s: %d lines; will retry', p.name, len(wl))
                continue
            self.seen.add(p.name)
            out.append((mt, p.name, uvvis.analyse(wl, ab, None)))
        return sorted(out, key=lambda r: r[0])

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

class PumpFault(Exception):
    pass

async def check_pumps(link, rpm1, rpm2, bad):
    r1 = float(await link.read(lv.PUMP1_ACT))
    r2 = float(await link.read(lv.PUMP2_ACT))
    if not bool(await link.read(lv.PSU_ON)):
        raise PumpFault('Power supply switched off unexpectedly')
    await run_all.read_voltage(link)
    now = time.time()
    if abs(r1 - rpm1) <= PUMP_TOL_RPM and abs(r2 - rpm2) <= PUMP_TOL_RPM:
        bad['since'] = None
        return
    if bad['since'] is None:
        bad['since'] = now
        run_all.log.warning('pump outside tolerance: cathode %.2f (target %.2f), anode %.2f (target %.2f)', r1, rpm1, r2, rpm2)
    elif now - bad['since'] >= PUMP_FAULT_S:
        raise PumpFault(f'cathode {r1:.2f} rpm (target {rpm1}), anode {r2:.2f} rpm (target {rpm2})')

async def apply_setpoints(link, rpm1, rpm2, current, state):
    await run_all.start_together(link, rpm1, rpm2, current, VOLTAGE_LIMIT_V)
    state['psu_on'], state['I'] = (True, current)

def flush_seconds(q, first_experiment):
    volumes = FIRST_EXPERIMENT_VOLUMES if first_experiment else LATER_EXPERIMENT_VOLUMES
    return volumes * CATHODE_VOLUME_ML / q * 60.0

async def run_experiment(link, spectra, q, charge, current, state, spec_writer, it):
    rpm1, rpm2 = (lv.flow_to_rpm(q, 'cathode'), lv.flow_to_rpm(q, 'anode'))
    bad = {'since': None}
    t_change = time.time()
    first_experiment = not state['psu_on']
    await apply_setpoints(link, rpm1, rpm2, current, state)
    flush_s = flush_seconds(q, first_experiment)
    t_measure = time.time() + flush_s
    deadline = t_measure + MAX_MEASURE_S
    run_all.log.info('experiment %d: Q %.5f mL/min, I %.5f A, charge %.5f F/mol; flush %.1f s', it, q, current, charge, flush_s)
    records = []
    voltage_records = []
    while time.time() < deadline:
        await check_pumps(link, rpm1, rpm2, bad)
        for mt, name, res in spectra.poll(since=t_change):
            x = conversion_of(res)
            actual_v = await run_all.read_voltage(link)
            eligible = mt >= t_measure
            spec_writer.writerow([datetime.now().astimezone().isoformat(), it, name, res['status'], res['wavelength_nm'], res['conc_M'], x, 'measurement' if eligible else 'flushing', q, current, charge, VOLTAGE_LIMIT_V, actual_v])
            if eligible and x is not None:
                records.append((mt, x))
                voltage_records.append(actual_v)
        if len(records) >= MEASURE_SPECTRA:
            xs = [x for _, x in records[-MEASURE_SPECTRA:]]
            if std(xs) <= STEADY_TOL_X:
                x_mean, x_std = (mean(xs), std(xs))
                prod = x_mean * FEED_CONC_M * q * 60.0
                p_ref = FEED_CONC_M * flow_limits()[1] * 60.0
                j = W_CONVERSION * x_mean + W_PRODUCTIVITY * prod / p_ref if OBJECTIVE == 'weighted' else prod / p_ref - W_PENALTY * max(0.0, CONVERSION_TARGET - x_mean)
                return {'X': x_mean, 'X_std': x_std, 'steady': True, 'n': len(xs), 'P': prod, 'FE': '', 'J': j, 't_start': t_change, 'target_met': x_mean >= CONVERSION_TARGET, 'voltage_actual_mean_V': mean(voltage_records[-MEASURE_SPECTRA:])}
        await asyncio.sleep(POLL_S)
    run_all.log.warning('experiment %d rejected: insufficient steady usable spectra', it)
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
    spec_w.writerow(['time_iso', 'iteration', 'file', 'status', 'edge_nm', 'concentration_M', 'conversion', 'phase', 'Q_mL_min', 'I_A', 'charge_F_mol', 'voltage_setpoint_V', 'voltage_actual_V'])
    run_all.log.info('History: %s', hist_path)
    link = run_all.Link()
    spectra = Spectra(SPECTRA_FOLDER, SPECTRA_GLOB)
    state = {'psu_on': False, 'I': 0.0}
    reason, normal = ('finished', False)
    campaign_mono = time.monotonic()
    failed = 0
    run_all.keep_awake(True)
    try:
        await link.connect()
        if float(await link.read(lv.PSU_ON)) != 0 or abs(float(await link.read(lv.PUMP1_ACT))) > 0.05 or abs(float(await link.read(lv.PUMP2_ACT))) > 0.05:
            raise RuntimeError('power supply is on or a pump is running - stop it first (python bo_loop.py --stop)')
        for it in range(done + 1, total + 1):
            if time.monotonic() - campaign_mono >= MAX_TOTAL_H * 3600:
                reason, normal = ('maximum campaign time reached', True)
                break
            phase = 'initial' if it <= N_INITIAL else 'bayes'
            q, current = map(float, opt.ask())
            ch = charge_for(q, current)
            remaining = MAX_TOTAL_H * 3600 - (time.monotonic() - campaign_mono)
            try:
                res = await asyncio.wait_for(run_experiment(link, spectra, q, ch, current, state, spec_w, it), remaining)
            except asyncio.TimeoutError:
                reason, normal = ('maximum campaign time reached', True)
                break
            spec_f.flush()
            if res is None:
                failed += 1
                if failed >= MAX_FAILED_IN_A_ROW:
                    raise RuntimeError(f'{failed} experiments in a row without usable spectra - is OceanView saving?')
                continue
            failed = 0
            opt.tell([q, current], -res['J'])
            hist_w.writerow([datetime.now().isoformat(timespec='seconds'), it, phase, round(q, 3), round(ch, 3), round(current, 3), round(res['X'], 4), round(res['X_std'], 4), res['steady'], res['n'], round(res['P'], 3), round(res['P'] / ELECTRODE_AREA_CM2, 5), round(1000.0 * current / ELECTRODE_AREA_CM2, 1), res['FE'], res['target_met'], round(res['J'], 4), VOLTAGE_LIMIT_V, round(res['voltage_actual_mean_V'], 4)])
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

def main():
    global DRY_RUN, VOLTAGE_LIMIT_V
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--live', action='store_true', help='operate the setup')
    mode.add_argument('--stop', action='store_true', help='request PSU off and both pumps stopped')
    parser.add_argument("--voltage-limit", "--voltage", dest="voltage_limit", type=float, default=None, help="PSU voltage ceiling in volts, maximum 25 V")
    args = parser.parse_args()
    if args.voltage_limit is not None:
        VOLTAGE_LIMIT_V = args.voltage_limit
    DRY_RUN = not args.live
    try:
        asyncio.run(stop_equipment() if args.stop else run())
    except KeyboardInterrupt:
        print('Interrupted; shutdown was requested. Check equipment/log for completion.')

if __name__ == "__main__":
    main()
