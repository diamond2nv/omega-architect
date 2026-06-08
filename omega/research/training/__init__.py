"""ModelRouter LightGBM training pipeline.

Phase C of the SquillaRouter absorption plan.  Provides data collection,
label generation, feature extraction, and LightGBM training for the
3-tier theorem complexity classifier.

Usage::

    # Step 0: Explore available data
    python -m omega.research.training.data_collection scan

    # Step 1: Download + build seed set
    python -m omega.research.training.data_collection download
    python -m omega.research.training.data_collection build

    # Step 2: (future) Generate labels + extract features
    python -m omega.research.training.label_generation generate
    python -m omega.research.training.feature_extraction extract

    # Step 3: (future) Train LightGBM
    python -m omega.research.training.train_lgbm
"""
