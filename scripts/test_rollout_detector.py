"""Pins the rollout detector. No cluster. validate.sh already discovers this."""
from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path


def _load():
    path = Path(__file__).resolve().parent / "verification" / "ordered-updates" / "run_v1_v6.py"
    spec = importlib.util.spec_from_file_location("run_v1_v6", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class DetectorSelfCheck(unittest.TestCase):
    def test_self_check(self):
        _load().self_check()


if __name__ == "__main__":
    unittest.main()
