"""错误分类：决定任务重试、待授权还是转人工。"""


class BiliError(Exception):
    """基类。"""

    def __init__(self, message: str, *, code=None):
        super().__init__(message)
        self.code = code


class TransientError(BiliError):
    """网络/5xx 等临时错误：退避重试。"""


class AuthExpired(BiliError):
    """登录失效（-101 / -111 等）：进入待授权状态。"""


class RateLimited(BiliError):
    """服务端限流（preupload code 601 上传过快等）：拉长退避。"""


class PermanentError(BiliError):
    """参数错误、风控等：转人工处理，不自动重试。"""


class UploadSessionLost(BiliError):
    """UPOS 上传会话失效：重新 preupload，保留素材。"""


def classify_bili_code(code: int, message: str) -> BiliError:
    if code in (-101, -111):
        return AuthExpired(f"B站返回 {code}: {message}", code=code)
    if code in (-352, -509, 601):
        return RateLimited(f"B站返回 {code}: {message}", code=code)
    if code == -403:
        return PermanentError(f"B站返回 {code}（权限不足）: {message}", code=code)
    return PermanentError(f"B站返回 {code}: {message}", code=code)
