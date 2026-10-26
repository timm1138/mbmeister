#!/usr/bin/env python3
"""
MBMeister - Modbus TCP register tool
========================
Simple Tkinter UI to read input registers and read/write holding registers of a
Modbus TCP device. Registers are described in a CSV configuration file.

Requirements:  pip install "pymodbus>=3.5"
Usage:         python mbmeister.py [registers.csv] [--host 192.168.0.10] [--port 502] [--unit 1]

Config file format (CSV, delimiter "," or ";" is auto-detected, "#" starts a comment line)
-------------------------------------------------------------------------------------------
register;area;description;type;unit;factor;length;enum
100;IR;Grid voltage;U16;V;0.1;;
101;IR;Status flags;B16;;;;0=Running|1=Fault|2=Warning
200;HR;Setpoint power;S32;W;1;;
210;HR;Operating mode;E16;;;;0=Off|1=Manual|2=Auto
220;HR;Device name;String;;;8;

register    Modbus address as sent on the wire (see "Offset" field in the UI for 1-based docs)
area        IR = input registers (read only), HR = holding registers (read/write). Default: HR
description free text
type        B16/B32 bitfield, S16/S32 signed, U16/U32 unsigned, E16 enum code, String
unit        free text, e.g. V, A, W (optional)
factor      multiplication factor for S/U types (optional, default 1)
length      number of registers for String (optional, default 8 = 16 characters)
enum        optional, "code=text" pairs separated by "|"; used for E16 (code names)
            and B16/B32 (bit number names)
"""

import argparse
import csv
import queue
import re
import threading
import tkinter as tk
from dataclasses import dataclass, field
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from pymodbus.client import ModbusTcpClient

MAX_BLOCK = 120  # max registers per read request (Modbus limit is 125)

TYPE_WORDS = {"B16": 1, "B32": 2, "S16": 1, "S32": 2, "U16": 1, "U32": 2, "E16": 1}
AREAS = {"IR": "IR", "INPUT": "IR", "3": "IR", "HR": "HR", "HOLDING": "HR", "4": "HR", "": "HR"}


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
class ConfigError(Exception):
    pass


@dataclass
class RegisterDef:
    address: int
    area: str  # "IR" | "HR"
    description: str
    type: str  # B16 B32 S16 S32 U16 U32 E16 STRING
    unit: str = ""
    factor: float = 1.0
    length: int = 8  # only for STRING
    enum: dict = field(default_factory=dict)

    @property
    def words(self) -> int:
        return self.length if self.type == "STRING" else TYPE_WORDS[self.type]

    @property
    def writable(self) -> bool:
        return self.area == "HR"


def parse_enum(text: str) -> dict:
    result = {}
    for part in text.split("|"):
        part = part.strip()
        if not part:
            continue
        if "=" not in part:
            raise ValueError(f"bad enum entry '{part}' (expected code=text)")
        code, label = part.split("=", 1)
        result[int(code.strip(), 0)] = label.strip()
    return result


def load_config(path) -> list:
    text = Path(path).read_text(encoding="utf-8-sig")
    lines = [(n, l) for n, l in enumerate(text.splitlines(), 1)
             if l.strip() and not l.lstrip().startswith("#")]
    if not lines:
        raise ConfigError("Configuration file is empty")

    header_line = lines[0][1]
    delim = ";" if header_line.count(";") > header_line.count(",") else ","
    rows = list(csv.reader([l for _, l in lines], delimiter=delim))
    header = [h.strip().lower() for h in rows[0]]
    aliases = {"reg": "register", "address": "register", "addr": "register",
               "desc": "description", "multiplier": "factor", "scale": "factor",
               "len": "length", "enums": "enum", "codes": "enum"}
    header = [aliases.get(h, h) for h in header]
    for required in ("register", "description", "type"):
        if required not in header:
            raise ConfigError(f"Missing column '{required}' in header: {rows[0]}")

    entries = []
    for (lineno, _), row in zip(lines[1:], rows[1:]):
        cell = {h: (row[i].strip() if i < len(row) else "") for i, h in enumerate(header)}
        try:
            rtype = cell["type"].upper()
            if rtype in ("STR", "STRING"):
                rtype = "STRING"
            elif rtype not in TYPE_WORDS:
                raise ValueError(f"unknown type '{cell['type']}'")
            area = AREAS.get(cell.get("area", "").upper())
            if area is None:
                raise ValueError(f"unknown area '{cell.get('area')}' (use IR or HR)")
            factor = float(cell.get("factor", "").replace(",", ".") or 1)
            length = int(cell.get("length", "") or 8)
            if length < 1 or length > MAX_BLOCK:
                raise ValueError("length out of range")
            entries.append(RegisterDef(
                address=int(cell["register"], 0),
                area=area,
                description=cell["description"],
                type=rtype,
                unit=cell.get("unit", ""),
                factor=factor,
                length=length,
                enum=parse_enum(cell.get("enum", "")),
            ))
        except (ValueError, KeyError) as exc:
            raise ConfigError(f"Line {lineno}: {exc}") from exc
    return entries


# --------------------------------------------------------------------------- #
# Encoding / decoding
# --------------------------------------------------------------------------- #
def decode_raw(e: RegisterDef, regs: list, low_word_first: bool):
    """Return int for numeric types, str for String."""
    if e.type == "STRING":
        data = b"".join(r.to_bytes(2, "big") for r in regs)
        return data.split(b"\x00", 1)[0].decode("ascii", "replace").rstrip()
    if e.words == 2:
        hi, lo = (regs[1], regs[0]) if low_word_first else (regs[0], regs[1])
        v = (hi << 16) | lo
    else:
        v = regs[0]
    if e.type.startswith("S"):
        bits = 16 * e.words
        if v >= 1 << (bits - 1):
            v -= 1 << bits
    return v


def format_raw(e: RegisterDef, v, regs: list) -> str:
    if e.type == "STRING":
        return " ".join(f"{r:04X}" for r in regs)
    if e.type.startswith("B"):
        bits = 16 * e.words
        b = format(v, f"0{bits}b")
        grouped = " ".join(b[i:i + 4] for i in range(0, bits, 4))
        return f"0x{v:0{e.words * 4}X}  {grouped}"
    return str(v)


def format_value(e: RegisterDef, v) -> str:
    if e.type == "STRING":
        return v
    if e.type.startswith("B"):
        names = []
        for bit in range(16 * e.words):
            if v >> bit & 1:
                names.append(e.enum.get(bit, f"bit {bit}"))
        return ", ".join(names) if names else "-"
    if e.type == "E16":
        return e.enum.get(v, f"unknown ({v})")
    return f"{v * e.factor:.10g}"


def edit_text(e: RegisterDef, v) -> str:
    if e.type == "STRING":
        return v
    if e.type.startswith("B"):
        return f"0x{v:0{e.words * 4}X}"
    if e.type == "E16":
        return f"{v} = {e.enum[v]}" if v in e.enum else str(v)
    return f"{v * e.factor:.10g}"


def describe(e: RegisterDef, regs: list, low_word_first: bool) -> str:
    """Human readable value incl. raw value, for confirmation dialogs."""
    v = decode_raw(e, regs, low_word_first)
    if e.type == "STRING":
        return f'"{v}"'
    if e.type == "E16":
        return f"{format_value(e, v)} (code {v})"
    if e.type.startswith("B"):
        return f"0x{v:0{e.words * 4}X} [{format_value(e, v)}]"
    unit = f" {e.unit}" if e.unit else ""
    return f"{format_value(e, v)}{unit} (raw {v})"


def parse_input(e: RegisterDef, text: str, raw_mode: bool):
    t = text.strip()
    if e.type == "STRING":
        return text
    if e.type.startswith("B"):
        return int(t, 0)
    if e.type == "E16":
        try:
            return int(t.split("=")[0].strip(), 0)
        except ValueError:
            for code, label in e.enum.items():
                if label.lower() == t.lower():
                    return code
            raise ValueError(f"'{t}' is not a valid code")
    if raw_mode:
        return int(t, 0)
    return int(round(float(t.replace(",", ".")) / e.factor))


def encode(e: RegisterDef, val, low_word_first: bool) -> list:
    if e.type == "STRING":
        try:
            data = val.encode("ascii")
        except UnicodeEncodeError:
            raise ValueError("only ASCII characters are supported")
        if len(data) > 2 * e.length:
            raise ValueError(f"string too long (max {2 * e.length} characters)")
        data = data.ljust(2 * e.length, b"\x00")
        return [int.from_bytes(data[i:i + 2], "big") for i in range(0, len(data), 2)]
    bits = 16 * e.words
    if e.type.startswith("S"):
        lo, hi = -(1 << (bits - 1)), (1 << (bits - 1)) - 1
    else:
        lo, hi = 0, (1 << bits) - 1
    if not lo <= val <= hi:
        raise ValueError(f"raw value {val} out of range ({lo} .. {hi})")
    v = val & ((1 << bits) - 1)
    if e.words == 1:
        return [v]
    hi_w, lo_w = v >> 16, v & 0xFFFF
    return [lo_w, hi_w] if low_word_first else [hi_w, lo_w]


# --------------------------------------------------------------------------- #
# Modbus worker thread (keeps the UI responsive)
# --------------------------------------------------------------------------- #
def plan_blocks(items):
    """Merge contiguous registers of the same area into read blocks."""
    blocks = []
    for area in ("IR", "HR"):
        sel = sorted(((i, e) for i, e in items if e.area == area),
                     key=lambda x: x[1].address)
        cur = None
        for i, e in sel:
            end = e.address + e.words
            if cur and e.address <= cur["end"] and max(end, cur["end"]) - cur["start"] <= MAX_BLOCK:
                cur["end"] = max(cur["end"], end)
                cur["items"].append((i, e))
            else:
                cur = {"area": area, "start": e.address, "end": end, "items": [(i, e)]}
                blocks.append(cur)
    return blocks


class ModbusWorker(threading.Thread):
    def __init__(self, ui_queue):
        super().__init__(daemon=True)
        self.tasks = queue.Queue()
        self.ui = ui_queue
        self.client = None
        self.unit = 1

    # pymodbus renamed the unit-id keyword several times (unit -> slave -> device_id)
    def _call(self, fn, *args, **kw):
        for key in ("device_id", "slave", "unit"):
            try:
                return fn(*args, **kw, **{key: self.unit})
            except TypeError:
                continue
        raise RuntimeError("Unsupported pymodbus version")

    def _read_regs(self, area, address, count):
        fn = self.client.read_input_registers if area == "IR" else self.client.read_holding_registers
        rr = self._call(fn, address, count=count)
        if rr.isError():
            raise IOError(str(rr))
        return list(rr.registers)

    def run(self):
        while True:
            task = self.tasks.get()
            kind = task[0]
            try:
                if kind == "quit":
                    if self.client:
                        self.client.close()
                    return
                elif kind == "connect":
                    _, host, port, unit = task
                    self.unit = unit
                    if self.client:
                        self.client.close()
                    self.client = ModbusTcpClient(host, port=port, timeout=3)
                    ok = self.client.connect()
                    self.ui.put(("connected", ok, f"Connected to {host}:{port}" if ok
                                 else f"Could not connect to {host}:{port}"))
                elif kind == "disconnect":
                    if self.client:
                        self.client.close()
                    self.ui.put(("connected", False, "Disconnected"))
                elif kind == "read":
                    self._do_read(task[1], task[2])
                elif kind == "write_many":
                    self._do_write_many(task[1], task[2])
            except Exception as exc:  # noqa: BLE001
                if kind == "read":
                    self.ui.put(("values", {i: (None, str(exc)) for i, _ in task[1]}))
                elif kind == "write_many":
                    self.ui.put(("write_result", [(i, False, str(exc)) for i, _, _ in task[1]]))
                else:
                    self.ui.put(("connected", False, f"Error: {exc}"))

    def _do_read(self, items, offset):
        if not self.client or not self.client.connected:
            raise IOError("Not connected")
        results = {}
        for blk in plan_blocks(items):
            start, end = blk["start"], blk["end"]
            try:
                if start + offset < 0:
                    raise IOError("negative address (check offset)")
                regs = self._read_regs(blk["area"], start + offset, end - start)
                for i, e in blk["items"]:
                    results[i] = (regs[e.address - start:e.address - start + e.words], None)
            except Exception:  # noqa: BLE001  -> fall back to single reads
                for i, e in blk["items"]:
                    try:
                        if e.address + offset < 0:
                            raise IOError("negative address (check offset)")
                        results[i] = (self._read_regs(e.area, e.address + offset, e.words), None)
                    except Exception as exc2:  # noqa: BLE001
                        results[i] = (None, str(exc2))
        self.ui.put(("values", results))

    def _do_write_many(self, items, offset):
        """items: list of (idx, entry, regs). Stops at the first error."""
        if not self.client or not self.client.connected:
            raise IOError("Not connected")
        results = []
        for idx, entry, regs in items:
            try:
                addr = entry.address + offset
                if addr < 0:
                    raise IOError("negative address (check offset)")
                if len(regs) == 1:
                    rr = self._call(self.client.write_register, addr, regs[0])
                else:
                    rr = self._call(self.client.write_registers, addr, regs)
                if rr.isError():
                    raise IOError(str(rr))
                results.append((idx, True, f"Wrote register {entry.address}"))
            except Exception as exc:  # noqa: BLE001
                results.append((idx, False, f"Register {entry.address} ({entry.description}): {exc}"))
                break
        self.ui.put(("write_result", results))


# --------------------------------------------------------------------------- #
# UI
# --------------------------------------------------------------------------- #
class WriteDialog(tk.Toplevel):
    def __init__(self, parent, entry: RegisterDef, current, low_word_first, staged=False):
        super().__init__(parent)
        self.title("Write holding register")
        self.transient(parent)
        self.resizable(False, False)
        self.e, self.low = entry, low_word_first
        self.result = None   # register words
        self.action = None   # "write" | "stage" | "unstage"

        ttk.Label(self, text=f"{entry.description}\nRegister {entry.address}  ({entry.type})",
                  justify="left").grid(row=0, column=0, columnspan=2, padx=10, pady=(10, 5), sticky="w")

        self.text = tk.StringVar(value=edit_text(entry, current) if current is not None else "")
        if entry.type == "E16" and entry.enum:
            w = ttk.Combobox(self, textvariable=self.text, width=34,
                             values=[f"{k} = {v}" for k, v in sorted(entry.enum.items())])
        else:
            w = ttk.Entry(self, textvariable=self.text, width=36)
        w.grid(row=1, column=0, columnspan=2, padx=10, pady=5)
        w.focus_set()

        self.raw_mode = tk.BooleanVar(value=False)
        hint = {"STRING": "ASCII text", "E16": "Code number or name"}.get(
            entry.type, "Bit pattern: 0x1F, 0b1010 or decimal" if entry.type.startswith("B") else "")
        if entry.type[0] in "SU":
            ttk.Radiobutton(self, text=f"Calculated value{' [' + entry.unit + ']' if entry.unit else ''}",
                            variable=self.raw_mode, value=False).grid(row=2, column=0, columnspan=2, padx=10, sticky="w")
            ttk.Radiobutton(self, text="Raw value", variable=self.raw_mode,
                            value=True).grid(row=3, column=0, columnspan=2, padx=10, sticky="w")
        elif hint:
            ttk.Label(self, text=hint, foreground="gray").grid(row=2, column=0, columnspan=2, padx=10, sticky="w")

        bf = ttk.Frame(self)
        bf.grid(row=4, column=0, columnspan=2, pady=10)
        ttk.Button(bf, text="Write now…", command=lambda: self._finish("write")).pack(side="left", padx=5)
        ttk.Button(bf, text="Stage", command=lambda: self._finish("stage")).pack(side="left", padx=5)
        if staged:
            ttk.Button(bf, text="Unstage", command=self._unstage).pack(side="left", padx=5)
        ttk.Button(bf, text="Cancel", command=self.destroy).pack(side="left", padx=5)
        # Enter stages (safe default); writing to the device needs an explicit click
        self.bind("<Return>", lambda _e: self._finish("stage"))
        self.bind("<Escape>", lambda _e: self.destroy())
        self.grab_set()

    def _finish(self, action):
        try:
            val = parse_input(self.e, self.text.get(), self.raw_mode.get())
            self.result = encode(self.e, val, self.low)
        except ValueError as exc:
            messagebox.showerror("Invalid input", str(exc), parent=self)
            return
        self.action = action
        self.destroy()

    def _unstage(self):
        self.action = "unstage"
        self.destroy()


class App(tk.Tk):
    COLUMNS = (("addr", "Register", 70), ("area", "Area", 45), ("desc", "Description", 260),
               ("type", "Type", 60), ("raw", "Raw", 220), ("value", "Value", 200),
               ("staged", "Staged (not yet written)", 170), ("unit", "Unit", 60))

    def __init__(self, args):
        super().__init__()
        self.title("MBMeister – Modbus TCP Register Tool")
        self.geometry("1280x640")

        self.entries: list = []
        self.regs: dict = {}      # idx -> list of registers
        self.errors: dict = {}    # idx -> error text
        self.config_path = None
        self.connected = False
        self.read_pending = False
        self.reread = False      # re-read once the running read cycle is done
        self.staged: dict = {}   # idx -> list of register words waiting to be written

        self.ui_q = queue.Queue()
        self.worker = ModbusWorker(self.ui_q)
        self.worker.start()

        self.host = tk.StringVar(value=args.host)
        self.port = tk.StringVar(value=str(args.port))
        self.unit = tk.StringVar(value=str(args.unit))
        self.offset = tk.StringVar(value="0")
        self.order = tk.StringVar(value="High word first")
        self.auto = tk.BooleanVar(value=False)
        self.interval = tk.StringVar(value="1.0")
        self.status = tk.StringVar(value="Not connected")
        self.cfg_label = tk.StringVar(value="No configuration loaded")

        self._build()
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.after(100, self._pump)

        if args.config:
            self.after(50, lambda: self.load(args.config))

    # ---- layout ---------------------------------------------------------- #
    def _build(self):
        top = ttk.LabelFrame(self, text="Connection")
        top.pack(fill="x", padx=8, pady=(8, 4))
        for label, var, w in (("Host", self.host, 16), ("Port", self.port, 6), ("Unit ID", self.unit, 4),
                              ("Offset", self.offset, 4)):
            ttk.Label(top, text=label).pack(side="left", padx=(8, 2), pady=6)
            ttk.Entry(top, textvariable=var, width=w).pack(side="left")
        ttk.Label(top, text="32-bit order").pack(side="left", padx=(8, 2))
        cb = ttk.Combobox(top, textvariable=self.order, state="readonly", width=15,
                          values=["High word first", "Low word first"])
        cb.pack(side="left")
        cb.bind("<<ComboboxSelected>>", lambda _e: self._refresh_all())
        self.btn_connect = ttk.Button(top, text="Connect", command=self._toggle_connect)
        self.btn_connect.pack(side="left", padx=12)

        bar = ttk.Frame(self)
        bar.pack(fill="x", padx=8, pady=4)
        ttk.Button(bar, text="Load config…", command=self._choose_config).pack(side="left")
        ttk.Button(bar, text="Reload", command=lambda: self.config_path and self.load(self.config_path)).pack(side="left", padx=4)
        ttk.Label(bar, textvariable=self.cfg_label, foreground="gray").pack(side="left", padx=8)
        self.btn_apply = ttk.Button(bar, text="Apply changes (0)", command=self.apply_staged, state="disabled")
        self.btn_apply.pack(side="right")
        self.btn_discard = ttk.Button(bar, text="Discard changes", command=self.discard_staged, state="disabled")
        self.btn_discard.pack(side="right", padx=4)
        ttk.Button(bar, text="Edit selected…", command=self._write_selected).pack(side="right")
        ttk.Button(bar, text="Read all", command=self.read_all).pack(side="right", padx=4)
        ttk.Entry(bar, textvariable=self.interval, width=5).pack(side="right")
        ttk.Checkbutton(bar, text="Auto refresh every (s):", variable=self.auto,
                        command=self.read_all).pack(side="right", padx=(0, 4))

        frame = ttk.Frame(self)
        frame.pack(fill="both", expand=True, padx=8, pady=4)
        self.tree = ttk.Treeview(frame, columns=[c[0] for c in self.COLUMNS], show="headings",
                                 selectmode="browse")
        for key, title, width in self.COLUMNS:
            self.tree.heading(key, text=title)
            self.tree.column(key, width=width, anchor="w", stretch=key in ("desc", "raw", "value"))
        sb = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.tree.tag_configure("err", foreground="#b00020")
        self.tree.tag_configure("ro", foreground="#444444")
        self.tree.tag_configure("staged", background="#ffe58a", foreground="#000000")
        self.tree.bind("<Double-1>", self._on_double_click)

        ttk.Label(self, textvariable=self.status, relief="sunken", anchor="w").pack(fill="x", side="bottom")
        ttk.Label(self, text="Double-click a holding register (HR) row to edit it: 'Stage' collects the change "
                             "(highlighted), 'Apply changes' writes all staged values; 'Write now…' writes a single value.",
                  foreground="gray").pack(fill="x", side="bottom", padx=8)

    # ---- config ---------------------------------------------------------- #
    def _choose_config(self):
        path = filedialog.askopenfilename(title="Register configuration",
                                          filetypes=[("CSV files", "*.csv *.txt"), ("All files", "*.*")])
        if path:
            self.load(path)

    def load(self, path):
        try:
            entries = load_config(path)
        except (ConfigError, OSError) as exc:
            messagebox.showerror("Configuration error", str(exc))
            return
        if self.staged and not messagebox.askokcancel(
                "Discard staged changes?",
                f"{len(self.staged)} staged change(s) have not been written and will be discarded."):
            return
        self.staged = {}
        self._update_staged_buttons()
        self.entries, self.regs, self.errors = entries, {}, {}
        self.config_path = path
        self.cfg_label.set(f"{Path(path).name}  ({len(entries)} registers)")
        self.tree.delete(*self.tree.get_children())
        for i, e in enumerate(entries):
            self.tree.insert("", "end", iid=str(i), values=("",) * len(self.COLUMNS))
            self._refresh_row(i)
        self.status.set(f"Loaded {len(entries)} registers")
        self.read_all()

    # ---- helpers --------------------------------------------------------- #
    def _low_first(self):
        return self.order.get().startswith("Low")

    def _offset(self):
        try:
            return int(self.offset.get(), 0)
        except ValueError:
            return 0

    def _refresh_row(self, i):
        e = self.entries[i]
        tags = ["ro"] if not e.writable else []
        raw = value = staged = ""
        if i in self.errors:
            value = f"ERROR: {self.errors[i]}"[:120]
            tags.append("err")
        elif i in self.regs:
            regs = self.regs[i]
            v = decode_raw(e, regs, self._low_first())
            raw, value = format_raw(e, v, regs), format_value(e, v)
        if i in self.staged:
            sv = decode_raw(e, self.staged[i], self._low_first())
            staged = f"→ {format_value(e, sv)}"
            tags.append("staged")
        self.tree.item(str(i), values=(e.address, e.area, e.description, e.type, raw, value, staged, e.unit),
                       tags=tags)

    def _refresh_all(self):
        for i in range(len(self.entries)):
            self._refresh_row(i)

    # ---- actions --------------------------------------------------------- #
    def _toggle_connect(self):
        if self.connected:
            self.auto.set(False)
            self.worker.tasks.put(("disconnect",))
            return
        try:
            host, port, unit = self.host.get().strip(), int(self.port.get()), int(self.unit.get())
        except ValueError:
            messagebox.showerror("Connection", "Port and Unit ID must be numbers")
            return
        self.btn_connect.config(state="disabled")
        self.status.set(f"Connecting to {host}:{port} …")
        self.worker.tasks.put(("connect", host, port, unit))

    def read_all(self):
        if not self.connected or self.read_pending or not self.entries:
            return
        self.read_pending = True
        self.worker.tasks.put(("read", list(enumerate(self.entries)), self._offset()))

    def _on_double_click(self, event):
        row = self.tree.identify_row(event.y)
        if row:
            self._write(int(row))

    def _write_selected(self):
        sel = self.tree.selection()
        if sel:
            self._write(int(sel[0]))

    def _write(self, i):
        e = self.entries[i]
        if not e.writable:
            messagebox.showinfo("Read only", "Input registers (IR) are read only.")
            return
        low = self._low_first()
        cur_regs = self.staged.get(i) or self.regs.get(i)
        current = decode_raw(e, cur_regs, low) if cur_regs else None
        dlg = WriteDialog(self, e, current, low, staged=i in self.staged)
        self.wait_window(dlg)
        if dlg.action == "stage":
            self.staged[i] = dlg.result
            self._refresh_row(i)
            self._update_staged_buttons()
            self.status.set(f"Staged register {e.address} ({len(self.staged)} pending change(s))")
        elif dlg.action == "unstage":
            self.staged.pop(i, None)
            self._refresh_row(i)
            self._update_staged_buttons()
            self.status.set(f"Unstaged register {e.address}")
        elif dlg.action == "write":
            self._send([(i, dlg.result)])

    # ---- staged changes -------------------------------------------------- #
    def _update_staged_buttons(self):
        n = len(self.staged)
        self.btn_apply.config(text=f"Apply changes ({n})", state="normal" if n else "disabled")
        self.btn_discard.config(state="normal" if n else "disabled")

    def apply_staged(self):
        if self.staged:
            self._send(sorted(self.staged.items()))

    def discard_staged(self):
        if not self.staged:
            return
        if not messagebox.askokcancel("Discard changes", f"Discard {len(self.staged)} staged change(s)?"):
            return
        old = list(self.staged)
        self.staged.clear()
        for i in old:
            self._refresh_row(i)
        self._update_staged_buttons()
        self.status.set("Staged changes discarded")

    def _confirm_write(self, items):
        low = self._low_first()
        lines = []
        for i, regs in items[:15]:
            e = self.entries[i]
            old = describe(e, self.regs[i], low) if i in self.regs else "unknown"
            lines.append(f"• HR {e.address}  {e.description}\n      {old}  →  {describe(e, regs, low)}")
        if len(items) > 15:
            lines.append(f"… and {len(items) - 15} more")
        text = (f"Write {len(items)} value(s) to the device "
                f"{self.host.get()}:{self.port.get()} (unit {self.unit.get()})?\n\n" + "\n".join(lines))
        return messagebox.askokcancel("Confirm write", text, icon="warning", parent=self)

    def _send(self, items):
        """items: list of (idx, regs). Asks for confirmation, then writes to the device."""
        if not self.connected:
            messagebox.showwarning("Not connected", "Connect to a device first.")
            return
        if not self._confirm_write(items):
            return
        self.status.set(f"Writing {len(items)} register(s) …")
        self.worker.tasks.put(("write_many", [(i, self.entries[i], regs) for i, regs in items],
                               self._offset()))

    # ---- worker messages ------------------------------------------------- #
    def _pump(self):
        try:
            while True:
                msg = self.ui_q.get_nowait()
                kind = msg[0]
                if kind == "connected":
                    _, ok, text = msg
                    self.connected = ok
                    self.read_pending = False
                    self.btn_connect.config(text="Disconnect" if ok else "Connect", state="normal")
                    self.status.set(text)
                    if ok:
                        self.read_all()
                elif kind == "values":
                    for i, (regs, err) in msg[1].items():
                        if err:
                            self.errors[i] = err
                            self.regs.pop(i, None)
                        else:
                            self.regs[i] = regs
                            self.errors.pop(i, None)
                        self._refresh_row(i)
                    self.read_pending = False
                    n_err = sum(1 for _, (_, err) in msg[1].items() if err)
                    self.status.set("Read OK" if not n_err else f"Read finished, {n_err} register(s) failed")
                    if self.reread:
                        self.reread = False
                        self.read_all()
                    if self.auto.get() and self.connected:
                        try:
                            ms = max(100, int(float(self.interval.get().replace(",", ".")) * 1000))
                        except ValueError:
                            ms = 1000
                        self.after(ms, self.read_all)
                elif kind == "write_result":
                    ok_n, failed = 0, None
                    for i, ok, text in msg[1]:
                        if ok:
                            ok_n += 1
                            self.staged.pop(i, None)
                            self._refresh_row(i)
                        elif failed is None:
                            failed = text
                    self._update_staged_buttons()
                    if failed:
                        self.status.set(f"Write failed: {failed}")
                        extra = f"\n\n{ok_n} register(s) were written before the error." if ok_n else ""
                        messagebox.showerror("Write failed", failed + extra)
                    else:
                        self.status.set(f"Wrote {ok_n} register(s)")
                    if self.read_pending:
                        self.reread = True
                    else:
                        self.read_all()
        except queue.Empty:
            pass
        self.after(100, self._pump)

    def _close(self):
        if self.staged and not messagebox.askokcancel(
                "Quit", f"{len(self.staged)} staged change(s) have not been written. Quit anyway?"):
            return
        self.worker.tasks.put(("quit",))
        self.destroy()


def main():
    p = argparse.ArgumentParser(prog="mbmeister", description="MBMeister - Modbus TCP register tool")
    p.add_argument("config", nargs="?", help="register configuration file (CSV)")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=502)
    p.add_argument("--unit", type=int, default=1)
    App(p.parse_args()).mainloop()


if __name__ == "__main__":
    main()
