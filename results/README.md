# Experimental result files

These files were uploaded by the experimenter and are preserved byte-for-byte. The CSV filename was simplified by removing the trailing copy marker ` 1`; its contents are unchanged.

- `rpm10_100_0p1M_bo_history_20261028_124154.csv`: 8 accepted experiments, 0.1 M feed, 10–100 RPM campaign.
- `full_flow_0p1M_bo_history_20261028_103346.csv.xlsx`: Excel workbook with 20 accepted experiments, 0.1 M feed, full-flow campaign.

The `.csv.xlsx` file is an actual Excel workbook despite its double extension. Both datasets contain historical columns from before apparent FE logging was added. Conversion is estimated from UV–Vis; productivity is vanillin consumed. Recorded current/flow are commanded values. Product hydrovanilloin yield, selectivity and measured product FE are not established by these histories.

For hydrovanilloin, conversion-based apparent FE (%) can be estimated as `100 * conversion / charge_F_mol`, assuming 100% selectivity. Two electrons per hydrovanilloin dimer equal one electron per vanillin molecule consumed. Values above 100% require investigation rather than clipping. The original records have not been modified to add calculated columns.
