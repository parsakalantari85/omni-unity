"""Module entry point: ``python -m project_omni``."""
import os
import sys
import traceback

from .agent import run

if __name__ == "__main__":
    try:
        run()
    except KeyboardInterrupt:
        # Backstop for a ctrl-c before the agent loop's own handler is live
        # (e.g. during argument parsing or project discovery). 130 = SIGINT.
        sys.exit(130)
    except Exception as exc:
        # The full-screen UI is already torn down by _amain's finally block,
        # so it's safe to write to stderr here. Show a clean line by default;
        # set OMNI_DEBUG=1 for the full traceback while developing.
        if os.environ.get("OMNI_DEBUG"):
            traceback.print_exc()
        else:
            print(f"omni: fatal: {exc}", file=sys.stderr)
            print("  (set OMNI_DEBUG=1 for a full traceback)", file=sys.stderr)
        sys.exit(1)
