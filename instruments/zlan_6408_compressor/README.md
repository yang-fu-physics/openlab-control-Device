# ZLAN 6408 Compressor I/O

This System Instrument connects to a ZLAN 6408I over Modbus TCP. `Compressor On`
closes DO1 and `Compressor Off` opens DO1. The status panel and
`instrument_status.dat` report DI1 as `1` (On) or `0` (Off); a successful DO1 write
does not override or predict the DI1 status.

ZLAN does not participate in generic VISA or TCP resource discovery. Its
`instrument.toml` declares `host`, `port`, I/O timeout, and Modbus unit ID as
instrument-specific fields. Instrument Scanner presents them on the ZLAN page and
writes them into the generated instance configuration:

```toml
host = "192.168.1.20"
port = 502
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
