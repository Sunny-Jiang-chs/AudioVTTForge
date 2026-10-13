import json
import os
import sys
import threading
import time
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.parse import urlsplit

import pytest

from audiovttforge import desktop
from audiovttforge.service import NotFoundError, ServiceError


SLOW_TOOL = """\
import json
import pathlib
import sys
import time

if "-show_entries" in sys.argv:
    print(json.dumps({"streams": [{"duration": "1.250"}]}))
    raise SystemExit(0)
output = pathlib.Path(sys.argv[-1])
output.parent.mkdir(parents=True, exist_ok=True)
output.with_suffix(".started").write_text("started")
while True:
    time.sleep(1)
"""


def test_workspace_persists_paths_and_filename_assignments_across_instances(tmp_path: Path) -> None:
    root = tmp_path / "设置 缓存"
    store = desktop.WorkspaceStore(root)
    source = str(tmp_path / "素材 第一集")
    assert store.save({
        "source_dir": source,
        "output_dir": str(tmp_path / "导出 视频"),
        "output_follows_source": False,
        "output_name": "第一集.mp4",
        "output_name_touched": True,
        "settings": {"font-size": "48", "merge-subtitles": True},
        "recent_sources": [source, source],
        "assignments": {"01 中文.wav": "封面 图片.png", "02.wav": None},
    })
    restored = desktop.WorkspaceStore(root).load()
    assert restored["source_dir"] == source
    assert restored["recent_sources"] == [source]
    assert restored["output_follows_source"] is False
    assert restored["settings"] == {"font-size": "48", "merge-subtitles": True}
    assert restored["assignments"]["01 中文.wav"] == "封面 图片.png"
    assert restored["assignments"]["02.wav"] is None
    store.save({"output_name": "第二集.mp4"})
    assert store.load()["settings"] == restored["settings"]
    assert not store.path.with_suffix(".json.tmp").exists()


def test_workspace_handles_corrupt_file_without_overwriting_it(tmp_path: Path) -> None:
    store = desktop.WorkspaceStore(tmp_path)
    store.path.write_text("invalid json", encoding="utf-8")
    assert store.load()["source_dir"] == ""
    assert store.path.read_text(encoding="utf-8") == "invalid json"
    with pytest.raises(ValueError):
        store.save({"assignments": {"01.wav": 4}})
    assert store.path.read_text(encoding="utf-8") == "invalid json"


def test_picker_returns_native_unicode_path_and_cancellation_keeps_settings(tmp_path: Path) -> None:
    source = tmp_path / "中文 素材"
    source.mkdir()
    store = desktop.WorkspaceStore(tmp_path / "data")
    store.save({"source_dir": str(source)})
    bridge = desktop.DesktopBridge(Mock(), store, SimpleNamespace(FileDialog=SimpleNamespace(FOLDER=30)))
    window = Mock(native=None)
    window.create_file_dialog.return_value = (str(source),)
    bridge._bind_window(window)
    assert bridge.select_directory("source") == str(source.resolve())
    window.create_file_dialog.assert_called_once_with(30, directory=str(source), allow_multiple=False)
    window.create_file_dialog.return_value = None
    before = store.load()
    assert bridge.select_directory("output", str(source)) is None
    assert store.load() == before
    with pytest.raises(ValueError):
        bridge.select_directory("anything")


def test_picker_marshals_dialog_to_window_thread(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    selected = tmp_path / "选择 素材"
    selected.mkdir()
    native = Mock(InvokeRequired=True)
    in_ui_call = threading.Event()

    def invoke(callback):
        in_ui_call.set()
        try:
            callback()
        finally:
            in_ui_call.clear()

    def dialog(*_args, **_kwargs):
        assert in_ui_call.is_set(), "COM folder picker must run in the native Invoke callback"
        return (str(selected),)

    native.Invoke.side_effect = invoke
    window = Mock(native=native)
    window.create_file_dialog.side_effect = dialog
    monkeypatch.setitem(sys.modules, "System", SimpleNamespace(Action=lambda callback: callback))
    bridge = desktop.DesktopBridge(Mock(), desktop.WorkspaceStore(tmp_path), SimpleNamespace(FileDialog=SimpleNamespace(FOLDER=30)))
    bridge._bind_window(window)
    assert bridge.select_directory("source") == str(selected)
    native.Invoke.assert_called_once()


def test_native_dispatch_propagates_dialog_errors_and_releases_picker_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    native = Mock(InvokeRequired=True)
    native.Invoke.side_effect = lambda callback: callback()
    monkeypatch.setitem(sys.modules, "System", SimpleNamespace(Action=lambda callback: callback))
    window = Mock(native=native)
    window.create_file_dialog.side_effect = [RuntimeError("dialog failed"), None]
    bridge = desktop.DesktopBridge(Mock(), desktop.WorkspaceStore(tmp_path), SimpleNamespace(FileDialog=SimpleNamespace(FOLDER=30)))
    bridge._bind_window(window)
    with pytest.raises(RuntimeError, match="dialog failed"):
        bridge.select_directory("source")
    assert bridge.select_directory("source") is None
    assert window.create_file_dialog.call_count == 2


def test_open_output_only_uses_a_completed_job_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    output = tmp_path / "导出 文件" / "视频.mp4"
    output.parent.mkdir()
    output.write_bytes(b"mp4")
    manager = Mock()
    manager.output_path.return_value = output
    open_directory = Mock()
    monkeypatch.setattr(desktop.os, "startfile", open_directory, raising=False)
    bridge = desktop.DesktopBridge(manager, desktop.WorkspaceStore(tmp_path), Mock())
    assert bridge.open_output("completed-id") == str(output.parent)
    manager.output_path.assert_called_once_with("completed-id")
    open_directory.assert_called_once_with(str(output.parent), "open")
    manager.output_path.side_effect = NotFoundError("unknown job")
    with pytest.raises(NotFoundError):
        bridge.open_output("D:\\arbitrary.exe")
    assert open_directory.call_count == 1


def test_image_picker_returns_distinct_original_paths_without_copying(tmp_path: Path) -> None:
    first = tmp_path / "图库 一" / "封面.png"
    second = tmp_path / "图库 二" / "封面.png"
    for path in (first, second):
        path.parent.mkdir()
        path.write_bytes(b"image")
    window = Mock(native=None)
    window.create_file_dialog.return_value = (str(first), str(second), str(first))
    bridge = desktop.DesktopBridge(Mock(), desktop.WorkspaceStore(tmp_path / "data"),
                                   SimpleNamespace(FileDialog=SimpleNamespace(OPEN=10)))
    bridge._bind_window(window)
    assert bridge.select_images(str(first.parent)) == [str(first), str(second)]
    assert window.create_file_dialog.call_args.kwargs["allow_multiple"] is True
    assert window.create_file_dialog.call_args.kwargs["directory"] == str(first.parent)
    assert not (tmp_path / "data").exists()
    window.create_file_dialog.return_value = None
    assert bridge.select_images() is None
    invalid = tmp_path / "document.txt"
    invalid.write_text("text")
    window.create_file_dialog.return_value = (str(invalid),)
    with pytest.raises(ValueError, match="图片"):
        bridge.select_images()
    window.create_file_dialog.return_value = (str(second),)
    assert bridge.select_images() == [str(second)]


def test_close_flushes_last_edit_before_shutdown(tmp_path: Path) -> None:
    store = desktop.WorkspaceStore(tmp_path)
    manager = Mock()
    window = Mock()
    window.evaluate_js.side_effect = lambda _script, callback: callback({
        "ok": True, "snapshot": {"settings": {"font-size": "67", "fps": "12"}},
    })
    manager.shutdown.side_effect = lambda **_kwargs: (
        store.load()["settings"]["font-size"] == "67" or pytest.fail("last edit not saved")
    )
    desktop.WindowLifecycle(manager, window, store)._finish_close()
    assert desktop.WorkspaceStore(tmp_path).load()["settings"]["fps"] == "12"
    window.destroy.assert_called_once()


def test_close_keeps_window_open_when_final_settings_save_fails(tmp_path: Path) -> None:
    manager = Mock()
    window = Mock()
    window.evaluate_js.side_effect = lambda _script, callback: callback({"ok": False, "error": "disk full"})
    lifecycle = desktop.WindowLifecycle(manager, window, desktop.WorkspaceStore(tmp_path))
    lifecycle._closing = True
    lifecycle._finish_close()
    manager.shutdown.assert_not_called()
    window.destroy.assert_not_called()
    assert lifecycle._closing is False
    assert "disk full" in window.create_confirmation_dialog.call_args.args[1]


def test_shutdown_cancels_running_and_queued_processes(tmp_path: Path) -> None:
    tool = tmp_path / "slow_tool.py"
    tool.write_text(SLOW_TOOL, encoding="utf-8")
    source = tmp_path / "中文 素材"
    source.mkdir()
    audio = source / "01.wav"
    audio.write_bytes(b"audio")
    manager = desktop.DesktopJobManager(tmp_path / "data")
    payload = {"source_dir": str(source), "ffmpeg": str(tool), "ffprobe": str(tool), "workers": 1}
    running = manager.submit(payload)
    queued = manager.submit({**payload, "output_name": "queued.mp4"})
    marker = running.job.output.parent / f".{running.job.output.stem}_parallel_work" / "0000.started"
    try:
        deadline = time.monotonic() + 8
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert marker.exists(), manager.describe(running.job_id)
        with manager._lock:
            processes = list(running.active_processes)
        assert processes
        manager.shutdown(wait=True)
        assert manager.describe(running.job_id)["state"] == "cancelled"
        assert manager.describe(queued.job_id)["state"] == "cancelled"
        assert all(process.poll() is not None for process in processes)
        assert not running.root.exists()
        assert not queued.root.exists()
        assert audio.read_bytes() == b"audio"
        with pytest.raises(ServiceError, match="正在关闭"):
            manager.submit(payload)
    finally:
        manager.shutdown(wait=True)


def test_close_declined_keeps_jobs_running() -> None:
    manager = Mock()
    manager.active_jobs.return_value = ["running"]
    window = Mock()
    window.create_confirmation_dialog.return_value = False
    lifecycle = desktop.WindowLifecycle(manager, window)
    assert lifecycle.on_closing() is False
    manager.shutdown.assert_not_called()
    window.destroy.assert_not_called()


def test_window_stays_open_until_workers_have_stopped() -> None:
    stop_started = threading.Event()
    permit_stop = threading.Event()
    window_destroyed = threading.Event()
    manager = Mock()
    manager.active_jobs.return_value = ["running"]

    def stop(**_kwargs):
        stop_started.set()
        assert permit_stop.wait(timeout=3)

    manager.shutdown.side_effect = stop
    window = Mock()
    window.create_confirmation_dialog.return_value = True
    window.destroy.side_effect = window_destroyed.set
    lifecycle = desktop.WindowLifecycle(manager, window)
    try:
        assert lifecycle.on_closing() is False
        assert stop_started.wait(timeout=3)
        window.destroy.assert_not_called()
        assert lifecycle.on_closing() is False
        assert window.create_confirmation_dialog.call_count == 1
    finally:
        permit_stop.set()
    assert window_destroyed.wait(timeout=3)
    assert lifecycle.on_closing() is True


def test_cancellation_failure_keeps_window_open_for_retry() -> None:
    manager = Mock()
    manager.shutdown.side_effect = ServiceError("worker is still stopping")
    window = Mock()
    lifecycle = desktop.WindowLifecycle(manager, window)
    lifecycle._closing = True
    lifecycle._finish_close()
    window.destroy.assert_not_called()
    window.create_confirmation_dialog.assert_called_once()
    assert lifecycle._closing is False


@pytest.mark.skipif(os.name != "nt", reason="Windows kernel activation")
def test_second_launch_activates_first_without_opening_job_storage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "单实例 缓存"
    first = desktop.SingleInstance(root)
    assert first.is_primary
    manager_factory = Mock(side_effect=AssertionError("second instance must not purge job cache"))
    monkeypatch.setattr(desktop, "DesktopJobManager", manager_factory)
    try:
        assert desktop.main(["--data-root", str(root)]) == 0
        assert first._kernel.WaitForSingleObject(first._event, 1000) == 0
        assert not root.exists()
        manager_factory.assert_not_called()
    finally:
        first.close()
    next_instance = desktop.SingleInstance(root)
    try:
        assert next_instance.is_primary
    finally:
        next_instance.close()


@pytest.mark.skipif(os.name != "nt", reason="Windows desktop entry point")
@pytest.mark.parametrize("fail_start", [False, True])
def test_entry_point_serves_shared_ui_without_console_and_always_closes_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fail_start: bool,
) -> None:
    class Event:
        def __iadd__(self, callback):
            self.callback = callback
            return self

    window = SimpleNamespace(events=SimpleNamespace(closing=Event(), shown=Event()))
    capture = {}

    def create_window(title, url, **kwargs):
        capture.update(url=url, bridge=kwargs["js_api"])
        assert title == desktop.APP_TITLE
        assert "desktop=1" in url
        return window

    def start(**kwargs):
        assert kwargs["gui"] == "edgechromium"
        parts = urlsplit(capture["url"])
        base = f"{parts.scheme}://{parts.netloc}"
        with urllib.request.urlopen(f"{base}/api/v1/health", timeout=3) as response:
            assert json.loads(response.read())["status"] == "ok"
        with urllib.request.urlopen(base, timeout=3) as response:
            assert b"AudioVTTForge" in response.read()
        assert capture["bridge"].get_workspace()["source_dir"] == ""
        if fail_start:
            raise RuntimeError("WebView2 failed to start")

    fake_webview = SimpleNamespace(create_window=create_window, start=start, settings={})
    monkeypatch.setitem(sys.modules, "webview", fake_webview)
    monkeypatch.setattr(sys, "stderr", None)
    error = Mock()
    monkeypatch.setattr(desktop, "show_startup_error", error)
    create_server = desktop.create_server

    def capture_server(*args, **kwargs):
        server = create_server(*args, **kwargs)
        capture["server"] = server
        return server

    monkeypatch.setattr(desktop, "create_server", capture_server)
    assert desktop.main(["--data-root", str(tmp_path / "entry")]) == int(fail_start)
    assert capture["server"].fileno() == -1
    assert fake_webview.settings["ALLOW_DOWNLOADS"] is True
    assert error.call_count == int(fail_start)
    if fail_start:
        assert "WebView2 failed to start" in error.call_args.args[0]
