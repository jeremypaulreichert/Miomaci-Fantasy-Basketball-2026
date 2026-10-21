"""Offline normalized input contract; no live source adapter is implied."""
from .local import Dataset, InputError, load_inputs, STAT_FIELDS

__all__ = ["Dataset", "InputError", "load_inputs", "STAT_FIELDS"]
