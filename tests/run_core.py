"""Core/data compatibility entry point, including the Python 3.11 CI job."""
import gettext
from pathlib import Path
import sys
import unittest

root = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(root), str(root / 'tests')]
gettext.install('gnollama')
suite = unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromName(name)
    for name in ('test_storage', 'test_vectors', 'test_transport', 'test_retrieval_reference'))
result = unittest.TextTestRunner(verbosity=2).run(suite)
sys.exit(not result.wasSuccessful())
