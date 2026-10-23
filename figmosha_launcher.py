"""User-level entry point: validate arguments, ensure bridge, run the CLI."""
import sys

from bridge_control import BridgeError, ensure_bridge

if __name__ == "__main__":
    try:
        from figmosha import main
        main(ensure_bridge=ensure_bridge)
    except (BridgeError, OSError, ValueError) as exc:
        print(f"figmosha: {exc}", file=sys.stderr)
        sys.exit(2)
