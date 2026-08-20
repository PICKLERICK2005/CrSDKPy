"""Focus-position movement characterization.

Answers whether a deterministic focus API is implementable on a given body and
lens: whether a commanded absolute position is actually reached, how long the
lens takes to get there and settle, and whether the same command twice lands in
the same place. That is what a focus-stacking caller needs and none of it can be
read from a property.

    python tools/focus_timing.py --confirm-moves
    python tools/focus_timing.py --confirm-moves --step 300 --label mf

This one MOVES THE LENS, so it refuses to run without --confirm-moves. Moves are
small, stay inside the property's advertised range, and the starting position is
commanded back at the end and verified.

Measured on an ILME-FX3A with an FE 50mm F1.4 GM: every commanded position was
reached exactly, a 600-unit move settled in about 1.1 s, and the same target
twice gave an identical result.
"""
import argparse
import json
import time

import crsdkpy

CUR = 0x0766          # FocusPositionCurrentValue
SET = 0x020E          # FocusPositionSetting
DRIVING = 0x0767      # FocusDrivingStatus
FOLLOW = 0x0757       # FollowFocusPositionCurrentValue
MODE = 0x0109
MODE_SET = 0x0179
PRE_AF = 0x0260

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--confirm-moves", action="store_true",
                    help="required: this drives the focus mechanism")
parser.add_argument("--step", type=int, default=600,
                    help="units to move either side of the start position")
parser.add_argument("--label", default="af_s",
                    help="focus mode the body is in, recorded in the output")
parser.add_argument("-o", "--output")
args = parser.parse_args()
if not args.confirm_moves:
    parser.error(
        "this probe moves the lens; pass --confirm-moves to allow it"
    )
STEP = args.step
LABEL = args.label

out = {"schema": "crsdkpy.focus_probe/1", "focus_mode_label": LABEL,
       "commanded_step": STEP, "moves": []}


def move(s, target, label, settle_quiet=0.4, cap=6.0):
    r = {"label": label, "target": target, "samples": []}
    r["from"] = s.raw.get_property(CUR).value
    drive0 = s.raw.get_property(DRIVING).value
    t0 = time.monotonic()
    try:
        s.raw.set_property(SET, target)
        r["write_error"] = None
    except crsdkpy.CrSDKPyError as exc:
        r["write_error"] = f"{type(exc).__name__}: {exc}"
    r["write_ms"] = round((time.monotonic() - t0) * 1000, 1)

    last, dlast = r["from"], drive0
    last_change = time.monotonic()
    r["first_change_ms"] = None
    r["settled_ms"] = None
    while time.monotonic() - t0 < cap:
        cur = s.raw.get_property(CUR).value
        drv = s.raw.get_property(DRIVING).value
        t = round((time.monotonic() - t0) * 1000, 1)
        if cur != last or drv != dlast:
            r["samples"].append({"t_ms": t, "pos": cur, "driving": drv})
            if cur != last and r["first_change_ms"] is None:
                r["first_change_ms"] = t
            last, dlast = cur, drv
            last_change = time.monotonic()
        if cur == target and time.monotonic() - last_change > settle_quiet:
            r["settled_ms"] = t
            break
        if (r["first_change_ms"] is not None
                and time.monotonic() - last_change > 0.9):
            r["settled_ms"] = t
            break
        time.sleep(0.02)
    r["final"] = s.raw.get_property(CUR).value
    r["follow_final"] = s.raw.get_property(FOLLOW).value
    r["reached_exact"] = r["final"] == target
    print(f"  {label:16} {r['from']:>6} -> {target:>6} | write "
          f"{r['write_ms']:>6.1f} ms | first change "
          f"{r['first_change_ms']} ms | settled {r['settled_ms']} ms | final "
          f"{r['final']} exact={r['reached_exact']} err={r['write_error']}",
          flush=True)
    out["moves"].append(r)
    return r


with crsdkpy.SDK(backend="host") as sdk:
    cam = sdk.discover()[0]
    with cam.open("remote") as s:
        for code, name in ((MODE, "focus_mode"), (MODE_SET, "focus_mode_setting"),
                           (PRE_AF, "pre_af")):
            out[name] = s.raw.get_property(code).value
        setting = s.raw.get_property(SET)
        start = s.raw.get_property(CUR).value
        out["start_position"] = start
        out["setting_writable"] = setting.writable
        out["setting_access"] = setting.access.value
        print(f"mode={out['focus_mode']} mode_setting={out['focus_mode_setting']} "
              f"pre_af={out['pre_af']}")
        print(f"start={start} setting_writable={out['setting_writable']}")

        lower = max(0, start - STEP)
        higher = min(65535, start + STEP)
        a = move(s, lower, "lower")
        time.sleep(0.3)
        move(s, higher, "higher")
        time.sleep(0.3)
        move(s, start, "back to start")
        time.sleep(0.3)
        d = move(s, lower, "repeat lower")
        time.sleep(0.3)
        move(s, start, "restore")

        out["repeatable"] = {
            "first_final": a["final"], "second_final": d["final"],
            "identical": a["final"] == d["final"],
        }
        out["restored_position"] = s.raw.get_property(CUR).value
        out["restored_exact"] = out["restored_position"] == start
        out["driving_values_seen"] = sorted({
            sm["driving"] for m in out["moves"] for sm in m["samples"]
        })
        print(f"\nrestored={out['restored_position']} (start {start}) "
              f"exact={out['restored_exact']}")
        print(f"repeatable: {a['final']} vs {d['final']} -> "
              f"{out['repeatable']['identical']}")
        print(f"FocusDrivingStatus values seen: {out['driving_values_seen']}")

path = args.output or f"focus_probe_{LABEL}.json"
with open(path, "w", encoding="utf-8") as fh:
    json.dump(out, fh, indent=1)
print(f"wrote {path}")
