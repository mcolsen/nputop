# nputop

A lightweight TUI monitor for AMD Ryzen AI NPUs.

Queries the `amdxdna` kernel driver via ioctls on `/dev/accel/accel0` and 
renders live telemetry (TOPS, clocks, power mode, runtime PM, per-process
hardware contexts) in a responsive Rich-based terminal UI.

## Install

```bash
git clone https://github.com/mcolsen/nputop.git
cd nputop
pip install -e .
```

## Requirements

- Linux with the `amdxdna` driver loaded and `/dev/accel/accel0` present.
  Your user needs read/write access to that device node.
- Python 3.10+

Tested on:

- AMD Ryzen AI 300 series ("Strix Halo"), firmware 1.1.2.65, Fedora 43, kernel 6.19.

## Usage

```bash
nputop
nputop --interval 0.5               # custom refresh rate
nputop --once                       # single snapshot, then exit
nputop --device /dev/accel/accel1   # non-default device
python -m nputop --once             # equivalent module invocation
```

Keyboard controls inside the live view: `+`/`-` adjust the refresh rate,
`q` or `Ctrl-C` quits.

## Display reference

### Header

```
nputop -- NPU Strix Halo [0000:c4:00.1] -- FW 1.1.2.65 -- AIE 1.1 (8x6)
```

| Field | Description |
|-------|-------------|
| Device name | NPU marketing name, from sysfs `vbnv` |
| BDF | PCI Bus:Device.Function address |
| FW | Firmware version running on the NPU (major.minor.patch.build) |
| AIE version | AI Engine silicon revision |
| Cols x Rows | Physical tile grid dimensions. Columns are the schedulable unit -- workloads get assigned column ranges |

### Utilization

```
91 % busy
[##########################...]
cols ########
Tasks 4/16   TOPS 59/58
```

| Field | Description |
|-------|-------------|
| busy | Mean busy percentage across the NPU's columns, read from the driver's sensor block. This is the true utilization metric -- 0 at idle regardless of clock speed |
| cols | Per-column busy percentage, one block glyph per column, taller = busier. Shows whether a workload is spread across the array or confined to a few columns |
| tops_curr / tops_max | Peak INT8 throughput (tera-operations/sec) **available at the current clock frequency**. This is a capacity metric, not utilization -- it scales with the NPU clock. At idle, power management clocks the NPU down, so `tops_curr` drops well below `tops_max`. It can also read slightly above `tops_max`, which the driver derives from a nominal clock |
| Tasks | Commands currently in-flight on the NPU vs. the firmware queue limit. Also a utilization metric -- 0 at idle, nonzero during inference |

If the driver does not report sensors this panel falls back to the TOPS
capacity bar and is titled `TOPS & Tasks`. Sensor readings come from amd_pmf
NPU metrics, which are unavailable on PHX/HPT parts and on driver builds
compiled without `HAVE_7_0_amd_pmf_get_npu_data`.

### Clocks and power

```
Clock: mp_npu_clock 1890 MHz   h_clock 1024 MHz  (max 1890 MHz)  Mode: DEFAULT
```

| Field | Description |
|-------|-------------|
| mp_npu_clock | Main NPU compute clock driving the AIE array. Directly determines TOPS. Scaled by power management |
| h_clock | Data fabric / interconnect clock. Affects memory bandwidth between tiles and to/from system memory |
| max | Maximum supported mp_npu_clock frequency |
| Mode | Power mode: `DEFAULT`, `LOW`, `MEDIUM`, `HIGH`, or `TURBO`. Controls how aggressively the driver clocks the NPU |
| Draw | NPU package power in watts. This is the NPU block alone -- on a shared-die part the CPU and iGPU are metered separately, so it is not SoC package power. Omitted when the driver reports no power sensor |

### Runtime PM

```
NPU:   active  (active 2h 14m)
```

| Field | Description |
|-------|-------------|
| Status | `active` = NPU is powered on; `suspended` = kernel has powered it down (wakes on demand) |
| Active time | Cumulative time in the active power state since boot |

### Context table

```
  CTX  HWCTX     PID  PROCESS         STATE   COLS  SUBMISSIONS  COMPLETIONS  GOPS  ERRORS
    1      2    1234  python            ACTIVE   0-3          500          498    12       0
```

Each row is a hardware context -- a workload session on the NPU.

| Field | Description |
|-------|-------------|
| CTX | DRM context ID (kernel-side handle) |
| HWCTX | Hardware context ID (firmware-side handle) |
| PID | Process ID of the owning userspace process |
| PROCESS | Process name from `/proc/{pid}/comm` |
| STATE | `ACTIVE` = executing or scheduled on the AIE array; `IDLE` = context exists but not running |
| COLS | AIE columns assigned to this context (e.g., `0-3`). The NPU scheduler partitions columns across concurrent workloads |
| SUBMISSIONS | Total commands submitted (cumulative) |
| COMPLETIONS | Total commands finished (cumulative). `submissions - completions` = in-flight commands |
| GOPS | Giga-operations/sec this context is currently achieving (reported by firmware) |
| ERRORS | Cumulative error count |

When no workload is running, the table is replaced with `(no active contexts)`.
