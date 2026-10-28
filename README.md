# HVN — Vanillin Electrolysis Bayesian Optimisation

A Python controller, available in single-file and modular editions, for controlling two pumps and a power supply, analysing inline UV–Vis spectra, and optimising continuous-flow vanillin electrolysis with **scikit-optimize**.

The program uses `skopt.Optimizer` with a Gaussian-process model and Expected Improvement. It searches flow and current within the calibrated pump and power-supply ranges, then derives the applied charge.

The main quick-start commands below use `vanillin_all_in_one.py`, which contains all control and analysis code. The modular edition provides the same BO workflow through separate files. The calibration workbook is not required at runtime in either edition.

## Separate-file edition

Keep these files together in the same folder:

| File | Purpose |
| --- | --- |
| `lv.py` | OPC UA tags, pump calibration, flow/RPM conversion, and low-level reads/writes |
| `uvvis.py` | Spectrum parsing, A = 0.75 edge analysis, and concentration calibration |
| `run_all.py` | Shared connection/start/shutdown helpers and an optional fixed-condition runner |
| `bo_loop.py` | scikit-optimize ask/tell loop, flushing, 15-spectrum measurements, objectives, and CSV history |
| `stop.py` | Manual shutdown entry point |
| `requirements.txt` | Dependencies for either edition |

Install dependencies:

```bash
python -m pip install -r requirements.txt
```

Preview or run modular Bayesian optimisation:

```bash
python bo_loop.py
python bo_loop.py --live
```

Request shutdown:

```bash
python bo_loop.py --stop
```

Or use:

```bash
python stop.py
```

For a fixed condition without BO, edit `FLOW_ML_MIN` and `CURRENT_A` in `run_all.py`, then preview or operate:

```bash
python run_all.py
python run_all.py --live
```

For the modular BO workflow, edit chemistry, flow/current bounds, spectra folder, and the **1.5-volume first / 2.5-volume later** timing in `bo_loop.py`; edit OPC configuration and pump calibration in `lv.py`; edit UV–Vis calibration in `uvvis.py`. The optional fixed-condition runner has its own operating settings in `run_all.py`.

The modular BO edition uses **0.5 M feed**, **70 mL dead volume**, **5–20 A**, and the same scikit-optimize configuration and objective-sign convention as the single-file edition. It needs no import of `vanillin_all_in_one.py`.

Choose one controller entry point at a time. Do not run the single-file controller, modular BO controller, or fixed-condition runner concurrently. A manual stop request does not terminate an independently running controller process.

## Current experiment settings

| Setting | Value |
| --- | --- |
| Starting/feed vanillin concentration | **0.5 mol/L** |
| NaOH concentration | 1.5 mol/L |
| Electrode area | 100 cm² |
| Cathode-side dead volume | **70 mL** |
| Shared pump flow range | Approximately **2.8667–98.4733 mL/min** |
| Applied current range | **5–20 A** |
| Power supply | EA PSI 9040-40 DT |
| Voltage command ceiling | **25 V maximum**; setpoint-only control |
| First experiment wait | **1.5 volumes = 105 mL** |
| Later experiment flush | **2.5 volumes = 175 mL** |
| First condition of a fresh campaign | Chosen by scikit-optimize from the full configured flow/current range |
| Initial experimental conditions | 6, using Latin hypercube sampling over the full configured range |
| Subsequent BO conditions | 14 |
| Spectra per accepted measurement | 15 usable post-flush spectra |
| Steadiness threshold | Conversion standard deviation ≤ 0.02 |
| Measurement timeout | 600 seconds after flushing |
| Consecutive failed conditions before stopping | 3 |
| Campaign time limit | 14 hours; shutdown/communication may take additional time |

The feed concentration and dead volume are experiment-specific settings. Recheck the settings when the chemistry or physical setup changes.

## Requirements

- Python **3.10 or newer**.
- Access to the configured OPC UA server from the laboratory computer.
- Pump and power-supply tags matching the actual setup.
- Spectrometer software continuously acquiring and saving individual spectra.
- A writable folder for CSV results and event logs.

Install the Python dependencies:

```bash
python -m pip install asyncua numpy "scikit-optimize>=0.10.2,<0.11"
```

scikit-optimize installs its scientific dependencies, including SciPy and scikit-learn.

## Quick start

### 1. Configure the script

Review these settings near the top of `vanillin_all_in_one.py`:

```python
FEED_CONC_M = 0.5
CATHODE_VOLUME_ML = 70.0

FIRST_EXPERIMENT_VOLUMES = 1.5
LATER_EXPERIMENT_VOLUMES = 2.5

I_MIN, I_MAX = 5.0, 20.0
VOLTAGE_LIMIT_V = 25.0

OPC_URL = "opc.tcp://localhost:57888"
SPECTRA_FOLDER = r"C:\path\to\UV VIS"
SPECTRA_GLOB = "*OnlineUVVisHVN*.txt"
```

Set `SPECTRA_FOLDER` to the actual export folder. The example path above is a placeholder. Confirm the feed concentration, electrode area, and remaining chemistry settings against the experiment.

### 2. Preview the plan

```bash
python vanillin_all_in_one.py
```

The default mode prints the search limits and first proposed condition. It does **not** connect to OPC UA or operate the hardware, and the spectra folder need not exist for this preview.

### 3. Run the experiment

Start from stopped pumps and a switched-off power supply:

```bash
python vanillin_all_in_one.py --live
```

The script proposes conditions, operates the setup, collects spectra, updates the optimiser, and saves results automatically.

Run only one controlling instance at a time.

### 4. Stop the equipment

Press **Ctrl+C** in the running process to request shutdown.

To request a separate shutdown using the same file:

```bash
python vanillin_all_in_one.py --stop
```

A separate stop command does not terminate another running BO process. Stop the active campaign as well so it cannot issue further operating commands.

Shutdown attempts, in order:

1. Power supply OFF.
2. Current setpoint zero.
3. Voltage setpoint zero.
4. Cathode pump setpoint zero.
5. Anode pump setpoint zero.

Every step is attempted even if an earlier shutdown write fails. Inspect the logs and equipment for confirmation; loss of communication can prevent remote shutdown.

## Startup and experiment sequence

1. Connect to OPC UA and check that both pump setpoints are zero. Actual pump motion and PSU on/off status are not read.
2. Obtain each flow/current proposal, including the first condition, from `Optimizer.ask()`.
3. Convert the shared flow target into separate calibrated cathode and anode RPM commands.
4. Prepare the voltage ceiling (25 V by default) and the requested current setpoint.
5. Submit both pump commands and the PSU-enable command concurrently, with no programmed pause or pump-ramp wait.
6. Pause spectrum reading during flushing; display a countdown.
7. Wait the appropriate transported volume before collecting spectra for the BO result.
8. Accept a measurement as soon as the first 15 usable post-flush spectra have been collected, regardless of their variation.
9. Calculate conversion, consumed-vanillin productivity, and the objective.
10. Update the optimiser using `Optimizer.tell()`.
11. Repeat until completion, timeout, interruption, or a fault; then request shutdown.

Concurrent software commands do not guarantee exactly simultaneous physical actuation. The PSU can be energised before measured flow is established. The controller does not read actual pump speed or verify physical flow.

## Transport and flushing

The delay is calculated using the **new experiment's flow**:

```text
delay_minutes = number_of_volumes × 70 / flow_mL_min
```

| Flow | First experiment: 1.5 volumes | Later experiments: 2.5 volumes |
| --- | --- | --- |
| 5 mL/min | 21 minutes | 35 minutes |
| 10 mL/min | 10.5 minutes | 17.5 minutes |
| 20 mL/min | 5.25 minutes | 8.75 minutes |

The delay begins after the setpoint commands complete. Python does not read or analyse spectrum files during flushing. After flushing, it ignores files timestamped before the flush completed and collects 15 new usable spectra.

The first condition after each campaign startup uses 1.5 volumes, including when resuming a saved history. Subsequent conditions use 2.5 volumes. A condition that started successfully but failed spectral acceptance still counts as a started condition, so the following condition uses 2.5 volumes.

## Pump calibration

Both pumps receive the same requested flow but generally require different RPM.

### Anode

The new calibration uses:

```text
flow_mL_min = 0.5071118969420833 × rpm
rpm = requested_flow / 0.5071118969420833
```

Measured RPM range: **5–300 rpm**.

### Cathode

The script uses piecewise linear interpolation between these measured mean flows:

| RPM | Flow, mL/min |
| --- | --- |
| 10 | 2.8666667 |
| 50 | 14.3733333 |
| 100 | 26.0733333 |
| 150 | 44.2066667 |
| 200 | 60.6766667 |
| 250 | 77.5700000 |
| 300 | 98.4733333 |

Measured RPM range: **10–300 rpm**.

The shared flow search range is the overlap of both pump calibrations, approximately **2.8667–98.4733 mL/min**. The optimiser does not extrapolate below the measured minimum RPM.

Fractional RPM commands are retained. If an OPC node accepts only integers, the script rejects fractional commands rather than silently truncating them. Configure command resolution before operating such hardware.

## OPC UA configuration

Default endpoint:

```text
opc.tcp://localhost:57888
```

Node format:

```text
ns=3;s=Hitec_OPC_DA20_Server->HVN:<tag>
```

| Function | Tag |
| --- | --- |
| Cathode pump setpoint | `PUMP1.W1` |
| Anode pump setpoint | `PUMP2.W1` |
| Power supply enable/disable command (written, not read) | `POWERSUPPLY.ON` |
| Current setpoint | `POWERSUPPLY.W2` |
| Voltage setpoint / CC compliance ceiling | `POWERSUPPLY.W1` |

The script checks OPC read quality and uses each node's reported data type for writes. It attempts reconnection after communication failures.

`POWERSUPPLY.W2` is the existing **current setpoint** tag; it is not an independently verified measured-current signal.

## Automatic voltage regulation with a 25 V ceiling

The configured supply is an **EA PSI 9040-40 DT**. The [EA PSI 9000 DT operating guide, section 3.2.2](https://elektroautomatik.com/shop/media/pdf/c5/d1/7d/06200500_EN.pdf#page=40) describes automatic current regulation. The supply transitions into CC operation when the load reaches the programmed current limit; the required voltage depends on the cell.

The controller therefore uses:

```python
MAX_VOLTAGE_V = 25.0
VOLTAGE_LIMIT_V = 25.0
```

Before enabling the output, it writes the voltage ceiling to `POWERSUPPLY.W1` and BO's requested current to `POWERSUPPLY.W2`. The voltage is not another BO search variable. The PSU performs the regulation; Python does not guess voltage from current or run a voltage-ramping feedback loop.

Power-supply voltage control uses only the `POWERSUPPLY.W1` setpoint. The script neither reads nor writes `POWERSUPPLY.X1`. This implementation continues using OPC UA with the same setpoint tags. Pump `PUMP1.X1` and `PUMP2.X1` are also unused. Fixed-run CSV columns explicitly identify commanded RPM as `pump1_setpoint_rpm` and `pump2_setpoint_rpm`.

Normal startup uses the default 25 V ceiling:

```bash
python vanillin_all_in_one.py --live
```

To request a lower ceiling, for example 20 V:

```bash
python vanillin_all_in_one.py --live --voltage-limit 20
```

The same option is available on `bo_loop.py` and `run_all.py`. `--voltage` is accepted as an alias. Zero, negative, non-finite, or above-25 V operating limits are rejected before hardware operation. Shutdown can still write zero volts.

CSV spectra and accepted BO history retain `voltage_setpoint_V`. Actual-voltage reads, measured-voltage columns, voltage averages, and voltage-readback shutdown checks have been removed. Pump actual-value reads and speed-tolerance checks are also removed. The script checks pump setpoints during preflight. PSU on/off status reads and checks are also removed, including the fixed-run status CSV column. Enable and disable commands are still written at startup and shutdown.

A voltage ceiling does not guarantee that the requested current can be attained. If the load requires more than 25 V, or another PSU limit applies, current can be below the request. Check the PSU's actual current and CC indication. An actual-current OPC tag has not been confirmed, so charge calculations still use the current **setpoint**, not measured current. Never resume history from a different voltage limit or experimental configuration.

The 25 V check limits commanded setpoints only. The script does not measure voltage or detect physical voltage excursions. Configure the PSU's own voltage/OVP protections for the installation's permitted maximum; the script does not program an OVP threshold because no OVP tag was supplied.

## UV–Vis acquisition and analysis

The instrument software must acquire continuously and save a **new file per spectrum**. Python watches the exported files; it does not trigger the spectrometer.

Expected files:

- Names matching `*OnlineUVVisHVN*.txt`.
- Two whitespace-separated columns: wavelength in nm and absorbance; UTF-8 text with or without a BOM is supported.
- 2048 valid points by default (`EXPECTED_SPECTRUM_POINTS`), with a strictly increasing wavelength axis after optional reversal.
- Finite numerical values.

Previously present files are ignored. Incomplete or invalid files are retried. File modification time is used to assign spectra to conditions, so export should be prompt and timestamps must be consistent with the controlling computer.

The existing UV–Vis method is retained:

- Default method: interpolated falling-edge crossing at **absorbance 0.75**.
- Analysis range: **357–435 nm**.
- Smoothing: five-point moving average.
- Concentration calibration:

```text
concentration_M = 10 ** ((edge_wavelength_nm - 430.03839) / 19.12081)
```

An optional band-fit method is available through `METHOD = "band"`.

Spectra are labelled `calibrated`, `extrapolated`, `too_concentrated`, `no_edge`, or other diagnostic statuses. Missing, unresolved, or saturated spectra are not assumed to represent complete conversion.

The current implementation permits usable extrapolated concentrations in BO and applies a 0.0005 M concentration floor for conversion calculations. Extrapolated values remain estimates outside the standards. In particular, **95% conversion from 0.5 M corresponds to 0.025 M**, near the lower end of the stated calibration standards. Lower outlet concentrations may require extrapolation and additional calibration for quantitative optimisation.

## Flow, current, and applied charge

At fixed feed concentration, flow, current, and applied charge are linked:

```text
charge_F_per_mol =
    current_A × 60000
    / (96485.33 × feed_concentration_M × flow_mL_min)
```

The optimiser searches **flow and current**, covering the feasible flow/charge combinations within the pump and 5–20 A limits. There is no separate 1–2 F/mol restriction.

## Objective

Conversion:

```text
X = clip(1 - outlet_concentration / feed_concentration, 0, 1)
```

Flow and current in the calculations below are commanded setpoints; actual delivery is not verified by the script.

Consumed-vanillin productivity:

```text
P_mmol_per_hour = X × feed_concentration_M × flow_mL_min × 60
```

Default weighted score:

```text
score = 0.5 × X + 0.5 × P / P_reference

P_reference = feed_concentration_M × maximum_shared_flow × 60
```

The weights are editable through `W_CONVERSION` and `W_PRODUCTIVITY`. This balances concentration reduction and throughput; it does not impose a guaranteed conversion target.

Setting `OBJECTIVE = "constrained"` selects a **soft penalty** for conversion below `CONVERSION_TARGET = 0.95`. It is not a hard feasibility constraint.

Vanillin disappearance alone does not establish product selectivity, product yield, or faradaic efficiency. The faradaic-efficiency column is intentionally blank.

## Bayesian optimisation implementation

The script uses:

```python
Optimizer(
    dimensions=[
        Real(q_lo, q_hi, name="flow_mL_min"),
        Real(I_MIN, I_MAX, name="current_A"),
    ],
    base_estimator="GP",
    n_initial_points=6,
    initial_point_generator="lhs",
    acq_func="EI",
    acq_optimizer="sampling",
    acq_func_kwargs={"xi": 0.01},
    acq_optimizer_kwargs={"n_points": 20000},
    random_state=1,
)
```

Every proposed condition, including the first, comes from `Optimizer.ask()`. The first six accepted observations use the LHS initial design over the full calibrated flow/current range, followed by BO-selected conditions. There is no fixed 10 mL/min or 5 A first condition. The random seed makes the initial design reproducible.

The 0.5 M feed, 25 V voltage ceiling, 1.5-volume first wait, and 2.5-volume later flush remain unchanged. Delays are calculated from each proposed flow. A resumed history continues through the same ask/tell interface.

The physical experiment runs between proposing a condition and telling the optimiser its result:

```python
flow, current = optimizer.ask()

# Operate equipment, wait for transport, collect and validate spectra.
score = measured_experimental_score

optimizer.tell([flow, current], -score)
```

**scikit-optimize minimises**, so the maximisation score is negated before `tell()`. CSV history retains the positive experimental score, and the reported best condition reverses the sign again.

Measurements containing 15 usable post-flush spectra are supplied to the optimiser; steadiness is not required. Failed measurements are not assigned an invented score. Without a new observation, the optimiser may propose the same condition again.

Reference: [scikit-optimize Bayesian optimisation example](https://scikit-optimize.github.io/stable/auto_examples/bayesian-optimization.html).

## Diagnosing waiting after flushing

Completing the flush starts the measurement window; it does not immediately produce a BO result. The controller needs the first **15 usable post-flush spectra**, then averages their conversions and proceeds immediately. Conversion standard deviation is logged for information only; it does not delay acceptance. The legacy `steady` CSV column is blank because steadiness is not assessed. It waits up to **600 seconds after flushing**, then rejects an unsuccessful condition.

The terminal now reports:

- The configured spectra folder and filename pattern.
- A flushing countdown approximately every 15 seconds (`PROGRESS_LOG_S`), followed by file-scan summaries during measurement.
- An explicit **Flush complete** message.
- Each new spectrum's filename, phase, analysis status, concentration, conversion, and usable post-flush count.
- Measurement progress, conversion standard deviation, remaining time, and file-scan counts.
- Invalid/incomplete-file reasons, retried without weakening data checks.

The default pattern is now `*OnlineUVVisHVN*.txt`. It matches both `OnlineUVVisHVN_Absorbance.txt` and timestamp-prefixed exports such as `1791544248160_OnlineUVVisHVN_Absorbance.txt`. The actual export must be a supported text spectrum; its prefix alone does not establish its format.

### Check exports without starting hardware

```bash
python vanillin_all_in_one.py --check-spectra
```

For the modular edition:

```bash
python bo_loop.py --check-spectra
```

This mode does not connect to OPC or write hardware commands. It prints the configured folder/pattern, matching file count, the newest eligible candidate's name and age, and its analysis result. It inspects the newest matching file at least 0.5 seconds old when available, so a brand-new file still being saved does not hide an older completed export. The normal point-count, finite-data and wavelength-axis checks remain in force. No usable export returns a nonzero exit code and diagnostic messages.

If no filenames match, diagnostics show example filenames from the folder, including other extensions such as `.rmn`. The text-spectrum reader does not parse RMN/JSON files.

| Message | Meaning / next check |
| --- | --- |
| `matched=0` | Check the folder, complete filename, extension, and whether text export is enabled. |
| `seen_or_ignored` increases but no new result | Existing files are already processed or excluded. Acquisition must save a distinct new filename per spectrum. |
| `writing` | The file is less than 0.5 seconds old; it will be retried. |
| `invalid` or `unreadable` | Read the accompanying reason; check point count, file completion, numeric values, and axis. |
| `status=no_edge`, `no_clear_edge`, or `no_data` | Files are found but do not provide a usable concentration with the current analysis. Check the spectrum and calibration. |
| Usable count below 15 | More accepted post-flush spectra are required. Check the export interval. |
| Count reaches 15 | The measurement is accepted regardless of standard deviation; BO selects the next condition. |

If acquisitions are slow, ensure `MAX_MEASURE_S` allows enough time to collect the required spectra. Do not treat missing or invalid spectra as complete conversion.

Edit the file actually being executed: running `vanillin_all_in_one.py` does not use operating settings from an open `run_all.py` editor.

## Output files

Files are saved beside the Python script:

| File | Contents |
| --- | --- |
| `bo_history_<timestamp>.csv` | Accepted conditions, flow, current, charge, conversion, productivity, objective, and voltage setpoint |
| `bo_spectra_<timestamp>.csv` | Post-flush per-spectrum analysis and voltage setpoint; no flush-period rows |
| `run_<timestamp>.log` | Proposed conditions, operating events, warnings, and shutdown results |

## Monitoring and limits

- Pump operation is controlled by setpoints only; actual RPM is not read and there is no speed-tolerance shutdown.
- PSU on/off status is not read or monitored. The controller still sends enable/disable commands, but does not verify the physical output state.
- Conditions without 15 usable post-flush spectra within the measurement window are rejected.
- Three consecutive failed conditions stop the campaign.
- The campaign deadline requests shutdown even during flushing; communication and cleanup can take additional time.
- On Windows, the script requests that the computer remain awake during the campaign.

## Validation status

The scikit-optimize version passed **12 offline checks**, including calibration conversions, real GP/EI proposals inside bounds, objective-sign handling, simulated experiment timing, transient logging, shutdown paths, and standalone command-line operation.

The setpoint-only voltage implementation is checked offline for voltage/current write ordering, concurrent enable, absence of pump/voltage X1 access and PSU status reads, setpoint-only CSV columns, voltage reset on shutdown, 25 V command limits, and CLI behaviour. These checks do not validate the site's OPC tag mapping, physical pump motion, or voltage regulation.

The export/progress update was tested with timestamp-prefixed and plain filenames, UTF-8 BOM input, incomplete files and retry, absent exports, unusable edges, nonsteady measurements, flush/measurement transitions, and hardware-free export inspection. Previously removed pump/voltage/PSU-status feedback reads remain removed.

**Physical pump, PSU, and spectrometer operation has not been validated by these offline checks.**
