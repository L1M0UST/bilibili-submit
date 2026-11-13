"""B 站创作中心 Web 接口客户端（协议对齐 biliup-rs master）。

协议链路：
  preupload(申请上传参数) → upos 分片上传(init/PUT/complete, X-Upos-Auth)
  → 封面上传(x/vu/web/cover/up) → 提交稿件(x/vu/web/add/v3)
  → 稿件列表(x/web/archives) / 分区预置(x/vupre/web/archive/pre)
"""

import json
import logging
import re
from pathlib import Path
from urllib.parse import parse_qsl

import httpx

from .errors import (
    AuthExpired,
    BiliError,
    PermanentError,
    RateLimited,
    TransientError,
    UploadSessionLost,
    classify_bili_code,
)
from .util import image_mime

log = logging.getLogger("bili_submit.client")

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36")
REFERER_UPLOAD = "https://member.bilibili.com/platform/upload/video/frame"

# 上传线路：query 拼在 preupload 后，probe_url 用于测速（对齐 biliup-rs line.rs）
LINES = {
    "bldsa":   ("zone=cs&upcdn=bldsa&probe_version=20221109", "//upos-cs-upcdnbldsa.bilivideo.com/OK"),
    "cnbldsa": ("zone=cs&upcdn=cnbldsa&probe_version=20221109", "//upos-cs-upcdnbldsa.bilivideo.cn/OK"),
    "andsa":   ("zone=cs&upcdn=andsa&probe_version=20221109", "//c3350892csdsa.anitama.cn/OK"),
    "atdsa":   ("zone=cs&upcdn=atdsa&probe_version=20221109", "//c3350892csdsa.anitama.net/OK"),
    "bda2":    ("probe_version=20221109&upcdn=bda2&zone=cs", "//upos-cs-upcdnbda2.bilivideo.com/OK"),
    "cnbd":    ("probe_version=20221109&upcdn=cnbd&zone=cs", "//upos-cs-upcdnbd.bilivideo.cn/OK"),
    "anbd":    ("probe_version=20221109&upcdn=anbd&zone=cs", "//c3350892csbd.anitama.cn/OK"),
    "atbd":    ("probe_version=20221109&upcdn=atbd&zone=cs", "//c3350892csbd.anitama.net/OK"),
    "tx":      ("zone=cs&upcdn=tx&probe_version=20221109", "//upos-cs-upcdntx.bilivideo.com/OK"),
    "cntx":    ("zone=cs&upcdn=cntx&probe_version=20221109", "//upos-cs-upcdntx.bilivideo.com/OK"),
    "antx":    ("zone=cs&upcdn=antx&probe_version=20221109", "//c3350892cstx.anitama.cn/OK"),
    "attx":    ("zone=cs&upcdn=attx&probe_version=20221109", "//c3350892cstx.anitama.net/OK"),
    "txa":     ("zone=cs&upcdn=txa&probe_version=20221109", "//upos-cs-upcdntxa.bilivideo.com/OK"),
    "alia":    ("zone=cs&upcdn=alia&probe_version=20221109", "//upos-cs-upcdnalia.bilivideo.com/OK"),
    "estx":    ("probe_version=20250923&upcdn=estx&zone=cs", "//e17962d5cstx.esheep.com/OK"),
    "akbd":    ("probe_version=20250923&upcdn=akbd&zone=cs", "//bb27c891csbd.aikobo.cn/OK"),
}

PROBE_URL = "https://member.bilibili.com/preupload?r=probe"
PREUPLOAD_URL = "https://member.bilibili.com/preupload"
COVER_UP_URL = "https://member.bilibili.com/x/vu/web/cover/up"
SUBMIT_URL = "https://member.bilibili.com/x/vu/web/add/v3"
ARCHIVES_URL = "https://member.bilibili.com/x/web/archives"
ARCHIVE_PRE_URL = "https://member.bilibili.com/x/vupre/web/archive/pre"


def build_client(cookies: dict, proxy: str | None, timeout: float) -> httpx.Client:
    headers = {
        "User-Agent": UA,
        "Referer": REFERER_UPLOAD,
        "Origin": "https://member.bilibili.com",
    }
    return httpx.Client(
        headers=headers, cookies=cookies, timeout=timeout,
        proxy=proxy or None, follow_redirects=False,
    )


def _envelope(resp: httpx.Response, context: str) -> dict:
    """统一处理 B 站 {code, message, data} 信封。code==0 返回整个 payload。"""
    if resp.status_code >= 500:
        raise TransientError(f"{context}: HTTP {resp.status_code}")
    try:
        payload = resp.json()
    except ValueError:
        if resp.status_code >= 400:
            raise PermanentError(f"{context}: HTTP {resp.status_code} {resp.text[:200]}")
        raise TransientError(f"{context}: 非 JSON 响应 HTTP {resp.status_code}")
    code = payload.get("code", 0)
    if code == 0:
        return payload
    raise classify_bili_code(code, str(payload.get("message", "")))


class BiliWebClient:
    def __init__(self, cookies: dict, proxy: str | None = None, timeout: float = 30.0):
        self._client = build_client(cookies, proxy, timeout)

    def close(self) -> None:
        self._client.close()

    # ---------- 上传参数 ----------

    def probe_line(self) -> tuple[str, str]:
        """auto 模式：拉线路列表并测速，返回 (query, probe_url)。"""
        payload = _envelope(self._client.get(PROBE_URL, timeout=15), "preupload?r=probe")
        lines = payload.get("lines") or []
        probe = payload.get("probe") or {}
        if not lines:
            raise TransientError("probe 未返回可用线路")
        best, best_cost = None, None
        for line in lines:
            url = line.get("probe_url", "")
            if not url:
                continue
            if not url.startswith("http"):
                url = "https:" + url
            try:
                import time as _time
                start = _time.monotonic()
                if probe.get("get") is not None:
                    resp = self._client.get(url, timeout=15)
                else:
                    mb = probe.get("post") or 0.1
                    resp = self._client.post(url, content=b"\0" * int(mb * 1024 * 1024), timeout=15)
                cost = _time.monotonic() - start
                if resp.status_code < 200 or resp.status_code >= 300:
                    continue
                if best_cost is None or cost < best_cost:
                    best, best_cost = line, cost
            except httpx.HTTPError:
                continue
        if best is None:
            raise TransientError("所有上传线路测速均失败")
        return best.get("query", ""), best.get("probe_url", "")

    def preupload(self, file_name: str, total_size: int, line: str = "auto") -> dict:
        """申请上传参数，返回 bucket：chunk_size/auth/endpoint/biz_id/upos_uri。"""
        if line == "auto":
            line_query, _ = self.probe_line()
        elif line in LINES:
            line_query = LINES[line][0]
        else:
            raise PermanentError(f"未知上传线路 {line!r}，可选：auto/{', '.join(LINES)}")

        params = dict(parse_qsl(line_query))
        params.update({
            "name": file_name,
            "r": "upos",
            "profile": "ugcupos/bup",   # ugcfx/bup 需额外传 metadata 与 frame.zip
            "ssl": "0",
            "version": "2.14.0",
            "build": "2140000",
            "size": str(total_size),
        })
        resp = self._client.get(PREUPLOAD_URL, params=params, timeout=30)
        try:
            payload = resp.json()
        except ValueError:
            raise TransientError(f"preupload 非 JSON 响应 HTTP {resp.status_code}")
        if "code" in payload and payload["code"] not in (0, None):
            raise classify_bili_code(payload["code"], str(payload.get("message", "")))
        for key in ("chunk_size", "auth", "endpoint", "biz_id", "upos_uri"):
            if key not in payload:
                raise TransientError(f"preupload 响应缺少 {key}：{str(payload)[:300]}")
        if not payload["chunk_size"]:
            raise TransientError("preupload 返回 chunk_size 为 0")
        log.info("preupload 成功：upos_uri=%s chunk=%s endpoint=%s",
                 payload["upos_uri"], payload["chunk_size"], payload["endpoint"])
        return payload

    # ---------- 视频分片上传（upos） ----------

    def upos_upload(
        self,
        bucket: dict,
        video_path: Path,
        *,
        resume: dict | None = None,
        on_progress=None,
        session_restarts: int = 2,
    ) -> str:
        """分片上传整个文件，返回 upos 视频文件名（无扩展名）。

        resume 为上次的 upload_progress JSON；会话失效时重新申请，最多
        session_restarts 次，素材本身不受影响。
        """
        url = _upos_url(bucket)
        auth = bucket["auth"]
        chunk_size = int(bucket["chunk_size"])
        file_size = video_path.stat().st_size
        chunks_num = (file_size + chunk_size - 1) // chunk_size
        headers = {"X-Upos-Auth": auth}

        restarts = 0
        while True:
            if resume and resume.get("upload_id"):
                upload_id, start_part = resume["upload_id"], int(resume.get("next_part", 1))
                resume = None
            else:
                upload_id, start_part = self._upos_init(url, headers), 1
            try:
                return self._upos_push(
                    url, headers, video_path, upload_id, start_part,
                    chunk_size, chunks_num, file_size,
                    int(bucket["biz_id"]), on_progress,
                )
            except UploadSessionLost:
                restarts += 1
                if restarts > session_restarts:
                    raise
                log.warning("上传会话失效，重新申请（第 %d 次）", restarts)
                bucket = self.preupload(video_path.name, file_size,
                                        line=self._current_line_query(bucket))
                url = _upos_url(bucket)
                headers = {"X-Upos-Auth": bucket["auth"]}
                chunk_size = int(bucket["chunk_size"])
                chunks_num = (file_size + chunk_size - 1) // chunk_size

    def _current_line_query(self, bucket: dict) -> str:
        for query, _ in LINES.values():
            if f"upcdn={bucket.get('upcdn', '')}" in query:
                return query
        return LINES["bldsa"][0]

    def _upos_init(self, url: str, headers: dict) -> str:
        resp = self._client.post(f"{url}?uploads&output=json", headers=headers, timeout=60)
        if resp.status_code >= 400:
            raise TransientError(f"upos init HTTP {resp.status_code}: {resp.text[:200]}")
        data = resp.json()
        upload_id = data.get("upload_id")
        if not upload_id:
            raise TransientError(f"upos init 未返回 upload_id：{str(data)[:200]}")
        return upload_id

    def _upos_push(
        self, url, headers, video_path, upload_id, start_part,
        chunk_size, chunks_num, file_size, biz_id, on_progress,
    ) -> str:
        done_bytes = min((start_part - 1) * chunk_size, file_size)
        with open(video_path, "rb") as f:
            for part in range(start_part, chunks_num + 1):
                f.seek((part - 1) * chunk_size)
                chunk = f.read(chunk_size)
                if not chunk:
                    break
                params = {
                    "uploadId": upload_id,
                    "chunks": str(chunks_num),
                    "total": str(file_size),
                    "chunk": str(part - 1),
                    "size": str(len(chunk)),
                    "partNumber": str(part),
                    "start": str((part - 1) * chunk_size),
                    "end": str((part - 1) * chunk_size + len(chunk)),
                }
                resp = self._client.put(
                    url, params=params, headers={**headers, "Content-Length": str(len(chunk))},
                    content=chunk, timeout=240,
                )
                log.debug("分片 %d PUT 响应：HTTP %s %s", part, resp.status_code, resp.text[:200])
                if resp.status_code >= 500:
                    raise TransientError(f"分片 {part} 上传 HTTP {resp.status_code}")
                if resp.status_code >= 400:
                    # 会话不存在等：重新申请；单分片偶发失败：小退避后重传同一分片
                    text = resp.text[:200]
                    if "NoSuchUpload" in text or resp.status_code in (404, 410):
                        raise UploadSessionLost(f"upos 会话失效：HTTP {resp.status_code}")
                    raise TransientError(f"分片 {part} 上传 HTTP {resp.status_code}: {text}")
                done_bytes += len(chunk)
                if on_progress:
                    on_progress(part, chunks_num, done_bytes, file_size)
                if part % 8 == 0 or part == chunks_num:
                    log.info("上传进度 %d/%d 分片（%.1f%%）",
                             part, chunks_num, done_bytes * 100.0 / file_size)
        return self._upos_complete(url, headers, upload_id, chunks_num,
                                   video_path.name, biz_id)

    def _upos_complete(self, url, headers, upload_id, chunks_num, local_name, biz_id) -> str:
        parts = [{"partNumber": i, "eTag": "etag"} for i in range(1, chunks_num + 1)]
        params = {"name": local_name, "uploadId": upload_id, "biz_id": str(biz_id),
                  "output": "json", "profile": "ugcupos/bup"}
        resp = self._client.post(url, params=params, headers=headers,
                                 json={"parts": parts}, timeout=60)
        if resp.status_code >= 400:
            raise TransientError(f"upos complete HTTP {resp.status_code}: {resp.text[:200]}")
        data = resp.json()
        log.debug("upos complete 响应：%s", str(data)[:300])
        if data.get("OK") != 1:
            raise TransientError(f"upos complete 响应异常：{str(data)[:200]}")
        # 存储层异步落盘，立即提交会触发 21015（视频上传异常）；等它稳定
        import time as _time
        _time.sleep(3)
        # 响应可能没有 upos_uri（只有 location/key）；统一交给 parse_video_filename
        # 取最后一段路径，绝不能把 bucket 前缀带进投稿 filename（会导致 21015）
        ref = data.get("upos_uri") or data.get("location") or data.get("key") \
            or url.split("?")[0].split("://", 1)[-1]
        return parse_video_filename(ref)

    # ---------- 封面 ----------

    def upload_cover(self, cover_path: Path, csrf: str) -> str:
        raw = cover_path.read_bytes()
        mime = image_mime(raw[:16])
        data_uri = f"data:{mime};base64," + __import__("base64").b64encode(raw).decode()
        resp = self._client.post(
            COVER_UP_URL,
            data={"cover": data_uri, "csrf": csrf},
            timeout=60,
        )
        payload = _envelope(resp, "封面上传")
        url = (payload.get("data") or {}).get("url")
        if not url:
            raise TransientError("封面上传成功但未返回 url")
        return url

    # ---------- 提交稿件 ----------

    def submit_archive(self, studio: dict, csrf: str) -> dict:
        """提交稿件。成功返回 data（含 aid/bvid）。"""
        import time as _time
        resp = self._client.post(
            SUBMIT_URL,
            params={"t": str(int(_time.time() * 1000)), "csrf": csrf},
            json=studio,
            timeout=60,
        )
        payload = _envelope(resp, "提交稿件")
        data = payload.get("data") or {}
        if not data.get("aid") and not data.get("bvid"):
            raise TransientError(f"提交返回缺少稿件标识：{str(payload)[:300]}")
        return data

    # ---------- 状态回查 ----------

    def list_archives(self, status: str = "all", pages: int = 3) -> list[dict]:
        """稿件管理列表。返回 [{aid,bvid,title,state,state_desc,dtime,ptime,...}]"""
        out = []
        for pn in range(1, pages + 1):
            resp = self._client.get(
                ARCHIVES_URL, params={"status": status, "pn": str(pn)}, timeout=60
            )
            payload = _envelope(resp, f"稿件列表 pn={pn}")
            data = payload.get("data") or {}
            audits = data.get("arc_audits") or []
            out.extend(item.get("Archive") or {} for item in audits if isinstance(item, dict))
            page = data.get("page") or {}
            if pn * (page.get("ps") or 20) >= (page.get("count") or 0):
                break
        return out

    def archive_pre(self) -> dict:
        """投稿预置信息（分区树等）。"""
        resp = self._client.get(ARCHIVE_PRE_URL, timeout=30)
        return _envelope(resp, "投稿预置信息").get("data") or {}

    def get_csrf(self) -> str:
        cookies = self._client.cookies
        csrf = cookies.get("bili_jct")
        if not csrf:
            raise AuthExpired("缺少 bili_jct Cookie，无法通过 CSRF 校验")
        return csrf


def _upos_url(bucket: dict) -> str:
    """upos://bucket/path + endpoint（形如 //host）→ https://host/path。
    endpoint 已带协议时原样使用（便于本地测试与未来变更）。"""
    endpoint = bucket["endpoint"]
    path = bucket["upos_uri"].removeprefix("upos://")
    if endpoint.startswith("//"):
        return "https:" + endpoint + "/" + path
    return endpoint.rstrip("/") + "/" + path


def parse_video_filename(upos_uri: str) -> str:
    """提取无扩展名的视频文件名：投稿接口按它与上传文件关联。

    兼容 upos://bucket/name.mp4、bucket/name.mp4、完整 URL 等形态，
    一律取最后一段路径再剥扩展名。
    """
    path = upos_uri.split("://", 1)[-1]
    name = path.rstrip("/").rsplit("/", 1)[-1]
    return re.sub(r"\.[^.]*$", "", name) or name
