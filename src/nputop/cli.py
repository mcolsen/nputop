"""nputop: a lightweight TUI monitor for the AMD XDNA2 NPU.

Usage:
    nputop                    # 1-second refresh
    nputop --interval 0.5
    nputop --once             # single snapshot, then exit
    python -m nputop --once   # equivalent module invocation
"""

import argparse
import select
import sys
import termios
import time
import tty

from rich import box
from rich.align import Align
from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .ioctl import _ACCEL_DEVICE, NpuDevice

_DEFAULT_INTERVAL = 1.0
_MIN_INTERVAL = 0.1
_MAX_INTERVAL = 10.0


def _format_duration(ms: int) -> str:
    """Format milliseconds into a human-readable duration."""
    secs = ms // 1000
    if secs < 60:
        return f"{secs}s"
    mins = secs // 60
    secs %= 60
    if mins < 60:
        return f"{mins}m {secs}s"
    hours = mins // 60
    mins %= 60
    return f"{hours}h {mins}m"


def _abbrev_num(n: int) -> str:
    """Compact numeric rendering for very narrow layouts."""
    if n < 1000:
        return str(n)
    if n < 1_000_000:
        return f"{n / 1000:.0f}k" if n >= 10_000 else f"{n / 1000:.1f}k"
    if n < 1_000_000_000:
        return f"{n / 1_000_000:.1f}M"
    return f"{n / 1_000_000_000:.1f}G"


def _bar(value: int, maximum: int, width: int = 30) -> Text:
    """Render a progress bar as a Rich Text object."""
    if maximum <= 0:
        return Text("?" * width, style="dim")
    ratio = min(value / maximum, 1.0)
    filled = int(ratio * width)
    empty = width - filled
    pct = ratio * 100

    if pct >= 80:
        style = "bold red"
    elif pct >= 50:
        style = "bold yellow"
    else:
        style = "bold green"

    bar = Text()
    bar.append("\u2588" * filled, style=style)
    bar.append("\u2591" * empty, style="dim")
    return bar


def _breakpoint(width: int) -> str:
    """Classify terminal width into a responsive layout tier."""
    if width >= 120:
        return "wide"
    if width >= 80:
        return "medium"
    if width >= 60:
        return "narrow"
    return "tiny"


def _header_panel(dev: NpuDevice, bp: str) -> Panel:
    """Device identity, firmware, AIE version, current time."""
    ts = time.strftime("%H:%M:%S")
    aie_meta = dev.aie_metadata
    aie = f"{aie_meta.version_major}.{aie_meta.version_minor}"
    grid = f"{aie_meta.cols}\u00d7{aie_meta.rows}"
    fw = dev.firmware_version

    if bp in ("wide", "medium"):
        body = Text()
        body.append(dev.device_name, style="bold")
        body.append(f"   [{dev.bdf}]   ", style="dim")
        body.append(f"FW {fw}   ")
        body.append(f"AIE {aie} ({grid})", style="dim")
    else:
        body = Text()
        body.append(dev.device_name, style="bold")
        body.append(f"\n[{dev.bdf}]", style="dim")
        body.append(f"  FW {fw}\n")
        body.append(f"AIE {aie} ({grid})", style="dim")

    return Panel(
        body,
        title=Text("nputop", style="bold cyan"),
        subtitle=Text(ts, style="dim"),
        border_style="cyan",
        box=box.ROUNDED,
        title_align="left",
        subtitle_align="right",
        padding=(0, 1),
    )


def _tops_panel(res, bar_w: int) -> Panel:
    body = Text()
    body.append(f"{res.tops_curr} / {res.tops_max} TOPS\n", style="bold")
    body.append_text(_bar(res.tops_curr, res.tops_max, width=bar_w))
    body.append(f"\nTasks  {res.task_curr} / {res.task_max}")
    return Panel(
        body,
        title=Text("TOPS & Tasks", style="bold"),
        border_style="blue",
        box=box.ROUNDED,
        title_align="left",
        padding=(0, 1),
    )


def _clocks_panel(clocks, res) -> Panel:
    body = Text()
    body.append(f"{clocks.mp_npu_name:<10} {clocks.mp_npu_mhz:>4} MHz\n")
    body.append(f"{clocks.h_clock_name:<10} {clocks.h_clock_mhz:>4} MHz\n")
    body.append(f"{'max':<10} {res.clk_max_mhz:>4} MHz", style="dim")
    return Panel(
        body,
        title=Text("Clocks", style="bold"),
        border_style="blue",
        box=box.ROUNDED,
        title_align="left",
        padding=(0, 1),
    )


def _power_panel(mode: str, pm) -> Panel:
    body = Text()
    body.append("Mode  ")
    body.append(
        f"{mode}\n",
        style="bold" if mode != "DEFAULT" else "",
    )
    body.append("NPU   ")
    body.append(
        f"{pm.status}\n",
        style="bold green" if pm.status == "active" else "dim",
    )
    body.append("up    ")
    body.append(_format_duration(pm.active_time_ms), style="dim")
    return Panel(
        body,
        title=Text("Power", style="bold"),
        border_style="blue",
        box=box.ROUNDED,
        title_align="left",
        padding=(0, 1),
    )


def _telemetry_renderable(res, clocks, mode: str, pm, bp: str, bar_w: int):
    """Side-by-side panels at wide/medium; stacked single panel at narrow/tiny."""
    if bp in ("wide", "medium"):
        grid = Table.grid(expand=True, padding=(0, 0))
        grid.add_column(ratio=1)
        grid.add_column(ratio=1)
        grid.add_column(ratio=1)
        grid.add_row(
            _tops_panel(res, bar_w),
            _clocks_panel(clocks, res),
            _power_panel(mode, pm),
        )
        return grid

    body = Text()
    body.append("TOPS  ")
    body.append(f"{res.tops_curr}/{res.tops_max}  ", style="bold")
    body.append_text(_bar(res.tops_curr, res.tops_max, width=bar_w))
    body.append(f"\nTasks {res.task_curr}/{res.task_max}\n")
    body.append(f"Clk   {clocks.mp_npu_mhz}/{res.clk_max_mhz} MHz\n", style="dim")
    body.append("Mode  ")
    body.append(
        f"{mode}\n",
        style="bold" if mode != "DEFAULT" else "",
    )
    body.append("NPU   ")
    body.append(
        f"{pm.status}",
        style="bold green" if pm.status == "active" else "dim",
    )
    body.append(f"  ({_format_duration(pm.active_time_ms)})", style="dim")
    return Panel(
        body,
        title=Text("Telemetry", style="bold"),
        border_style="blue",
        box=box.ROUNDED,
        title_align="left",
        padding=(0, 1),
    )


def _contexts_panel(contexts, bp: str) -> Panel:
    title = Text(f"Hardware Contexts ({len(contexts)})", style="bold")
    panel_kwargs = dict(
        title=title,
        border_style="blue",
        box=box.ROUNDED,
        title_align="left",
        padding=(0, 1),
    )

    if not contexts:
        return Panel(
            Align.center(Text("(no active contexts)", style="dim")),
            **panel_kwargs,
        )

    has_errors = any(c.errors > 0 for c in contexts)

    show_ctx = bp == "wide"
    show_hwctx = bp == "wide"
    show_pid = bp != "tiny"
    show_cols = bp in ("wide", "medium")
    show_complete = bp in ("wide", "medium")
    show_gops = bp in ("wide", "medium")
    show_errors = bp == "wide" or has_errors
    abbrev = bp == "tiny"

    table = Table(
        box=box.SIMPLE_HEAD,
        expand=True,
        pad_edge=False,
        show_edge=False,
        header_style="bold",
    )

    if show_ctx:
        table.add_column("CTX", justify="right", no_wrap=True)
    if show_hwctx:
        table.add_column("HWCTX", justify="right", no_wrap=True)
    if show_pid:
        table.add_column("PID", justify="right", no_wrap=True)
    table.add_column("PROCESS", max_width=16, overflow="ellipsis", no_wrap=True)
    table.add_column("STATE", no_wrap=True)
    if show_cols:
        table.add_column("COLS", justify="right", no_wrap=True)
    table.add_column("SUBMIT", justify="right", no_wrap=True)
    if show_complete:
        table.add_column("COMPLETE", justify="right", no_wrap=True)
    if show_gops:
        table.add_column("GOPS", justify="right", no_wrap=True)
    if show_errors:
        table.add_column("ERRORS", justify="right", no_wrap=True)

    def num(n: int) -> str:
        return _abbrev_num(n) if abbrev else f"{n:,}"

    for c in contexts:
        cols_str = (
            f"{c.start_col}-{c.start_col + c.num_col - 1}"
            if c.num_col > 1
            else str(c.start_col)
        )
        state_text = Text(
            c.state,
            style="bold green" if c.state == "ACTIVE" else "dim",
        )
        errors_text = Text(
            str(c.errors),
            style="bold red" if c.errors > 0 else "dim",
        )

        row = []
        if show_ctx:
            row.append(str(c.context_id))
        if show_hwctx:
            row.append(str(c.hwctx_id))
        if show_pid:
            row.append(str(c.pid))
        row.append(c.process_name)
        row.append(state_text)
        if show_cols:
            row.append(cols_str)
        row.append(num(c.command_submissions))
        if show_complete:
            row.append(num(c.command_completions))
        if show_gops:
            row.append(str(c.gops))
        if show_errors:
            row.append(errors_text)

        table.add_row(*row)

    return Panel(table, **panel_kwargs)


def _footer_text(interval: float, bp: str) -> Text:
    if bp == "tiny":
        return Text(f" [q]uit  [+/-] {interval:.1f}s", style="dim")
    return Text(f" [q] quit   [+/-] refresh: {interval:.2f}s", style="dim")


def build_display(dev: NpuDevice, console: Console, interval: float):
    """Query the NPU and build a responsive renderable for the given console."""
    width = console.size.width
    bp = _breakpoint(width)
    if bp == "wide":
        bar_w = 30
    elif bp == "medium":
        bar_w = max(10, width // 3 - 10)
    else:
        bar_w = max(6, width - 20)

    res = dev.query_resource_info()
    clocks = dev.query_clocks()
    mode = dev.query_power_mode()
    pm = dev.query_runtime_pm()
    contexts = dev.query_hw_contexts()

    return Group(
        _header_panel(dev, bp),
        _telemetry_renderable(res, clocks, mode, pm, bp, bar_w),
        _contexts_panel(contexts, bp),
        _footer_text(interval, bp),
    )


def _read_key(timeout: float) -> str | None:
    """Wait up to `timeout` seconds for a keypress; return the key or None on timeout."""
    ready, _, _ = select.select([sys.stdin], [], [], timeout)
    if not ready:
        return None
    return sys.stdin.read(1)


def _apply_key(key: str, interval: float) -> tuple[float, bool]:
    """Return (new_interval, should_quit) based on a keypress."""
    if key in ("q", "Q", "\x03"):
        return interval, True
    if key in ("+", "="):
        return max(_MIN_INTERVAL, round(interval - 0.1, 2)), False
    if key in ("-", "_"):
        return min(_MAX_INTERVAL, round(interval + 0.1, 2)), False
    return interval, False


def run_loop(dev: NpuDevice, interval: float) -> None:
    """Main refresh loop with keyboard handling."""
    console = Console()
    stdin_is_tty = sys.stdin.isatty()

    old_settings = None
    if stdin_is_tty:
        old_settings = termios.tcgetattr(sys.stdin)
        tty.setcbreak(sys.stdin.fileno())

    try:
        with Live(
            build_display(dev, console, interval),
            console=console,
            refresh_per_second=4,
            screen=True,
        ) as live:
            while True:
                if stdin_is_tty:
                    key = _read_key(interval)
                    if key is not None:
                        interval, should_quit = _apply_key(key, interval)
                        if should_quit:
                            break
                else:
                    time.sleep(interval)
                live.update(build_display(dev, console, interval))
    finally:
        if old_settings is not None:
            termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_settings)


def run_once(dev: NpuDevice) -> None:
    """Print a single snapshot and exit."""
    console = Console()
    console.print(build_display(dev, console, interval=_DEFAULT_INTERVAL))


def main() -> None:
    parser = argparse.ArgumentParser(
        description="nputop: lightweight NPU monitor for AMD XDNA2"
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=_DEFAULT_INTERVAL,
        help=f"refresh interval in seconds (default: {_DEFAULT_INTERVAL})",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="print a single snapshot and exit",
    )
    parser.add_argument(
        "--device",
        default=_ACCEL_DEVICE,
        help=f"accel device path (default: {_ACCEL_DEVICE})",
    )
    args = parser.parse_args()

    if not _MIN_INTERVAL <= args.interval <= _MAX_INTERVAL:
        parser.error(
            f"--interval must be between {_MIN_INTERVAL} and {_MAX_INTERVAL} seconds"
        )

    try:
        dev = NpuDevice(args.device)
    except OSError as e:
        print(f"Error: cannot open {args.device}: {e}", file=sys.stderr)
        print(
            "Make sure the amdxdna driver is loaded and you have access to the device.",
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        if args.once:
            run_once(dev)
        else:
            run_loop(dev, args.interval)
    except KeyboardInterrupt:
        pass
    finally:
        dev.close()


if __name__ == "__main__":
    main()
