# Lake Shore Model 340

Version `0.2.1` supports OpenLab Control 0.19–0.20 and keeps a numeric
`old reading` available while treating unused invalid inputs as unavailable.

This System Instrument controls a Lake Shore Model 340 through a PyVISA
GPIB resource. It reads both A/B temperatures freshly for status and
measurement requests. The core controller value, target and stability follow
the configured primary loop; target and ramp writes are sent once to both
enabled loops.

The driver only verifies loop input, kelvin units and the physical loop-enable
state during `open()`. It does not change CSET, PID, heater range, output mode
or power-up settings.

The Model 340 `old reading` status alone retains its numeric value; the manual
documents that status on every other reading for some thermally compensated
sensors. Other input-status flags are fatal only when that reading belongs to an
enabled controller whose role is `sample_temp` or `field`. Other displayed
readings are returned as unavailable, so an unused or out-of-range auxiliary
sensor does not disconnect the whole physical instance. `hold()` remains
stricter: every active control-loop input must have a usable reading before any
setpoint is written.

Install this folder as `system_instruments/lakeshore340`, then select the GPIB
resource in Instrument Scanner. The template defines one `control` panel, plus
`temperatures`, `loop_1`, and `loop_2` readout grids. They all share one physical
instance and one GPIB session. The final preview can enable/disable and globally
reorder these fixed panels; it cannot create extra panels.

The single controller can use `temp_a` or `temp_b` as its reading. Scanner also
collects `loop1_input`, `loop2_input`, `loop2_enabled`, and `visa_timeout_ms`
from the manifest defaults. `visa_timeout_ms` is limited to 1–9999 ms so it
remains shorter than the core's 10-second instrument-operation timeout. The
chosen controller reading must belong to exactly one enabled physical control
loop.

`loop2_enabled = false` prevents Loop 2 queries and writes. Disabling only the
`loop_2` readout panel hides that panel but does not change physical control
behavior. Neither setting sends a CSET command. Confirm all safety limits and
stability values in the scanner preview for the actual cryostat before saving.
