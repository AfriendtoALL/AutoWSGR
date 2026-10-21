---
name: emulator-debug
description: 'Debug and fix emulator connection issues in AutoWSGR. Use when: ADB device not found, scrcpy connection timeout, ModuleNotFoundError for cv2/retry/adbutils, emulator serial mismatch, no first frame, screenshot timeout, emulator detection failure.'
argument-hint: 'Describe the error message or connection symptom'
---

# Emulator Connection Debugging

Diagnose and fix emulator connection problems in AutoWSGR's scrcpy-based device control pipeline.

## Connection Pipeline Overview

```
usersettings.yaml (emulator.type / serial)
  -> resolve_serial()          # autowsgr/emulator/detector.py
  -> adb connect <serial>      # TCP devices only (port in serial)
  -> adbutils.adb.device()     # autowsgr/emulator/controller/scrcpy.py
  -> window_size()             # adbutils shell
  -> deploy scrcpy-server.jar  # push to /data/local/tmp/
  -> start scrcpy server       # app_process on device
  -> connect video socket      # LOCAL_ABSTRACT 'scrcpy'
  -> wait first frame (10s)    # H264 decode via av
```

## Diagnostic Procedure

### Step 1: Confirm Emulator is Running

```powershell
Get-Process | Where-Object {
  $_.ProcessName -match 'ld|dnplayer|bluestacks|mumu|nox|memu'
} | Select-Object ProcessName, Id
```

Map process names to emulator types:

| ProcessName pattern | Emulator type |
|---|---|
| `LdVBoxHeadless`, `dnplayer` | 雷电 |
| `MuMuVMMHeadless`, `MuMuNx*` | MuMu |
| `HD-Player`, `Bluestacks` | 蓝叠 |

### Step 2: Verify ADB Connectivity

Locate the ADB binary used by the project:

```python
uv run python -c "from adbutils._utils import adb_path; print(adb_path())"
```

Then check devices:

```powershell
& "<adb_path>" devices -l
```

If the device list is empty, find the listening port and connect manually:

```powershell
netstat -ano | Select-String "LISTENING" | Select-String "16384|16416|7555|5555|5554"
& "<adb_path>" connect 127.0.0.1:<port>
```

### Step 3: Validate usersettings.yaml

Key fields in `usersettings.yaml`:

```yaml
emulator:
  type: MuMu        # Must match the actual running emulator
  serial: null       # null = auto-detect; or explicit like 127.0.0.1:16384
  path: null         # null = auto-detect from registry
  process_name: null # null = auto-infer
```

**Supported `type` values**: 雷电, 蓝叠, MuMu, 云手机, 其他 (see `autowsgr/types.py`)

## Common Errors and Fixes

| Error | Cause | Fix |
|---|---|---|
| `device 'emulator-5554' not found` | Config `type` mismatches actual emulator (e.g., 雷电 config but MuMu running) | Change `emulator.type` in usersettings.yaml |
| `device '127.0.0.1:16384' not found` | ADB server not connected to TCP device | Code auto-connects now; if still failing, manually `adb connect 127.0.0.1:16384` |
| `ModuleNotFoundError: cv2` | opencv-python + opencv-python-headless conflict | `uv pip uninstall opencv-python` then `uv sync` (project uses headless) |
| `ModuleNotFoundError: retry` | `retry2` not installed (provides `retry` module) | `uv sync` (retry2 is a transitive dep of adbutils) |
| `scrcpy video stream no first frame` | JAR not pushed, device too slow, or port conflict | Check `autowsgr/data/bin/scrcpy-server.jar` exists; increase screenshot_timeout |
| `未发现已连接的 ADB 设备` | No emulator process running or ADB server down | Start emulator; run `adb start-server` |
| `EmulatorConnectionError` after 3 retries | Persistent ADB issue | Kill all adb processes, restart emulator, then retry |

## Serial Auto-Detection Logic

`resolve_serial()` in `autowsgr/emulator/detector.py` follows this priority:

1. `config.serial` explicitly set -> use directly
2. Only 1 ADB device online -> auto-select
3. Multiple devices, but `config.type` matches exactly 1 -> auto-select
4. Multiple ambiguous devices -> interactive prompt (TTY only)
5. No devices -> raise `EmulatorConnectionError`

Serial-to-type matching patterns:

| Pattern | Type |
|---|---|
| `emulator-\d+` | 雷电 |
| `127.0.0.1:1638x-16xxx` | MuMu 12 |
| `127.0.0.1:620xx` | MuMu (old) |
| `127.0.0.1:555x-59xx` | 蓝叠 |

## Key Source Files

| File | Role |
|---|---|
| `autowsgr/emulator/controller/scrcpy.py` | ScrcpyController: connect, screenshot, deploy |
| `autowsgr/emulator/detector.py` | detect_emulators, resolve_serial, identify_emulator_type |
| `autowsgr/infra/config.py` | EmulatorConfig model, YAML loading |
| `autowsgr/types.py` | EmulatorType enum |
| `autowsgr/scheduler/launcher.py` | Launcher.connect() entry point |
| `autowsgr/data/bin/scrcpy-server.jar` | Bundled scrcpy server (v2.7) |
