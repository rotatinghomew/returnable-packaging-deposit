"""Convenience module alias.

The implementation lives in :mod:`turnbox.service`.  Keeping a top-level
``service.py`` also lets small deployments use ``python service.py``.
"""

from turnbox.service import *  # noqa: F401,F403
from turnbox.service import TurnBoxService, sample_service  # noqa: F401
from turnbox.errors import *  # noqa: F401,F403

if __name__ == "__main__":
    from turnbox.cli import main

    raise SystemExit(main())
