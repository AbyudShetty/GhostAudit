"""Command-line entry point: ``python code/main.py <command> ...`` from the repository root (or ``python main.py`` from code/).

Python puts this file's directory on ``sys.path``, so the ``ghostaudit`` package imports from any working directory.
See ``ghostaudit/cli.py`` for the commands.
"""

import sys

from ghostaudit.cli import main

if __name__ == "__main__":
    sys.exit(main())
