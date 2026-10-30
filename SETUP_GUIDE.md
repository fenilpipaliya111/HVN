# Simple guide: use this setup for automatic optimisation

This program repeats a laboratory experiment automatically:

**Choose flow and current → run pumps and PSU → flush → read 15 spectra → calculate conversion → choose the next experiment.**

Bayesian optimisation (BO) uses the accepted results to choose later conditions. The current objective rewards vanillin conversion and the rate of vanillin consumption. It does not independently measure hydrovanilloin yield.

## 1. Choose your Python file

For the simplest setup, download **one standalone version** from the [version table in the README](README.md#choose-a-standalone-version). Select the correct feed concentration and flow range.

For example, vanillin_10_100rpm_0p1M.py uses 0.1 M feed and limits both pumps to 10–100 RPM.

Each standalone file contains its own settings and calibrations. **Edit the file you will actually run.** Editing uvvis.py or lv.py does not change a standalone file. Changes are not automatically copied to other versions.

## 2. Install the packages

Install Python 3.10+ and download requirements.txt. In a terminal opened in your experiment folder:

~~~powershell
python -m pip install -r requirements.txt
~~~

Use the same Python interpreter for installation and running. See the [README commands](README.md#start-preview-inspect-spectra-and-stop) for the lab PC's full Windows paths.

## How Python communicates with LabVision

This setup uses **OPC Expert as the bridge between Python and the LabVision control system**.

~~~text
Python controller (asyncua OPC UA client)
        |
        | OPC UA reads/writes
        v
OPC Expert bridge / OPC UA endpoint
        |
        | configured OPC connection to LabVision's instrument tags
        v
LabVision control system
        |
        v
Pumps and power supply
~~~

Python uses the **asyncua** package to connect to the OPC UA endpoint exposed for this setup:

~~~text
opc.tcp://localhost:57888
~~~

The configured item prefix is:

~~~text
Hitec_OPC_DA20_Server->HVN:
~~~

For example, the full cathode pump setpoint NodeId is:

~~~text
ns=3;s=Hitec_OPC_DA20_Server->HVN:PUMP1.W1
~~~

Python writes pump RPM, current, voltage-limit and enable/disable commands to these OPC nodes; the configured OPC Expert/LabVision connection passes the commands to the equipment. Python is not directly controlling the pumps through a serial cable or the PSU through a vendor driver in this implementation.

In OPC Expert, the LabVision OPC connection and the required items must be available to the OPC UA endpoint used by Python. The item names retain the source server name Hitec_OPC_DA20_Server. Use the actual bridge/source configuration on your own installation; the scripts do not create or configure OPC Expert or LabVision.

**localhost means the computer running Python.** If OPC Expert's endpoint is on another PC, set OPC_URL (or URL in lv.py) to that PC's reachable OPC UA endpoint. Your own namespace index, prefix and item names may differ; browse them in OPC Expert and update node() and the tag settings accordingly. The existing code uses namespace index 3 and a connection without explicit username/certificate configuration; a server requiring authentication/security needs matching client configuration.

The communication path and the analysis path are separate: **UV–Vis spectra are read from instrument-exported TXT files**, not streamed through these OPC tags.

## 3. Connect your own equipment

The current controller uses **OPC UA**. It is not a universal driver for every pump, PSU or spectrometer.

In your standalone file, find and edit:

| Setting | What to enter |
|---|---|
| OPC_URL | Your OPC UA server address |
| PREFIX | Your instrument tag prefix |
| node() | Your namespace/node-address format if different |
| PUMP1_SET / PUMP2_SET | Cathode/anode pump RPM setpoint tags |
| PSU_SET | Current setpoint tag |
| PSU_VOLTAGE_SET | Voltage ceiling setpoint tag |
| PSU_ON | PSU enable/disable tag and correct command values |

The existing settings expect RPM, amperes and volts. If your server uses scaled values, different units or a different protocol, the control functions must be adapted too.

The program writes commands but does not verify actual pump speed, actual voltage or PSU on/off state. Do not treat a written setpoint as a measured physical value.

## 4. Where to edit calibration

| Workflow | Pump calibration location | UV–Vis calibration location |
|---|---|---|
| One standalone vanillin_*.py file | Same file: ANODE_K, PUMP_CAL, flow_to_rpm(), rpm_to_flow() | Same file: UV-VIS SETTINGS, conc_from_wavelength() |
| Modular bo_loop*.py | lv.py: ANODE_K, CAL, flow_to_rpm(), rpm_to_flow() | uvvis.py: settings and conc_from_wavelength() |
| Fixed-condition run_all.py | lv.py | uvvis.py |

Pump and UV–Vis calibration changes must be made in every standalone edition you intend to use. Keep the calibration measurements and a dated copy of the edited code.

### Pump calibration

Measure delivered volume over a known time at several RPM values using the intended tubing and operating liquid:

~~~text
flow_mL_min = collected_volume_mL / collection_time_min
~~~

The current model is a straight line through zero:

~~~text
flow_mL_min = K × RPM
RPM = flow_mL_min / K
~~~

Both pumps currently share the anode coefficient:

~~~python
ANODE_K = 0.5071118969420833
~~~

Replace that number with your fitted slope **only if the measured data support this model**. The number is in mL/min per RPM, not mL per revolution. The name ANODE_K is historical; the current code uses it for both pumps.

Also update:

- **PUMP_CAL** in a standalone file, or **CAL** in lv.py: the measured RPM limits for each pump.
- **Q_MIN / Q_MAX** in the BO controller: the desired search limits in mL/min. These are intersected with the calibrated pump limits.
- Full-flow editions use Q_MIN = None and Q_MAX = None, so their flow limits follow the pump calibration automatically.

If the two pumps have different measured slopes, use a separate coefficient for each pump in **both** flow_to_rpm() and rpm_to_flow(). If there is an offset or a curved response, changing ANODE_K alone is insufficient: update both functions to implement the fitted model and its inverse. Keep commands inside the measured range.

After any calibration change, preview the search range and check a known flow against its expected RPM before running the campaign.

### UV–Vis calibration

The default method finds the wavelength where the falling spectral edge crosses absorbance **0.75**. It does not use absorbance at one fixed wavelength.

The existing model is:

~~~text
edge_nm = CAL_A0 + CAL_B × log10(concentration_M)
concentration_M = 10 ** ((edge_nm - CAL_A0) / CAL_B)
~~~

To replace it:

1. Prepare several known concentration standards spanning the intended range, using the same electrolyte, optical path and sample preparation as the experiment.
2. Export their spectra and obtain edge wavelengths using the same edge-analysis settings as the controller.
3. Fit edge wavelength against log10(concentration in mol/L).
4. Enter the fitted intercept as CAL_A0 and slope as CAL_B.
5. Update CAL_LOW_NM and CAL_HIGH_NM to reflect the validated edge range.
6. Check the resulting concentrations with independent standards.

Current coefficients, for reference:

~~~python
CAL_A0 = 430.03839
CAL_B = 19.12081
CAL_LOW_NM = 399.0
CAL_HIGH_NM = 431.0
~~~

Additional settings:

| Setting | Purpose |
|---|---|
| LEVEL | Absorbance crossing level, currently 0.75 |
| WL_MIN_NM / WL_MAX_NM | Wavelength interval to analyse |
| SMOOTH_POINTS | Smoothing before finding the crossing |
| FLOOR_NM | Lower wavelength boundary used by the analysis |
| C_FLOOR_M | Concentration floor used in BO conversion calculations |
| EXPECTED_SPECTRUM_POINTS | Required valid spectrum rows in BO files, currently 2048 |

C_FLOOR_M and EXPECTED_SPECTRUM_POINTS belong to the selected BO file, including for modular BO. They are not in uvvis.py.

Changing the crossing level or analysis method requires validating/refitting the calibration. Changing FEED_CONC_M alone does not recalibrate the instrument. Changing only the coefficients is insufficient if your instrument requires a different mathematical model: update conc_from_wavelength() and validate the analysis.

The default accepts extrapolated concentrations below the calibration standards. Treat those as estimates; the software does not independently validate their accuracy. If samples are diluted before measurement, the current code has no separate dilution-factor setting: its reported concentration must be converted to the reactor-outlet basis before conversion/productivity are calculated.

## 5. Enter your experiment settings

Edit these in the selected standalone BO file, or selected bo_loop*.py for modular BO:

| Setting | Meaning |
|---|---|
| FEED_CONC_M | Starting vanillin concentration in mol/L |
| NAOH_CONC_M | Fixed electrolyte concentration recorded in the setup settings |
| I_MIN / I_MAX | Current search bounds; current implementation enforces 5–20 A |
| VOLTAGE_LIMIT_V | Voltage ceiling; current implementation enforces at most 25 V |
| ELECTRODE_AREA_CM2 | Area used for current density/productivity per area |
| CATHODE_VOLUME_ML | Liquid transport volume used for flushing; default 70 mL |
| FIRST_EXPERIMENT_VOLUMES | First wait, default 1.5 volumes |
| LATER_EXPERIMENT_VOLUMES | Later flush, default 2.5 volumes |
| SPECTRA_FOLDER / SPECTRA_GLOB | Instrument export folder and matching filenames |
| MEASURE_SPECTRA | Number of usable post-flush spectra, default 15 |
| MAX_MEASURE_S | Maximum collection time after flushing, default 600 s |
| N_INITIAL / N_ITER | Initial-design and later BO attempts |
| MAX_TOTAL_H | Campaign duration limit |

The current code deliberately enforces its 5–20 A and 25 V limits in validation functions too. Editing the search constants alone cannot expand those hardware limits.

For a different reaction, also adapt the concentration model, objective, stoichiometry and efficiency calculation. The hydrovanilloin electron assumption is not universal.

## 6. Start the spectrum export

Set the spectrometer software to acquire and save a **new two-column TXT file per spectrum**:

~~~text
wavelength_nm    absorbance
~~~

Example settings:

~~~python
SPECTRA_FOLDER = r"C:\YourLab\UV VIS"
SPECTRA_GLOB = "*OnlineUVVisHVN*.txt"
~~~

Use your actual folder and filename pattern. BO expects 2048 valid rows by default, finite values and a strictly increasing wavelength axis after reversal if needed. Files written before flushing finishes are ignored. The instrument may acquire during flushing, but Python waits before reading.

Python does not start the spectrometer acquisition.

## 7. Preview, check and run

Replace the example filename with the one you edited:

~~~powershell
# Show search bounds and first proposal; equipment stays uncommanded.
python vanillin_10_100rpm_0p1M.py

# Read the newest spectrum without operating equipment.
python vanillin_10_100rpm_0p1M.py --check-spectra

# Start automatic optimisation.
python vanillin_10_100rpm_0p1M.py --live
~~~

During a campaign:

- **S** in the Windows terminal skips the active experiment. On other systems use S then Enter.
- **Ctrl+C** ends the campaign and requests pump/PSU shutdown.
- The first condition waits 1.5 volumes; subsequent conditions flush 2.5 volumes.
- After flushing, the first 15 usable spectra complete the measurement. There is no steadiness wait.
- Skipped results do not train BO; the controller selects a different condition.

Only one controller should run at a time. After the controller has exited, a separate shutdown request is available:

~~~powershell
python vanillin_10_100rpm_0p1M.py --stop
~~~

## 8. Find the results

CSV and log files are saved beside the script:

- **...bo_spectra_...csv**: individual post-flush spectra and calculated concentrations.
- **...bo_history_...csv**: accepted experiment averages, charge, productivity, objective and apparent FE.
- **...run_...log**: progress, skips, errors and shutdown messages.

Open CSV files in Excel. The controller does not directly create XLSX workbooks. History may appear empty until the first accepted result.

The efficiency column is **apparent_hvn_FE_percent = 100 × conversion / charge_F_mol**. Hydrovanilloin requires two electrons per dimer, equivalent to one per vanillin consumed. This assumes 100% product selectivity and uses commanded flow/current. It is not independently measured product Faradaic efficiency. Values above 100% are flagged, not hidden.

For detailed version differences, objective equations, diagnostics and dependency installation, see the [README](README.md).
