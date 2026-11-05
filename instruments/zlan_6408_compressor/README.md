# ZLAN Compressor I/O

This System Instrument controls either a four-output ZLAN 6408I or an eight-output
ZLAN 6842 over Modbus TCP. Add DI and DO channels to the same **Status Conditions**
list in the scanner. The status is On only when every selected bit is 1,
Off if any bit is 0, and blank when the list is empty. A failed read is a
communication error, not Off. The condition
is used for display and logging, never as an interlock or a condition on a write.
DO readback alone does not indicate that the compressor is running.
For the four-channel model, choose only channels 1–4. The driver rejects
out-of-range conditions before contacting the instrument.

ZLAN does not participate in generic VISA or TCP resource discovery. Its
`instrument.toml` declares `host`, `port`, I/O timeout, and Modbus unit ID as
instrument-specific fields. Instrument Scanner presents them on the ZLAN page and
writes them into the generated instance configuration. Select the physical
output count, then add/reorder the two independent step lists. Use DO1 in both
lists for a four-output unit wired to DO1. For an eight-output unit wired to
DO7 and DO8, an example configuration is:

```toml
host = "192.168.1.20"
port = 502
io_timeout_seconds = 0.5
unit_id = 1
output_count = "8"
on_channels = ["DO7", "DO8"]
off_channels = ["DO8", "DO7"]
step_delay_seconds = 0.3
status_conditions = ["DI1", "DO7"]
```

The ZLAN network module must be configured for Modbus TCP. The manual's standard
server port is 502. `unit_id` is the Modbus station address and defaults to 1. Before
use, turn off unsolicited reports and verify that writes return their echo.
Check that no device-side hold timer or DI-to-DO mapping can override these actions.
The fixed delay is applied between steps, never after the last step. If a write
cannot be confirmed, its connection is closed and the next step uses a fresh one;
the failed write is not retried or rolled back. After attempting the remaining
steps, the action reports any unconfirmed steps as an error. A sequence whose
I/O timeouts and delays do not fit the framework's operation budget is rejected
before connecting with `INSTRUMENT_IO_TIMEOUT_TOO_HIGH`. Reduce the I/O timeout
or step delay shown in the error; do not treat this as a network failure.

Scanning, connecting, polling and closing OpenLab Control never write DOs.
This driver works with the existing core v0.20.4.dev3 (or newer below v0.21).
Copy the `zlan_6408_compressor/` folder into `system_instruments/`. If upgrading
from the two-list version, select the desired status conditions again and save
with the existing scanner. Validate the configured order
without the compressor load connected before using it on the equipment.
