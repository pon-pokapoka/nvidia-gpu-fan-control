# NVIDIA GeForce Fan Control CLI (Wayland-Friendly)

This project provides a Python CLI to control NVIDIA GeForce fan speed using `nvidia-settings` (NV-CONTROL) and temperature data from `nvidia-smi`.

It works with:
- Wayland desktop sessions (by targeting an NVIDIA X display)
- Headless systems (by running a dedicated minimal Xorg display)

## Files

- [fanctl.py](fanctl.py): CLI controller
- [curve.json](curve.json): Default temperature-to-fan curve

## Requirements

- NVIDIA proprietary driver
- `nvidia-smi`
- `nvidia-settings`
- Coolbits enabled for your NVIDIA X screen (typically Coolbits `4` or `12`)

## Quick Start

Run from the project directory:

```bash
chmod +x fanctl.py
```

Show status:

```bash
./fanctl.py --display :0 status
```

Set fixed fan speed:

```bash
sudo ./fanctl.py --display :0 set --speed 55
```

Restore automatic control:

```bash
sudo ./fanctl.py --display :0 auto
```

Run curve loop:

```bash
sudo ./fanctl.py --display :0 curve --config curve.json
```

Run one curve evaluation and exit:

```bash
sudo ./fanctl.py --display :0 curve --config curve.json --once
```

## Wayland Notes

NVIDIA manual fan control on GeForce is usually exposed through NV-CONTROL (X11 path), even if your desktop is Wayland.

If your main desktop is Wayland, either:
- use the Xwayland-accessible NVIDIA display if fan controls are exposed, or
- run a dedicated Xorg display (for example `:99`) and point the CLI to it.

You can pass display and auth explicitly:

```bash
sudo ./fanctl.py --display :99 --xauthority /path/to/Xauthority status
```

## Headless Notes

Without a window system, `nvidia-settings` cannot control fans directly. For GeForce, run a small Xorg service with Coolbits enabled, then use:

```bash
sudo ./fanctl.py --display :99 --xauthority /run/nvidia-fan/Xauthority curve --config curve.json
```

## Start On Boot (systemd)

This repo includes:

- `systemd/nvidia-fan-curve.service`
- `systemd/nvidia-fan-curve.env.example`

Install files to system locations:

```bash
sudo install -d /etc/nvidia-fan
sudo install -m 0755 fanctl.py /etc/nvidia-fan/fanctl.py
sudo install -m 0644 curve.json /etc/nvidia-fan/curve.json
sudo install -m 0644 systemd/nvidia-fan-curve.service /etc/systemd/system/nvidia-fan-curve.service
sudo install -m 0644 systemd/nvidia-fan-curve.env.example /etc/default/nvidia-fan-curve
```

Edit runtime settings:

```bash
sudoedit /etc/default/nvidia-fan-curve
```

Enable and start at boot:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now nvidia-fan-curve.service
```

Check status and logs:

```bash
systemctl status nvidia-fan-curve.service
journalctl -u nvidia-fan-curve.service -f
```

Notes:

- `DISPLAY_NUM=:0.0` is the correct value for most desktop Xorg/Wayland sessions (include the screen number).
- `DISPLAY_NUM=:99` is for a dedicated headless Xorg service.
- `XAUTHORITY_PATH` can be left empty; set it only if `nvidia-settings` cannot find the auth cookie automatically.

## Curve Configuration

The curve config format:

```json
{
  "points": [[30, 32], [38, 54], [45, 68], [52, 78], [60, 86], [66, 90], [84, 90]],
  "hysteresis": 2,
  "interval_sec": 5,
  "min_speed": 30,
  "max_speed": 90
}
```

- `points`: `[temperature_c, fan_percent]` pairs, linearly interpolated
- `hysteresis`: minimum temperature delta before applying a new speed
- `interval_sec`: polling interval
- `min_speed` / `max_speed`: safety clamp for computed output

## Safety Recommendations

- Keep `max_speed` at or below `90` unless full fan is truly needed.
- Keep `--restore-auto` enabled (default) in curve mode.
- Test your curve under load and verify thermals with `nvidia-smi`.

## Command Reference

Global options:

- `--gpu`: GPU index (default `0`)
- `--display`: X display (for example `:0`, `:99`)
- `--xauthority`: Xauthority file path
- `--dry-run`: print commands without applying fan changes

Subcommands:

- `status`: show temp and fan state
- `set --speed N`: set a fixed fan speed (1-100)
- `auto`: return control to automatic mode
- `curve --config FILE`: run fan curve loop

## Troubleshooting

- If you see `Missing tool: nvidia-settings`, install NVIDIA settings package for your distro.
- If fan controls are not found, verify:
  - Coolbits is enabled for the NVIDIA X screen.
  - You are targeting the correct X display.
  - The selected GPU/fan is actually controllable on your hardware/driver.
