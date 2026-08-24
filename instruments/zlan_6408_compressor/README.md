# ZLAN 6408 Compressor I/O

This System Instrument connects to a ZLAN 6408I over Modbus TCP. `Compressor On`
closes DO1 and `Compressor Off` opens DO1. The status panel and
`instrument_status.dat` report DI1 as `1` (On) or `0` (Off); a successful DO1 write
does not override or predict the DI1 status.

Use Instrument Scanner to add the TCP address and port, then select
`ZLAN 6408 Compressor I/O`. A typical resource entry is:

```toml
[[resources]]
id = "compressor_io"
address = "tcp://192.168.1.20:502"
identity = ""
purpose = "system"
system_instrument = "zlan_6408_compressor"
auxiliary_readings = []
```

Add the logical instrument to `configs/site.local.toml`:

```toml
[[instruments]]
id = "compressor"
display_name = "Compressor"
kind = "monitor"
resource = "compressor_io"
control_enabled = false
operation_timeout_seconds = 3.0
shutdown_timeout_seconds = 1.0
io_timeout_seconds = 1.0
unit_id = 1
```

The ZLAN network module must be configured for Modbus TCP. The manual's standard
server port is 502. `unit_id` is the Modbus station address and defaults to 1. Before
use, also confirm that DI active reporting, DO hold time, and "write DO without a
reply" are disabled. Wire/configure DI1 so a returned bit value of 1 means the
compressor is on.
Connection and polling never change DO1. Closing OpenLab Control only closes the TCP
connection. A write timeout or mismatched reply is not retried because the relay may
already have changed.
