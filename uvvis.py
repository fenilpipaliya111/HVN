"""UV-Vis analysis: edge wavelength at A = 0.75 -> vanillin concentration.

Reads an OceanView .txt spectrum (no header; column 1 wavelength in nm, column 2
absorbance) and finds the wavelength where the absorbance crosses A = 0.75 on the
falling edge (long-wavelength side), then converts it to a concentration.

METHOD = "crossing": interpolate between the two neighbouring points around 0.75
                     (the work flow you asked for; no exact 0.75 value is needed)
METHOD = "band":     straight-line fit through all points in 0.75 +/- BAND

Calibration (A = 0.75 curve, x axis = log10 of concentration in mol/L):
    wavelength = CAL_A0 + CAL_B * log10(c)
"""

# ------------------------------- settings ----------------------------------
METHOD = "crossing"     # "crossing" (interpolated 0.75 crossing) or "band"
LEVEL = 0.75            # absorbance level that is tracked
BAND = 0.05             # METHOD "band": points with LEVEL-BAND <= A <= LEVEL+BAND are used
CLUSTER_NM = 2.0        # METHOD "crossing": noise crossings within this distance are averaged
MAX_CROSSINGS = 6       # METHOD "crossing": more crossings than this within MAX_SPAN_NM = noise
WL_MIN_NM = 357.0       # analysed range: 357 nm ...
WL_MAX_NM = 435.0       # ... up to 435 nm (the spectrum itself ends at about 434 nm)
SMOOTH_POINTS = 5       # moving-average length before searching (1 = no smoothing)
MIN_BAND_POINTS = 3     # at least this many points must lie in the band
MAX_SPAN_NM = 10.0      # band wider than this (or too many crossings in it) -> no clear edge

CAL_A0 = 430.03839      # intercept of the A = 0.75 fit (nm at 1 mol/L)
CAL_B = 19.12081        # slope of the A = 0.75 fit (nm per decade of concentration)
CAL_LOW_NM = 399.0      # lowest calibration standard is at about 399.9 nm (0.02 M)
CAL_HIGH_NM = 431.0     # above this the solution is too concentrated (about 1 M)
FLOOR_NM = 357.0        # edge below this: not resolved (same as the start of the range,
                        # so every edge found in 357 - 434 nm gets a concentration)
# ----------------------------------------------------------------------------


def load_txt(path):
    """Return (wavelengths, absorbances) from a two-column text file."""
    wl, ab = [], []
    with open(path, "r", encoding="utf-8-sig", errors="ignore") as f:
        for line in f:
            parts = line.replace(",", ".").split()
            if len(parts) < 2:
                continue
            try:
                x = float(parts[0])
                y = float(parts[1])
            except ValueError:
                continue                      # header or broken line
            wl.append(x)
            ab.append(y)
    if len(wl) > 1 and wl[0] > wl[-1]:        # make sure wavelengths ascend
        wl.reverse()
        ab.reverse()
    return wl, ab


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
        return None, "no_data", 0, 0.0
    w = [wl[i] for i in idx]
    a = smooth([ab[i] for i in idx], SMOOTH_POINTS)

    lo, hi = LEVEL - BAND, LEVEL + BAND
    if a[-1] >= lo:                           # still absorbing at the red end
        return None, "too_concentrated", 0, 0.0

    # scan from the red end towards shorter wavelengths
    j = len(a) - 1
    while j >= 0 and a[j] < lo:
        j -= 1
    if j < 0:                                 # never reaches the band
        return None, "no_edge", 0, 0.0

    # collect the continuous stretch of points inside the band
    pts = []
    while j >= 0 and a[j] <= hi:
        pts.append((w[j], a[j]))
        j -= 1
    n = len(pts)
    if n < MIN_BAND_POINTS:
        return None, "no_clear_edge", n, 0.0
    span = max(p[0] for p in pts) - min(p[0] for p in pts)
    if span > MAX_SPAN_NM:
        return None, "no_clear_edge", n, span

    # straight-line fit: wavelength = mw + m * (A - ma)
    ma = sum(p[1] for p in pts) / n
    mw = sum(p[0] for p in pts) / n
    sxx = sum((p[1] - ma) ** 2 for p in pts)
    sxy = sum((p[1] - ma) * (p[0] - mw) for p in pts)
    if sxx < 1e-9:
        return None, "no_clear_edge", n, span
    m = sxy / sxx
    if m >= 0:                                # absorbance must fall as wavelength rises
        return None, "no_clear_edge", n, span
    edge = mw + m * (LEVEL - ma)
    wmin = min(p[0] for p in pts)
    wmax = max(p[0] for p in pts)
    if not (wmin - 1.0 <= edge <= wmax + 1.0):
        return None, "no_clear_edge", n, span
    return edge, "ok", n, span


def find_edge_crossing(wl, ab):
    """Interpolated crossing of A = LEVEL on the falling edge.

    Scans from the red end towards shorter wavelengths, interpolates the wavelength
    between the two points that straddle LEVEL, and averages crossings that lie
    within CLUSTER_NM of the first one (noise wiggles on the same edge).
    Returns (edge_wavelength_nm or None, status, n_crossings_used, spread_nm).
    """
    idx = [i for i, w in enumerate(wl) if WL_MIN_NM <= w <= WL_MAX_NM]
    if len(idx) < 20:
        return None, "no_data", 0, 0.0
    w = [wl[i] for i in idx]
    a = smooth([ab[i] for i in idx], SMOOTH_POINTS)
    if a[-1] >= LEVEL:                        # still absorbing at the red end
        return None, "too_concentrated", 0, 0.0

    crossings = []                            # from the red end downwards
    for i in range(len(a) - 1, 0, -1):
        if a[i] < LEVEL <= a[i - 1]:          # absorbance rises towards shorter wavelength
            x = w[i - 1] + (LEVEL - a[i - 1]) * (w[i] - w[i - 1]) / (a[i] - a[i - 1])
            crossings.append(x)
    if not crossings:
        return None, "no_edge", 0, 0.0

    first = crossings[0]
    if len([x for x in crossings if first - x <= MAX_SPAN_NM]) > MAX_CROSSINGS:
        return None, "no_clear_edge", len(crossings), 0.0       # flat / very noisy
    cluster = [x for x in crossings if first - x <= CLUSTER_NM]
    edge = sum(cluster) / len(cluster)
    return edge, "ok", len(cluster), max(cluster) - min(cluster)


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
    finder = find_edge_crossing if METHOD == "crossing" else find_edge
    edge, status, n, span = finder(wl, ab)
    in_range = [v for w, v in zip(wl, smooth(ab, SMOOTH_POINTS))
                if WL_MIN_NM <= w <= WL_MAX_NM]
    result = {"wavelength_nm": edge, "conc_M": None, "conversion": None,
              "status": status, "n_points": n, "span_nm": span,
              "max_abs": max(in_range) if in_range else None}
    if edge is None:
        return result
    if edge > CAL_HIGH_NM:
        result["status"] = "too_concentrated"
        return result
    if edge < FLOOR_NM:
        result["status"] = "at_floor"
        return result
    conc = conc_from_wavelength(edge)
    result["conc_M"] = conc
    result["status"] = "calibrated" if edge >= CAL_LOW_NM else "extrapolated"
    if feed_conc_m:
        result["conversion"] = 1.0 - conc / feed_conc_m
    return result


if __name__ == "__main__":
    import sys
    for name in sys.argv[1:]:
        x, y = load_txt(name)
        print(name, analyse(x, y))
