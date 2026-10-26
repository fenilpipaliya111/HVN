"""Single-file vanillin electrolysis Bayesian optimisation with scikit-optimize.

Python 3.10+. Install dependencies once:
    python -m pip install asyncua numpy "scikit-optimize>=0.10.2,<0.11"

Preview: python vanillin_all_in_one.py
Operate: python vanillin_all_in_one.py --live
Stop:    python vanillin_all_in_one.py --stop

Contains pump calibration/control, power supply control, UV-Vis analysis,
Bayesian optimisation, CSV logging and shutdown. No other local Python files
or calibration workbook required. Instrument software must save spectra.

Review FEED_CONC_M, SPECTRA_FOLDER and OPC_URL before live use.
Both pumps and PSU enable are commanded concurrently, without ramp waits;
exact physical simultaneous actuation is not guaranteed. The current setpoint
is prepared before enable. First condition: 1.5 volumes, later: 2.5 volumes.
At 70 mL and 5 mL/min, this is 21 min initially and 35 min subsequently.
Spectra log immediately; only post-flush steady spectra train BO.
Productivity is vanillin consumed, not independently measured product yield.
"""
import argparse
import asyncio
import csv
import logging
import os
import time
import warnings
from bisect import bisect_right
from datetime import datetime
from math import isfinite
from pathlib import Path

import numpy as np
from asyncua import Client, ua
from sklearn.exceptions import ConvergenceWarning
from skopt import Optimizer
from skopt.space import Real



# =================== EXPERIMENT / BO SETTINGS ===================
DRY_RUN = True

ELECTRODE_AREA_CM2 = 100.0

I_MIN, I_MAX = (5.0, 20.0)

J_MIN, J_MAX = (1000 * I_MIN / ELECTRODE_AREA_CM2, 1000 * I_MAX / ELECTRODE_AREA_CM2)

Q_MIN, Q_MAX = (2.8666666666666667, 98.47333333333334)

FEED_CONC_M = 0.5  # starting/feed vanillin concentration, mol/L

NAOH_CONC_M = 1.5

C_FLOOR_M = 0.0005

OBJECTIVE = 'weighted'

CONVERSION_TARGET = 0.95

W_PENALTY = 5.0

W_CONVERSION = 0.5

W_PRODUCTIVITY = 0.5

START_FLOW_ML_MIN = 10.0  # First accepted condition of a fresh campaign.
START_CURRENT_A = 5.0
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

# =================== OPC AND PUMP CALIBRATION ===================
OPC_URL = 'opc.tcp://localhost:57888'

PREFIX = 'Hitec_OPC_DA20_Server->HVN:'

PUMP1_SET, PUMP1_ACT = ('PUMP1.W1', 'PUMP1.X1')

PUMP2_SET, PUMP2_ACT = ('PUMP2.W1', 'PUMP2.X1')

PSU_ON, PSU_SET = ('POWERSUPPLY.ON', 'POWERSUPPLY.W2')
PSU_VOLTAGE_SET, PSU_VOLTAGE_ACT = "POWERSUPPLY.W1", "POWERSUPPLY.X1"

ANODE_K = 0.5071118969420833

CATHODE_RPM = (10.0, 50.0, 100.0, 150.0, 200.0, 250.0, 300.0)

CATHODE_FLOW = (2.8666666666666667, 14.373333333333335, 26.073333333333334, 44.20666666666667, 60.67666666666667, 77.57, 98.47333333333334)

PUMP_CAL = {'cathode': {'rpm_min': 10.0, 'rpm_max': 300.0}, 'anode': {'rpm_min': 5.0, 'rpm_max': 300.0}}

# =================== UV-VIS SETTINGS ===================
METHOD = 'crossing'

LEVEL = 0.75

BAND = 0.05

CLUSTER_NM = 2.0

MAX_CROSSINGS = 6

WL_MIN_NM = 357.0

WL_MAX_NM = 435.0

SMOOTH_POINTS = 5

MIN_BAND_POINTS = 3

MAX_SPAN_NM = 10.0

CAL_A0 = 430.03839

CAL_B = 19.12081

CAL_LOW_NM = 399.0

CAL_HIGH_NM = 431.0

FLOOR_NM = 357.0

# =================== CONNECTION / DERIVED VALUES ===================
RECONNECT_TIMEOUT_S = 60

RECONNECT_WAIT_S = 5.0

log = logging.getLogger('run')

FARADAY = 96485.33

K_AMP = FARADAY * (FEED_CONC_M or 0) / 60000.0

HIST_HEADER = ['time_iso', 'iteration', 'phase', 'Q_mL_min', 'charge_F_mol', 'I_A', 'conversion', 'conversion_std', 'steady', 'n_spectra', 'productivity_mmol_h', 'productivity_mmol_h_cm2', 'current_density_mA_cm2', 'faradaic_eff_not_measured', 'target_met', 'objective']
HIST_HEADER += ["voltage_setpoint_V", "voltage_actual_mean_V"]

warnings.filterwarnings("ignore", category=ConvergenceWarning)

# =================== PUMP / OPC FUNCTIONS ===================
def interpolate(x, xs, ys):
    i = max(0, min(len(xs) - 2, bisect_right(xs, x) - 1))
    return ys[i] + (x - xs[i]) * (ys[i + 1] - ys[i]) / (xs[i + 1] - xs[i])

def node(client, tag):
    return client.get_node(f'ns=3;s={PREFIX}{tag}')

async def opc_read(client, tag):
    dv = await node(client, tag).read_data_value()
    if not dv.StatusCode.is_good():
        raise RuntimeError(f'{tag}: bad status {dv.StatusCode.name}')
    return dv.Value.Value

async def opc_write(client, tag, value):
    n = node(client, tag)
    vtype = await n.read_data_type_as_variant_type()
    if vtype == ua.VariantType.Boolean:
        value = bool(value)
    elif vtype in (ua.VariantType.Float, ua.VariantType.Double):
        value = float(value)
    else:
        if float(value) != int(value):
            raise ValueError(f'{tag} requires integer commands; configure pump resolution before running')
        value = int(value)
    await n.write_value(ua.DataValue(ua.Variant(value, vtype)))

def flow_to_rpm(flow, pump):
    flow = float(flow)
    c = PUMP_CAL[pump]
    if not isfinite(flow) or flow <= 0:
        raise ValueError('flow must be finite and positive')
    rpm = flow / ANODE_K if pump == 'anode' else interpolate(flow, CATHODE_FLOW, CATHODE_RPM)
    if not c['rpm_min'] - 1e-09 <= rpm <= c['rpm_max'] + 1e-09:
        raise ValueError(f'{pump}: {flow} mL/min outside measured calibration')
    return min(c['rpm_max'], max(c['rpm_min'], rpm))

def rpm_to_flow(rpm, pump):
    rpm = float(rpm)
    if not isfinite(rpm):
        raise ValueError('RPM must be finite')
    if rpm == 0:
        return 0.0
    if pump == 'anode':
        return ANODE_K * rpm
    return interpolate(rpm, CATHODE_RPM, CATHODE_FLOW)

# =================== COMMUNICATION / START / STOP ===================
class ConnectionLost(Exception):
    pass

def setup_logging(stamp):
    log.setLevel(logging.INFO)
    if log.handlers:
        return
    fmt = logging.Formatter('%(asctime)s %(levelname)s %(message)s')
    fh = logging.FileHandler(OUTPUT_FOLDER / f'run_{stamp}.log', encoding='utf-8')
    sh = logging.StreamHandler()
    for h in (fh, sh):
        h.setFormatter(fmt)
        log.addHandler(h)

def keep_awake(on):
    """Stop Windows from going to sleep while the script runs."""
    if os.name != 'nt':
        return
    try:
        import ctypes
        es_continuous, es_system_required = (2147483648, 1)
        flags = es_continuous | (es_system_required if on else 0)
        ctypes.windll.kernel32.SetThreadExecutionState(flags)
    except Exception as e:
        log.warning('could not change sleep setting: %r', e)

class Link:
    """OPC UA connection that reconnects by itself after short interruptions."""

    def __init__(self):
        self.client = None
        self.gen = 0
        self._lock = asyncio.Lock()

    async def connect(self):
        client = Client(OPC_URL, timeout=60)
        await client.connect()
        self.client = client

    async def close(self):
        if self.client is not None:
            try:
                await self.client.disconnect()
            except Exception:
                pass

    async def _reconnect(self, gen):
        async with self._lock:
            if self.gen != gen:
                return
            try:
                if self.client is not None:
                    await self.client.disconnect()
            except Exception:
                pass
            try:
                await self.connect()
                self.gen += 1
                log.info('OPC connection re-established')
            except Exception as e:
                log.warning('reconnect failed: %r', e)

    async def _call(self, fn, tag, *extra, timeout_s=None):
        deadline = time.monotonic() + (RECONNECT_TIMEOUT_S if timeout_s is None else timeout_s)
        while True:
            gen = self.gen
            try:
                return await fn(self.client, tag, *extra)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning('OPC call on %s failed: %r', tag, e)
                if time.monotonic() >= deadline:
                    raise ConnectionLost(f'{tag}: {e!r}') from e
                await asyncio.sleep(RECONNECT_WAIT_S)
                await self._reconnect(gen)

    async def read(self, tag, timeout_s=None):
        return await self._call(opc_read, tag, timeout_s=timeout_s)

    async def write(self, tag, value, timeout_s=None):
        return await self._call(opc_write, tag, value, timeout_s=timeout_s)

async def safe_shutdown(link, reason, normal=False):
    """Power supply off first, then both pumps. Every step is tried."""
    (log.info if normal else log.error)('SHUTDOWN: %s', reason)
    if link.client is None:
        log.error('no OPC connection was ever made, nothing to switch off')
        return False
    complete = True
    for tag, val in ((PSU_ON, 0), (PSU_SET, 0), (PSU_VOLTAGE_SET, 0), (PUMP1_SET, 0), (PUMP2_SET, 0)):
        try:
            await link.write(tag, val, timeout_s=60)
            log.info('shutdown: %s = %s', tag, val)
        except Exception as e:
            complete = False
            log.error('COULD NOT WRITE %s = %s: %r', tag, val, e)
    return complete

def validate_voltage(value):
    """Validate the commanded CC voltage limit against the 25 V ceiling."""
    if value is None:
        raise ValueError("Set VOLTAGE_LIMIT_V or pass --voltage-limit VOLTS before live operation")
    voltage = float(value)
    if not isfinite(voltage) or not 0 < voltage <= MAX_VOLTAGE_V:
        raise ValueError("Voltage limit must be finite, greater than zero, and no more than 25 V")
    return voltage


async def read_voltage(link):
    """Read actual voltage; never write to X1. Over-ceiling readings stop the run."""
    actual_v = float(await link.read(PSU_VOLTAGE_ACT))
    if not isfinite(actual_v):
        raise RuntimeError("Actual voltage readback is not finite")
    if actual_v > MAX_VOLTAGE_V:
        raise RuntimeError(f"Actual voltage {actual_v:.3f} V exceeds the 25 V maximum")
    return actual_v


async def start_together(link, rpm1, rpm2, current, voltage):
    voltage = validate_voltage(voltage)
    if not 5.0 <= current <= 20.0:
        raise ValueError("Current must stay within 5-20 A")
    # Prepare both setpoints before PSU enable; never write to actual-voltage X1.
    await link.write(PSU_VOLTAGE_SET, voltage)
    await link.write(PSU_SET, current)
    tasks = [asyncio.create_task(link.write(tag, value)) for tag, value in (
        (PUMP1_SET, rpm1), (PUMP2_SET, rpm2), (PSU_ON, 1))]
    try:
        await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


# =================== UV-VIS ANALYSIS ===================
def load_txt(path):
    """Return (wavelengths, absorbances) from a two-column text file."""
    wl, ab = ([], [])
    with open(path, 'r', errors='ignore') as f:
        for line in f:
            parts = line.replace(',', '.').split()
            if len(parts) < 2:
                continue
            try:
                x = float(parts[0])
                y = float(parts[1])
            except ValueError:
                continue
            wl.append(x)
            ab.append(y)
    if len(wl) > 1 and wl[0] > wl[-1]:
        wl.reverse()
        ab.reverse()
    return (wl, ab)

def smooth(y, n):
    """Centered moving average (shorter window at the ends)."""
    if n <= 1:
        return list(y)
    half = n // 2
    prefix = [0.0]
    for v in y:
        prefix.append(prefix[-1] + v)
    out = []
    size = len(y)
    for i in range(size):
        lo = max(0, i - half)
        hi = min(size, i + half + 1)
        out.append((prefix[hi] - prefix[lo]) / (hi - lo))
    return out

def find_edge(wl, ab):
    """Return (edge_wavelength_nm or None, status, n_points, span_nm)."""
    idx = [i for i, w in enumerate(wl) if WL_MIN_NM <= w <= WL_MAX_NM]
    if len(idx) < 20:
        return (None, 'no_data', 0, 0.0)
    w = [wl[i] for i in idx]
    a = smooth([ab[i] for i in idx], SMOOTH_POINTS)
    lo, hi = (LEVEL - BAND, LEVEL + BAND)
    if a[-1] >= lo:
        return (None, 'too_concentrated', 0, 0.0)
    j = len(a) - 1
    while j >= 0 and a[j] < lo:
        j -= 1
    if j < 0:
        return (None, 'no_edge', 0, 0.0)
    pts = []
    while j >= 0 and a[j] <= hi:
        pts.append((w[j], a[j]))
        j -= 1
    n = len(pts)
    if n < MIN_BAND_POINTS:
        return (None, 'no_clear_edge', n, 0.0)
    span = max((p[0] for p in pts)) - min((p[0] for p in pts))
    if span > MAX_SPAN_NM:
        return (None, 'no_clear_edge', n, span)
    ma = sum((p[1] for p in pts)) / n
    mw = sum((p[0] for p in pts)) / n
    sxx = sum(((p[1] - ma) ** 2 for p in pts))
    sxy = sum(((p[1] - ma) * (p[0] - mw) for p in pts))
    if sxx < 1e-09:
        return (None, 'no_clear_edge', n, span)
    m = sxy / sxx
    if m >= 0:
        return (None, 'no_clear_edge', n, span)
    edge = mw + m * (LEVEL - ma)
    wmin = min((p[0] for p in pts))
    wmax = max((p[0] for p in pts))
    if not wmin - 1.0 <= edge <= wmax + 1.0:
        return (None, 'no_clear_edge', n, span)
    return (edge, 'ok', n, span)

def find_edge_crossing(wl, ab):
    """Interpolated crossing of A = LEVEL on the falling edge.

    Scans from the red end towards shorter wavelengths, interpolates the wavelength
    between the two points that straddle LEVEL, and averages crossings that lie
    within CLUSTER_NM of the first one (noise wiggles on the same edge).
    Returns (edge_wavelength_nm or None, status, n_crossings_used, spread_nm).
    """
    idx = [i for i, w in enumerate(wl) if WL_MIN_NM <= w <= WL_MAX_NM]
    if len(idx) < 20:
        return (None, 'no_data', 0, 0.0)
    w = [wl[i] for i in idx]
    a = smooth([ab[i] for i in idx], SMOOTH_POINTS)
    if a[-1] >= LEVEL:
        return (None, 'too_concentrated', 0, 0.0)
    crossings = []
    for i in range(len(a) - 1, 0, -1):
        if a[i] < LEVEL <= a[i - 1]:
            x = w[i - 1] + (LEVEL - a[i - 1]) * (w[i] - w[i - 1]) / (a[i] - a[i - 1])
            crossings.append(x)
    if not crossings:
        return (None, 'no_edge', 0, 0.0)
    first = crossings[0]
    if len([x for x in crossings if first - x <= MAX_SPAN_NM]) > MAX_CROSSINGS:
        return (None, 'no_clear_edge', len(crossings), 0.0)
    cluster = [x for x in crossings if first - x <= CLUSTER_NM]
    edge = sum(cluster) / len(cluster)
    return (edge, 'ok', len(cluster), max(cluster) - min(cluster))

def conc_from_wavelength(wavelength_nm):
    """Concentration in mol/L from the edge wavelength (A = 0.75 calibration)."""
    return 10 ** ((wavelength_nm - CAL_A0) / CAL_B)

def analyse(wl, ab, feed_conc_m=None):
    """Full analysis of one spectrum. Returns a dict.

    status:
      calibrated        edge inside the calibrated range (about 0.02 - 1 M)
      extrapolated      edge between FLOOR_NM and CAL_LOW_NM: same equation, but
                        outside your standards, treat as a trend only
      too_concentrated  edge beyond about 431 nm / still absorbing at the red end
      at_floor          edge below FLOOR_NM: not resolved any more
      no_clear_edge, no_edge, no_data   no usable edge in this spectrum
    """
    finder = find_edge_crossing if METHOD == 'crossing' else find_edge
    edge, status, n, span = finder(wl, ab)
    in_range = [v for w, v in zip(wl, smooth(ab, SMOOTH_POINTS)) if WL_MIN_NM <= w <= WL_MAX_NM]
    result = {'wavelength_nm': edge, 'conc_M': None, 'conversion': None, 'status': status, 'n_points': n, 'span_nm': span, 'max_abs': max(in_range) if in_range else None}
    if edge is None:
        return result
    if edge > CAL_HIGH_NM:
        result['status'] = 'too_concentrated'
        return result
    if edge < FLOOR_NM:
        result['status'] = 'at_floor'
        return result
    conc = conc_from_wavelength(edge)
    result['conc_M'] = conc
    result['status'] = 'calibrated' if edge >= CAL_LOW_NM else 'extrapolated'
    if feed_conc_m:
        result['conversion'] = 1.0 - conc / feed_conc_m
    return result

# =================== OPTIMISATION AND CAMPAIGN ===================
def current_for(q, charge):
    return charge * K_AMP * q

def charge_for(q, current):
    return current / (K_AMP * q)

def pump_flow_limits():
    lo = max((rpm_to_flow(PUMP_CAL[p]['rpm_min'], p) for p in ('cathode', 'anode')))
    hi = min((rpm_to_flow(PUMP_CAL[p]['rpm_max'], p) for p in ('cathode', 'anode')))
    return (lo, hi)

def flow_limits():
    lo, hi = pump_flow_limits()
    return (max(Q_MIN, lo), min(Q_MAX, hi))

def create_optimizer(q_lo, q_hi):
    """scikit-optimize minimises; tell it negative experimental scores."""
    return Optimizer(
        dimensions=[
            Real(q_lo, q_hi, name="flow_mL_min"),
            Real(I_MIN, I_MAX, name="current_A"),
        ],
        base_estimator="GP",
        n_initial_points=N_INITIAL,
        initial_point_generator="lhs",
        acq_func="EI",
        acq_optimizer="sampling",
        acq_func_kwargs={"xi": EI_XI},
        acq_optimizer_kwargs={"n_points": 20000},
        random_state=RANDOM_SEED,
    )


def next_condition(optimizer):
    """Start fresh campaigns low; let BO choose after an accepted result."""
    if not optimizer.Xi:
        return float(START_FLOW_ML_MIN), float(START_CURRENT_A)
    return tuple(map(float, optimizer.ask()))


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
                wl, ab = load_txt(p)
            except OSError:
                continue
            if len(wl) != 2048 or not np.isfinite(wl).all() or (not np.isfinite(ab).all()) or (not np.all(np.diff(wl) > 0)):
                self.tries[p.name] = self.tries.get(p.name, 0) + 1
                if self.tries[p.name] == 5:
                    log.warning('still incomplete/invalid %s: %d lines; will retry', p.name, len(wl))
                continue
            self.seen.add(p.name)
            out.append((mt, p.name, analyse(wl, ab, None)))
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
    r1 = float(await link.read(PUMP1_ACT))
    r2 = float(await link.read(PUMP2_ACT))
    if not bool(await link.read(PSU_ON)):
        raise PumpFault('Power supply switched off unexpectedly')
    await read_voltage(link)
    now = time.time()
    if abs(r1 - rpm1) <= PUMP_TOL_RPM and abs(r2 - rpm2) <= PUMP_TOL_RPM:
        bad['since'] = None
        return
    if bad['since'] is None:
        bad['since'] = now
        log.warning('pump outside tolerance: cathode %.2f (target %.2f), anode %.2f (target %.2f)', r1, rpm1, r2, rpm2)
    elif now - bad['since'] >= PUMP_FAULT_S:
        raise PumpFault(f'cathode {r1:.2f} rpm (target {rpm1}), anode {r2:.2f} rpm (target {rpm2})')

async def apply_setpoints(link, rpm1, rpm2, current, state):
    await start_together(link, rpm1, rpm2, current, VOLTAGE_LIMIT_V)
    state['psu_on'], state['I'] = (True, current)

def flush_seconds(q, first_experiment):
    volumes = FIRST_EXPERIMENT_VOLUMES if first_experiment else LATER_EXPERIMENT_VOLUMES
    return volumes * CATHODE_VOLUME_ML / q * 60.0

async def run_experiment(link, spectra, q, charge, current, state, spec_writer, it):
    rpm1, rpm2 = (flow_to_rpm(q, 'cathode'), flow_to_rpm(q, 'anode'))
    bad = {'since': None}
    t_change = time.time()
    first_experiment = not state['psu_on']
    await apply_setpoints(link, rpm1, rpm2, current, state)
    flush_s = flush_seconds(q, first_experiment)
    t_measure = time.time() + flush_s
    deadline = t_measure + MAX_MEASURE_S
    log.info('experiment %d: Q %.5f mL/min, I %.5f A, charge %.5f F/mol; flush %.1f s', it, q, current, charge, flush_s)
    records = []
    voltage_records = []
    while time.time() < deadline:
        await check_pumps(link, rpm1, rpm2, bad)
        for mt, name, res in spectra.poll(since=t_change):
            x = conversion_of(res)
            actual_v = await read_voltage(link)
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
    log.warning('experiment %d rejected: insufficient steady usable spectra', it)
    return None

def load_history(path):
    pts = []
    with open(path, newline='') as f:
        for row in csv.DictReader(f):
            pts.append(([float(row['Q_mL_min']), float(row['I_A'])], float(row['objective'])))
    return pts

async def run():
    stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    setup_logging(stamp)
    if VOLTAGE_LIMIT_V is not None or not DRY_RUN:
        validate_voltage(VOLTAGE_LIMIT_V)
    log.info("Voltage limit: %s V", VOLTAGE_LIMIT_V if VOLTAGE_LIMIT_V is not None else "not configured")
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
    if not (np.isfinite(START_FLOW_ML_MIN) and q_lo <= START_FLOW_ML_MIN <= q_hi):
        raise ValueError("START_FLOW_ML_MIN must be within the shared flow bounds")
    if not (np.isfinite(START_CURRENT_A) and I_MIN <= START_CURRENT_A <= I_MAX):
        raise ValueError("START_CURRENT_A must be within the current bounds")
    if q_lo >= q_hi:
        raise ValueError('no flow rate satisfies the current and charge limits at this feed concentration')
    p_lo, p_hi = pump_flow_limits()
    log.info('Pump calibrations allow %.2f - %.2f mL/min (cathode and anode)', p_lo, p_hi)
    log.info('Electrode %.0f cm2, current density %.0f-%.0f mA/cm2 = %.1f-%.1f A; vanillin %.2f M, NaOH %.2f M (both fixed)', ELECTRODE_AREA_CM2, J_MIN, J_MAX, I_MIN, I_MAX, FEED_CONC_M, NAOH_CONC_M)
    log.info('c0 = %.3f M: charge 1 F/mol at 1 mL/min = %.3f A', FEED_CONC_M, K_AMP)
    log.info('Search: Q %.5f-%.5f mL/min; I %.1f-%.1f A', q_lo, q_hi, I_MIN, I_MAX)
    log.info('Derived charge range %.5f-%.5f F/mol (no independent charge restriction)', charge_for(q_hi, I_MIN), charge_for(q_lo, I_MAX))
    opt = create_optimizer(q_lo, q_hi)
    done = 0
    if RESUME_CSV:
        for x, y in load_history(RESUME_CSV):
            if not (q_lo <= x[0] <= q_hi and I_MIN <= x[1] <= I_MAX and np.isfinite(y)):
                raise ValueError('Resume history has invalid or out-of-range observations')
            opt.tell(x, -y)  # skopt minimises; CSV stores maximisation scores.
            done += 1
        log.info('resumed with %d earlier experiments from %s', done, RESUME_CSV)
    total = N_INITIAL + N_ITER
    log.info('Plan: %d experiments (%d start points, then %d chosen by the optimiser)', total, N_INITIAL, N_ITER)
    log.info("Optimiser: skopt.Optimizer, GP, expected improvement; fixed low start, then LHS initial points")
    if DRY_RUN:
        q, current = next_condition(opt)
        log.info("First proposed condition: Q %.5f mL/min, I %.5f A, charge %.5f F/mol",
                 q, current, charge_for(q, current))
        log.info('DRY RUN: nothing was started.')
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
    log.info('History: %s', hist_path)
    link = Link()
    spectra = Spectra(SPECTRA_FOLDER, SPECTRA_GLOB)
    state = {'psu_on': False, 'I': 0.0}
    reason, normal = ('finished', False)
    campaign_mono = time.monotonic()
    failed = 0
    keep_awake(True)
    try:
        await link.connect()
        if float(await link.read(PSU_ON)) != 0 or abs(float(await link.read(PUMP1_ACT))) > 0.05 or abs(float(await link.read(PUMP2_ACT))) > 0.05:
            raise RuntimeError('power supply is on or a pump is running - stop it first (python vanillin_all_in_one.py --stop)')
        for it in range(done + 1, total + 1):
            if time.monotonic() - campaign_mono >= MAX_TOTAL_H * 3600:
                reason, normal = ('maximum campaign time reached', True)
                break
            phase = 'initial' if it <= N_INITIAL else 'bayes'
            q, current = next_condition(opt)
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
            opt.tell([q, current], -res['J'])  # Minimise the negative score.
            hist_w.writerow([datetime.now().isoformat(timespec='seconds'), it, phase, round(q, 3), round(ch, 3), round(current, 3), round(res['X'], 4), round(res['X_std'], 4), res['steady'], res['n'], round(res['P'], 3), round(res['P'] / ELECTRODE_AREA_CM2, 5), round(1000.0 * current / ELECTRODE_AREA_CM2, 1), res['FE'], res['target_met'], round(res['J'], 4), VOLTAGE_LIMIT_V, round(res['voltage_actual_mean_V'], 4)])
            hist_f.flush()
            log.info('experiment %d done: conversion %.1f %% (target %.0f %%: %s), productivity %.2f mmol/h consumed vanillin, objective %.3f', it, 100 * res['X'], 100 * CONVERSION_TARGET, 'met' if res['target_met'] else 'NOT met', res['P'], res['J'])
        else:
            reason, normal = ('all experiments finished', True)
    except asyncio.CancelledError:
        reason = 'interrupted (Ctrl+C)'
        raise
    except Exception as e:
        reason = f'error: {e!r}'
        log.exception('campaign stopped by an error')
        raise
    finally:
        await safe_shutdown(link, reason, normal)
        await link.close()
        hist_f.close()
        spec_f.close()
        keep_awake(False)
    if opt.yi:
        i = int(np.argmin(opt.yi))
        q, current = opt.Xi[i]
        log.info('BEST MEASURED Q %.5f mL/min, I %.5f A, charge %.5f F/mol; objective %.5f', q, current, charge_for(q, current), -opt.yi[i])

async def stop_equipment():
    """Connect, request PSU off/current zero and stop both pumps."""
    setup_logging(datetime.now().strftime("%Y%m%d_%H%M%S"))
    link = Link()
    try:
        await link.connect()
        complete = await safe_shutdown(link, "manual stop requested", normal=True)
        if not complete:
            raise RuntimeError("Shutdown incomplete; inspect the log and equipment")
    finally:
        await link.close()


def main():
    global DRY_RUN, VOLTAGE_LIMIT_V
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--live", action="store_true", help="operate the setup")
    mode.add_argument("--stop", action="store_true", help="request PSU off and both pumps stopped")
    parser.add_argument("--voltage-limit", "--voltage", dest="voltage_limit", type=float, default=None, help="PSU voltage ceiling in volts, maximum 25 V")
    args = parser.parse_args()
    if args.voltage_limit is not None:
        VOLTAGE_LIMIT_V = args.voltage_limit
    DRY_RUN = not args.live
    try:
        asyncio.run(stop_equipment() if args.stop else run())
    except KeyboardInterrupt:
        print("Interrupted; shutdown was requested. Check equipment/log for completion.")


if __name__ == "__main__":
    main()
