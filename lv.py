import asyncio
from asyncua import Client, ua

URL = "opc.tcp://localhost:57888"
PREFIX = "Hitec_OPC_DA20_Server->HVN:"

PUMP1_SET = "PUMP1.W1"  # Cathode RPM setpoint.
PUMP2_SET = "PUMP2.W1"  # Anode RPM setpoint.
PSU_ON, PSU_SET = "POWERSUPPLY.ON", "POWERSUPPLY.W2"
PSU_VOLTAGE_SET = "POWERSUPPLY.W1"

# Shared user-requested anode water calibration for both pumps: mL/min = ANODE_K * RPM.
from math import isfinite
from bisect import bisect_right
ANODE_K = 0.5071118969420833
CAL = {
    "cathode": {"rpm_min": 5., "rpm_max": 300.},
    "anode": {"rpm_min": 5., "rpm_max": 300.},
}


def interpolate(x, xs, ys):
    i = max(0, min(len(xs)-2, bisect_right(xs, x)-1))
    return ys[i] + (x-xs[i]) * (ys[i+1]-ys[i]) / (xs[i+1]-xs[i])


DRY_RUN = False   # run_all.py switches this to True for a dry run


def node(client, tag):
    return client.get_node(f"ns=3;s={PREFIX}{tag}")


async def read(client, tag):
    dv = await node(client, tag).read_data_value()
    if not dv.StatusCode.is_good():
        raise RuntimeError(f"{tag}: bad status {dv.StatusCode.name}")
    return dv.Value.Value


async def write(client, tag, value):
    if DRY_RUN:
        print(f"[DRY RUN] write {tag} = {value}")
        return
    n = node(client, tag)
    vtype = await n.read_data_type_as_variant_type()
    if vtype == ua.VariantType.Boolean:
        value = bool(value)
    elif vtype in (ua.VariantType.Float, ua.VariantType.Double):
        value = float(value)
    else:
        if float(value) != int(value):
            raise ValueError(f"{tag} requires integer commands; configure pump resolution before running")
        value = int(value)
    await n.write_value(ua.DataValue(ua.Variant(value, vtype)))


def flow_to_rpm(flow, pump):
    flow = float(flow)
    c = CAL[pump]
    if not isfinite(flow) or flow <= 0:
        raise ValueError("flow must be finite and positive")
    rpm = flow / ANODE_K
    if not c["rpm_min"] - 1e-9 <= rpm <= c["rpm_max"] + 1e-9:
        raise ValueError(f"{pump}: {flow} mL/min outside measured calibration")
    # Do not silently quantize: pump must support floating point RPM commands.
    return min(c["rpm_max"], max(c["rpm_min"], rpm))


def rpm_to_flow(rpm, pump):
    rpm = float(rpm)
    if not isfinite(rpm):
        raise ValueError("RPM must be finite")
    if rpm == 0:
        return 0.0
    return ANODE_K * rpm


async def shutdown(client):
    """Power supply off first, then pumps. Every step is tried even if one fails."""
    errors = []
    for tag, val in ((PSU_ON, 0), (PSU_SET, 0), (PSU_VOLTAGE_SET, 0), (PUMP1_SET, 0), (PUMP2_SET, 0)):
        try:
            await write(client, tag, val)
        except Exception as e:
            errors.append(f"{tag}: {e}")
    if errors:
        raise RuntimeError("Shutdown incomplete: " + "; ".join(errors))
