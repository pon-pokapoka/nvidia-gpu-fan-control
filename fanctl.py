#!/usr/bin/env python3
import argparse
import glob
import json
import logging
import os
import pwd
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import List, Tuple, Optional


logger = logging.getLogger("fanctl")


def run_cmd(cmd: List[str], env: Optional[dict] = None, check: bool = True) -> str:
    p = subprocess.run(
        cmd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if check and p.returncode != 0:
        raise RuntimeError(
            "Command failed:\n"
            + " ".join(cmd)
            + "\n\nstdout:\n"
            + p.stdout
            + "\n\nstderr:\n"
            + p.stderr
        )
    return p.stdout.strip()


def _xauth_candidates_for_uid(uid: str) -> List[str]:
    candidates: List[str] = []
    for pattern in [
        f"/run/user/{uid}/.mutter-Xwaylandauth.*",
        f"/run/user/{uid}/Xauthority",
        f"/run/user/{uid}/gdm/Xauthority",
    ]:
        candidates += glob.glob(pattern)
    try:
        home = pwd.getpwuid(int(uid)).pw_dir
        candidates.append(os.path.join(home, ".Xauthority"))
    except Exception:
        pass
    return candidates


def find_xauthority() -> Optional[str]:
    """Find a readable Xauthority file from any active graphical user session."""
    # 1. Use loginctl to find active graphical sessions
    try:
        sessions_out = subprocess.run(
            ["loginctl", "list-sessions", "--no-legend", "--no-pager"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        ).stdout.strip()
        for line in sessions_out.splitlines():
            parts = line.split()
            if not parts:
                continue
            session_id = parts[0]
            info_out = subprocess.run(
                ["loginctl", "show-session", session_id,
                 "--property=Type", "--property=UID", "--property=State"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            ).stdout
            props = {}
            for kv in info_out.splitlines():
                if "=" in kv:
                    k, v = kv.split("=", 1)
                    props[k] = v
            if props.get("State") not in ("active", "online"):
                continue
            if props.get("Type") not in ("x11", "wayland", "mir"):
                continue
            uid = props.get("UID")
            if not uid:
                continue
            for path in _xauth_candidates_for_uid(uid):
                if os.path.isfile(path):
                    return path
    except Exception:
        pass

    # 2. Glob fallback
    candidates: List[str] = []
    candidates += glob.glob("/run/user/*/.mutter-Xwaylandauth.*")
    candidates += glob.glob("/run/user/*/Xauthority")
    candidates += glob.glob("/run/user/*/gdm/Xauthority")
    candidates += glob.glob("/home/*/.Xauthority")
    candidates += glob.glob("/tmp/.xauth*")
    for path in candidates:
        if os.path.isfile(path):
            return path
    return None


@dataclass
class NvControl:
    display: Optional[str]
    xauthority: Optional[str]
    dry_run: bool = False
    _resolved_xauthority: Optional[str] = field(default=None, init=False, repr=False)
    _searched_xauthority: bool = field(default=False, init=False, repr=False)
    _logged_xauthority: bool = field(default=False, init=False, repr=False)

    def env(self) -> dict:
        e = os.environ.copy()
        if self.display:
            e["DISPLAY"] = self.display
        if self.xauthority and self.xauthority.strip():
            xauth = self.xauthority.strip()
        else:
            if not self._searched_xauthority:
                # When running as root from systemd there is no inherited cookie;
                # search for one from the active desktop session once and cache it.
                self._resolved_xauthority = find_xauthority()
                self._searched_xauthority = True
            xauth = self._resolved_xauthority
        if xauth:
            if not self._logged_xauthority:
                logger.info("[xauth] using %s", xauth)
                self._logged_xauthority = True
            e["XAUTHORITY"] = xauth
        else:
            if not self._logged_xauthority:
                logger.warning("[xauth] no Xauthority file found; relying on inherited env")
                self._logged_xauthority = True
        return e

    def q(self, target: str) -> str:
        return run_cmd(["nvidia-settings", "-q", target, "-t"], env=self.env())

    def a(self, target: str, value: int) -> None:
        cmd = ["nvidia-settings", "-a", f"{target}={value}"]
        if self.dry_run:
            logger.info("DRY-RUN: %s", " ".join(cmd))
            return
        run_cmd(cmd, env=self.env())

    def list_fans(self, max_fans: int = 12) -> List[int]:
        fans = []
        for i in range(max_fans):
            try:
                _ = self.q(f"[fan:{i}]/GPUTargetFanSpeed")
                fans.append(i)
            except Exception:
                continue
        return fans

    def set_manual(self, gpu: int, manual: bool) -> None:
        self.a(f"[gpu:{gpu}]/GPUFanControlState", 1 if manual else 0)

    def get_manual(self, gpu: int) -> int:
        return int(self.q(f"[gpu:{gpu}]/GPUFanControlState"))

    def set_fan_speed(self, fan: int, speed: int) -> None:
        self.a(f"[fan:{fan}]/GPUTargetFanSpeed", speed)

    def get_fan_speed(self, fan: int) -> int:
        return int(self.q(f"[fan:{fan}]/GPUTargetFanSpeed"))


class NvidiaSmi:
    @staticmethod
    def gpu_temp(gpu: int) -> int:
        out = run_cmd(
            [
                "nvidia-smi",
                "-i",
                str(gpu),
                "--query-gpu=temperature.gpu",
                "--format=csv,noheader,nounits",
            ]
        )
        return int(out.splitlines()[0].strip())


def clamp(v: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, v))


def lerp_curve(points: List[Tuple[int, int]], t: int) -> int:
    pts = sorted(points, key=lambda x: x[0])
    if t <= pts[0][0]:
        return pts[0][1]
    if t >= pts[-1][0]:
        return pts[-1][1]
    for i in range(len(pts) - 1):
        t1, s1 = pts[i]
        t2, s2 = pts[i + 1]
        if t1 <= t <= t2:
            if t2 == t1:
                return s2
            ratio = (t - t1) / (t2 - t1)
            return int(round(s1 + ratio * (s2 - s1)))
    return pts[-1][1]


def load_curve(path: str):
    with open(path, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    points = [(int(a), int(b)) for a, b in cfg["points"]]
    hysteresis = int(cfg.get("hysteresis", 2))
    interval = float(cfg.get("interval_sec", 5))
    min_speed = int(cfg.get("min_speed", 20))
    max_speed = int(cfg.get("max_speed", 90))
    return points, hysteresis, interval, min_speed, max_speed


def ensure_tools() -> None:
    for tool in ["nvidia-settings", "nvidia-smi"]:
        try:
            run_cmd(["which", tool])
        except Exception:
            raise RuntimeError(f"Missing tool: {tool}")


def cmd_status(args):
    nv = NvControl(args.display, args.xauthority, args.dry_run)
    temp = NvidiaSmi.gpu_temp(args.gpu)
    fans = nv.list_fans()
    manual = nv.get_manual(args.gpu)

    logger.info("GPU %s temp: %s C", args.gpu, temp)
    logger.info("GPU %s manual fan control: %s", args.gpu, manual)
    if not fans:
        logger.warning("No controllable fans found through NV-CONTROL.")
        return

    for fan in fans:
        try:
            speed = nv.get_fan_speed(fan)
            logger.info("fan %s target speed: %s%%", fan, speed)
        except Exception as e:
            logger.warning("fan %s read failed: %s", fan, e)


def cmd_set(args):
    nv = NvControl(args.display, args.xauthority, args.dry_run)
    speed = clamp(args.speed, 1, 100)
    fans = nv.list_fans()
    if not fans:
        raise RuntimeError("No controllable fans found.")

    nv.set_manual(args.gpu, True)
    for fan in fans:
        nv.set_fan_speed(fan, speed)
    logger.info("Set fans %s to %s%% on GPU %s", fans, speed, args.gpu)


def cmd_auto(args):
    nv = NvControl(args.display, args.xauthority, args.dry_run)
    nv.set_manual(args.gpu, False)
    logger.info("Restored automatic fan control for GPU %s", args.gpu)


def cmd_curve(args):
    nv = NvControl(args.display, args.xauthority, args.dry_run)
    points, hysteresis, interval, min_speed, max_speed = load_curve(args.config)

    fans = nv.list_fans()
    if not fans:
        raise RuntimeError("No controllable fans found.")

    stop = {"now": False}

    def _stop(_sig, _frm):
        stop["now"] = True

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    nv.set_manual(args.gpu, True)
    logger.info(
        "Curve loop started: gpu=%s, fans=%s, interval=%ss, hysteresis=%sC",
        args.gpu,
        fans,
        interval,
        hysteresis,
    )

    last_change_temp = None
    last_applied = None

    try:
        while not stop["now"]:
            temp = NvidiaSmi.gpu_temp(args.gpu)
            target = clamp(lerp_curve(points, temp), min_speed, max_speed)

            should_apply = False
            if last_applied is None:
                should_apply = True
            elif abs(temp - (last_change_temp if last_change_temp is not None else temp)) >= hysteresis and target != last_applied:
                should_apply = True

            if should_apply:
                for fan in fans:
                    nv.set_fan_speed(fan, target)
                last_applied = target
                last_change_temp = temp
                logger.info("temp=%sC -> fan=%s%%", temp, target)
            else:
                logger.info("temp=%sC -> keep fan=%s%%", temp, last_applied)

            if args.once:
                break
            time.sleep(interval)
    finally:
        if args.restore_auto:
            nv.set_manual(args.gpu, False)
            logger.info("Automatic fan control restored.")


def build_parser():
    p = argparse.ArgumentParser(description="GeForce fan controller (NV-CONTROL + CLI)")
    p.add_argument("--gpu", type=int, default=0, help="GPU index (default: 0)")
    p.add_argument("--display", default=os.environ.get("DISPLAY"), help="X display, e.g. :0 or :99")
    p.add_argument("--xauthority", default=os.environ.get("XAUTHORITY"), help="Path to XAUTHORITY file")
    p.add_argument("--dry-run", action="store_true", help="Print commands without changing fan state")

    sub = p.add_subparsers(dest="cmd", required=True)

    s_status = sub.add_parser("status", help="Show temp and fan status")
    s_status.set_defaults(fn=cmd_status)

    s_set = sub.add_parser("set", help="Set fixed fan speed percent")
    s_set.add_argument("--speed", type=int, required=True, help="Fan speed 1-100")
    s_set.set_defaults(fn=cmd_set)

    s_auto = sub.add_parser("auto", help="Return control to automatic mode")
    s_auto.set_defaults(fn=cmd_auto)

    s_curve = sub.add_parser("curve", help="Run temperature curve loop from JSON config")
    s_curve.add_argument("--config", required=True, help="Path to curve JSON")
    s_curve.add_argument("--once", action="store_true", help="Evaluate one cycle and exit")
    s_curve.add_argument(
        "--restore-auto",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Restore automatic fan control on exit (default: true)",
    )
    s_curve.set_defaults(fn=cmd_curve)

    return p


def main():
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    try:
        ensure_tools()
        parser = build_parser()
        args = parser.parse_args()
        args.fn(args)
    except Exception as e:
        logger.error("ERROR: %s", e)
        sys.exit(1)


if __name__ == "__main__":
    main()
