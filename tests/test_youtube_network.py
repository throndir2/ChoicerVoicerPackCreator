from __future__ import annotations

import io
import json
import threading
import time
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

import pytest

from choicer_voicer_pack_creator import youtube
from choicer_voicer_pack_creator.diagnostics import ApplicationDiagnostics, application_log_path
from choicer_voicer_pack_creator.media import MediaTools

URL = "https://www.youtube.com/watch?v=abcdefghijk"


@pytest.fixture
def local_server(tmp_path):
    started = threading.Event()
    stop = threading.Event()
    cookies = []

    class Handler(SimpleHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):  # noqa: N802
            if self.path == "/stalled":
                started.set()
                stop.wait(20)
                self.close_connection = True
                return
            if self.path == "/captions":
                cookies.append(self.headers.get("Cookie"))
                self.send_response(200)
                self.send_header("Set-Cookie", "visitor=local-test; Path=/")
                body = b'{"events":[]}'
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            super().do_GET()

    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(Handler, directory=str(tmp_path)))
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", started, cookies
    finally:
        stop.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()


@pytest.mark.parametrize("cancel", [True, False])
def test_real_blocked_network_request_is_cancellable_and_bounded(
    tmp_path, local_server, monkeypatch, cancel,
):
    base, connected, _cookies = local_server
    monkeypatch.setattr(youtube, "CAPTION_TIMEOUT", 3.0)
    request = youtube._YouTubeRequest(tmp_path, URL, "ffmpeg.exe", youtube.youtube_runtime_path())
    started = time.monotonic()
    with pytest.raises(
        youtube.YouTubeCancelled if cancel else youtube.YouTubeError,
    ):
        youtube._run_youtube_stage(
            request, "captions", base + "/stalled", [], lambda *_args: None,
            connected.is_set if cancel else lambda: False,
        )
    assert connected.is_set()
    assert time.monotonic() - started < (6 if cancel else 12)
    assert request.ipv4 is not cancel
    assert list(tmp_path.iterdir()) == []


def test_spawned_network_stages_preserve_automatic_session_cookies(tmp_path, local_server):
    base, _connected, cookies = local_server
    request = youtube._YouTubeRequest(tmp_path, URL, "ffmpeg.exe", youtube.youtube_runtime_path())
    for _ in range(2):
        assert youtube._run_youtube_stage(
            request, "captions", base + "/captions", [], lambda *_args: None, lambda: False,
        ) == {"events": []}
    assert cookies == [None, "visitor=local-test"]


@pytest.mark.integration
def test_real_spawned_transfer_probe_and_diagnostics_publish_only_complete_media(
    tmp_path, local_server, monkeypatch,
):
    media = MediaTools()
    source = tmp_path / "fixture.mp4"
    media.run([
        media.ffmpeg, "-v", "error", "-f", "lavfi", "-i", "color=s=32x32:r=10",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=16000", "-t", "1",
        "-c:v", "mpeg4", "-c:a", "aac", str(source),
    ], "Creating an offline download fixture")
    base, _connected, _cookies = local_server
    real_run = youtube.run_process_worker

    def run(target, args, **kwargs):
        if args[1] == "metadata":
            return youtube._YouTubeResponse({
                "id": "abcdefghijk", "title": "Generated local fixture", "extractor": "youtube",
                "webpage_url": URL,
                "formats": [{
                    "url": base + "/fixture.mp4", "format_id": "18", "ext": "mp4",
                    "vcodec": "mpeg4", "acodec": "aac", "filesize": source.stat().st_size,
                }],
            }, [])
        return real_run(target, args, **kwargs)

    monkeypatch.setattr(youtube, "run_process_worker", run)
    events = []
    with ApplicationDiagnostics(tmp_path / "diagnostics"):
        result = youtube.download_youtube(
            media, URL, tmp_path, "auto", progress=lambda *event: events.append(event),
            cancelled=lambda: False,
        )
    assert result.video_path.read_bytes() == source.read_bytes()
    assert result.duration == pytest.approx(1.0, abs=0.1)
    assert events[-1] == ("YouTube video ready", 1.0)
    assert not list(tmp_path.glob(".cvpc-youtube-*"))
    records = [
        json.loads(line)
        for line in application_log_path(tmp_path / "diagnostics").read_text().splitlines()
    ]
    assert any(row["event"] == "youtube_transfer" and row.get("worker_pid") for row in records)
    assert any(row["event"] == "media_probed" and row.get("worker_pid") for row in records)


def test_http_access_errors_are_not_network_retry_candidates():
    from yt_dlp.networking.common import Response
    from yt_dlp.networking.exceptions import HTTPError

    for status in (401, 403, 404, 429):
        error = HTTPError(Response(io.BytesIO(), "https://youtube.com/", {}, status))
        assert not youtube._is_network_error(error)
