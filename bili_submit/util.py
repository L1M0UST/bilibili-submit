"""通用工具：哈希、时区、日志。"""

import hashlib
import logging
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

TZ_SHANGHAI = ZoneInfo("Asia/Shanghai")

log = logging.getLogger("bili_submit")


def setup_logging(log_dir: Path, verbose: bool = False) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    level = logging.DEBUG if verbose else logging.INFO
    fmt = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )
    root = logging.getLogger("bili_submit")
    root.setLevel(level)
    root.handlers.clear()
    sh = logging.StreamHandler(sys.stderr)
    sh.setFormatter(fmt)
    root.addHandler(sh)
    fh = logging.FileHandler(log_dir / "bili_submit.log", encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            block = f.read(chunk_size)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def parse_local_time(s: str) -> int:
    """把 'YYYY-MM-DD HH:MM'（Asia/Shanghai）解析成 unix 时间戳。"""
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M"):
        try:
            dt = datetime.strptime(s.strip(), fmt).replace(tzinfo=TZ_SHANGHAI)
            return int(dt.timestamp())
        except ValueError:
            continue
    raise ValueError(f"时间格式无法解析：{s!r}，期望 YYYY-MM-DD HH:MM（Asia/Shanghai）")


def now_ts() -> int:
    return int(datetime.now().timestamp())


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def ts_to_local(ts: int | None) -> str:
    if not ts:
        return "-"
    return datetime.fromtimestamp(ts, TZ_SHANGHAI).strftime("%Y-%m-%d %H:%M")


def human_size(n: int | None) -> str:
    if not n:
        return "-"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f}{unit}" if unit != "B" else f"{n}B"
        n /= 1024
    return f"{n:.1f}PB"


def image_mime(head: bytes) -> str:
    if head[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    raise ValueError(f"不支持的封面图片格式（文件头 {head[:12].hex()}）")
