"""Windows desktop host for the shared HTML interface and local REST service.

pywebview is deliberately imported only when starting the app. The renderer,
REST API, and automated checks do not require a graphical environment.
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import os
import sys
import threading
import traceback
from pathlib import Path
from typing import Any

from .preferences import WorkspaceStore
from .media import IMAGE_EXTENSIONS
from .service import JobManager, ServiceError, default_data_root
from .web import create_server

APP_TITLE = "AudioVTTForge"
ACTIVE_STATES = {"queued", "running"}


def desktop_data_root() -> Path:
    """Keep desktop caches separate from an independently running web server."""
    return default_data_root().parent / "desktop"


class DesktopJobManager(JobManager):
    """Stop accepting new work while closing, then stop every owned child."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self._accepting_jobs = True
        super().__init__(*args, **kwargs)

    def submit(self, payload: dict[str, Any]):
        with self._lock:
            if not self._accepting_jobs:
                raise ServiceError("应用正在关闭，请稍后重试。")
            return super().submit(payload)

    def active_jobs(self) -> list[str]:
        with self._lock:
            return [record.job_id for record in self._jobs.values() if record.state in ACTIVE_STATES]

    def cancel_active_jobs(self) -> None:
        with self._lock:
            self._accepting_jobs = False
            records = [record for record in self._jobs.values() if record.state in ACTIVE_STATES]
            # Signal the entire queue before waiting for any one task. Otherwise
            # stopping the first render could start the next queued render.
            for record in records:
                record.cancel_requested = True
                record.cancel_event.set()
        try:
            for record in records:
                try:
                    self.cancel(record.job_id)
                except ServiceError:
                    # Finishing between the snapshot and cancel is harmless.
                    if self.describe(record.job_id)["state"] in ACTIVE_STATES:
                        raise
        except Exception:
            with self._lock:
                self._accepting_jobs = True
            raise

    def shutdown(self, wait: bool = True, cancel_futures: bool = True) -> None:
        self.cancel_active_jobs()
        super().shutdown(wait=wait, cancel_futures=cancel_futures)


def _on_window_thread(window: Any, callback: Any) -> Any:
    """Run a native modal dialog on the WinForms window's STA thread.

    pywebview dispatches JS API calls on ordinary Python worker threads, but
    its Windows folder-dialog implementation calls COM directly. Marshal the
    dialog to its owner instead of opening it on that worker thread.
    """
    native = getattr(window, "native", None)
    if native is None or not native.InvokeRequired:
        return callback()
    # Loaded by pywebview on Windows; keep the import out of headless paths.
    from System import Action

    result: list[Any] = []
    errors: list[Exception] = []

    def run() -> None:
        try:
            result.append(callback())
        except Exception as exc:
            errors.append(exc)

    native.Invoke(Action(run))
    if errors:
        raise errors[0]
    return result[0]


class DesktopBridge:
    """Only the local application page receives these native capabilities."""

    def __init__(self, manager: DesktopJobManager, store: WorkspaceStore, webview: Any) -> None:
        self._manager = manager
        self._store = store
        self._webview = webview
        self._window: Any = None
        self._picker_lock = threading.Lock()

    def _bind_window(self, window: Any) -> None:
        self._window = window

    def get_workspace(self) -> dict[str, Any]:
        return self._store.load()

    def save_workspace(self, data: dict[str, Any]) -> bool:
        return self._store.save(data)

    def select_directory(self, kind: str, initial: str = "") -> str | None:
        if kind not in {"source", "output"}:
            raise ValueError("请选择素材目录或输出目录。")
        if self._window is None:
            raise RuntimeError("桌面窗口尚未就绪。")
        if not self._picker_lock.acquire(blocking=False):
            return None
        try:
            candidates = (initial, self._store.load()[f"{kind}_dir"], str(Path.home()))
            directory = next(
                (str(Path(value).expanduser()) for value in candidates if value and Path(value).expanduser().is_dir()),
                str(Path.home()),
            )
            selected = _on_window_thread(
                self._window,
                lambda: self._window.create_file_dialog(
                    self._webview.FileDialog.FOLDER,
                    directory=directory,
                    allow_multiple=False,
                ),
            )
            if not selected:
                return None
            result = Path(selected[0]).expanduser().resolve(strict=True)
            if not result.is_dir():
                raise ValueError("选择的路径不是目录。")
            return str(result)
        finally:
            self._picker_lock.release()

    def select_images(self, initial: str = "") -> list[str] | None:
        if self._window is None:
            raise RuntimeError("桌面窗口尚未就绪。")
        if not self._picker_lock.acquire(blocking=False):
            return None
        try:
            saved = self._store.load()
            recent_image = next(iter(saved.get("image_paths", [])), "")
            candidates = (initial, str(Path(recent_image).parent) if recent_image else "",
                          saved["source_dir"], str(Path.home()))
            directory = next((str(Path(value).expanduser()) for value in candidates
                              if value and Path(value).expanduser().is_dir()), str(Path.home()))
            extensions = ";".join(f"*{ext}" for ext in sorted(IMAGE_EXTENSIONS))
            selected = _on_window_thread(
                self._window,
                lambda: self._window.create_file_dialog(
                    self._webview.FileDialog.OPEN,
                    directory=directory,
                    allow_multiple=True,
                    file_types=(f"图片文件 ({extensions})",),
                ),
            )
            if not selected:
                return None
            paths = list(dict.fromkeys(Path(value).expanduser().resolve(strict=True) for value in selected))
            if any(not path.is_file() or path.suffix.lower() not in IMAGE_EXTENSIONS for path in paths):
                raise ValueError("请选择支持的图片文件。")
            return [str(path) for path in paths]
        finally:
            self._picker_lock.release()

    def open_output(self, job_id: str) -> str:
        output = self._manager.output_path(job_id)
        directory = output.parent.resolve(strict=True)
        os.startfile(str(directory), "open")
        return str(directory)


class WindowLifecycle:
    def __init__(self, manager: DesktopJobManager, window: Any, store: WorkspaceStore | None = None) -> None:
        self.manager = manager
        self.window = window
        self.store = store
        self._closing = False
        self._ready = False
        self._lock = threading.Lock()

    def on_closing(self) -> bool:
        with self._lock:
            if self._ready:
                return True
            if self._closing:
                return False
            if self.manager.active_jobs() and not self.window.create_confirmation_dialog(
                "停止任务并退出",
                "仍有合成任务正在运行或排队。退出会取消这些任务，保留原始素材和已完成的视频。是否退出？",
            ):
                return False
            self._closing = True
        self.window.set_title("AudioVTTForge · 正在保存并退出…")
        threading.Thread(target=self._finish_close, name="desktop-close", daemon=False).start()
        return False

    def _save_before_close(self) -> None:
        if self.store is None:
            return
        finished = threading.Event()
        result: list[Any] = []

        def complete(value: Any) -> None:
            result.append(value)
            finished.set()

        self.window.evaluate_js("""(async () => {
            try {
                const snapshot = typeof window.audioVttFlushWorkspace === 'function'
                    ? await window.audioVttFlushWorkspace() : null;
                return {ok: true, snapshot};
            } catch (error) {
                return {ok: false, error: String(error.message || error)};
            }
        })()""", complete)
        if not finished.wait(timeout=10):
            raise RuntimeError("设置保存未完成，请稍后再次关闭窗口。")
        saved = result[0]
        if not isinstance(saved, dict) or not saved.get("ok"):
            raise RuntimeError(f"设置未能保存：{saved.get('error', '未知错误') if isinstance(saved, dict) else saved}")
        if isinstance(saved.get("snapshot"), dict):
            self.store.save(saved["snapshot"])

    def _finish_close(self) -> None:
        try:
            self._save_before_close()
            self.manager.shutdown(wait=True)
        except Exception as exc:
            with self._lock:
                self._closing = False
            self.window.set_title(APP_TITLE)
            try:
                self.window.evaluate_js("window.audioVttResumeWorkspace?.()")
            except Exception:
                pass
            self.window.create_confirmation_dialog(
                "未能完成退出",
                f"请稍后再次关闭窗口。\n\n{exc}",
            )
            return
        with self._lock:
            self._ready = True
        self.window.destroy()


class SingleInstance:
    """A kernel-owned lifetime guard and activation event, scoped to data root."""

    def __init__(self, root: Path) -> None:
        if os.name != "nt":
            raise RuntimeError("此桌面版本目前支持 Windows 10/11。")
        from ctypes import wintypes

        self._kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self._kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        self._kernel.CreateMutexW.restype = wintypes.HANDLE
        self._kernel.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
        self._kernel.CreateEventW.restype = wintypes.HANDLE
        self._kernel.SetEvent.argtypes = [wintypes.HANDLE]
        self._kernel.SetEvent.restype = wintypes.BOOL
        self._kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        self._kernel.WaitForSingleObject.restype = wintypes.DWORD
        self._kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self._kernel.CloseHandle.restype = wintypes.BOOL
        identity = hashlib.sha256(str(root.resolve()).casefold().encode("utf-8")).hexdigest()
        name = f"Local\\AudioVTTForge-{identity}"
        ctypes.set_last_error(0)
        self._mutex = self._kernel.CreateMutexW(None, False, name)
        if not self._mutex:
            raise ctypes.WinError(ctypes.get_last_error())
        self.is_primary = ctypes.get_last_error() != 183  # ERROR_ALREADY_EXISTS
        self._event = self._kernel.CreateEventW(None, False, False, name + "-activate")
        if not self._event:
            self._kernel.CloseHandle(self._mutex)
            raise ctypes.WinError(ctypes.get_last_error())
        self._stopped = threading.Event()
        self._watcher: threading.Thread | None = None

    def activate(self) -> None:
        self._kernel.SetEvent(self._event)

    def watch(self, window: Any) -> None:
        if self._watcher is not None:
            return
        minimized = threading.Event()
        window.events.minimized += minimized.set
        window.events.restored += minimized.clear
        window.events.maximized += minimized.clear

        def listen() -> None:
            while not self._stopped.is_set():
                if self._kernel.WaitForSingleObject(self._event, 250) == 0:
                    if self._stopped.is_set():
                        break
                    try:
                        if minimized.is_set():
                            window.restore()
                        window.show()
                    except Exception:
                        # Closing the first instance can race with activation.
                        pass

        self._watcher = threading.Thread(target=listen, name="desktop-activate", daemon=True)
        self._watcher.start()

    def close(self) -> None:
        self._stopped.set()
        self._kernel.SetEvent(self._event)
        if self._watcher is not None:
            self._watcher.join(timeout=2)
        self._kernel.CloseHandle(self._event)
        self._kernel.CloseHandle(self._mutex)


def show_startup_error(message: str) -> None:
    if sys.stderr is not None:
        print(message, file=sys.stderr)
    if os.name == "nt":
        ctypes.windll.user32.MessageBoxW(None, message, "AudioVTTForge 无法启动", 0x10)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="启动 AudioVTTForge Windows 桌面应用。")
    parser.add_argument("--data-root", type=Path, default=None, help="工作区设置和任务缓存目录")
    parser.add_argument("--debug", action="store_true", help="启用 WebView 开发工具")
    parser.add_argument("--debug-port", type=int, default=None, help="开发测试用 WebView2 调试端口")
    args = parser.parse_args(argv)
    if args.debug_port is not None and not 1024 <= args.debug_port <= 65535:
        parser.error("--debug-port 必须在 1024 到 65535 之间")
    instance = None
    manager = None
    server = None
    server_thread = None
    root = None
    try:
        root = (args.data_root or desktop_data_root()).expanduser().resolve()
        instance = SingleInstance(root)
        if not instance.is_primary:
            instance.activate()
            return 0
        try:
            import webview
        except ImportError as exc:
            raise RuntimeError("缺少桌面运行依赖。请使用项目的桌面启动器，或安装 requirements.txt 中的依赖。") from exc
        manager = DesktopJobManager(root)
        server = create_server("127.0.0.1", 0, manager=manager)
        server_thread = threading.Thread(target=server.serve_forever, name="desktop-rest", daemon=True)
        server_thread.start()
        store = manager.workspace
        bridge = DesktopBridge(manager, store, webview)
        window = webview.create_window(
            APP_TITLE,
            f"http://127.0.0.1:{server.server_port}/?desktop=1",
            js_api=bridge,
            width=1320,
            height=880,
            min_size=(960, 680),
            background_color="#111317",
        )
        bridge._bind_window(window)
        lifecycle = WindowLifecycle(manager, window, store)
        window.events.closing += lifecycle.on_closing
        window.events.shown += lambda: instance.watch(window)
        webview.settings["ALLOW_DOWNLOADS"] = True
        webview.settings["REMOTE_DEBUGGING_PORT"] = args.debug_port
        # Force modern Edge WebView2; falling back to IE cannot render this UI.
        webview.start(gui="edgechromium", debug=args.debug)
        return 0
    except Exception as exc:
        diagnostic = ""
        if root is not None:
            try:
                root.mkdir(parents=True, exist_ok=True)
                log_path = root / "startup-error.log"
                log_path.write_text(traceback.format_exc(), encoding="utf-8")
                diagnostic = f"\n\n启动日志：{log_path}"
            except OSError:
                pass
        show_startup_error(
            f"{exc}\n\n请确认 Microsoft Edge WebView2 Runtime 已安装。{diagnostic}"
        )
        return 1
    finally:
        if server is not None:
            if server_thread is not None and server_thread.is_alive():
                server.shutdown()
                server_thread.join(timeout=5)
            server.server_close()
        elif manager is not None:
            manager.shutdown(wait=True)
        if instance is not None:
            instance.close()


if __name__ == "__main__":
    raise SystemExit(main())
