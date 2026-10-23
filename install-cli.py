#!/usr/bin/env python3
"""Install/remove the user-level figmosha symlink; never edit shell profiles."""
import argparse
import os
from pathlib import Path
import shutil

ROOT = Path(__file__).resolve().parent
SOURCE = ROOT / "bin/figmosha"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bin-dir", type=Path, default=Path.home() / ".local/bin")
    parser.add_argument("--uninstall", action="store_true")
    args = parser.parse_args()
    directory = args.bin_dir.expanduser().absolute()
    target = directory / "figmosha"
    ours = target.is_symlink() and target.resolve() == SOURCE
    if args.uninstall:
        if ours:
            target.unlink()
            print(f"Removed {target}; bridge and checkout were left intact.")
        elif target.exists() or target.is_symlink():
            parser.error(f"{target} belongs to another tool; refusing to remove it")
        else:
            print(f"{target} is not installed")
        return
    if not (ROOT / "venv/bin/python").is_file():
        parser.error("create the project venv and install requirements.txt first")
    if (target.exists() or target.is_symlink()) and not ours:
        parser.error(f"{target} already exists; refusing to overwrite it")
    existing = shutil.which("figmosha")
    if existing and Path(existing).resolve() != SOURCE:
        parser.error(f"another figmosha command is on PATH: {existing}; refusing to shadow it")
    directory.mkdir(parents=True, exist_ok=True)
    if not ours:
        target.symlink_to(SOURCE)
    print(f"Installed {target} -> {SOURCE}")
    if str(directory) not in os.environ.get("PATH", "").split(os.pathsep):
        print(f"{directory} is not on PATH; invoke {target} directly or add that directory to your shell PATH.")
    print("No shell profiles or login services were modified.")


if __name__ == "__main__":
    main()
