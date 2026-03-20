#!/usr/bin/env python3
"""Windows-friendly GUI entry point for FakeScanner.

Use this script with `pythonw.exe` or PyInstaller's `--windowed` mode so the
desktop control panel starts without opening a console window.
"""

from fake_scanner import ScannerConfig, run_ui


def main() -> int:
    return run_ui(ScannerConfig.load())


if __name__ == "__main__":
    raise SystemExit(main())
