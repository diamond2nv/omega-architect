"""Experiment runner — launch from project root.
Usage: python3 run_hybrid_experiment.py
"""
import sys
import pathlib

_HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_HERE / "experiments"))

# Pre-import to break circular chain
import omega.resource.budget  # noqa: F401

# Now run the experiment
import experiments.hybrid_vs_dialogue  # noqa: F401, E402
