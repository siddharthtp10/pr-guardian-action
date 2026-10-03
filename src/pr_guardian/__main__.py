"""Enables `python -m pr_guardian`, which is how action.yml launches us."""

import sys

from pr_guardian.cli import main

sys.exit(main())
