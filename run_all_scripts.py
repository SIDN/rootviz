#!/usr/bin/env python3
"""
Run the pipeline stages in order.

- Runs from the directory containing the config file, so relative paths in
  config.yaml work from cron.
- Holds an exclusive lock so overlapping cron runs don't process the same
  files concurrently.
- Stops at the first failing stage and exits non-zero.
"""

import logging
import os
import subprocess
import sys

from common import setup_logging
from config import config_path, load_config

DEFAULT_SCRIPTS = [
    "get_atlas_data.py",
    "probes_metadata.py",
    "geo_results.py",
    "aggregator.py",
    "post-json-to-grafana.py",
]
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

logger = logging.getLogger(__name__)


def acquire_lock(path: str):
    """Return an open lock file handle, or None if another run holds it."""
    try:
        import fcntl
    except ImportError:  # non-POSIX: no locking
        return open(path, "a")
    fh = open(path, "a")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return None
    return fh


def run_scripts(base_path: str, scripts, env) -> int:
    for script in scripts:
        script_path = os.path.join(base_path, script)
        logger.info(f"Running {script}")
        result = subprocess.run([sys.executable, script_path], env=env)
        if result.returncode != 0:
            logger.error(f"{script} failed with exit code {result.returncode}; stopping")
            return result.returncode
    return 0


def main() -> int:
    cfg_file = os.path.abspath(config_path())
    config = load_config(cfg_file)
    setup_logging(config.get("logging_level", "INFO"))

    work_dir = os.path.dirname(cfg_file)
    os.chdir(work_dir)
    env = dict(os.environ, ROOTVIZ_CONFIG=cfg_file)

    lock = acquire_lock(config.get("lock_file", ".rootviz.lock"))
    if lock is None:
        logger.warning("Another pipeline run is still in progress; exiting")
        return 0
    with lock:
        base_path = config.get("base_path") or SCRIPT_DIR
        return run_scripts(base_path, config.get("scripts") or DEFAULT_SCRIPTS, env)


if __name__ == "__main__":
    sys.exit(main())
