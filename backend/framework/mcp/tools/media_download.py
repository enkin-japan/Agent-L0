"""media_download：在线视频/音频抓取（yt-dlp 包壳）。

用户给出视频页 URL（YouTube/B站/推特等 yt-dlp 支持的上千站点），本工具按模式取回：
probe = 只取元数据不下载；audio = 抽音轨存 m4a；video = 合流存 mp4。产物落
sessions/{id}/assets/（与 deck PDF 同一条出口），经 /api/v1/files/ 出下载链接——
后端可能跑在远端机上，落用户 ~/Downloads 对远程客户端毫无意义，served link 三端通用。

守护：
- 纯播放列表 URL 一律拒绝下载（probe 如实报告是列表）；单视频带 list= 参数时
  noplaylist 取单集。没有这道闸，一个 URL 能拖回几百个文件。
- 分辨率上限（默认 1080p）+ 体积熔断（默认 2GB，yt-dlp max_filesize 在已知体积
  超限时中止）——开发机 16GB/磁盘有限，4K 整片不该默认可拖。
- 文件名 restrictfilenames（纯 ASCII）+ 恒定 media- 前缀：markdown 链接里空格/CJK
  会断链、出货白名单要求首字符字母数字；原标题在回执文本里给模型。

依赖：pip yt-dlp + 系统 ffmpeg（合流与抽音轨都要它）；缺任一不注册（探活舍弃）。
"""
import shutil
from pathlib import Path
from typing import Any, Dict

from ..base import BaseTool
from ...agents import runtime

# 下载产物文件名模板：恒定前缀保首字符过出货白名单（restrictfilenames 消毒后 CJK
# 标题可能剩下前导下划线，而 _ASSET_NAME_RE 要求首字符是字母数字）+ 标题截 60 字节
# + 站点视频 id 保唯一；ext 由后处理器决定
_OUTTMPL = "media-%(title).60B-%(id)s.%(ext)s"

# 漂移巡检探针目标：YouTube 现存最老的视频（2005 "Me at the zoo"），元数据取不回
# 基本可断定提取器被站点改坏而非视频消失
_CANARY_URL = "https://www.youtube.com/watch?v=jNQXAC9IVRw"


def build_ydl_opts(mode: str, dest_dir: Path, max_height: int = 1080,
                   max_filesize_mb: int = 2048) -> Dict[str, Any]:
    """按模式装配 yt-dlp 选项（纯函数，金测试离线可验）。

    probe 模式只用公共项（skip_download 由调用方 extract_info(download=False) 承担）。
    """
    opts: Dict[str, Any] = {
        "outtmpl": str(dest_dir / _OUTTMPL),
        # 纯 ASCII 文件名：markdown 链接不因空格/CJK 断链，出货白名单正则才放行。
        # ⚠️ 键名是 restrictfilenames（无 ed）——曾写错成 restrictedfilenames 被
        # yt-dlp 静默忽略，日文原题直接落盘（实机 v0.16.0(44) 事故，S.10）
        "restrictfilenames": True,
        "noplaylist": True,            # 单视频带 list= 参数时只取当前集
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "socket_timeout": 30,
        "retries": 2,
    }
    if mode == "video":
        opts["format"] = (f"bestvideo[height<={int(max_height)}]+bestaudio"
                          f"/best[height<={int(max_height)}]")
        opts["merge_output_format"] = "mp4"
        opts["max_filesize"] = int(max_filesize_mb) * 1024 * 1024
    elif mode == "audio":
        opts["format"] = "bestaudio/best"
        opts["postprocessors"] = [{"key": "FFmpegExtractAudio",
                                   "preferredcodec": "m4a"}]
        opts["max_filesize"] = int(max_filesize_mb) * 1024 * 1024
    return opts


def format_probe(info: Dict[str, Any]) -> str:
    """把 extract_info 元数据压成给模型看的短摘要（纯函数）。"""
    if info.get("_type") == "playlist":
        n = info.get("playlist_count") or len(info.get("entries") or [])
        return (f"This URL is a PLAYLIST ({info.get('title', '?')}, ~{n} entries). "
                "Downloading whole playlists is not supported — ask the user for a "
                "specific video URL from it.")
    dur = info.get("duration")
    mins = f"{int(dur // 60)}m{int(dur % 60):02d}s" if dur else "?"
    size = info.get("filesize_approx") or info.get("filesize")
    size_s = f"{size / 1024 / 1024:.1f}MB (approx, best format)" if size else "unknown"
    lines = [
        f"Title: {info.get('title', '?')}",
        f"Uploader: {info.get('uploader') or info.get('channel') or '?'}",
        f"Duration: {mins}",
        f"Upload date: {info.get('upload_date') or '?'}",
        f"Size: {size_s}",
        f"Site: {info.get('extractor_key') or '?'}",
    ]
    return "\n".join(lines)


class MediaDownloadTool(BaseTool):
    name = "media_download"
    description = (
        "Fetch online video/audio from a URL the user explicitly provided (YouTube, Bilibili, "
        "and 1000+ sites via yt-dlp). mode='probe' returns metadata only (title/duration/size) — "
        "use it first when unsure what the URL holds. mode='audio' extracts the soundtrack as "
        "m4a; mode='video' downloads mp4 (default cap 1080p, 2GB). Returns a markdown download "
        "link — include it verbatim in your reply so the user can save the file. Only for "
        "user-supplied URLs, never for URLs you invented; playlist URLs are refused (ask the "
        "user for one specific video). Downloads may take minutes."
    )
    requires_approval = False   # 用户点名 URL 的抓取；产物只落本会话 assets，无系统副作用
    is_concurrency_safe = False
    # 整片下载以分钟计，全局 120s 熔断必死；probe 也走同一上限（无害）
    timeout_seconds = 900
    parameters = {
        "properties": {
            "url": {"type": "string",
                    "description": "The media page URL, exactly as the user gave it."},
            "mode": {"type": "string", "enum": ["probe", "audio", "video"],
                     "description": "probe = metadata only; audio = m4a soundtrack; "
                                    "video = mp4 file. Default probe."},
            "max_height": {"type": "integer",
                           "description": "Video mode only: max resolution height "
                                          "(default 1080). Use 720 to keep files small."},
        },
        "required": ["url"],
    }

    # 漂移巡检：yt-dlp 对抗上千视频站的提取器军备竞赛，官方近乎月更；版本不动
    # 站点一改就坏，是全仓漂移最快的依赖
    drift_class = "adversarial"
    drift_fix = "backend/.venv/bin/pip install -U yt-dlp（升级后重启 backend 生效）"

    def is_available(self) -> bool:
        try:
            import yt_dlp  # noqa: F401
        except Exception as e:
            print(f"[MCP-media_download] ⚠️ yt-dlp 不可用（{e}），工具不注册。")
            return False
        if shutil.which("ffmpeg") is None:
            print("[MCP-media_download] ⚠️ 系统缺 ffmpeg（合流/抽音轨必需），工具不注册。")
            return False
        return True

    def canary(self):
        """真实功能探针：probe 一个恒存视频的元数据（不下载）。"""
        import yt_dlp
        with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "noplaylist": True,
                               "socket_timeout": 30}) as ydl:
            info = ydl.extract_info(_CANARY_URL, download=False)
        title = (info or {}).get("title") or ""
        if not title:
            return False, "探针视频元数据为空（YouTube 提取器疑似被站点改坏）"
        return True, f"probe ok: {title[:40]}"

    def execute(self, url: str, mode: str = "probe", max_height: int = 1080) -> str:
        import yt_dlp

        session = runtime.get_current_session()
        if session is None:
            return ("System Error: no active session context; media_download must run "
                    "inside a chat turn.")
        if mode not in ("probe", "audio", "video"):
            return f"System Error: unknown mode {mode!r} (use probe / audio / video)."

        dest = Path(session.session_dir) / "assets"
        dest.mkdir(parents=True, exist_ok=True)

        # 前置轻探（process=False 不解析格式）：纯播放列表 URL 在这里拦下，
        # 否则 noplaylist 只管「视频+列表二义 URL」，纯列表照样全量拖回。
        runtime.report_progress("正在解析链接", url)
        try:
            with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True,
                                   "noplaylist": True, "socket_timeout": 30}) as ydl:
                pre = ydl.extract_info(url, download=False, process=False)
        except Exception as e:
            return f"System Error: could not resolve URL ({type(e).__name__}: {e})"
        if pre.get("_type") == "playlist":
            return format_probe(pre)

        opts = build_ydl_opts(mode, dest, max_height=max_height)
        if mode == "probe":
            try:
                with yt_dlp.YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(url, download=False)
            except Exception as e:
                return f"System Error: probe failed ({type(e).__name__}: {e})"
            return format_probe(info)

        runtime.report_progress("正在下载媒体", mode)
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)
        except Exception as e:
            return f"System Error: download failed ({type(e).__name__}: {e})"

        # 后处理器会改扩展名（audio→.m4a、合流→.mp4）：requested_downloads 里的
        # filepath 是后处理完成后的真实落盘路径，prepare_filename 只知道处理前的名字
        try:
            filepath = Path(info["requested_downloads"][0]["filepath"])
        except (KeyError, IndexError):
            return ("System Error: download reported success but the output file could "
                    "not be located.")
        if not filepath.exists():
            return f"System Error: expected output file {filepath.name} is missing."

        size_mb = filepath.stat().st_size / 1024 / 1024
        link = f"/api/v1/files/{session.session_id}/assets/{filepath.name}"
        return (f"Downloaded ({mode}, {size_mb:.1f}MB): {info.get('title', filepath.name)}\n"
                f"Include this markdown link verbatim in your reply so the user can save "
                f"the file:\n[{info.get('title', 'media file')}]({link})")
