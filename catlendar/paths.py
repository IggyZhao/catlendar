"""Where Catlendar keeps its files, on whichever platform it is running."""
import os
import sys

HOME = os.path.expanduser("~")

IS_MAC = sys.platform == "darwin"
IS_WINDOWS = os.name == "nt"


def _default_data_dir():
    if IS_WINDOWS:
        base = os.environ.get("APPDATA") or os.path.join(HOME, "AppData", "Roaming")
        return os.path.join(base, "Catlendar")
    if IS_MAC:
        return os.path.join(HOME, "Library", "Application Support", "Catlendar")
    base = os.environ.get("XDG_DATA_HOME") or os.path.join(HOME, ".local", "share")
    return os.path.join(base, "catlendar")


DATA_DIR = os.environ.get("CATLENDAR_DATA_DIR") or _default_data_dir()
DB_PATH = os.environ.get("CATLENDAR_DB") or os.path.join(DATA_DIR, "catlendar.db")
LOG_DIR = os.path.join(DATA_DIR, "logs")
REPORT_DIR = os.path.join(DATA_DIR, "reports")
PKG_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(PKG_DIR)
ASSETS_DIR = os.path.join(ROOT_DIR, "assets")
CONFIG_PATH = os.path.join(DATA_DIR, "projects.yaml")
DEFAULT_CONFIG_PATH = os.path.join(ROOT_DIR, "examples", "projects.yaml")

for _d in (DATA_DIR, LOG_DIR, REPORT_DIR):
    os.makedirs(_d, exist_ok=True)
