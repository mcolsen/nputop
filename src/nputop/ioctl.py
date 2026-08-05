"""Low-level ioctl interface to the AMD XDNA2 NPU kernel driver.

Provides typed query functions for /dev/accel/accel0 using DRM ioctls
defined in /usr/include/drm/amdxdna_accel.h.
"""

import ctypes
import errno
import fcntl
import os
import struct
from dataclasses import dataclass
from pathlib import Path

# ---------------------------------------------------------------------------
# DRM ioctl number computation
# ---------------------------------------------------------------------------

_IOC_WRITE = 1
_IOC_READ = 2
_DRM_TYPE = ord("d")  # 0x64
_DRM_COMMAND_BASE = 0x40

# enum amdxdna_drm_ioctl_id
_DRM_AMDXDNA_GET_INFO = 7
_DRM_AMDXDNA_GET_ARRAY = 10


def _iowr(type_: int, nr: int, size: int) -> int:
    return ((_IOC_READ | _IOC_WRITE) << 30) | (size << 16) | (type_ << 8) | nr


# struct amdxdna_drm_get_info: u32 param + u32 buffer_size + u64 buffer = 16 bytes
_IOC_GET_INFO = _iowr(_DRM_TYPE, _DRM_COMMAND_BASE + _DRM_AMDXDNA_GET_INFO, 16)

# struct amdxdna_drm_get_array: u32 param + u32 element_size + u32 num_element + u32 pad + u64 buffer = 24 bytes
_IOC_GET_ARRAY = _iowr(_DRM_TYPE, _DRM_COMMAND_BASE + _DRM_AMDXDNA_GET_ARRAY, 24)

# enum amdxdna_drm_get_param
_PARAM_AIE_METADATA = 1
_PARAM_CLOCK_METADATA = 3
_PARAM_SENSORS = 4
_PARAM_FIRMWARE_VERSION = 8
_PARAM_POWER_MODE = 9
_PARAM_RESOURCE_INFO = 12

# GET_ARRAY params
_HW_CONTEXT_ALL = 0

# Sensor types (struct amdxdna_drm_query_sensor.type)
_SENSOR_TYPE_POWER = 0
_SENSOR_TYPE_COLUMN_UTILIZATION = 1
_SENSOR_TYPE_TEMPERATURE = 2

# The driver emits one power sensor plus one per column, capped at 8 columns.
# It truncates silently rather than returning ENOSPC, so ask for more than
# that and trust the byte count it writes back.
_MAX_SENSORS = 32

# Power mode names (enum amdxdna_power_mode_type)
POWER_MODES = ["DEFAULT", "LOW", "MEDIUM", "HIGH", "TURBO"]

# ---------------------------------------------------------------------------
# ctypes structure definitions (matching amdxdna_accel.h)
# ---------------------------------------------------------------------------


class _AieVersion(ctypes.Structure):
    _fields_ = [("major", ctypes.c_uint32), ("minor", ctypes.c_uint32)]


class _TileMetadata(ctypes.Structure):
    _fields_ = [
        ("row_count", ctypes.c_uint16),
        ("row_start", ctypes.c_uint16),
        ("dma_channel_count", ctypes.c_uint16),
        ("lock_count", ctypes.c_uint16),
        ("event_reg_count", ctypes.c_uint16),
        ("pad", ctypes.c_uint16 * 3),
    ]


class _AieMetadata(ctypes.Structure):
    _fields_ = [
        ("col_size", ctypes.c_uint32),
        ("cols", ctypes.c_uint16),
        ("rows", ctypes.c_uint16),
        ("version", _AieVersion),
        ("core", _TileMetadata),
        ("mem", _TileMetadata),
        ("shim", _TileMetadata),
    ]


class _Clock(ctypes.Structure):
    _fields_ = [
        ("name", ctypes.c_char * 16),
        ("freq_mhz", ctypes.c_uint32),
        ("pad", ctypes.c_uint32),
    ]


class _ClockMetadata(ctypes.Structure):
    _fields_ = [
        ("mp_npu_clock", _Clock),
        ("h_clock", _Clock),
    ]


class _ResourceInfo(ctypes.Structure):
    _fields_ = [
        ("npu_clk_max", ctypes.c_uint64),
        ("npu_tops_max", ctypes.c_uint64),
        ("npu_task_max", ctypes.c_uint64),
        ("npu_tops_curr", ctypes.c_uint64),
        ("npu_task_curr", ctypes.c_uint64),
    ]


class _Sensor(ctypes.Structure):
    _fields_ = [
        ("label", ctypes.c_char * 64),
        ("input", ctypes.c_uint32),
        ("max", ctypes.c_uint32),
        ("average", ctypes.c_uint32),
        ("highest", ctypes.c_uint32),
        ("status", ctypes.c_char * 64),
        ("units", ctypes.c_char * 16),
        ("unitm", ctypes.c_int8),
        ("type", ctypes.c_uint8),
        ("pad", ctypes.c_uint8 * 6),
    ]


class _PowerMode(ctypes.Structure):
    _fields_ = [
        ("power_mode", ctypes.c_uint8),
        ("pad", ctypes.c_uint8 * 7),
    ]


class _FirmwareVersion(ctypes.Structure):
    _fields_ = [
        ("major", ctypes.c_uint32),
        ("minor", ctypes.c_uint32),
        ("patch", ctypes.c_uint32),
        ("build", ctypes.c_uint32),
    ]


class _HwctxEntry(ctypes.Structure):
    _fields_ = [
        ("context_id", ctypes.c_uint32),
        ("start_col", ctypes.c_uint32),
        ("num_col", ctypes.c_uint32),
        ("hwctx_id", ctypes.c_uint32),
        ("pid", ctypes.c_int64),
        ("command_submissions", ctypes.c_uint64),
        ("command_completions", ctypes.c_uint64),
        ("migrations", ctypes.c_uint64),
        ("preemptions", ctypes.c_uint64),
        ("errors", ctypes.c_uint64),
        ("priority", ctypes.c_uint64),
        ("heap_usage", ctypes.c_uint64),
        ("suspensions", ctypes.c_uint64),
        ("state", ctypes.c_uint32),
        ("pasid", ctypes.c_uint32),
        ("gops", ctypes.c_uint32),
        ("fps", ctypes.c_uint32),
        ("dma_bandwidth", ctypes.c_uint32),
        ("latency", ctypes.c_uint32),
        ("frame_exec_time", ctypes.c_uint32),
        ("txn_op_idx", ctypes.c_uint32),
        ("ctx_pc", ctypes.c_uint32),
        ("fatal_error_type", ctypes.c_uint32),
        ("fatal_error_exception_type", ctypes.c_uint32),
        ("fatal_error_exception_pc", ctypes.c_uint32),
        ("fatal_error_app_module", ctypes.c_uint32),
        ("pad", ctypes.c_uint32),
    ]


# ---------------------------------------------------------------------------
# Friendly dataclasses returned to callers
# ---------------------------------------------------------------------------


@dataclass
class AieMetadata:
    cols: int
    rows: int
    version_major: int
    version_minor: int
    core_rows: int
    mem_rows: int
    shim_rows: int


@dataclass
class ClockInfo:
    mp_npu_name: str
    mp_npu_mhz: int
    h_clock_name: str
    h_clock_mhz: int


@dataclass
class ResourceInfo:
    clk_max_mhz: int
    tops_max: int
    task_max: int
    tops_curr: int
    task_curr: int


@dataclass
class FirmwareVersion:
    major: int
    minor: int
    patch: int
    build: int

    def __str__(self) -> str:
        return f"{self.major}.{self.minor}.{self.patch}.{self.build}"


@dataclass
class HwContext:
    context_id: int
    hwctx_id: int
    pid: int
    process_name: str
    state: str  # "ACTIVE" or "IDLE"
    start_col: int
    num_col: int
    command_submissions: int
    command_completions: int
    gops: int
    errors: int
    heap_usage: int
    migrations: int
    preemptions: int
    suspensions: int
    priority: int


@dataclass
class Sensors:
    """Live NPU sensor readings.

    `total_power_w` is the NPU package draw, not the SoC package -- on a
    shared-die part the CPU and iGPU are counted separately.
    """

    total_power_w: float | None
    column_utilization: list[float]

    @property
    def mean_utilization(self) -> float | None:
        """Average busy percentage across columns, or None if unreported."""
        if not self.column_utilization:
            return None
        return sum(self.column_utilization) / len(self.column_utilization)


@dataclass
class RuntimePM:
    status: str  # "active", "suspended", etc.
    active_time_ms: int
    suspended_time_ms: int


# ---------------------------------------------------------------------------
# Device handle
# ---------------------------------------------------------------------------

_ACCEL_DEVICE = "/dev/accel/accel0"


def _find_pci_path(device_path: str) -> str | None:
    """Resolve the sysfs PCI device path for the given /dev/accel/accelN node."""
    name = Path(device_path).name
    accel_path = Path(f"/sys/class/accel/{name}/device")
    if accel_path.exists():
        return str(accel_path.resolve())
    return None


class NpuDevice:
    """Handle to the AMD XDNA2 NPU, providing typed query methods."""

    def __init__(self, device_path: str = _ACCEL_DEVICE):
        self._fd = os.open(device_path, os.O_RDWR)
        self._pci_path = _find_pci_path(device_path)
        self._device_name = self._read_sysfs("vbnv", fallback="NPU")
        self._bdf = self._parse_bdf()
        self._aie_metadata = self.query_aie_metadata()
        self._firmware_version = self.query_firmware_version()

    def _parse_bdf(self) -> str:
        uevent = self._read_sysfs("uevent", fallback="")
        for line in uevent.splitlines():
            if line.startswith("PCI_SLOT_NAME="):
                return line.split("=", 1)[1]
        return Path(self._pci_path).name if self._pci_path else ""

    def close(self) -> None:
        if self._fd >= 0:
            os.close(self._fd)
            self._fd = -1

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    @property
    def device_name(self) -> str:
        return self._device_name

    @property
    def bdf(self) -> str:
        return self._bdf

    @property
    def aie_metadata(self) -> "AieMetadata":
        return self._aie_metadata

    @property
    def firmware_version(self) -> "FirmwareVersion":
        return self._firmware_version

    # -- ioctl helpers -------------------------------------------------------

    def _get_info(self, param: int, buf: ctypes.Structure) -> None:
        """Issue DRM_IOCTL_AMDXDNA_GET_INFO."""
        payload = bytearray(
            struct.pack("IIQ", param, ctypes.sizeof(buf), ctypes.addressof(buf))
        )
        fcntl.ioctl(self._fd, _IOC_GET_INFO, payload)

    def _get_info_sized(self, param: int, buf) -> int:
        """Issue GET_INFO for a variable-length reply; return bytes written.

        Unlike _get_info, which expects the reply to fill a fixed struct, this
        passes an oversized buffer and reports how much of it the kernel
        actually used (it writes the byte count back into buffer_size).
        """
        payload = bytearray(
            struct.pack("IIQ", param, ctypes.sizeof(buf), ctypes.addressof(buf))
        )
        fcntl.ioctl(self._fd, _IOC_GET_INFO, payload)
        _, written, _ = struct.unpack("IIQ", payload)
        return min(written, ctypes.sizeof(buf))

    def _get_array(
        self, param: int, element_type: type, max_elements: int
    ) -> list:
        """Issue DRM_IOCTL_AMDXDNA_GET_ARRAY and return a list of elements.

        The kernel returns ENOSPC and writes the required element count back
        into num_element when our buffer is too small; retry with a larger
        buffer using that hint.
        """
        for _ in range(4):
            ArrayType = element_type * max_elements
            arr = ArrayType()
            payload = bytearray(
                struct.pack(
                    "IIIIQ",
                    param,
                    ctypes.sizeof(element_type),
                    max_elements,
                    0,
                    ctypes.addressof(arr),
                )
            )
            try:
                fcntl.ioctl(self._fd, _IOC_GET_ARRAY, payload)
            except OSError as e:
                if e.errno != errno.ENOSPC:
                    raise
                _, _, needed, _, _ = struct.unpack("IIIIQ", payload)
                max_elements = max(needed, max_elements * 2)
                continue
            _, _, num_returned, _, _ = struct.unpack("IIIIQ", payload)
            return [arr[i] for i in range(num_returned)]
        raise OSError(
            errno.ENOSPC,
            f"GET_ARRAY param={param}: buffer kept being too small after retries",
        )

    def _read_sysfs(self, name: str, fallback: str = "") -> str:
        if self._pci_path is None:
            return fallback
        path = Path(self._pci_path) / name
        try:
            return path.read_text().strip()
        except OSError:
            return fallback

    # -- public query methods ------------------------------------------------

    def query_aie_metadata(self) -> AieMetadata:
        buf = _AieMetadata()
        self._get_info(_PARAM_AIE_METADATA, buf)
        return AieMetadata(
            cols=buf.cols,
            rows=buf.rows,
            version_major=buf.version.major,
            version_minor=buf.version.minor,
            core_rows=buf.core.row_count,
            mem_rows=buf.mem.row_count,
            shim_rows=buf.shim.row_count,
        )

    def query_clocks(self) -> ClockInfo:
        buf = _ClockMetadata()
        self._get_info(_PARAM_CLOCK_METADATA, buf)
        return ClockInfo(
            mp_npu_name=buf.mp_npu_clock.name.decode().rstrip("\x00"),
            mp_npu_mhz=buf.mp_npu_clock.freq_mhz,
            h_clock_name=buf.h_clock.name.decode().rstrip("\x00"),
            h_clock_mhz=buf.h_clock.freq_mhz,
        )

    def query_resource_info(self) -> ResourceInfo:
        buf = _ResourceInfo()
        self._get_info(_PARAM_RESOURCE_INFO, buf)
        return ResourceInfo(
            clk_max_mhz=buf.npu_clk_max,
            tops_max=buf.npu_tops_max,
            task_max=buf.npu_task_max,
            tops_curr=buf.npu_tops_curr,
            task_curr=buf.npu_task_curr,
        )

    def query_sensors(self) -> "Sensors | None":
        """Read package power and per-column busy percentages.

        Returns None when the driver cannot supply sensors. The query is
        backed by amd_pmf NPU metrics, so it is unavailable on PHX/HPT parts
        and on driver builds without HAVE_7_0_amd_pmf_get_npu_data. Note that
        all-zero readings are a legitimate idle result, not a missing sensor.
        """
        buf = (_Sensor * _MAX_SENSORS)()
        try:
            written = self._get_info_sized(_PARAM_SENSORS, buf)
        except OSError:
            return None

        power = None
        columns = []
        for i in range(written // ctypes.sizeof(_Sensor)):
            sensor = buf[i]
            # The driver reports a raw integer plus a decimal exponent;
            # scaling by 10**unitm lands on the SI base unit, so the power
            # sensor's mW with unitm -3 becomes watts.
            value = sensor.input * (10.0**sensor.unitm)
            if sensor.type == _SENSOR_TYPE_POWER:
                power = value
            elif sensor.type == _SENSOR_TYPE_COLUMN_UTILIZATION:
                columns.append(value)
        return Sensors(total_power_w=power, column_utilization=columns)

    def query_power_mode(self) -> str:
        buf = _PowerMode()
        self._get_info(_PARAM_POWER_MODE, buf)
        idx = buf.power_mode
        return POWER_MODES[idx] if idx < len(POWER_MODES) else f"UNKNOWN({idx})"

    def query_firmware_version(self) -> FirmwareVersion:
        buf = _FirmwareVersion()
        self._get_info(_PARAM_FIRMWARE_VERSION, buf)
        return FirmwareVersion(
            major=buf.major, minor=buf.minor, patch=buf.patch, build=buf.build
        )

    def query_hw_contexts(self) -> list[HwContext]:
        entries = self._get_array(_HW_CONTEXT_ALL, _HwctxEntry, max_elements=16)
        results = []
        for e in entries:
            proc = _pid_to_name(e.pid)
            results.append(
                HwContext(
                    context_id=e.context_id,
                    hwctx_id=e.hwctx_id,
                    pid=e.pid,
                    process_name=proc,
                    state="ACTIVE" if e.state == 1 else "IDLE",
                    start_col=e.start_col,
                    num_col=e.num_col,
                    command_submissions=e.command_submissions,
                    command_completions=e.command_completions,
                    gops=e.gops,
                    errors=e.errors,
                    heap_usage=e.heap_usage,
                    migrations=e.migrations,
                    preemptions=e.preemptions,
                    suspensions=e.suspensions,
                    priority=e.priority,
                )
            )
        return results

    def query_runtime_pm(self) -> RuntimePM:
        status = self._read_sysfs("power/runtime_status", fallback="unknown")
        active_ms = int(self._read_sysfs("power/runtime_active_time", fallback="0"))
        suspended_ms = int(
            self._read_sysfs("power/runtime_suspended_time", fallback="0")
        )
        return RuntimePM(
            status=status,
            active_time_ms=active_ms,
            suspended_time_ms=suspended_ms,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _pid_to_name(pid: int) -> str:
    try:
        return Path(f"/proc/{pid}/comm").read_text().strip()
    except OSError:
        return "?"
