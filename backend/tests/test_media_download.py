"""media_download 金测试：全部离线（不碰网络、不真下载）。

钉住的行为：
1. 选项装配（build_ydl_opts 纯函数）——播放列表闸、体积熔断、分辨率上限、
   restrictedfilenames 是安全守护，回归即事故；
2. probe 摘要（format_probe 纯函数）——播放列表要报「拒绝下载」而非静默；
3. 无会话上下文 → System Error（会话级工具的统一契约）;
4. 探活：缺 ffmpeg 不注册。
"""
from pathlib import Path
from unittest.mock import patch

from framework.mcp.tools.media_download import (
    MediaDownloadTool, build_ydl_opts, format_probe,
)


def test_ydl_opts_video_guards():
    opts = build_ydl_opts("video", Path("/tmp/x"), max_height=720, max_filesize_mb=100)
    assert opts["noplaylist"] is True
    assert "height<=720" in opts["format"]
    assert opts["merge_output_format"] == "mp4"
    assert opts["max_filesize"] == 100 * 1024 * 1024
    assert opts["outtmpl"].startswith("/tmp/x/")


def test_filenames_conform_to_asset_whitelist():
    """文件名守护要对着真实消费方钉（S.10：restrictedfilenames 错键名被 yt-dlp
    静默忽略、日文原题落盘——自己写的字典自己验 = 自洽但错）：
    ① 键名必须被 yt-dlp 本尊认账；② 模板首字符恒定过出货白名单。"""
    import yt_dlp
    from framework.api.routes import _ASSET_NAME_RE

    opts = build_ydl_opts("video", Path("/tmp/x"))
    # 模板以恒定 ASCII 前缀开头：restrictfilenames 消毒后 CJK 标题可能只剩前导
    # 下划线，而白名单要求首字符字母数字——前缀兜死这一层。
    # （先取再实例化：YoutubeDL 会把 outtmpl 原地规整成 dict）
    tmpl = Path(opts["outtmpl"]).name
    with yt_dlp.YoutubeDL(opts) as ydl:
        assert ydl.params.get("restrictfilenames") is True
    assert tmpl.startswith("media-")
    assert _ASSET_NAME_RE.match("media-_PV_-v9nUcqxVXc.mp4")  # 消毒后的极端形态可出货


def test_ydl_opts_audio_extracts_m4a():
    opts = build_ydl_opts("audio", Path("/tmp/x"))
    assert opts["format"] == "bestaudio/best"
    assert opts["postprocessors"][0]["key"] == "FFmpegExtractAudio"
    assert opts["postprocessors"][0]["preferredcodec"] == "m4a"
    assert opts["max_filesize"] == 2048 * 1024 * 1024  # 默认 2GB 熔断


def test_probe_digest_and_playlist_refusal():
    digest = format_probe({"title": "T", "uploader": "U", "duration": 125,
                           "upload_date": "20260101", "filesize_approx": 10 * 1024 * 1024,
                           "extractor_key": "Youtube"})
    assert "T" in digest and "2m05s" in digest and "10.0MB" in digest
    pl = format_probe({"_type": "playlist", "title": "PL", "playlist_count": 42})
    assert "PLAYLIST" in pl and "not supported" in pl


def test_execute_without_session_is_system_error():
    out = MediaDownloadTool().execute(url="https://example.com/v")
    assert out.startswith("System Error") and "session" in out


def test_unavailable_without_ffmpeg():
    with patch("shutil.which", return_value=None):
        assert MediaDownloadTool().is_available() is False


def test_schema_shape():
    schema = MediaDownloadTool().get_schema()
    assert schema["name"] == "media_download"
    assert schema["parameters"]["required"] == ["url"]
    assert set(schema["parameters"]["properties"]) == {"url", "mode", "max_height"}
