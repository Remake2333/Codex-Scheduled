"""Run isolated local tests, preserve their exit status and produce a test artifact."""
import io
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
stream = io.StringIO()
suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"), top_level_dir=str(ROOT))
result = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
out = ROOT / "out"
out.mkdir(exist_ok=True)
summary = stream.getvalue()
(out / "tests.txt").write_text(summary)
print(summary)
raise SystemExit(0 if result.testsRun > 0 and result.wasSuccessful() and not result.skipped else 1)
