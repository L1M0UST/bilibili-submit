"""配置加载：默认值 + data/config.json 覆盖。"""

import copy
import json
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
CONFIG_PATH = DATA_DIR / "config.json"
CREDENTIALS_PATH = DATA_DIR / "credentials.json"
DB_PATH = DATA_DIR / "bili_submit.db"
LOG_DIR = DATA_DIR / "logs"

DEFAULT_CONFIG = {
    "account": {
        "account_id": "main",
        # 投稿前核对真实 UID，避免投错账号；null 表示不校验
        "expected_uid": None,
    },
    "defaults": {
        "tid": 21,          # 默认分区：日常
        "tags": "",
        "copyright": 1,     # 1 自制 / 2 转载
        "no_reprint": 0,
        "line": "auto",     # auto=测速选线；可固定 bda2/bldsa/tx/...
    },
    "upload": {
        "max_retries": 5,
        "backoff_base_sec": 30,
        "backoff_max_sec": 3600,
        "session_restarts": 2,   # 上传会话失效后最多重新申请几次
    },
    "schedule": {
        "timezone": "Asia/Shanghai",
        "loop_interval_sec": 30,
        "reconcile_interval_sec": 600,
        "pending_confirm_timeout_hours": 24,
    },
    "network": {
        "proxy": None,
        "timeout_sec": 30,
    },
}


def load_config() -> dict:
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH, encoding="utf-8") as f:
            user = json.load(f)
        for section, values in user.items():
            if isinstance(values, dict) and isinstance(cfg.get(section), dict):
                cfg[section].update(values)
            else:
                cfg[section] = values
    return cfg


def save_config_sample() -> None:
    """首次运行时落一份可编辑的配置文件。"""
    if not CONFIG_PATH.exists():
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_CONFIG, f, ensure_ascii=False, indent=2)
