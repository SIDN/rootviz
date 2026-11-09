#!/usr/bin/env python3
"""
Shared configuration loader for all pipeline scripts.

The config file is looked up in this order:
  1. the path in the ROOTVIZ_CONFIG environment variable
  2. ./config.yaml (current working directory)

String values may reference other top-level string/number values with
``${name}``, e.g. ``agg_output_dir: "${save_dir}/agg"``.
"""

import os
import re
from typing import Any, Dict

import yaml

_VAR_RE = re.compile(r"\$\{([A-Za-z0-9_]+)\}")


def _substitute(config: Dict[str, Any]) -> Dict[str, Any]:
    """Resolve ${var} references against other top-level scalar values."""
    scalars = {k: v for k, v in config.items() if isinstance(v, (str, int, float))}

    def resolve(value: str, seen: frozenset) -> str:
        def repl(m: re.Match) -> str:
            name = m.group(1)
            if name in seen:
                raise ValueError(f"Circular reference in config: ${{{name}}}")
            if name not in scalars:
                raise KeyError(f"Unknown config variable: ${{{name}}}")
            ref = scalars[name]
            return resolve(ref, seen | {name}) if isinstance(ref, str) else str(ref)

        return _VAR_RE.sub(repl, value)

    return {
        k: resolve(v, frozenset({k})) if isinstance(v, str) else v
        for k, v in config.items()
    }


def config_path(config_file: str = None) -> str:
    return config_file or os.environ.get("ROOTVIZ_CONFIG") or "config.yaml"


def load_config(config_file: str = None) -> Dict[str, Any]:
    """Load configuration from YAML file.

    Raises:
        FileNotFoundError: If config file does not exist
    """
    path = config_path(config_file)
    if not os.path.exists(path):
        raise FileNotFoundError(
            f"Config file not found: {path} (set ROOTVIZ_CONFIG or run from the config directory)"
        )
    with open(path, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f) or {}
    if not isinstance(config, dict):
        raise ValueError(f"Config file {path} must contain a YAML mapping")
    return _substitute(config)
