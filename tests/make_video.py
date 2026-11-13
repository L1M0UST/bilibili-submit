"""生成 REA 介绍视频：PIL 绘制字幕页 + ffmpeg 编码成 H.264 MP4（无声）。"""

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "data" / "video"
W, H = 1280, 720
BG = (16, 20, 28)
FG = (232, 236, 244)
ACCENT = (80, 200, 220)
DIM = (150, 158, 172)

FONT_PATH = r"C:\Windows\Fonts\msyh.ttc"


def font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(
        FONT_PATH if not bold else r"C:\Windows\Fonts\msyhbd.ttc", size)


def draw_slide(lines, out: Path):
    """lines: [(text, size, bold, color, gap_after)]"""
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, W, 6], fill=ACCENT)

    total = sum(size + gap for _, size, _, _, gap in lines)
    y = (H - total) // 2
    for text, size, bold, color, gap in lines:
        f = font(size, bold)
        tw = d.textlength(text, font=f)
        d.text(((W - tw) / 2, y), text, font=f, fill=color)
        y += size + gap
    img.save(out)
    print("slide:", out.name)


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    slides = [
        [("REA", 110, True, ACCENT, 18),
         ("Reverse Engineer Anything", 34, False, FG, 10),
         ("把逆向分析引擎接进 AI 代理的工具链", 30, False, DIM, 0)],
        [("一句话定位", 40, True, ACCENT, 20),
         ("当结论依赖的是「发行出来的二进制」、", 38, False, FG, 8),
         ("反编译产物或运行时证据——而不是源码时，用 REA", 38, False, FG, 0)],
        [("覆盖目标", 40, True, ACCENT, 20),
         ("Native 机器码 · .NET 程序集 · Electron/JavaScript", 34, False, FG, 8),
         ("打包应用（APK / IPA / MSIX / DMG）", 34, False, FG, 8),
         ("固件镜像 · 浏览器与 Electron 运行时", 34, False, FG, 8),
         ("流量抓包（HAR/SAZ/PCAP/qlog）· 逻辑信号录制", 34, False, FG, 0)],
        [("工作方式", 40, True, ACCENT, 20),
         ("通过 MCP 接入 AI 代理，按目标自动路由工具", 34, False, FG, 8),
         ("Ghidra / Hopper / JADX / tshark / Binwalk …", 34, False, FG, 8),
         ("产出带完整证据链的 Evidence 记录，而不是编造猜测", 34, False, FG, 0)],
        [("开源 · MIT · github.com/morluto/rea", 36, True, FG, 22),
         ("本视频由纯 HTTP 协议投稿管线自动提交", 30, False, DIM, 8),
         ("（B 站创作中心 Web 接口 · 真实上线测试）", 30, False, DIM, 0)],
    ]
    paths = []
    for i, lines in enumerate(slides, 1):
        p = OUT_DIR / f"slide{i}.png"
        draw_slide(lines, p)
        paths.append(p)

    concat = OUT_DIR / "concat.txt"
    with open(concat, "w", encoding="utf-8") as f:
        for p in paths:
            f.write(f"file '{p.as_posix()}'\nduration 4.2\n")
        f.write(f"file '{paths[-1].as_posix()}'\n")  # concat 要求末尾重复最后一帧

    import imageio_ffmpeg
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    video = OUT_DIR / "rea_intro.mp4"
    cmd = [ffmpeg, "-y", "-f", "concat", "-safe", "0", "-i", str(concat),
           "-vf", "fps=30,format=yuv420p", "-c:v", "libx264", "-preset", "medium",
           "-crf", "23", "-movflags", "+faststart", str(video)]
    import subprocess
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit(r.stderr[-1500:])
    print("video:", video, video.stat().st_size, "bytes")


if __name__ == "__main__":
    main()
