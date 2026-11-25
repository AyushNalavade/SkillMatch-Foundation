"""
data_store.py — Member 1's responsibility (shared utility used by everyone)

Every other module reads/writes JSON ONLY through these two functions.
No other file should call open()/json.load()/json.dump() directly except
certificate.py, which writes plain .txt letters (not JSON).
"""

import json
import os

DATA_DIR = "data"


def load(filename):
    """
    Load and return a dict from a JSON file inside DATA_DIR.
    Returns an empty dict {} if the file doesn't exist yet
    (so the very first run of the program doesn't crash).
    """
    path = os.path.join(DATA_DIR, filename)
    if not os.path.exists(path):
        return {}
    with open(path, "r") as f:
        return json.load(f)


def save(filename, data):
    """
    Save a dict to a JSON file inside DATA_DIR.
    Creates the DATA_DIR folder automatically if it doesn't exist.
    """
    os.makedirs(DATA_DIR, exist_ok=True)
    path = os.path.join(DATA_DIR, filename)
    with open(path, "w") as f:
        json.dump(data, f, indent=4)
