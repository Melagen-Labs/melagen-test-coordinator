# Power telemetry integration

## Scope

The power monitor persists each record to the Jetson-side JSONL log first and
then mirrors that record to the laptop using one validated JSON object per UDP
datagram. The UDP channel is live telemetry only. Packet loss does not remove
the Jetson's local evidence, and this feature does not stop CUDA, shut down
Linux, change INA3221 settings, or operate electrical protection hardware.

## Ports

- TCP 6000: existing test-control commands and acknowledgments.
- UDP 5555: existing heartbeat channel.
- UDP 6001: power telemetry introduced by this stage.

## Local simulation

Open the power-aware mock coordinator on the laptop:

```powershell
& "C:\msys64\ucrt64\bin\python.exe" `
  ".\app_power_demo.py"
```

In a second terminal, publish the simulated monitor stream to it:

```powershell
& "C:\msys64\ucrt64\bin\python.exe" `
  ".\power_monitor.py" `
  --simulate `
  --config ".\power_config.example.json" `
  --run-id "local-power-demo" `
  --telemetry-host "127.0.0.1" `
  --telemetry-port 6001
```

The graphical interface should display current, voltage, power, measurement
state, red-flag latch state, Jetson ID, run ID, sequence, and LIVE/STALE/LOST
link status. Stop the monitor with Ctrl+C.

## Real laptop and Jetson

Start the power-aware coordinator on the laptop. Replace the DUT address with
the Jetson's control address:

```powershell
& "C:\msys64\ucrt64\bin\python.exe" `
  ".\app_power_tcp.py" `
  --host "192.168.1.20" `
  --telemetry-bind-host "0.0.0.0" `
  --telemetry-port 6001
```

Start the monitor on the Jetson and replace `LAPTOP_IP` with the laptop's direct
Ethernet address:

```bash
python3 power_monitor.py \
  --config power_config.example.json \
  --run-id RUN_ID \
  --jetson-id JETSON_ID \
  --telemetry-host LAPTOP_IP \
  --telemetry-port 6001
```

Allow inbound UDP port 6001 on the laptop firewall for the direct-Ethernet
interface. Keep the JSONL logs on both machines:

- Jetson: `logs/power/power_monitor_*.jsonl`
- Laptop: `logs/power_telemetry_received.jsonl`

## Data handling

The receiver validates schema version, record type, sequence, session, device,
run, timestamp, state, and engineering-unit fields. Duplicate and out-of-order
records from one monitor session are ignored by the live display. A new monitor
session supersedes the previous session, and delayed records from a retired
session do not overwrite the current display.

Tkinter widgets are updated only by the main graphical-interface thread. The
UDP receiver performs socket I/O, validation, and durable logging in a daemon
thread and sends immutable snapshots through a bounded queue.

## Current limitation

The coordinator does not yet launch the power monitor or automatically inject
the active test request ID into `--run-id`. Until control integration is added,
operators must start the monitor with the intended run ID. The UDP stream is
not an independent Single-Event Latchup protection mechanism.
