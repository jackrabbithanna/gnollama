"""Core/data compatibility entry point, including the Python 3.11 CI job."""
from pathlib import Path
import sys
import unittest

root = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(root), str(root / 'tests')]
suite = unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromName(name)
    for name in ('test_storage', 'test_vectors', 'test_transport', 'test_retrieval_reference', 'test_decision',
                 'test_decision_run', 'test_decision_store', 'test_image_generation',
                 'test_image_store', 'test_image_providers', 'test_image_run'))
result = unittest.TextTestRunner(verbosity=2).run(suite)
sys.exit(not result.wasSuccessful())
