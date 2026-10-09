import json
import socket
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlencode

import pytest

from audiovttforge import web as web_module
from audiovttforge.service import JobManager
from audiovttforge.web import AudioVTTForgeServer


FAKE_TOOL = """\
import json
import pathlib
import sys

args = sys.argv[1:]
if "-show_entries" in args:
    print(json.dumps({"streams": [{"duration": "1.250"}]}))
    raise SystemExit(0)
output = pathlib.Path(args[-1])
output.parent.mkdir(parents=True, exist_ok=True)
output.write_bytes(b"fake mp4")
"""

SLOW_TOOL = """\
import json
import pathlib
import sys
import time

args = sys.argv[1:]
if "-show_entries" in args:
    print(json.dumps({"streams": [{"duration": "1.250"}]}))
    raise SystemExit(0)
output = pathlib.Path(args[-1])
output.parent.mkdir(parents=True, exist_ok=True)
output.with_suffix(".started").write_text("started")
while True:
    time.sleep(1)
"""


def request_json(url: str, method: str = "GET", payload: dict | None = None) -> dict:
    body = None
    headers = {}
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    with urllib.request.urlopen(request, timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))


def multipart_body(files: list[tuple[str, bytes]]) -> tuple[bytes, str]:
    boundary = "----AudioVTTForgeTestBoundary"
    chunks: list[bytes] = []
    for name, content in files:
        chunks.extend(
            [
                f"--{boundary}\r\n".encode(),
                f'Content-Disposition: form-data; name="files"; filename="{name}"\r\n'.encode(),
                b"Content-Type: application/octet-stream\r\n\r\n",
                content,
                b"\r\n",
            ]
        )
    chunks.append(f"--{boundary}--\r\n".encode())
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def wait_for_state(url: str, state: str) -> dict:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        current = request_json(url)
        if current["state"] == state:
            return current
        time.sleep(0.02)
    raise AssertionError(f"job did not reach {state}")


def test_rest_resources_serve_ui_upload_and_job(tmp_path: Path) -> None:
    tool = tmp_path / "fake_tool.py"
    tool.write_text(FAKE_TOOL, encoding="utf-8")
    manager = JobManager(tmp_path / "data")
    static_root = Path(__file__).parents[1] / "audiovttforge" / "web_static"
    server = AudioVTTForgeServer(("127.0.0.1", 0), manager, static_root)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_port}"
    try:
        assert request_json(f"{base_url}/api/v1/health")["status"] == "ok"
        with urllib.request.urlopen(f"{base_url}/", timeout=5) as response:
            assert "AudioVTTForge" in response.read().decode("utf-8")

        body, content_type = multipart_body(
            [("01.wav", b"audio"), ("01.wav.vtt", b"WEBVTT\n"), ("cover.png", b"image")]
        )
        upload_request = urllib.request.Request(
            f"{base_url}/api/v1/uploads",
            data=body,
            headers={"Content-Type": content_type},
            method="POST",
        )
        with urllib.request.urlopen(upload_request, timeout=5) as response:
            uploads = json.loads(response.read().decode("utf-8"))["items"]
        by_name = {item["name"]: item for item in uploads}

        job = request_json(
            f"{base_url}/api/v1/jobs",
            "POST",
            {
                "files": [item["id"] for item in uploads],
                "audio": [by_name["01.wav"]["id"]],
                "images": [by_name["cover.png"]["id"]],
                "workers": 1,
                "ffmpeg": str(tool),
                "ffprobe": str(tool),
            },
        )
        completed = wait_for_state(f"{base_url}/api/v1/jobs/{job['id']}", "succeeded")
        assert completed["output_ready"] is True
        assert Path(completed["output_path"]).parent == manager.outputs_root
        assert not manager.get(job["id"]).root.exists()
        events = request_json(f"{base_url}/api/v1/jobs/{job['id']}/events")
        assert events["items"][-1]["type"] == "job_finished"
        with urllib.request.urlopen(f"{base_url}{completed['download_url']}", timeout=5) as response:
            assert response.read() == b"fake mp4"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_rest_scans_local_source_directory(tmp_path: Path) -> None:
    source = tmp_path / "episode01"
    source.mkdir()
    (source / "10.wav").write_bytes(b"audio")
    (source / "01.wav").write_bytes(b"audio")
    (source / "01.wav.vtt").write_text("WEBVTT\n", encoding="utf-8")
    (source / "cover.png").write_bytes(b"image")
    manager = JobManager(tmp_path / "data")
    static_root = Path(__file__).parents[1] / "audiovttforge" / "web_static"
    server = AudioVTTForgeServer(("127.0.0.1", 0), manager, static_root)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        scan = request_json(
            f"http://127.0.0.1:{server.server_port}/api/v1/sources/scan",
            "POST",
            {"path": str(source)},
        )
        assert [item["name"] for item in scan["audio"]] == ["01.wav", "10.wav"]
        assert scan["audio"][0]["subtitle"] == "01.wav.vtt"
        assert "缺少字幕：10.wav.vtt" in scan["warnings"]
        assert scan["ready"] is True
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_rest_serves_only_images_found_in_scanned_source_directory(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    image = source / "cover.png"
    image.write_bytes(b"png preview bytes")
    (source / "notes.txt").write_text("not an image", encoding="utf-8")
    manager = JobManager(tmp_path / "data")
    static_root = Path(__file__).parents[1] / "audiovttforge" / "web_static"
    server = AudioVTTForgeServer(("127.0.0.1", 0), manager, static_root)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_port}"
    try:
        query = urlencode({"directory": str(source), "name": image.name})
        with urllib.request.urlopen(f"{base_url}/api/v1/sources/image?{query}", timeout=5) as response:
            assert response.headers.get_content_type() == "image/png"
            assert response.read() == image.read_bytes()

        query = urlencode({"directory": str(source), "name": "notes.txt"})
        try:
            urllib.request.urlopen(f"{base_url}/api/v1/sources/image?{query}", timeout=5)
        except urllib.error.HTTPError as exc:
            assert exc.code == 404
        else:
            raise AssertionError("non-image files must not be served by the preview route")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_rest_download_streams_file_with_non_ascii_output_name(tmp_path: Path) -> None:
    tool = tmp_path / "fake_tool.py"
    tool.write_text(FAKE_TOOL, encoding="utf-8")
    source = tmp_path / "source"
    source.mkdir()
    (source / "01.wav").write_bytes(b"audio")
    (source / "cover.png").write_bytes(b"image")
    manager = JobManager(tmp_path / "data")
    static_root = Path(__file__).parents[1] / "audiovttforge" / "web_static"
    server = AudioVTTForgeServer(("127.0.0.1", 0), manager, static_root)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_port}"
    try:
        job = request_json(
            f"{base_url}/api/v1/jobs",
            "POST",
            {
                "source_dir": str(source),
                "output_dir": str(tmp_path / "exports"),
                "output_name": "第01集.mp4",
                "workers": 1,
                "ffmpeg": str(tool),
                "ffprobe": str(tool),
            },
        )
        completed = wait_for_state(f"{base_url}/api/v1/jobs/{job['id']}", "succeeded")
        assert Path(completed["output_path"]).name == "第01集.mp4"
        with urllib.request.urlopen(f"{base_url}{completed['download_url']}", timeout=5) as response:
            assert response.headers["Content-Length"] == str(len(b"fake mp4"))
            disposition = response.headers["Content-Disposition"]
            # send_header encodes latin-1, so the CJK name must travel in the
            # RFC 5987 form with an ASCII fallback.
            assert "filename*=UTF-8''" in disposition
            assert "%E7%AC%AC01%E9%9B%86.mp4" in disposition
            assert response.read() == b"fake mp4"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        manager.shutdown()


def test_rest_exposes_capabilities_and_job_collection(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "01.wav").write_bytes(b"audio")
    (source / "cover.png").write_bytes(b"image")
    manager = JobManager(tmp_path / "data")
    static_root = Path(__file__).parents[1] / "audiovttforge" / "web_static"
    server = AudioVTTForgeServer(("127.0.0.1", 0), manager, static_root)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_port}"
    try:
        report = request_json(f"{base_url}/api/v1/capabilities")
        assert report["defaults"]["subtitle"] == "burnin"
        assert report["limits"]["workers"] == [1, 10]
        assert report["options"]["workers"] == list(range(1, 11))
        assert ".wav" in report["upload_kinds"]["audio"]

        assert request_json(f"{base_url}/api/v1/jobs")["items"] == []

        job = request_json(
            f"{base_url}/api/v1/jobs",
            "POST",
            {"source_dir": str(source), "workers": 1},
        )
        listing = request_json(f"{base_url}/api/v1/jobs")
        assert listing["total"] == 1
        assert [item["id"] for item in listing["items"]] == [job["id"]]

        try:
            request_json(f"{base_url}/api/v1/jobs/does-not-exist")
        except urllib.error.HTTPError as exc:
            assert exc.code == 404
        else:
            raise AssertionError("unknown job must be reported as 404")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        manager.shutdown()


def test_static_root_follows_the_frozen_bundle_layout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = tmp_path / "bundle"
    assets = bundle / "audiovttforge" / "web_static"
    assets.mkdir(parents=True)
    (assets / "index.html").write_text("<!doctype html>", encoding="utf-8")

    monkeypatch.setattr(web_module.sys, "frozen", True, raising=False)
    monkeypatch.setattr(web_module.sys, "_MEIPASS", str(bundle), raising=False)

    assert web_module.static_root() == assets
    server = web_module.create_server("127.0.0.1", 0, tmp_path / "data")
    try:
        assert server.static_root == assets.resolve()
    finally:
        server.server_close()


def test_main_uses_a_free_port_when_the_requested_one_is_busy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    blocker = socket.socket()
    blocker.bind(("127.0.0.1", 0))
    blocker.listen(1)
    busy_port = blocker.getsockname()[1]
    served: list[int] = []

    def fake_serve_forever(self: AudioVTTForgeServer, poll_interval: float = 0.5) -> None:
        served.append(self.server_address[1])

    monkeypatch.setattr(AudioVTTForgeServer, "serve_forever", fake_serve_forever)
    # shutdown() would block because serve_forever never set its event.
    monkeypatch.setattr(AudioVTTForgeServer, "shutdown", lambda self: None)
    try:
        assert web_module.main(
            ["--host", "127.0.0.1", "--port", str(busy_port), "--data-root", str(tmp_path / "data")]
        ) == 0
    finally:
        blocker.close()

    output = capsys.readouterr().out
    assert "unavailable" in output
    assert served and served[0] != busy_port


def test_rest_delete_cancels_running_job_and_cleans_temporary_files(tmp_path: Path) -> None:
    tool = tmp_path / "slow_tool.py"
    tool.write_text(SLOW_TOOL, encoding="utf-8")
    source = tmp_path / "source"
    source.mkdir()
    (source / "01.wav").write_bytes(b"audio")
    (source / "cover.png").write_bytes(b"image")
    manager = JobManager(tmp_path / "data")
    static_root = Path(__file__).parents[1] / "audiovttforge" / "web_static"
    server = AudioVTTForgeServer(("127.0.0.1", 0), manager, static_root)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_port}"
    try:
        job = request_json(
            f"{base_url}/api/v1/jobs",
            "POST",
            {
                "source_dir": str(source),
                "workers": 1,
                "ffmpeg": str(tool),
                "ffprobe": str(tool),
            },
        )
        record = manager.get(job["id"])
        work = record.job.output.parent / f".{record.job.output.stem}_parallel_work"
        started = work / "0000.started"
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not started.exists():
            time.sleep(0.02)
        assert started.is_file(), "fake FFmpeg did not start"

        cancelled = request_json(f"{base_url}/api/v1/jobs/{job['id']}", "DELETE")

        assert cancelled["state"] == "cancelled"
        assert cancelled["cancelable"] is False
        assert not record.root.exists()
        assert (source / "01.wav").is_file()
        assert (source / "cover.png").is_file()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        manager.shutdown()


def test_rest_rejects_non_json_job_body(tmp_path: Path) -> None:
    manager = JobManager(tmp_path / "data")
    static_root = Path(__file__).parents[1] / "audiovttforge" / "web_static"
    server = AudioVTTForgeServer(("127.0.0.1", 0), manager, static_root)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        request = urllib.request.Request(
            f"http://127.0.0.1:{server.server_port}/api/v1/jobs",
            data=b"{}",
            headers={"Content-Type": "text/plain"},
            method="POST",
        )
        try:
            urllib.request.urlopen(request, timeout=5)
        except urllib.error.HTTPError as exc:
            assert exc.code == 415
        else:
            raise AssertionError("request should fail with 415")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_rest_returns_validation_error_for_invalid_job(tmp_path: Path) -> None:
    manager = JobManager(tmp_path / "data")
    upload = manager.upload("01.wav", b"audio")
    static_root = Path(__file__).parents[1] / "audiovttforge" / "web_static"
    server = AudioVTTForgeServer(("127.0.0.1", 0), manager, static_root)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        request = urllib.request.Request(
            f"http://127.0.0.1:{server.server_port}/api/v1/jobs",
            data=json.dumps(
                {
                    "files": [upload.upload_id],
                    "audio": [upload.upload_id],
                    "assignments": "not-an-array",
                }
            ).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            urllib.request.urlopen(request, timeout=5)
        except urllib.error.HTTPError as exc:
            assert exc.code == 422
            payload = json.loads(exc.read().decode("utf-8"))
            assert payload["error"]["status"] == 422
            assert "assignments" in payload["error"]["message"]
        else:
            raise AssertionError("request should fail with 422")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
