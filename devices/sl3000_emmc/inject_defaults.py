#!/usr/bin/env python3
"""Inject only a validated setup Wi-Fi password; never print its value."""

import argparse
import os
from pathlib import Path
import shlex


def validate_password(password):
    if not 8 <= len(password) <= 63 or any(not 32 <= ord(c) <= 126 for c in password):
        raise ValueError("DEFAULT_WIFI_PASSWORD must contain 8-63 printable ASCII characters")


def shell_assignment(password):
    validate_password(password)
    return "wifi_password=" + shlex.quote(password)


def inject(tree, password):
    assignment = shell_assignment(password)
    template = Path(__file__).with_name("firstboot.sh").read_text()
    if template.count("# WIFI_PASSWORD_INJECTED_HERE") != 1:
        raise ValueError("Unexpected firstboot template")
    destination = tree / "files/etc/uci-defaults/99-sl3000-setup"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(template.replace("# WIFI_PASSWORD_INJECTED_HERE", assignment))
    destination.chmod(0o700)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("openwrt", type=Path)
    args = parser.parse_args()
    try:
        inject(args.openwrt, os.environ.get("DEFAULT_WIFI_PASSWORD", ""))
    except ValueError as error:
        parser.exit(1, str(error) + "\n")
