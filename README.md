# mbmeister
A Modbus TCP client for accessing modbus devices, using a simple Tkinter UI.
Reads input registers and read/writes holding registers.
The register layout is configurable with a .csv configuration file.

A sample configuration for AISWEI based Solar inverters is added as example configuration.

### Requirements
pip install "pymodbus>=3.5"

### Usage
python mbmeister.py [registers.csv] [--host HOST_NAME] [--port PORT] [--unit UNIT_NUMBER]

### Configuration
Config file format (CSV, delimiter "," or ";" is auto-detected, "#" starts a comment line)

```
register;area;description;type;unit;factor;length;enum
100;IR;Grid voltage;U16;V;0.1;;
101;IR;Status flags;B16;;;;0=Running|1=Fault|2=Warning
200;HR;Setpoint power;S32;W;1;;
210;HR;Operating mode;E16;;;;0=Off|1=Manual|2=Auto
220;HR;Device name;String;;;8;
```

|Entry      |Description  |
| ---       | ----         |
|register    |Modbus address as sent on the wire (see "Offset" field in the UI for 1-based docs)   |
|area        |IR = input registers (read only), HR = holding registers (read/write). Default: HR |
|description |free text |
|type        |B16/B32 bitfield, S16/S32 signed, U16/U32 unsigned, E16 enum code, String |
|unit        |free text, e.g. V, A, W (optional) |
|factor      |multiplication factor for S/U types (optional, default 1) |
|length      |number of registers for String (optional, default 8 = 16 characters) |
|enum        |optional, "code=text" pairs separated by "\|"; used for E16 (code names) and B16/B32 (bit number names) |

### Writing
The tool can write to holding registers (`HR` as area). New values can be staged or written directly.

In case of failure not all staged values might be written.

### screenshot
with exmaple configuration
<img width="1015" height="667" alt="grafik" src="https://github.com/user-attachments/assets/6b052a59-ba2d-461b-b0e2-8e8b6f0ef130" />
