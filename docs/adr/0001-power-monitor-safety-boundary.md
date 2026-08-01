# ADR 0001: Power-monitor safety boundary

- Status: Accepted for development
- Date: 2026-07-31
- Scope: `feature/power-monitor`

## Context

The Jetson Orin Nano is the Device Under Test during proton-beam Single-Event Effects testing. Software executing on the Device Under Test can stop scheduling, reboot, lose network connectivity, or produce incorrect results during the same fault that the monitor is intended to observe. The Jetson INA3221 `VDD_IN` channel is therefore useful telemetry but is not an independent protection mechanism.

The current threshold values are temporary development assumptions. They have not been established from a characterized workload baseline and have not been approved as Single-Event Latchup protection limits.

## Decision

1. The internal power monitor is read-only diagnostic software.
2. It may read `VDD_IN`, classify measurements, latch diagnostic incidents, write durable JSONL records, and publish telemetry to the laptop arbiter.
3. It must not write INA3221 limit files.
4. It must not stop CUDA automatically.
5. It must not request Linux shutdown automatically.
6. It must not operate a relay, load switch, eFuse, or other physical cutoff.
7. `2300 mA` for `3.0 seconds` remains a temporary development/simulation rule only.
8. A confirmed diagnostic red flag remains latched until an authorized reset outside an active run.
9. Any future automatic protective action requires separate electrical and radiation-test approval plus hardware-in-the-loop validation.
10. Safety-critical current interruption requires an independent external sensing and cutoff path that remains functional if the Jetson hangs.

## Consequences

- The software can support testing, evidence collection, and operator alerts without being represented as hardware protection.
- The laptop may correlate power events with heartbeat, CUDA, boot, and network events but must not assume silence means normal power.
- Final thresholds must be derived from measured current profiles across boot, idle, CUDA startup, steady state, shutdown, temperature, voltage, and power modes.
- The feature branch must not be merged as an automatic protection feature.

## Review trigger

Review this decision after external current-sensor data, Jetson baseline measurements, recovery policy, and electrical/radiation-test approvals are available.
