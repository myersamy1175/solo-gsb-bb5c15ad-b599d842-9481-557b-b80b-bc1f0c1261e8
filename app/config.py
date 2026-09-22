from __future__ import annotations

import os
from pathlib import Path

# SQLite 数据库文件路径,容器内默认 /data/samples.db,本地默认仓库下 data/samples.db
_DEFAULT_DB = Path(__file__).resolve().parent.parent / "data" / "samples.db"
DB_PATH: str = os.getenv("SAMPLE_DB_PATH", str(_DEFAULT_DB))

# 余量审计时允许的浮点误差(绝对值)
BALANCE_TOLERANCE: float = float(os.getenv("BALANCE_TOLERANCE", "1e-6"))
