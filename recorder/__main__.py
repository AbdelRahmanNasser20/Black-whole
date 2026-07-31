"""Allows `python -m recorder <command>` as a shorthand for
`python -m recorder.cli <command>`. All cron/launchd entrypoints use the
longer `recorder.cli` form (see scripts/recorder_cron.sh /
scripts/recorder_local.sh) — this file exists for interactive convenience."""
import sys

from recorder.cli import main

if __name__ == "__main__":
    sys.exit(main())
