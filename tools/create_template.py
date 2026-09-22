from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import config
from src.connections.xlsx_connection import create_template


if __name__ == "__main__":
    create_template(config.XLSX_PATH)
    print(f"Created template: {config.XLSX_PATH}")
