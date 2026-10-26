import asyncio
from asyncua import Client, ua

URL = "opc.tcp://localhost:57888"
PREFIX = "Hitec_OPC_DA20_Server->HVN:"

PUMP1_SET, PUMP1_ACT = "PUMP1.W1", "PUMP1.X1"   # cathode
PUMP2_SET, PUMP2_ACT = "PUMP2.W1", "PUMP2.X1"   # anode
PSU_ON, PSU_SET = "POWERSUPPLY.ON", "POWERSUPPLY.W2"
PSU_VOLTAGE_SET, PSU_VOLTAGE_ACT = "POWERSUPPLY.W1", "POWERSUPPLY.X1"

# New labdos calibration: rpm versus mean flow (mL/min).
from math import isfinite
from bisect import bisect_right
ANODE_K = 0.5071118969420833
CATHODE_RPM = (10., 50., 100., 150., 200., 250., 300.)
CATHODE_FLOW = (2.8666666666666667, 14.373333333333335,
                26.073333333333334, 44.20666666666667,
                60.67666666666667, 77.57, 98.47333333333334)
CAL = {
    "cathode": {"rpm_min": 10., "rpm_max": 300.},
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
    rpm = flow / ANODE_K if pump == "anode" else interpolate(flow, CATHODE_FLOW, CATHODE_RPM)
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
    if pump == "anode":
        return ANODE_K * rpm
    return interpolate(rpm, CATHODE_RPM, CATHODE_FLOW)


async def wait_until_rpm(client, tag, pump, target_rpm, tol_rpm=0.1, timeout=300, poll=1.0):
    if DRY_RUN:
        print(f"[DRY RUN] would wait for {tag} to reach {target_rpm} rpm")
        return target_rpm
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    while loop.time() - t0 < timeout:
        rpm = float(await read(client, tag))
        print(f"{pump}: {rpm:.2f} rpm (~{rpm_to_flow(rpm, pump):.2f} mL/min)")
        if abs(rpm - target_rpm) <= tol_rpm:
            return rpm
        await asyncio.sleep(poll)
    raise TimeoutError(f"{pump} did not reach {target_rpm} rpm in {timeout}s")


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
