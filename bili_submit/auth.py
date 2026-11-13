"""登录与凭据：Web 扫码登录、Cookie 导入、登录状态检查。

凭据保存于 data/credentials.json（独立于业务配置，不进版本库）。
"""

import json
import logging
import stat
import sys
import time
from pathlib import Path

import httpx

from . import config as cfg
from .errors import AuthExpired, PermanentError, TransientError

log = logging.getLogger("bili_submit.auth")

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36")

QR_GENERATE = "https://passport.bilibili.com/x/passport-login/web/qrcode/generate"
QR_POLL = "https://passport.bilibili.com/x/passport-login/web/qrcode/poll"
NAV = "https://api.bilibili.com/x/web-interface/nav"

# data.code 的含义
QR_WAY_CREATED = 86101   # 未扫描
QR_SCANNED = 86090       # 已扫描，待手机确认
QR_EXPIRED = 86038       # 二维码已过期


def _restricted_write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    path.write_text(text, encoding="utf-8")
    try:
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)  # 600；Windows 上尽力而为
    except OSError:
        pass


def load_all_credentials() -> dict:
    if not cfg.CREDENTIALS_PATH.exists():
        return {"accounts": {}}
    return json.loads(cfg.CREDENTIALS_PATH.read_text(encoding="utf-8"))


def load_cookies(account_id: str) -> dict | None:
    """返回 {cookie_name: value}；未登录返回 None。"""
    data = load_all_credentials().get("accounts", {}).get(account_id)
    if not data:
        return None
    return data.get("cookies") or None


def save_cookies(account_id: str, cookies: dict, mid: int | None = None) -> None:
    all_creds = load_all_credentials()
    all_creds.setdefault("accounts", {})[account_id] = {
        "mid": mid,
        "cookies": cookies,
        "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    _restricted_write(cfg.CREDENTIALS_PATH, all_creds)
    log.info("凭据已保存：account=%s mid=%s -> %s", account_id, mid, cfg.CREDENTIALS_PATH)


def import_cookies_file(path: Path) -> dict:
    """从文件导入 Cookie。支持三种形态：
    1) {"SESSDATA": "...", "bili_jct": "..."}
    2) biliup 格式 {"cookie_info": {"cookies": [{"name":..,"value":..}, ...]}}
    3) Netscape cookies.txt（每行以 tab 分隔，# 开头为注释）
    """
    text = path.read_text(encoding="utf-8", errors="replace")
    cookies: dict[str, str] = {}
    stripped = text.lstrip()
    if stripped.startswith("{"):
        data = json.loads(text)
        if "cookie_info" in data:
            data = data["cookie_info"]
        if isinstance(data.get("cookies"), list):
            for item in data["cookies"]:
                if isinstance(item, dict) and "name" in item and "value" in item:
                    cookies[item["name"]] = item["value"]
        else:
            cookies.update({k: v for k, v in data.items() if isinstance(v, str)})
    else:
        for line in text.splitlines():
            line = line.rstrip("\n")
            if not line or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) >= 7:  # Netscape 格式
                cookies[parts[5]] = parts[6]
    required = {"SESSDATA", "bili_jct", "DedeUserID"}
    missing = required - set(cookies)
    if missing:
        raise PermanentError(
            f"导入的 Cookie 缺少必要字段：{sorted(missing)}（需要 SESSDATA/bili_jct/DedeUserID）"
        )
    return cookies


def qr_login(print_qr: bool = True, qr_png: Path | None = None,
             max_rounds: int = 3) -> tuple[dict, int | None]:
    """扫码登录，返回 (cookies, mid)。需要用户手机确认；二维码过期自动换新码。"""
    try:
        import qrcode
    except ImportError:
        qrcode = None

    def show_qr(client: httpx.Client) -> str:
        gen = client.get(QR_GENERATE).json()
        if gen.get("code") != 0:
            raise TransientError(f"申请二维码失败：{gen}")
        url, key = gen["data"]["url"], gen["data"]["qrcode_key"]
        log.info("二维码已生成，请用 B 站 App 扫码确认（180 秒内）")
        qr = None
        if qrcode is not None:
            qr = qrcode.QRCode(border=1)
            qr.add_data(url)
            qr.make(fit=True)
        if print_qr and qr is not None:
            try:
                qr.print_ascii(invert=True)
            except UnicodeEncodeError:
                log.info("终端无法显示字符二维码，请扫描下方链接生成的二维码")
        if qr_png is not None and qr is not None:
            qr_png.parent.mkdir(parents=True, exist_ok=True)
            qr.make_image().save(qr_png)
            log.info("二维码图片： %s", qr_png)
        if print_qr or qr_png:
            log.info("或把下面链接转成二维码后扫描：\n%s", url)
        return key

    with httpx.Client(headers={"User-Agent": UA}, timeout=30) as client:
        for round_no in range(1, max_rounds + 1):
            if round_no > 1:
                log.info("已自动更换新二维码（第 %d/%d 轮），请继续扫码", round_no, max_rounds)
            key = show_qr(client)
            deadline = time.time() + 180
            last_state = None
            while time.time() < deadline:
                time.sleep(2)
                resp = client.get(QR_POLL, params={"qrcode_key": key})
                data = resp.json().get("data") or {}
                code = data.get("code")
                if code == 0:
                    cookies = {c.name: c.value for c in client.cookies.jar}
                    for name, value in resp.cookies.items():
                        cookies.setdefault(name, value)
                    mid = int(cookies.get("DedeUserID", "0")) or None
                    if "SESSDATA" not in cookies or "bili_jct" not in cookies:
                        raise TransientError("登录成功但缺少 SESSDATA/bili_jct，请重试")
                    return cookies, mid
                if code == QR_WAY_CREATED:
                    if last_state != code:
                        log.info("等待扫描…")
                    last_state = code
                elif code == QR_SCANNED:
                    if last_state != code:
                        log.info("已扫描，请在手机上确认…")
                    last_state = code
                elif code == QR_EXPIRED:
                    log.warning("二维码已过期")
                    break  # 换新码
                else:
                    raise TransientError(f"扫码登录失败：code={code} {data.get('message')}")
    raise TransientError(f"扫码超时（{max_rounds} 轮共约 {max_rounds * 3} 分钟），请重新执行 login")


def check_auth(cookies: dict) -> dict:
    """核对登录状态。返回 {mid, uname}；未登录抛 AuthExpired。"""
    with httpx.Client(headers={"User-Agent": UA}, cookies=cookies, timeout=30) as client:
        resp = client.get(NAV)
        try:
            payload = resp.json()
        except ValueError:
            raise TransientError(f"nav 响应非 JSON：HTTP {resp.status_code}")
        code = payload.get("code")
        if code == 0:
            data = payload["data"]
            return {"mid": data.get("mid"), "uname": data.get("uname", ""),
                    "isLogin": bool(data.get("isLogin", True))}
        if code == -101:
            raise AuthExpired("登录已失效（-101 未登录）", code=code)
        raise TransientError(f"nav 返回异常：code={code} {payload.get('message')}")


def verify_account(account_id: str, expected_uid: int | None) -> dict:
    """加载凭据并核对登录与 UID；返回 {mid, uname, cookies}。"""
    cookies = load_cookies(account_id)
    if not cookies:
        raise AuthExpired(f"账号 {account_id} 尚未登录：先执行 login 或 login --file")
    info = check_auth(cookies)
    if expected_uid and info["mid"] != expected_uid:
        raise PermanentError(
            f"凭据属于 UID {info['mid']}，但配置要求 UID {expected_uid}，拒绝投稿"
        )
    return {**info, "cookies": cookies}
