"""Installed package resources, never paths relative to the old repository."""
from pathlib import Path
import os
import shutil


def resources_root():
    return Path(__file__).resolve().parents[1] / "resources"


def gauss_home(explicit=None):
    if explicit:
        return Path(explicit)
    if os.environ.get("GAUSSHOME"):
        return Path(os.environ["GAUSSHOME"])
    binary = shutil.which("gsql")
    if binary:
        return Path(binary).resolve().parent.parent
    raise ValueError("Provide gauss_home, GAUSSHOME or gsql on PATH")
