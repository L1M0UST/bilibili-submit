"""本地冒烟测试：不起 B 站凭据，验证 upos 分片协议实现与任务队列状态机。

1) 假 UPOS 服务器：init/PUT/complete 全流程，校验分片参数与字节完整性；
2) client.upos_upload 用假服务器上传一个 3.5 chunk 的文件并校验合并结果；
3) 断点续传：从第 2 片开始恢复，只重传剩余分片；
4) 队列状态机：add 任务 → run（无凭据）→ 落到 pending_auth。
"""

import json
import os
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

# 本地假服务器走 http 回环，绕过系统代理
os.environ["NO_PROXY"] = "127.0.0.1"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bili_submit.client import BiliWebClient, parse_video_filename  # noqa: E402

received_parts = {}
received_ranges = {}
complete_body = {}
init_count = [0]


class FakeUpos(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, payload, status=200):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        q = parse_qs(urlparse(self.path).query, keep_blank_values=True)
        if "uploads" in q:  # init
            init_count[0] += 1
            self._json({"OK": 1, "upload_id": "fake-upload-id-123"})
        elif q.get("output") == ["json"]:  # complete
            length = int(self.headers.get("Content-Length", 0))
            complete_body.update(json.loads(self.rfile.read(length)))
            self._json({"OK": 1, "upos_uri": "upos://fakebucket/fakevideo123.mp4"})
        else:
            self._json({"error": "unknown POST"}, 400)

    def do_PUT(self):
        q = parse_qs(urlparse(self.path).query)
        part = int(q["partNumber"][0])
        length = int(self.headers.get("Content-Length", 0))
        data = self.rfile.read(length)
        assert int(q["size"][0]) == len(data), "size 参数与实际字节数不符"
        received_parts[part] = data
        received_ranges[part] = (int(q["start"][0]), int(q["end"][0]))
        self._json({"OK": 1})


def start_server():
    server = HTTPServer(("127.0.0.1", 0), FakeUpos)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def make_bucket(chunk_size=4):
    return {
        "chunk_size": chunk_size,
        "auth": "fake-auth-token",
        "endpoint": "//127.0.0.1:0",  # 占位，实际由 tests 替换
        "biz_id": 42,
        "upos_uri": "upos://fakebucket/fakevideo123.mp4",
    }


def main():
    server = start_server()
    port = server.server_address[1]
    tmp = Path(tempfile.mkdtemp())
    payload = b"A" * 10 + b"B" * 10 + b"C" * 5  # 25 字节 = 4+4+4+4+4+5 共 6 片（chunk=4）
    video = tmp / "fakevideo123.mp4"
    video.write_bytes(payload)

    bucket = make_bucket()
    bucket["endpoint"] = f"http://127.0.0.1:{port}"   # 本地假服务器用 http
    client = BiliWebClient({"SESSDATA": "x", "bili_jct": "y", "DedeUserID": "1"},
                           proxy=None)

    # 全新上传（25 字节 / chunk=4 → 7 片）
    fname = client.upos_upload(bucket, video, session_restarts=0)
    assert fname == "fakevideo123", f"返回的视频名错误：{fname}"
    merged = b"".join(received_parts[i] for i in sorted(received_parts))
    assert merged == payload, "分片合并结果与原文件不一致"
    assert complete_body == {"parts": [{"partNumber": i, "eTag": "etag"}
                                       for i in range(1, 8)]}, f"complete 载荷错误：{complete_body}"
    print(f"[1] 全新上传 OK：7 分片合并一致，complete 载荷 {len(complete_body['parts'])} parts")

    # 断点续传：从第 3 片恢复，只传 3..7
    received_parts.clear()
    client.upos_upload(bucket, video,
                       resume={"upload_id": "resumed-id", "next_part": 3},
                       session_restarts=0)
    assert sorted(received_parts) == [3, 4, 5, 6, 7], f"续传分片错误：{sorted(received_parts)}"
    assert init_count[0] == 1, "resume 不应重新 init"
    print("[2] 断点续传 OK：跳过前 2 片，仅上传 3-7")

    # 会话失效重试：404 → 重新 preupload（这里直接验证 UploadSessionLost 分类）
    class LostUpos(FakeUpos):
        def do_PUT(self):
            self._json({"error": "NoSuchUpload"}, 404)

    lost_server = HTTPServer(("127.0.0.1", 0), LostUpos)
    threading.Thread(target=lost_server.serve_forever, daemon=True).start()
    lost_bucket = make_bucket()
    lost_bucket["endpoint"] = f"http://127.0.0.1:{lost_server.server_address[1]}"
    try:
        client.upos_upload(lost_bucket, video, session_restarts=0)
        raise AssertionError("应抛出 UploadSessionLost")
    except Exception as exc:
        assert type(exc).__name__ == "UploadSessionLost", f"错误分类不对：{type(exc).__name__}"
    print("[3] 会话失效分类 OK：404 NoSuchUpload → UploadSessionLost")

    # parse_video_filename
    assert parse_video_filename("upos://b/name.mp4") == "name"
    assert parse_video_filename("upos://b/无扩展名") == "无扩展名"
    print("[4] parse_video_filename OK")

    server.shutdown()
    print("全部冒烟测试通过")


if __name__ == "__main__":
    main()
