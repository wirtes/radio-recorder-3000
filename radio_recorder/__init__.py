from __future__ import annotations

import os
from pathlib import Path

from flask import Flask

from .db import init_db
from .routes import bp
from .scheduler import start_scheduler


def create_app(test_config: dict | None = None) -> Flask:
    app = Flask(__name__)
    app.config.from_mapping(
        SECRET_KEY=os.environ.get("SECRET_KEY", "change-me"),
        DATA_DIR=os.environ.get("DATA_DIR", "./data"),
        FINAL_DIR=os.environ.get("FINAL_DIR", "./recordings"),
        DATABASE=None,
        START_SCHEDULER=True,
    )
    if test_config:
        app.config.update(test_config)

    data_dir = Path(app.config["DATA_DIR"]).resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "artwork").mkdir(exist_ok=True)
    (data_dir / "station-logos").mkdir(exist_ok=True)
    (data_dir / "work").mkdir(exist_ok=True)
    app.config["DATABASE"] = app.config["DATABASE"] or str(data_dir / "radio-recorder.sqlite3")

    init_db(app)
    app.register_blueprint(bp)

    if app.config["START_SCHEDULER"]:
        start_scheduler(app)
    return app
