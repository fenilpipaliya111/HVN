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
| `bo_loop.py` | scikit-optimize ask/tell loop, flushing, steady measurements, objectives, and CSV history |
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
| Voltage command ceiling | **25 V maximum**; actual voltage follows the load/current regulation |
| First experiment wait | **1.5 volumes = 105 mL** |
| Later experiment flush | **2.5 volumes = 175 mL** |
| First condition of a fresh campaign | **10 mL/min at 5 A** |
| Initial experimental conditions | 6 total: one fixed low-flow condition, then five LHS-generated conditions |
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
SPECTRA_GLOB = "OnlineUVVisHVN*.txt"
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

1. Connect to OPC UA and check that both pumps and the PSU are stopped.
2. Use the fixed low-flow starting condition for a fresh campaign; obtain later flow/current proposals from `Optimizer.ask()`.
3. Convert the shared flow target into separate calibrated cathode and anode RPM commands.
4. Prepare the voltage ceiling (25 V by default) and the requested current setpoint.
5. Submit both pump commands and the PSU-enable command concurrently, with no programmed pause or pump-ramp wait.
6. Analyse and log new spectra immediately, marking flush-period rows as `flushing`.
7. Wait the appropriate transported volume before collecting spectra for the BO result.
8. Accept a measurement when the latest 15 usable post-flush spectra meet the steadiness threshold.
9. Calculate conversion, consumed-vanillin productivity, and the objective.
10. Update the optimiser using `Optimizer.tell()`.
11. Repeat until completion, timeout, interruption, or a fault; then request shutdown.

Concurrent software commands do not guarantee exactly simultaneous physical actuation. The PSU can be energised before measured flow is established. Pump monitoring is a software check, not a hardware interlock.

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

The delay begins after the setpoint commands complete. Spectra are analysed and logged during the delay, but those rows are excluded from the BO measurement.

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
| Cathode pump actual RPM | `PUMP1.X1` |
| Anode pump setpoint | `PUMP2.W1` |
| Anode pump actual RPM | `PUMP2.X1` |
| Power supply enable | `POWERSUPPLY.ON` |
| Current setpoint | `POWERSUPPLY.W2` |
| Voltage setpoint / CC compliance ceiling | `POWERSUPPLY.W1` |
| Actual measured voltage (read only) | `POWERSUPPLY.X1` |

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

`POWERSUPPLY.X1` is **read only**. The supplied HTTP gateway would use `/read?item=...` to obtain its value, not `/write`. This implementation continues using OPC UA with the same tag names.

Normal startup uses the default 25 V ceiling:

```bash
python vanillin_all_in_one.py --live
```

To request a lower ceiling, for example 20 V:

```bash
python vanillin_all_in_one.py --live --voltage-limit 20
```

The same option is available on `bo_loop.py` and `run_all.py`. `--voltage` is accepted as an alias. Zero, negative, non-finite, or above-25 V operating limits are rejected before hardware operation. Shutdown can still write zero volts.

The script reads actual voltage during monitoring and spectrum logging. A sampled reading above 25 V or a non-finite voltage raises an error and requests shutdown. It does not require actual voltage to equal the programmed ceiling. CSV spectra include `voltage_setpoint_V` and `voltage_actual_V`; accepted BO history includes `voltage_setpoint_V` and `voltage_actual_mean_V`, averaged over the accepted measurement window. Readbacks are taken at logging time, not retrospectively at acquisition time.

A voltage ceiling does not guarantee that the requested current can be attained. If the load requires more than 25 V, or another PSU limit applies, current can be below the request. Check the PSU's actual current and CC indication. An actual-current OPC tag has not been confirmed, so charge calculations still use the current **setpoint**, not measured current. Never resume history from a different voltage limit or experimental configuration.

The 25 V check is a software command/readback limit, not a high-speed hardware interlock or guarantee against transient overshoot. Configure the PSU's own voltage/OVP protections for the installation's permitted maximum; the script does not program an OVP threshold because no OVP tag was supplied.

## UV–Vis acquisition and analysis

The instrument software must acquire continuously and save a **new file per spectrum**. Python watches the exported files; it does not trigger the spectrometer.

Expected files:

- Names matching `OnlineUVVisHVN*.txt`.
- Two whitespace-separated columns: wavelength in nm and absorbance.
- 2048 valid points with a strictly increasing wavelength axis after optional reversal.
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

Fresh campaigns begin at `START_FLOW_ML_MIN = 10.0` and `START_CURRENT_A = 5.0`. This fixed condition counts as the first of the six initial observations. After an accepted result, the remaining initial conditions come from the LHS generator, followed by BO-selected conditions. The full calibrated flow range and 5–20 A current range remain available after the first condition.

At 0.5 M feed, this starting condition corresponds to approximately **0.6219 F/mol**. The first 105 mL wait takes **10.5 minutes** at 10 mL/min, followed by collection of steady spectra. Subsequent conditions use the existing 2.5-volume flush.

The first condition is repeated if it fails spectral acceptance and no usable observation has been collected, up to the existing failure limit. Resuming a non-empty history continues the optimiser rather than forcing another low-flow initial condition.

Edit the `START_FLOW_ML_MIN` and `START_CURRENT_A` settings in the chosen controller to change this first condition; they must stay within the configured flow and current bounds. These settings do not restrict later BO proposals to low flows.

The physical experiment runs between proposing a condition and telling the optimiser its result:

```python
flow, current = next_condition(optimizer)

# Operate equipment, wait for transport, collect and validate spectra.
score = measured_experimental_score

optimizer.tell([flow, current], -score)
```

**scikit-optimize minimises**, so the maximisation score is negated before `tell()`. CSV history retains the positive experimental score, and the reported best condition reverses the sign again.

Only accepted steady measurements are supplied to the optimiser. Failed measurements are not assigned an invented score. Without a new observation, the optimiser may propose the same condition again.

Reference: [scikit-optimize Bayesian optimisation example](https://scikit-optimize.github.io/stable/auto_examples/bayesian-optimization.html).

## Output files

Files are saved beside the Python script:

| File | Contents |
| --- | --- |
| `bo_history_<timestamp>.csv` | Accepted conditions, flow, current, charge, conversion, productivity, objective, voltage ceiling, and mean measured voltage |
| `bo_spectra_<timestamp>.csv` | Per-spectrum analysis and set/measured voltage, including `flushing` and `measurement` rows |
| `run_<timestamp>.log` | Proposed conditions, operating events, warnings, and shutdown results |

## Monitoring and limits

- A pump deviation greater than 0.3 rpm lasting 30 seconds triggers a fault.
- An unexpectedly disabled PSU triggers a fault.
- Non-finite actual voltage or a sampled reading above 25 V requests shutdown.
- Conditions without sufficient steady usable spectra within the measurement window are rejected.
- Three consecutive failed conditions stop the campaign.
- The campaign deadline requests shutdown even during flushing; communication and cleanup can take additional time.
- On Windows, the script requests that the computer remain awake during the campaign.

## Validation status

The scikit-optimize version passed **12 offline checks**, including calibration conversions, real GP/EI proposals inside bounds, objective-sign handling, simulated experiment timing, transient logging, shutdown paths, and standalone command-line operation.

The voltage extension was also checked offline for voltage/current write ordering, concurrent enable, read-only X1 handling, telemetry columns, voltage reset on shutdown, 25 V command/readback limits, and CLI behaviour. These checks do not validate the site's OPC tag mapping or physical voltage regulation.

**Physical pump, PSU, and spectrometer operation has not been validated by these offline checks.**
