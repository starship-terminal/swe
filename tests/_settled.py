"""Let a test's just-written files count as old for the session caches.

The parse and status caches store nothing for a file whose ctime is under
two seconds old (see ``swe.sessions.parse_cache``), and a ctime cannot be
set back. A test that asserts a warm lookup reads nothing calls
``settle_cache(self)`` first.
"""

import os
import unittest
from unittest.mock import patch


def settle_cache(case: unittest.TestCase) -> None:
    patcher = patch.dict(os.environ, {"SWE_CACHE_RACY_WINDOW_MS": "0"})
    patcher.start()
    case.addCleanup(patcher.stop)
