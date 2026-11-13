"""命令行入口：python -m bili_submit <command>"""

import argparse
import json
import logging
import sys
import uuid
from pathlib import Path

import httpx

from . import __version__, auth, config as cfg, db
from .client import BiliWebClient
from .errors import AuthExpired, BiliError, PermanentError, TransientError
from .util import human_size, now_ts, parse_local_time, setup_logging, ts_to_local
from .worker import Worker

log = logging.getLogger("bili_submit.cli")


def _boot(verbose: bool) -> None:
    cfg.DATA_DIR.mkdir(parents=True, exist_ok=True)
    cfg.save_config_sample()
    setup_logging(cfg.LOG_DIR, verbose=verbose)


def _new_client(cookies: dict) -> BiliWebClient:
    net = cfg.load_config()["network"]
    return BiliWebClient(cookies, net.get("proxy"), net.get("timeout_sec", 30))


# ---------------- 命令 ----------------

def cmd_doctor(args) -> int:
    ok = True
    print("== 环境自检 ==")
    print(f"Python       : {sys.version.split()[0]}")
    try:
        import httpx, qrcode  # noqa: F401
        print("依赖         : httpx / qrcode 已安装")
    except ImportError as exc:
        ok = False
        print(f"依赖         : 缺失 {exc}（pip install -r requirements.txt）")

    cfg.save_config_sample()
    db.init_db()
    print(f"数据目录     : {cfg.DATA_DIR}")
    print(f"数据库       : {cfg.DB_PATH} 已就绪")
    print(f"配置文件     : {cfg.CONFIG_PATH}")

    creds = auth.load_cookies(cfg.load_config()["account"]["account_id"])
    print(f"本地凭据     : {'已保存' if creds else '无（未登录）'}")

    print("-- 网络连通性 --")
    with httpx.Client(timeout=20, headers={"User-Agent": auth.UA}) as client:
        try:
            gen = client.get(auth.QR_GENERATE).json()
            print(f"passport 扫码接口 : {'正常' if gen.get('code') == 0 else gen}")
        except Exception as exc:
            ok = False
            print(f"passport 扫码接口 : 不可达 {exc}")
        try:
            nav = client.get(auth.NAV).json()
            code = nav.get("code")
            print(f"nav 登录接口      : 正常（未登录返回 {code} 属预期）"
                  if code in (0, -101) else f"nav 接口异常：{nav}")
        except Exception as exc:
            ok = False
            print(f"nav 登录接口      : 不可达 {exc}")
        try:
            resp = client.get("https://member.bilibili.com/preupload?r=probe")
            probe = resp.json()
            print(f"preupload 接口    : 正常（{len(probe.get('lines') or [])} 条线路）")
        except Exception as exc:
            ok = False
            print(f"preupload 接口    : 不可达 {exc}")
    print("自检结果     : 通过" if ok else "自检结果     : 存在问题（见上）")
    return 0 if ok else 1


def cmd_login(args) -> int:
    account_id = cfg.load_config()["account"]["account_id"]
    if args.file:
        cookies = auth.import_cookies_file(Path(args.file))
        log.info("Cookie 导入成功，正在核对登录状态…")
    else:
        cookies, _ = auth.qr_login(print_qr=not args.no_qr,
                                   qr_png=Path(args.qr_png) if args.qr_png else None)
    try:
        info = auth.check_auth(cookies)
    except AuthExpired:
        log.error("导入的 Cookie 已失效")
        return 1
    expected = cfg.load_config()["account"].get("expected_uid")
    if expected and info["mid"] != expected:
        log.error("凭据属于 UID %s，与配置的 expected_uid %s 不符，拒绝保存",
                  info["mid"], expected)
        return 1
    auth.save_cookies(account_id, cookies, info["mid"])
    print(f"登录成功：{info['uname']}（UID {info['mid']}）→ account={account_id}")
    return 0


def cmd_check(args) -> int:
    c = cfg.load_config()
    info = auth.verify_account(c["account"]["account_id"], c["account"].get("expected_uid"))
    print(f"登录正常：{info['uname']}（UID {info['mid']}）")
    return 0


def cmd_categories(args) -> int:
    c = cfg.load_config()
    info = auth.verify_account(c["account"]["account_id"], c["account"].get("expected_uid"))
    client = _new_client(info["cookies"])
    try:
        data = client.archive_pre()
    finally:
        client.close()

    def walk(nodes, depth=0):
        for node in nodes or []:
            tid = node.get("tid") or node.get("id")
            tname = node.get("tname") or node.get("name")
            print(f"{'  ' * depth}{tid}\t{tname}")
            walk(node.get("children") or node.get("sub") or [], depth + 1)

    walk(data.get("typelist"))
    return 0


def cmd_add(args) -> int:
    c = cfg.load_config()
    db.init_db()
    video = Path(args.video).resolve()
    if not video.is_file():
        print(f"错误：视频文件不存在 {video}", file=sys.stderr)
        return 1
    fields = {
        "job_id": args.job_id or uuid.uuid4().hex[:12],
        "account_id": c["account"]["account_id"],
        "video_path": str(video),
        "cover_path": str(Path(args.cover).resolve()) if args.cover else None,
        "title": args.title,
        "description": args.desc or "",
        "tags": args.tags or c["defaults"]["tags"],
        "tid": int(args.tid if args.tid is not None else c["defaults"]["tid"]),
        "copyright": int(args.copyright),
        "source": args.source or "",
        "no_reprint": int(args.no_reprint),
        "submit_at": parse_local_time(args.submit_at) if args.submit_at else now_ts(),
        "dtime": parse_local_time(args.publish_at) if args.publish_at else None,
        "state": "pending",
    }
    if fields["copyright"] == 2 and not fields["source"]:
        print("错误：转载稿件必须提供 --source", file=sys.stderr)
        return 1
    with db.connect() as conn:
        try:
            task_id = db.insert_task(conn, fields)
        except Exception as exc:
            print(f"错误：任务写入失败（job_id 重复？）{exc}", file=sys.stderr)
            return 1
        row = db.get_task(conn, id_=task_id)
    print(f"任务已创建：{row['job_id']}  状态 pending")
    print(f"  视频   : {row['video_path']}（{human_size(row['video_size'])}）")
    print(f"  标题   : {row['title']}")
    print(f"  分区   : tid={row['tid']}  标签: {row['tags'] or '-'}")
    print(f"  计划提交: {ts_to_local(row['submit_at'])}")
    print(f"  定时公开: {ts_to_local(row['dtime']) if row['dtime'] else '提交后直接公开'}")
    print("执行 `python -m bili_submit run` 开始投稿。")
    return 0


def cmd_list(args) -> int:
    db.init_db()
    states = [s.strip() for s in args.state.split(",")] if args.state else None
    with db.connect() as conn:
        rows = db.list_tasks(conn, states)
    if not rows:
        print("（无任务）")
        return 0
    for r in rows:
        print(f"{r['job_id']:<12} {r['state']:<16} {r['title'][:24]:<26} "
              f"提交@{ts_to_local(r['submit_at'])} 公开@{ts_to_local(r['dtime'])} "
              f"{r['bvid'] or '-':<14} {r['last_error'] or ''}")
    return 0


def cmd_show(args) -> int:
    db.init_db()
    with db.connect() as conn:
        row = db.get_task(conn, job_id=args.job)
        if not row:
            print(f"找不到任务 {args.job}", file=sys.stderr)
            return 1
        events = db.task_events(conn, row["id"])
    d = dict(row)
    d["upload_progress"] = row["upload_progress"]
    print(json.dumps({k: v for k, v in d.items()}, ensure_ascii=False, indent=2,
                     default=str))
    print("-- 事件 --")
    for ev in events:
        print(f"{ev['ts']}  [{ev['state'] or '-'}] {ev['message']}")
    return 0


def cmd_run(args) -> int:
    db.init_db()
    c = cfg.load_config()
    worker = Worker(c)
    if args.loop:
        interval = int(c["schedule"].get("loop_interval_sec", 30))
        recon_interval = int(c["schedule"].get("reconcile_interval_sec", 600))
        last_recon = 0
        log.info("进入调度循环（间隔 %ds，Ctrl+C 退出）", interval)
        while True:
            worker.process_due(max_tasks=50)  # 一个周期内把到期任务清空
            if now_ts() - last_recon >= recon_interval:
                worker.reconcile()
                last_recon = now_ts()
            import time
            time.sleep(interval)
    n = worker.process_due(max_tasks=max(1, args.once))
    if not args.skip_reconcile:
        worker.reconcile()
    log.info("本轮处理任务数：%d", n)
    return 0


def cmd_reconcile(args) -> int:
    db.init_db()
    c = cfg.load_config()
    worker = Worker(c)
    n = worker.reconcile()
    print(f"已回查 {n} 个任务")
    return 0


def cmd_cancel(args) -> int:
    db.init_db()
    with db.connect() as conn:
        row = db.get_task(conn, job_id=args.job)
        if not row:
            print(f"找不到任务 {args.job}", file=sys.stderr)
            return 1
        if row["state"] in ("submitted", "reviewing", "published"):
            print(f"任务已提交（{row['state']}），不能本地取消；请在 B 站后台操作", file=sys.stderr)
            return 1
        db.update_task(conn, row["id"], {"state": "cancelled"}, event="用户取消")
    print(f"任务 {args.job} 已取消")
    return 0


def cmd_retry(args) -> int:
    """重置失败/待人工任务：清上传进度与错误，重新走完整流程。"""
    db.init_db()
    with db.connect() as conn:
        row = db.get_task(conn, job_id=args.job)
        if not row:
            print(f"找不到任务 {args.job}", file=sys.stderr)
            return 1
        if row["state"] in ("submitted", "reviewing", "published"):
            print(f"任务已提交（{row['state']}），不能重置（会导致重复投稿）", file=sys.stderr)
            return 1
        if row["bvid"] or row["aid"]:
            print("任务已有稿件标识，请先在 B 站后台核对，不要重置", file=sys.stderr)
            return 1
        db.update_task(conn, row["id"], {
            "state": "pending", "retries": 0, "next_attempt_at": None,
            "upos_filename": None, "upload_progress": None, "cover_url": None,
            "last_error": None, "claimed_at": None, "runner_id": None,
        }, event="用户重置：重新校验、重新上传、重新提交")
    print(f"任务 {args.job} 已重置为 pending")
    return 0


# ---------------- 参数解析 ----------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="bili_submit",
        description="B 站创作中心 Web 接口自动投稿（纯 HTTP 协议层实现）")
    p.add_argument("--verbose", "-v", action="store_true", help="调试日志")
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("doctor", help="环境与接口自检").set_defaults(func=cmd_doctor)

    lp = sub.add_parser("login", help="扫码登录（或导入 Cookie）")
    lp.add_argument("--file", help="从文件导入 Cookie（dict / biliup cookie_info / Netscape）")
    lp.add_argument("--no-qr", action="store_true", help="不在终端打印二维码")
    lp.add_argument("--qr-png", help="同时输出二维码图片到该路径")
    lp.set_defaults(func=cmd_login)

    sub.add_parser("check", help="核对登录状态与 UID").set_defaults(func=cmd_check)
    sub.add_parser("categories", help="列出投稿分区（需登录）").set_defaults(func=cmd_categories)

    ap = sub.add_parser("add", help="创建投稿任务")
    ap.add_argument("--video", required=True, help="视频文件路径")
    ap.add_argument("--title", required=True, help="标题（≤80 字符，超长自动截断）")
    ap.add_argument("--cover", help="封面图片路径（可选）")
    ap.add_argument("--desc", help="简介")
    ap.add_argument("--tags", help="标签，逗号分隔")
    ap.add_argument("--tid", type=int, help="分区 id（categories 命令查询）")
    ap.add_argument("--copyright", type=int, choices=(1, 2), default=1, help="1 自制 / 2 转载")
    ap.add_argument("--source", help="转载来源（copyright=2 必填）")
    ap.add_argument("--no-reprint", type=int, choices=(0, 1), default=0, help="1 禁止转载")
    ap.add_argument("--publish-at", help="期望公开时间 YYYY-MM-DD HH:MM（定时发布，需晚于 4h）")
    ap.add_argument("--submit-at", help="计划提交时间 YYYY-MM-DD HH:MM（默认立即到期）")
    ap.add_argument("--job-id", help="自定义任务 ID（默认随机）")
    ap.set_defaults(func=cmd_add)

    lp2 = sub.add_parser("list", help="任务列表")
    lp2.add_argument("--state", help="按状态过滤，逗号分隔")
    lp2.set_defaults(func=cmd_list)

    sp = sub.add_parser("show", help="任务详情与事件")
    sp.add_argument("--job", required=True)
    sp.set_defaults(func=cmd_show)

    rp = sub.add_parser("run", help="处理到期任务")
    rp.add_argument("--once", type=int, default=1, help="本轮最多处理 N 个任务")
    rp.add_argument("--loop", action="store_true", help="常驻调度循环")
    rp.add_argument("--skip-reconcile", action="store_true", help="run 后不回查状态")
    rp.set_defaults(func=cmd_run)

    sub.add_parser("reconcile", help="回查已提交任务的转码/审核/公开状态").set_defaults(
        func=cmd_reconcile)

    cp = sub.add_parser("cancel", help="取消未提交的任务")
    cp.add_argument("--job", required=True)
    cp.set_defaults(func=cmd_cancel)

    tp = sub.add_parser("retry", help="重置失败任务重新处理（含重新上传）")
    tp.add_argument("--job", required=True)
    tp.set_defaults(func=cmd_retry)

    return p


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _boot(args.verbose)
    try:
        return args.func(args)
    except BiliError as exc:
        log.error("%s", exc)
        return 2
    except KeyboardInterrupt:
        print("\n已中断")
        return 130


if __name__ == "__main__":
    sys.exit(main())
