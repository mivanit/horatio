"""Hardware sensors (temperatures, fans, power) read straight from Linux hwmon sysfs.

Every file read here is world-readable, so the unprivileged dashboard does this itself;
the root collectors keep only what needs root (IPMI, RAPL energy counters, SMART).
This replaces Glances' sensors plugin. Glances is psutil underneath, and psutil:

- drops a chip's fans when its attributes live under ``hwmonN/device/`` (old drivers
  such as f71882fg) as soon as any other chip has a fan in ``hwmonN/``;
- has no power readings at all;
- has no fault or enable checks, so a disconnected input shows as -128 °C.

Rows keep Glances' JSON shape (``type``, ``label``, ``value``, ``unit``, ``warning``,
``critical``), so the frontend's existing ``/api/4/sensors`` path, the OpenWrt Lua
backend (which emits the same shape) and the history recorder all read one format.

Temperature labels reproduce Glances' naming exactly (``f71889ed 0``, ``Core 0``,
``Package id 0``, a duplicate label gets `` 1``, `` 2``...), because history records and
``temp_labels`` / ``temp_thresholds`` are keyed by them. Fans and power had no Glances
history to stay compatible with, so they get names that do not move: the kernel's own
label when it has one, else ``<chip> fan<N>`` / ``<chip> power<N>`` with the sysfs
number. Power from drivers whose meaning is known gets a readable name instead (e.g.
``fam15h_power`` is ``CPU (est.)``).

Pass a fake tree as ``root`` / ``thermal_root`` in tests.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Final

HWMON_ROOT: Final[str] = "/sys/class/hwmon"
THERMAL_ROOT: Final[str] = "/sys/class/thermal"

TEMP: Final[str] = "temperature_core"  # Glances' type names, kept for the frontend
FAN: Final[str] = "fan_speed"
POWER: Final[str] = "power"

# A reading outside this range is a disconnected or faulty input (nct6775 AUXTIN pins
# read -128 or 127.5, some chips 255), never a real component temperature.
TEMP_PLAUSIBLE: Final[tuple[float, float]] = (-40.0, 200.0)

# Power drivers whose meaning is known. `total` = the whole machine's draw (the power
# bar shows the parts against it); everything else is one part of the machine.
POWER_DRIVERS: Final[dict[str, dict[str, Any]]] = {
    # AMD family 15h/16h: the CPU's own estimate of processor power (APM), not a
    # measurement. Its power1_crit is the TDP.
    "fam15h_power": {"label": "CPU (est.)", "total": False},
    # The ACPI power meter on servers measures what the box draws from the wall.
    "acpi_power_meter": {"label": "System (ACPI)", "total": True},
    # amdgpu board power; NVIDIA cards come from nvidia-smi instead (no hwmon).
    "amdgpu": {"label": "GPU (amdgpu)", "total": False},
}

_NUMBERED: Final[re.Pattern[str]] = re.compile(r"^(temp|fan|power)(\d+)_")


def _read(path: Path) -> str | None:
    """A sysfs attribute, stripped, or None when it cannot be read. Reads fail per
    attribute for ordinary reasons (-ENODATA on a sleeping GPU, -EIO on an absent
    input, -EPERM), and one bad file must never cost the rest of the scan."""
    try:
        return path.read_text().strip()
    except (OSError, UnicodeDecodeError):
        return None


def _number(path: Path, scale: float) -> float | None:
    """A numeric attribute divided by `scale`, or None when missing or not a number."""
    text: str | None = _read(path)
    if text is None:
        return None
    try:
        return float(text) / scale
    except ValueError:
        return None


def _round1(value: float | None) -> float | None:
    return None if value is None else round(value, 1)


def _chip_name(hwmon_dir: Path) -> str | None:
    """The chip's ``name``: in the hwmon directory, or for old drivers under device/."""
    return _read(hwmon_dir / "name") or _read(hwmon_dir / "device" / "name")


def _is_power_supply(hwmon_dir: Path) -> bool:
    """Batteries, chargers, USB-C ports and wireless-mouse batteries register hwmons
    too. Their "power" is a charge rate and they come and go, so fans and power skip
    them (temperatures keep them, as Glances did, for unchanged names)."""
    subsystem: Path = hwmon_dir / "device" / "subsystem"
    try:
        return os.path.basename(os.readlink(subsystem)) == "power_supply"
    except OSError:
        return False


def _attr_dirs(root: Path) -> list[Path]:
    """Every hwmon directory and its device/ subdirectory, in sorted order."""
    dirs: list[Path] = []
    hwmon: Path
    for hwmon in sorted(root.glob("hwmon*"), key=lambda p: str(p)):
        dirs.append(hwmon)
        if (hwmon / "device").is_dir():
            dirs.append(hwmon / "device")
    return dirs


def _numbered(attr_dir: Path, kind: str) -> list[int]:
    """The sensor numbers N with any ``<kind>N_*`` file in `attr_dir`, ascending."""
    found: set[int] = set()
    entry: str
    try:
        entries: list[str] = os.listdir(attr_dir)
    except OSError:
        return []
    for entry in entries:
        m: re.Match[str] | None = _NUMBERED.match(entry)
        if m and m.group(1) == kind:
            found.add(int(m.group(2)))
    return sorted(found)


# ---- temperatures ----------------------------------------------------------------


def _glances_label(
    chip: str, index: int, label: str, taken: list[str], counter: list[int]
) -> str:
    """Glances' naming rule (glances/plugins/sensors, update()): no kernel label is
    ``"<chip> <index within the chip>"``; a label already used anywhere earlier is
    ``"<label> <n>"`` with n counting from 1 per chip; otherwise the label itself."""
    if label == "":
        return f"{chip} {index}"
    if label in taken:
        name: str = f"{label} {counter[0]}"
        counter[0] += 1
        return name
    return label


def _temperatures(root: Path, thermal_root: Path) -> list[dict[str, Any]]:
    """Temperatures in psutil's order and Glances' names, then without junk inputs.

    psutil walks every ``temp*_*`` base under hwmonN/ and hwmonN/device/, sorted as
    path strings (so hwmon10 sorts before hwmon2 and temp10 before temp2), skips a
    base whose ``_input`` or chip ``name`` cannot be read, and groups by chip name in
    order of first appearance (two chips with the same name become one list). The
    names are assigned on that full list; junk inputs are dropped only afterwards,
    so dropping one never renames the sensors after it."""
    bases: list[tuple[str, Path, int]] = []
    attr_dir: Path
    for attr_dir in _attr_dirs(root):
        n: int
        for n in _numbered(attr_dir, "temp"):
            bases.append((f"{attr_dir}/temp{n}", attr_dir, n))
    bases.sort(key=lambda b: b[0])

    chips: dict[str, list[dict[str, Any]]] = {}
    for _, attr_dir, n in bases:
        value: float | None = _number(attr_dir / f"temp{n}_input", 1000)
        chip: str | None = _read(attr_dir / "name")
        if value is None or chip is None:
            continue
        chips.setdefault(chip, []).append(
            {
                "kernel_label": _read(attr_dir / f"temp{n}_label") or "",
                "value": value,
                "warning": _number(attr_dir / f"temp{n}_max", 1000),
                "critical": _number(attr_dir / f"temp{n}_crit", 1000),
                "junk": _read(attr_dir / f"temp{n}_fault") == "1"
                or _read(attr_dir / f"temp{n}_enable") == "0",
            }
        )
    if not bases:
        # psutil's fallback for boxes with no hwmon temperatures at all (older ARM
        # kernels, some VMs): thermal zones, named by their type.
        chips = _thermal_zones(thermal_root)

    rows: list[dict[str, Any]] = []
    taken: list[str] = []
    readings: list[dict[str, Any]]
    for chip, readings in chips.items():
        counter: list[int] = [1]
        index: int
        reading: dict[str, Any]
        for index, reading in enumerate(readings):
            label: str = _glances_label(
                chip, index, reading["kernel_label"], taken, counter
            )
            taken.append(label)
            low, high = TEMP_PLAUSIBLE
            if reading["junk"] or not low <= reading["value"] <= high:
                continue
            warning: float | None = reading["warning"]
            critical: float | None = reading["critical"]
            # BIOS-set limits can be backwards (bodel's Fintek: max 85, crit 80).
            if warning is not None and critical is not None and warning > critical:
                warning, critical = critical, warning
            rows.append(
                {
                    "type": TEMP,
                    "label": label,
                    "value": round(reading["value"], 1),
                    "unit": "C",
                    "warning": warning,
                    "critical": critical,
                    "chip": chip,
                }
            )
    return rows


def _thermal_zones(thermal_root: Path) -> dict[str, list[dict[str, Any]]]:
    """psutil's thermal-zone fallback: one reading per zone, chip = the zone's type,
    limits from its ``hot`` / ``critical`` trip points."""
    chips: dict[str, list[dict[str, Any]]] = {}
    zone: Path
    for zone in sorted(thermal_root.glob("thermal_zone*"), key=lambda p: str(p)):
        value: float | None = _number(zone / "temp", 1000)
        chip: str | None = _read(zone / "type")
        if value is None or chip is None:
            continue
        trips: dict[str, float | None] = {}
        trip: Path
        for trip in sorted(zone.glob("trip_point_*_type")):
            kind: str | None = _read(trip)
            if kind in ("hot", "critical") and kind not in trips:
                trips[kind] = _number(
                    trip.with_name(trip.name.replace("_type", "_temp")), 1000
                )
        chips.setdefault(chip, []).append(
            {
                "kernel_label": "",
                "value": value,
                "warning": trips.get("hot"),
                "critical": trips.get("critical"),
                "junk": False,
            }
        )
    return chips


# ---- fans and power ------------------------------------------------------------


def _unique(label: str, taken: set[str]) -> str:
    """`label`, or `label 2`, `label 3`... when two chips would give the same name."""
    name: str = label
    n: int = 2
    while name in taken:
        name = f"{label} {n}"
        n += 1
    taken.add(name)
    return name


def _fans_and_power(root: Path) -> list[dict[str, Any]]:
    """Every fan and power reading of every chip that is not a power supply.

    For each hwmon directory the attributes are read from the directory itself, or
    from device/ when the driver puts them there; a chip is read once either way."""
    rows: list[dict[str, Any]] = []
    taken: set[str] = set()
    hwmon: Path
    for hwmon in sorted(root.glob("hwmon*"), key=lambda p: str(p)):
        chip: str | None = _chip_name(hwmon)
        if chip is None or _is_power_supply(hwmon):
            continue
        attr_dir: Path
        for attr_dir in (hwmon, hwmon / "device"):
            fans: list[int] = _numbered(attr_dir, "fan")
            powers: list[int] = _numbered(attr_dir, "power")
            if not fans and not powers:
                continue
            n: int
            for n in fans:
                rpm: float | None = _number(attr_dir / f"fan{n}_input", 1)
                if rpm is None:
                    continue
                own: str | None = _read(attr_dir / f"fan{n}_label")
                rows.append(
                    {
                        "type": FAN,
                        "label": _unique(own or f"{chip} fan{n}", taken),
                        "value": rpm,
                        "unit": "R",
                        "warning": None,
                        "critical": None,
                        "chip": chip,
                    }
                )
            for n in powers:
                # µW. Some drivers (older amdgpu, acpi_power_meter) only average.
                watts: float | None = _number(attr_dir / f"power{n}_input", 1e6)
                if watts is None:
                    watts = _number(attr_dir / f"power{n}_average", 1e6)
                if watts is None:
                    continue
                known: dict[str, Any] = POWER_DRIVERS.get(chip, {})
                own = _read(attr_dir / f"power{n}_label")
                label: str = known.get("label") or own or f"{chip} power{n}"
                rows.append(
                    {
                        "type": POWER,
                        "label": _unique(label, taken),
                        "value": round(watts, 1),
                        "unit": "W",
                        "warning": None,
                        "critical": _round1(_number(attr_dir / f"power{n}_crit", 1e6)),
                        "total": bool(known.get("total", False)),
                        "chip": chip,
                    }
                )
            break  # read from the first directory that has them
    return rows


def read_sensors(
    root: str | Path = HWMON_ROOT, thermal_root: str | Path = THERMAL_ROOT
) -> list[dict[str, Any]]:
    """All temperature, fan and power rows. Never raises for a bad sensor; an
    unreadable root gives an empty list."""
    return _temperatures(Path(root), Path(thermal_root)) + _fans_and_power(Path(root))


# ---- fan rules -------------------------------------------------------------------

FAN_DEFAULT: Final[dict[str, Any]] = {
    "min_rpm": None,  # alarm (or note) when the fan reads below this
    "on_low": "alarm",  # "alarm" or "note"
    "hide_if_zero": True,  # unused headers read 0 RPM; a fan with a min_rpm is never hidden
}


def fan_findings(
    rows: list[dict[str, Any]], config: Any
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    """Apply config.json ``fans`` to the fan rows: (rows to show, alarms, notes).

    Each alarm is ``{"key": "fan/<label>", "text": ...}``: the text carries the
    reading and changes with it, the key is the alarm's identity, which is what the
    server's alarm history goes by.

    ``config`` maps a fan's label to its rule, plus an optional ``default`` rule for
    every fan not named::

        {"f71889ed fan1": {"min_rpm": 300}, "default": {"hide_if_zero": true}}

    A fan below its ``min_rpm`` is an alarm (or a note, with ``"on_low": "note"``) and
    always stays visible. With ``hide_if_zero`` a fan reading 0 that has no minimum is
    dropped: an empty header is not a stopped fan. A rule naming a fan that does not
    exist is a note, so a typo or a renamed chip cannot silently disable an alarm."""
    rules: dict[str, Any] = config if isinstance(config, dict) else {}
    default: dict[str, Any] = {**FAN_DEFAULT, **_rule(rules.get("default"))}
    alarms: list[dict[str, Any]] = []
    notes: list[str] = []
    shown: list[dict[str, Any]] = []
    present: set[str] = set()
    row: dict[str, Any]
    for row in rows:
        if row.get("type") != FAN:
            shown.append(row)
            continue
        present.add(row["label"])
        rule: dict[str, Any] = {**default, **_rule(rules.get(row["label"]))}
        minimum: object = rule.get("min_rpm")
        value: float = row["value"]
        if isinstance(minimum, (int, float)) and not isinstance(minimum, bool):
            if value < minimum:
                line: str = f"fan {row['label']}: {value:g} RPM, below its {minimum:g} RPM minimum"
                if rule.get("on_low") == "note":
                    notes.append(line)
                else:
                    alarms.append({"key": f"fan/{row['label']}", "text": line})
            shown.append(row)
        elif value == 0 and rule.get("hide_if_zero"):
            continue
        else:
            shown.append(row)
    name: str
    for name in rules:
        if name != "default" and name not in present:
            notes.append(f"fans config names {name!r}, which this box does not have")
    return shown, alarms, notes


def apply_fan_rules(
    rows: list[dict[str, Any]], config: Any
) -> tuple[list[dict[str, Any]], list[str], list[str]]:
    """``fan_findings`` with the alarms as their text alone."""
    shown: list[dict[str, Any]]
    items: list[dict[str, Any]]
    notes: list[str]
    shown, items, notes = fan_findings(rows, config)
    return shown, [item["text"] for item in items], notes


def _rule(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


# ---- setup hints -----------------------------------------------------------------

DESKTOP_CHASSIS: Final[frozenset[str]] = frozenset(
    {"3", "4", "5", "6", "7", "13", "15", "16"}  # SMBIOS desktop/tower/AIO/mini types
)


def setup_notes(
    rows: list[dict[str, Any]],
    dmi_root: str | Path = "/sys/class/dmi/id",
    cpuinfo: str | Path = "/proc/cpuinfo",
) -> list[str]:
    """Hints for a box whose sensors are missing for a fixable reason: a virtual
    machine (no hardware sensors, nothing to fix), or a desktop board with no fan
    readings, which almost always means its Super I/O driver is not loaded."""
    flags: str = _read(Path(cpuinfo)) or ""
    if re.search(r"^flags\s*:.*\bhypervisor\b", flags, re.MULTILINE):
        return [] if rows else ["virtual machine: no hardware sensors to read"]
    chassis: str | None = _read(Path(dmi_root) / "chassis_type")
    if chassis in DESKTOP_CHASSIS and not any(r.get("type") == FAN for r in rows):
        return [
            "no fan sensors: on a desktop board this usually means the Super I/O "
            "driver is not loaded; run `sudo sensors-detect`, then load the module it "
            "names and add it to /etc/modules-load.d/"
        ]
    return []
