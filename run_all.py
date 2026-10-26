"""Automatic run: both pumps and power supply together -> UV-Vis logging.

Edit the SETTINGS block, then start:   python run_all.py
Needs lv.py and uvvis.py in the same folder. Nothing asks for input while it runs.
This is a fixed-condition runner; use bo_loop.py for Bayesian optimisation.
"""
import asyncio
import csv
import logging
import os
import time
from math import isfinite
from datetime import datetime
from pathlib import Path

import lv
import uvvis

# ========================= SETTINGS (edit before each run) =========================
DRY_RUN = True                  # True: only log what would happen, nothing is written
FLOW_ML_MIN = 5.0               # target flow for both pumps
MAX_VOLTAGE_V = 25.0  # User-approved ceiling; never command above this.
VOLTAGE_LIMIT_V = 25.0  # CC compliance limit; PSU performs CC regulation.
CURRENT_A = 5.0                 # power supply current setpoint (placeholder, set yours)
PSU_DELAY_S = 0                 # wait between setting the current and switching on

CATHODE_VOLUME_ML = 70.0        # volume from cell entrance to the UV-Vis cuvette (cathode side)
DELAY_FACTOR = 1.5              # delay before the first spectrum = volume / flow * this

FEED_CONC_M = 0.5              # feed concentration of vanillin, e.g. 0.5 (None = no conversion)
FEED_VOLUME_ML_CATHODE = None   # volume of feed in the cathode reservoir (None = not used)
FEED_VOLUME_ML_ANODE = None     # volume of feed in the anode reservoir (None = not used)
FEED_USE_FRACTION = 0.9         # stop when this fraction of the feed would be used
RUN_DURATION_H = 8.0            # hard maximum run time, counted from pump 1 start

SPECTRA_FOLDER = r"C:\Users\fen36728\Desktop\UV VIS"   # folder where OceanView writes the .txt files
SPECTRA_GLOB = "*OnlineUVVisHVN*.txt"     # which files in that folder are spectra

# ----------------------- protection and timing (rarely changed) -----------------------
MONITOR_S = 5.0                 # how often the pumps are checked
POLL_S = 2.0                    # how often the spectra folder is checked
RECONNECT_TIMEOUT_S = 60        # OPC connection must be back within this time
RECONNECT_WAIT_S = 5.0
NO_SPECTRUM_WARN_S = 600        # warn if OceanView writes nothing for this long
HEARTBEAT_S = 60                # status line in the log every minute
COUNTDOWN_LOG_S = 30
OUTPUT_FOLDER = Path(__file__).resolve().parent
# ====================================================================================

log = logging.getLogger("run")

CSV_HEADER = ["time_iso", "t_since_pump_start_s", "file", "pump1_setpoint_rpm", "pump2_setpoint_rpm",
              "psu_current_setpoint_A", "voltage_setpoint_V", "edge_wavelength_nm", "concentration_M",
              "conversion", "status", "band_points", "band_span_nm", "max_absorbance"]


class ConnectionLost(Exception):
    pass


def setup_logging(stamp):
    log.setLevel(logging.INFO)
    if log.handlers:
        return
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    fh = logging.FileHandler(OUTPUT_FOLDER / f"run_{stamp}.log", encoding="utf-8")
    sh = logging.StreamHandler()
    for h in (fh, sh):
        h.setFormatter(fmt)
        log.addHandler(h)


def keep_awake(on):
    """Stop Windows from going to sleep while the script runs."""
    if os.name != "nt":
        return
    try:
        import ctypes
        es_continuous, es_system_required = 0x80000000, 0x00000001
        flags = es_continuous | (es_system_required if on else 0)
        ctypes.windll.kernel32.SetThreadExecutionState(flags)
    except Exception as e:                                   # not critical
        log.warning("could not change sleep setting: %r", e)


class Link:
    """OPC UA connection that reconnects by itself after short interruptions."""

    def __init__(self):
        self.client = None
        self.gen = 0
        self._lock = asyncio.Lock()

    async def connect(self):
        client = lv.Client(lv.URL, timeout=60)
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
            if self.gen != gen:                  # another task already reconnected
                return
            try:
                if self.client is not None:
                    await self.client.disconnect()
            except Exception:
                pass
            try:
                await self.connect()
                self.gen += 1
                log.info("OPC connection re-established")
            except Exception as e:
                log.warning("reconnect failed: %r", e)

    async def _call(self, fn, tag, *extra, timeout_s=None):
        deadline = time.monotonic() + (RECONNECT_TIMEOUT_S if timeout_s is None else timeout_s)
        while True:
            gen = self.gen
            try:
                return await fn(self.client, tag, *extra)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("OPC call on %s failed: %r", tag, e)
                if time.monotonic() >= deadline:
                    raise ConnectionLost(f"{tag}: {e!r}") from e
                await asyncio.sleep(RECONNECT_WAIT_S)
                await self._reconnect(gen)

    async def read(self, tag, timeout_s=None):
        return await self._call(lv.read, tag, timeout_s=timeout_s)

    async def write(self, tag, value, timeout_s=None):
        return await self._call(lv.write, tag, value, timeout_s=timeout_s)


async def safe_shutdown(link, reason, normal=False):
    """Power supply off first, then both pumps. Every step is tried."""
    (log.info if normal else log.error)('SHUTDOWN: %s', reason)
    if link.client is None:
        log.error('no OPC connection was ever made, nothing to switch off')
        return False
    complete = True
    for tag, val in ((lv.PSU_ON, 0), (lv.PSU_SET, 0), (lv.PSU_VOLTAGE_SET, 0), (lv.PUMP1_SET, 0), (lv.PUMP2_SET, 0)):
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


async def start_together(link, rpm1, rpm2, current, voltage):
    voltage = validate_voltage(voltage)
    if not 5.0 <= current <= 20.0:
        raise ValueError("Current must stay within 5-20 A")
    # Prepare voltage/current setpoints before PSU enable.
    await link.write(lv.PSU_VOLTAGE_SET, voltage)
    await link.write(lv.PSU_SET, current)
    tasks = [asyncio.create_task(link.write(tag, value)) for tag, value in (
        (lv.PUMP1_SET, rpm1), (lv.PUMP2_SET, rpm2), (lv.PSU_ON, 1))]
    try:
        await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def read_process(link):
    """Pump/current/voltage setpoints (blank if link is down)."""
    out = []
    for tag in (lv.PUMP1_SET, lv.PUMP2_SET, lv.PSU_SET, lv.PSU_VOLTAGE_SET):
        try:
            out.append(round(float(await link.read(tag, timeout_s=10)), 3))
        except ConnectionLost:
            out.append("")
    return out


async def uvvis_loop(link, writer, fh, t_pump1, arrival_ts):
    folder = Path(SPECTRA_FOLDER)
    seen, tries = set(), {}
    last_spec = arrival_ts
    last_warn = 0.0
    last_count = 0.0
    last_status = 0.0
    log.info("UV-Vis: first spectrum is read %.0f s after the cathode pump start",
             arrival_ts - t_pump1)
    while True:
        try:
            now = time.time()
            if now < arrival_ts:
                if now - last_count >= COUNTDOWN_LOG_S:
                    log.info("UV-Vis: waiting for the solution to reach the cuvette, "
                             "%.0f s left", arrival_ts - now)
                    last_count = now
                await asyncio.sleep(min(POLL_S, arrival_ts - now))
                continue

            files = []
            for p in folder.glob(SPECTRA_GLOB):
                try:
                    files.append((p.stat().st_mtime, p))
                except OSError:
                    pass
            if time.time() - last_status >= 60:      # once a minute: what does the folder look like?
                last_status = time.time()
                if files:
                    newest_m, newest = max(files)
                    log.info("UV-Vis: %d file(s) match %s; newest is %s, written %.0f s ago "
                             "(solution arrived %.0f s ago)", len(files), SPECTRA_GLOB,
                             newest.name, time.time() - newest_m, time.time() - arrival_ts)
                else:
                    log.warning("UV-Vis: NO file in %s matches %s - check SPECTRA_FOLDER "
                                "and SPECTRA_GLOB", SPECTRA_FOLDER, SPECTRA_GLOB)
            for mtime, p in sorted(files):
                if p.name in seen:
                    continue
                if mtime < arrival_ts:               # written before the solution arrived
                    seen.add(p.name)
                    continue
                if time.time() - mtime < 0.5:        # may still be being written
                    continue
                wl, ab = uvvis.load_txt(p)
                if len(wl) < 100:
                    tries[p.name] = tries.get(p.name, 0) + 1
                    if tries[p.name] >= 5:
                        log.warning("skipping %s: only %d valid lines", p.name, len(wl))
                        seen.add(p.name)
                    continue
                seen.add(p.name)
                res = uvvis.analyse(wl, ab, FEED_CONC_M)
                proc = await read_process(link)

                def fmt(v, nd):
                    return "" if v is None else round(v, nd)

                writer.writerow([
                    datetime.now().isoformat(timespec="seconds"),
                    round(mtime - t_pump1, 1), p.name, *proc,
                    fmt(res["wavelength_nm"], 2), fmt(res["conc_M"], 5),
                    fmt(res["conversion"], 4), res["status"],
                    res["n_points"], round(res["span_nm"], 2), fmt(res["max_abs"], 3)])
                fh.flush()
                last_spec = time.time()
                log.info("spectrum %s: edge %s nm, c = %s M, %s (highest absorbance %s)",
                         p.name, fmt(res["wavelength_nm"], 2), fmt(res["conc_M"], 4),
                         res["status"], fmt(res["max_abs"], 2))

            if time.time() - last_spec > NO_SPECTRUM_WARN_S and \
                    time.time() - last_warn > NO_SPECTRUM_WARN_S:
                log.warning("UV-Vis: no new spectrum for %.0f min - is OceanView still "
                            "saving?", (time.time() - last_spec) / 60)
                last_warn = time.time()
            await asyncio.sleep(POLL_S)
        except asyncio.CancelledError:
            raise
        except Exception as e:                       # never let the logger stop the run
            log.warning("UV-Vis loop error: %r", e)
            await asyncio.sleep(POLL_S)


async def monitor(link, rpm1, rpm2, t_pump1, max_run_s):
    """Watch duration and report commanded pump RPM; no PSU-state reads."""
    last_beat = 0.0
    while True:
        now = time.time()
        remaining = t_pump1 + max_run_s - now
        if remaining <= 0:
            return "maximum run time / feed limit reached (normal end)", True
        if now - last_beat >= HEARTBEAT_S:
            last_beat = now
            log.info("running %.1f min, %.1f min left | commanded cathode %.2f rpm, anode %.2f rpm",
                     (now - t_pump1) / 60, remaining / 60, rpm1, rpm2)
        await asyncio.sleep(min(MONITOR_S, remaining))


async def run():
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    setup_logging(stamp)
    if VOLTAGE_LIMIT_V is not None or not DRY_RUN:
        validate_voltage(VOLTAGE_LIMIT_V)
    log.info("Voltage limit: %s V", VOLTAGE_LIMIT_V if VOLTAGE_LIMIT_V is not None else "not configured")

    # ---- check all settings before anything is started ----
    rpm1 = lv.flow_to_rpm(FLOW_ML_MIN, "cathode")        # raises if outside calibration
    rpm2 = lv.flow_to_rpm(FLOW_ML_MIN, "anode")
    if not 5.0 <= CURRENT_A <= 20.0:
        raise ValueError("CURRENT_A must be between 5 and 20 A")
    if not DRY_RUN and not Path(SPECTRA_FOLDER).is_dir():
        raise FileNotFoundError(f"SPECTRA_FOLDER does not exist: {SPECTRA_FOLDER}")
    delay_s = CATHODE_VOLUME_ML / FLOW_ML_MIN * 60.0 * DELAY_FACTOR
    limits = [RUN_DURATION_H * 3600.0]
    for vol in (FEED_VOLUME_ML_CATHODE, FEED_VOLUME_ML_ANODE):
        if vol:
            limits.append(vol / FLOW_ML_MIN * 60.0 * FEED_USE_FRACTION)
    max_run_s = min(limits)

    log.info("Run settings: %.2f mL/min -> cathode %.2f rpm, anode %.2f rpm, %.3f A",
             FLOW_ML_MIN, rpm1, rpm2, CURRENT_A)
    log.info("Transport reference (spectra log immediately): %.2f mL / %.2f mL/min x %.2f = %.0f s",
             CATHODE_VOLUME_ML, FLOW_ML_MIN, DELAY_FACTOR, delay_s)
    log.info("Maximum run time: %.2f h%s", max_run_s / 3600.0, "  (DRY RUN)" if DRY_RUN else "")

    if DRY_RUN:
        log.info("DRY RUN: no OPC connection or writes. Immediate spectra logging; transport %.0f s", delay_s)
        return
    lv.DRY_RUN = False
    csv_path = OUTPUT_FOLDER / f"uvvis_{stamp}.csv"
    csv_file = open(csv_path, "w", newline="")
    writer = csv.writer(csv_file)
    writer.writerow(CSV_HEADER)
    csv_file.flush()
    log.info("Data file: %s", csv_path)

    link = Link()
    reason = "finished"
    normal = False
    uv_task = None
    keep_awake(True)
    try:
        await link.connect()
        for tag in (lv.PUMP1_SET, lv.PUMP2_SET, lv.PSU_SET):   # preflight
            log.info("preflight %s = %s", tag, await link.read(tag))

        t_pump1 = time.time()
        if (abs(float(await link.read(lv.PUMP1_SET))) > 0.05
                or abs(float(await link.read(lv.PUMP2_SET))) > 0.05):
            raise RuntimeError("Set both pump commands to zero before starting")
        await start_together(link, rpm1, rpm2, CURRENT_A, VOLTAGE_LIMIT_V)
        arrival_ts = t_pump1   # log immediately, including transport transients
        if not DRY_RUN:
            uv_task = asyncio.create_task(uvvis_loop(link, writer, csv_file, t_pump1, arrival_ts))
        log.info("Pump setpoints written; PSU enable command sent")
        if DRY_RUN:
            reason, normal = "dry run finished", True
            return

        reason, normal = await monitor(link, rpm1, rpm2, t_pump1, max_run_s)
        if not normal:
            log.error(reason)
    except asyncio.CancelledError:
        reason = "interrupted (Ctrl+C)"
        raise
    except Exception as e:
        reason = f"error: {e!r}"
        log.exception("run stopped by an error")
    finally:
        if uv_task is not None:
            uv_task.cancel()
            await asyncio.gather(uv_task, return_exceptions=True)
        await safe_shutdown(link, reason, normal)
        await link.close()
        csv_file.close()
        keep_awake(False)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--voltage-limit", "--voltage", dest="voltage_limit", type=float, default=None, help="PSU voltage ceiling in volts, maximum 25 V")
    args = parser.parse_args()
    DRY_RUN = not args.live
    if args.voltage_limit is not None:
        VOLTAGE_LIMIT_V = args.voltage_limit
    asyncio.run(run())
