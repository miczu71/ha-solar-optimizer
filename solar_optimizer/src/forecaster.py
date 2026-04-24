"""Phase-2 LightGBM load forecaster. Disabled until 30+ days of data available."""
import logging
import os
import pickle
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

from data_pipeline import FEATURE_COLS, TARGET_COL, build_training_features

log = logging.getLogger(__name__)

MODEL_DIR = Path(os.environ.get("MODEL_DIR", "/data/models"))
MODEL_PATH = MODEL_DIR / "lgbm_base_load.pkl"
META_PATH = MODEL_DIR / "lgbm_meta.json"
MIN_TRAINING_DAYS = 30
STALE_DAYS = 14


class LoadForecaster:
    def __init__(self) -> None:
        self._model = None
        self._trained_at: Optional[datetime] = None
        MODEL_DIR.mkdir(parents=True, exist_ok=True)
        self._try_load()

    def _try_load(self) -> None:
        if MODEL_PATH.exists():
            try:
                with MODEL_PATH.open("rb") as f:
                    self._model = pickle.load(f)
                mtime = MODEL_PATH.stat().st_mtime
                self._trained_at = datetime.fromtimestamp(mtime, tz=timezone.utc)
                log.info("LightGBM model loaded, trained at %s", self._trained_at)
            except Exception as exc:
                log.warning("Failed to load LightGBM model: %s", exc)
                self._model = None

    def is_ready(self) -> bool:
        if self._model is None:
            return False
        if self._trained_at is None:
            return False
        age = datetime.now(timezone.utc) - self._trained_at
        return age.days < STALE_DAYS

    def train(self, influx) -> bool:
        try:
            import lightgbm as lgb
            df = build_training_features(influx, days_back=90)
            if len(df) < MIN_TRAINING_DAYS * 48:
                log.warning(
                    "Insufficient data for LightGBM (%d slots, need %d)",
                    len(df), MIN_TRAINING_DAYS * 48,
                )
                return False
            X = df[FEATURE_COLS]
            y = df[TARGET_COL]
            model = lgb.LGBMRegressor(
                n_estimators=300,
                learning_rate=0.05,
                num_leaves=31,
                min_child_samples=20,
                random_state=42,
            )
            model.fit(X, y)
            with MODEL_PATH.open("wb") as f:
                pickle.dump(model, f)
            self._model = model
            self._trained_at = datetime.now(timezone.utc)
            log.info("LightGBM model trained on %d rows", len(df))
            return True
        except Exception as exc:
            log.error("LightGBM training failed: %s", exc)
            return False

    def predict_48slots(self, feature_rows: list[dict]) -> list[float]:
        if not self.is_ready():
            raise RuntimeError("Forecaster not ready")
        import pandas as pd
        X = pd.DataFrame(feature_rows)[FEATURE_COLS]
        preds = self._model.predict(X)
        return [max(0.0, float(p)) for p in preds]
