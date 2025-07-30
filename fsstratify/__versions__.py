"""This module only keeps the versions numbers used in fsstratify."""

import importlib.metadata

FSSTRATIFY_VERSION = importlib.metadata.version("fsstratify")
STRATA_LOG_VERSION = "0.0.1"
PLAYBOOK_VERSION = "0.0.1"

__version__ = FSSTRATIFY_VERSION
