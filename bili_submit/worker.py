"""任务执行：校验 → 上传 → 提交 → 回查，含重试与状态机流转。"""

import json
import logging
import socket
import uuid
from pathlib import Path

import httpx

from . import db, auth
from .client import BiliWebClient, parse_video_filename
from .errors import (
    AuthExpired,
    BiliError,
    PermanentError,
    RateLimited,
    TransientError,
    UploadSessionLost,
)
from .util import human_size, now_ts, sha256_file

log = logging.getLogger("bili_submit.worker")

# B 站限制：标题 80 字符；dtime 定时发布距提交需大于 4 小时（biliup 文档口径）
TITLE_MAX_CHARS = 80
DTIME_MIN_LEAD_SEC = 4 * 3600


def _backoff(retries: int, base: int, maxsec: int) -> int:
    return min(base * (2 ** max(retries, 0)), maxsec)


class Worker:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.account_id = cfg["account"]["account_id"]
        self.expected_uid = cfg["account"]["expected_uid"]
        self.up = cfg["upload"]
        self.net = cfg["network"]

    # ---------- 单任务处理 ----------

    def process_due(self, max_tasks: int = 1) -> int:
        """处理到期任务，返回处理数量。"""
        done = 0
        with db.connect() as conn:
            while done < max_tasks:
                task = db.claim_due(conn, runner_id=socket.gethostname() + ":" + str(uuid.uuid4())[:8])
                if not task:
                    break
                try:
                    self._process(conn, task)
                except Exception as exc:  # 兜底：任何异常都不允许中断整个 run
                    log.exception("任务 %s 处理异常", task["job_id"])
                    db.update_task(conn, task["id"], {
                        "state": "manual", "last_error": f"未分类异常: {exc}",
                    }, event=f"未分类异常: {exc}")
                done += 1
        return done

    def _process(self, conn, task) -> None:
        job = task["job_id"]
        log.info("== 开始处理任务 %s (%s)", job, task["title"])

        # 崩溃恢复：提交阶段中断过的任务，远端可能已收到请求，只能核对不能重投
        if task["state"] == "submitting":
            db.update_task(conn, task["id"], {
                "state": "pending_confirm",
                "last_error": "进程中断于提交阶段，转结果核对",
            }, event="崩溃恢复：提交阶段中断，转结果核对")
            return

        # 1. 校验（校验通过前不产生任何远端动作）
        if task["state"] in ("pending", "retrying"):
            try:
                self._validate(conn, task)
                task = db.get_task(conn, id_=task["id"])
            except BiliError as exc:
                self._fail(conn, task, exc, manual=True)
                return
            db.update_task(conn, task["id"], {"state": "validated"}, event="校验通过")

        # 2. 登录检查（不产生投稿）
        try:
            info = auth.verify_account(self.account_id, self.expected_uid)
        except BiliError as exc:
            self._fail(conn, task, exc, manual=False, state="pending_auth")
            return
        log.info("登录正常：%s (UID %s)", info["uname"], info["mid"])
        db.update_task(conn, task["id"], {"state": "uploading"})
        task = db.get_task(conn, id_=task["id"])

        client = BiliWebClient(info["cookies"], self.net.get("proxy"),
                               self.net.get("timeout_sec", 30))
        try:
            try:
                self._run_pipeline(conn, task, client)
            except AuthExpired as exc:
                self._fail(conn, task, exc, state="pending_auth")
            except PermanentError as exc:
                self._fail(conn, task, exc)
            except BiliError as exc:
                self._fail(conn, task, exc)
            except (httpx.HTTPError, socket.timeout, OSError) as exc:
                self._fail(conn, task, TransientError(f"网络错误: {exc}"))
        finally:
            client.close()

    def _run_pipeline(self, conn, task, client: BiliWebClient) -> None:
        job = task["job_id"]
        # 3. 视频上传（可断点续传）
        if not task["upos_filename"]:
            self._upload_video(conn, task, client)
            task = db.get_task(conn, id_=task["id"])
        else:
            log.info("视频已上传过（%s），跳过", task["upos_filename"])
        db.update_task(conn, task["id"], {"state": "uploaded"}, event="视频上传完成")

        # 4. 封面（可选）
        if task["cover_path"] and not task["cover_url"]:
            cover_url = client.upload_cover(Path(task["cover_path"]), client.get_csrf())
            db.update_task(conn, task["id"], {"cover_url": cover_url},
                           event=f"封面上传完成 {cover_url}")
            task = db.get_task(conn, id_=task["id"])

        # 5. 提交意图先落库，再发请求（结果待核对机制的前提）
        db.update_task(conn, task["id"], {"state": "submitting"}, event="提交稿件意图已落库")
        task = db.get_task(conn, id_=task["id"])
        try:
            data = self._submit(conn, task, client)
        except (httpx.HTTPError, socket.timeout, TransientError) as exc:
            # 超时/网络断：远端可能已收到请求，先核对，不盲目重投
            db.update_task(conn, task["id"], {
                "state": "pending_confirm",
                "last_error": f"提交响应未确认: {exc}",
            }, event=f"提交响应未确认，转结果核对: {exc}")
            return
        db.update_task(conn, task["id"], {
            "state": "submitted", "aid": data.get("aid"), "bvid": data.get("bvid"),
            "submitted_at": now_ts(), "last_error": None,
        }, event=f"提交成功 aid={data.get('aid')} bvid={data.get('bvid')}")
        log.info("任务 %s 投稿成功：%s (av%s)", job, data.get("bvid"), data.get("aid"))

    # ---------- 阶段实现 ----------

    def _validate(self, conn, task) -> None:
        video = Path(task["video_path"])
        if not video.is_file():
            raise PermanentError(f"视频文件不存在：{video}")
        size = video.stat().st_size
        if size <= 0:
            raise PermanentError(f"视频文件为空：{video}")
        fields: dict = {"video_size": size}

        if not task["content_hash"]:
            log.info("计算内容摘要 %s …", video.name)
            fields["content_hash"] = sha256_file(video)

        dup = conn.execute(
            "SELECT job_id FROM tasks WHERE account_id=? AND content_hash=? "
            "AND state NOT IN ('cancelled') AND id != ?",
            (task["account_id"], fields.get("content_hash") or task["content_hash"], task["id"]),
        ).fetchone()
        if dup:
            raise PermanentError(
                f"内容摘要与已存在任务 {dup['job_id']} 重复（疑似误放的重复素材）"
            )

        title = task["title"]
        if not title.strip():
            raise PermanentError("标题不能为空")
        if len(title) > TITLE_MAX_CHARS:
            log.warning("标题超过 %d 字符，将截断", TITLE_MAX_CHARS)
            fields["title"] = title[: TITLE_MAX_CHARS - 3] + "..."
        if int(task["copyright"]) == 2 and not task["source"]:
            raise PermanentError("转载稿件必须提供来源 source")
        if task["cover_path"] and not Path(task["cover_path"]).is_file():
            raise PermanentError(f"封面文件不存在：{task['cover_path']}")
        if task["dtime"] and task["dtime"] <= now_ts() + DTIME_MIN_LEAD_SEC:
            raise PermanentError("定时发布时间必须晚于当前 4 小时以上（B 站限制）")
        db.update_task(conn, task["id"], fields)
        log.info("校验通过：%s 大小 %s", video.name, human_size(size))

    def _upload_video(self, conn, task, client: BiliWebClient) -> None:
        video = Path(task["video_path"])
        file_size = task["video_size"] or video.stat().st_size

        def on_progress(part, total, done_bytes, total_bytes):
            if part % 4 == 0 or part == total:
                db.update_task(conn, task["id"], {"upload_progress": json.dumps({
                    "next_part": part + 1, "parts_total": total,
                    "uploaded_bytes": done_bytes,
                }, ensure_ascii=False)})

        bucket = client.preupload(video.name, file_size, self.cfg["defaults"].get("line", "auto"))
        resume = json.loads(task["upload_progress"]) if task["upload_progress"] else None
        try:
            fname = client.upos_upload(
                bucket, video, resume=resume, on_progress=on_progress,
                session_restarts=int(self.up.get("session_restarts", 2)),
            )
        except UploadSessionLost as exc:
            raise TransientError(f"上传会话多次失效：{exc}")
        db.update_task(conn, task["id"], {
            "upos_filename": fname,
            "upload_progress": json.dumps({"done": True}, ensure_ascii=False),
        }, event=f"视频上传完成 upos={fname}")

    def _submit(self, conn, task, client: BiliWebClient) -> dict:
        csrf = client.get_csrf()
        studio = {
            "copyright": int(task["copyright"]),
            "source": task["source"] or "",
            "tid": int(task["tid"]),
            "cover": task["cover_url"] or "",
            "title": task["title"],
            "desc_format_id": 0,
            "desc": task["description"] or "",
            "dynamic": "",
            "subtitle": {"open": 0, "lan": ""},
            "tag": task["tags"] or "",
            "videos": [{
                "filename": task["upos_filename"],
                "title": Path(task["video_path"]).stem[:TITLE_MAX_CHARS],
                "desc": "",
            }],
            "interactive": 0,
            "dolby": 0,
            "lossless_music": 0,
            "no_reprint": int(task["no_reprint"] or 0),
            "charging_pay": 0,
            "open_subtitle": False,
            "up_selection_reply": False,
            "up_close_reply": False,
            "up_close_danmu": False,
        }
        if task["dtime"]:
            studio["dtime"] = int(task["dtime"])
        return client.submit_archive(studio, csrf)

    # ---------- 失败与重试 ----------

    def _fail(self, conn, task, exc: BiliError, manual: bool = False,
              state: str | None = None) -> None:
        if state is None and isinstance(exc, AuthExpired):
            state = "pending_auth"
        if state:
            db.update_task(conn, task["id"], {"state": state, "last_error": str(exc)},
                           event=f"进入 {state}: {exc}")
            log.warning("任务 %s 进入 %s：%s", task["job_id"], state, exc)
            return
        if manual or isinstance(exc, PermanentError):
            db.update_task(conn, task["id"], {"state": "manual", "last_error": str(exc)},
                           event=f"待人工处理: {exc}")
            log.error("任务 %s 待人工处理：%s", task["job_id"], exc)
            return
        retries = int(task["retries"] or 0) + 1
        if retries > int(self.up.get("max_retries", 5)):
            db.update_task(conn, task["id"], {"state": "manual", "retries": retries,
                                              "last_error": str(exc)},
                           event=f"重试超限，转人工: {exc}")
            log.error("任务 %s 重试超限：%s", task["job_id"], exc)
            return
        base = int(self.up.get("backoff_base_sec", 30))
        maxsec = int(self.up.get("backoff_max_sec", 3600))
        wait = _backoff(retries, base, maxsec)
        if isinstance(exc, RateLimited):
            wait = min(wait * 4, maxsec)
        next_at = now_ts() + wait
        back_state = "validated" if task["state"] in ("uploaded", "uploading") else "retrying"
        db.update_task(conn, task["id"], {
            "state": back_state, "retries": retries, "next_attempt_at": next_at,
            "last_error": str(exc),
        }, event=f"第 {retries} 次重试安排在 {wait}s 后: {exc}")
        log.warning("任务 %s 将重试（第 %d 次，%ds 后）：%s", task["job_id"], retries, wait, exc)

    # ---------- 结果核对（reconcile） ----------

    def reconcile(self, min_interval_sec: int = 0) -> int:
        """回查 submitted/reviewing/pending_confirm 任务的最新平台状态。"""
        checked = 0
        try:
            info = auth.verify_account(self.account_id, self.expected_uid)
        except BiliError as exc:
            log.warning("reconcile 跳过（登录不可用）：%s", exc)
            return 0
        client = BiliWebClient(info["cookies"], self.net.get("proxy"),
                               self.net.get("timeout_sec", 30))
        try:
            archives = client.list_archives(status="all", pages=3)
        finally:
            client.close()
        index = {}
        for arc in archives:
            if arc.get("bvid"):
                index[arc["bvid"]] = arc
            if arc.get("aid"):
                index[str(arc["aid"])] = arc
        with db.connect() as conn:
            tasks = db.list_tasks(conn, list(db.RECONCILABLE))
            for task in tasks:
                if task["reconciled_at"] and now_ts() - task["reconciled_at"] < min_interval_sec:
                    continue
                arc = None
                if task["bvid"] and task["bvid"] in index:
                    arc = index[task["bvid"]]
                elif task["aid"] and str(task["aid"]) in index:
                    arc = index[str(task["aid"])]
                else:  # 提交结果未确认：按标题匹配
                    arc = next((a for a in archives if a.get("title") == task["title"]), None)
                now = now_ts()
                if arc:
                    new_state = _map_archive_state(arc)
                    fields = {
                        "state": new_state, "last_error": arc.get("reject_reason") or None,
                        "reconciled_at": now,
                    }
                    if arc.get("bvid"):
                        fields["bvid"] = arc["bvid"]
                    if arc.get("aid"):
                        fields["aid"] = arc["aid"]
                    if new_state == "published":
                        fields["published_at"] = int(arc.get("ptime") or now)
                    desc = arc.get("state_desc") or arc.get("desc") or ""
                    db.update_task(conn, task["id"], fields,
                                   event=f"回查：state={arc.get('state')} {desc}")
                    log.info("任务 %s 平台状态：%s（%s）", task["job_id"], new_state, desc)
                else:
                    if task["state"] == "pending_confirm":
                        timeout = int(
                            self.cfg["schedule"].get("pending_confirm_timeout_hours", 24))
                        from datetime import datetime
                        updated = datetime.fromisoformat(task["updated_at"]).timestamp()
                        if now - updated > timeout * 3600:
                            db.update_task(conn, task["id"], {"state": "manual",
                                                              "reconciled_at": now},
                                           event="长期未找到稿件，转人工核对")
                            log.warning("任务 %s 提交后 %d 小时仍未找到稿件，转人工核对",
                                        task["job_id"], timeout)
                            continue
                    db.update_task(conn, task["id"], {"reconciled_at": now})
                checked += 1
        return checked


def _map_archive_state(arc: dict) -> str:
    """平台稿件状态 → 本地状态。

    实测（2026-10）：state=-30 + state_desc='审核中' 表示审核进行中，
    与旧文档『-30=审核失败』不同——因此以 state_desc 为主、状态码为兜底。
    """
    desc = str(arc.get("state_desc") or "")
    try:
        state = int(arc.get("state"))
    except (TypeError, ValueError):
        state = None
    if state == 0:
        return "published"
    if desc:
        if any(k in desc for k in ("审核中", "转码中", "等待", "提交中", "上传中")):
            return "reviewing"
        if "转码失败" in desc:
            return "manual"
        if any(k in desc for k in ("不通过", "退回", "审核失败", "已删除")):
            return "returned"
        if "通过" in desc:
            return "published"
    if state == -16:                # 转码失败
        return "manual"
    if state in (-30, -100):        # 无描述时按旧语义兜底
        return "returned"
    return "reviewing"              # 其余未知状态保守归入审核线
