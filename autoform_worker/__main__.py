"""``python -m autoform_worker`` — same entry as the ``autoform`` console script."""
from .cli import cli_main

raise SystemExit(cli_main())
