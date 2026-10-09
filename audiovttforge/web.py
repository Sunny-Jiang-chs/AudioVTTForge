"""Local REST API and browser server for AudioVTTForge."""

from __future__ import annotations

import argparse
import json
import mimetypes
import shutil
import sys
import threading
import webbrowser
from email.parser import BytesParser
from email.policy import default as email_policy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlsplit

from .service import (
    JobManager,
    NotFoundError,
    RequestValidationError,
    ServiceError,
    capabilities,
    default_data_root,
)

MAX_JSON_BYTES = 2 * 1024 * 1024
MAX_UPLOAD_BYTES = 1024 * 1024 * 1024
DOWNLOAD_CHUNK_BYTES = 4 * 1024 * 1024

# Single source of truth for the API surface: (method, path pattern).  ``None``
# matches exactly one variable segment, so any new collection endpoint (for
# example "GET /api/v1/jobs") only needs a row here and a branch in _dispatch.
ROUTES: tuple[tuple[str, tuple[str | None, ...]], ...] = (
    ("GET", ("api", "v1", "health")),
    ("GET", ("api", "v1", "capabilities")),
    ("GET", ("api", "v1", "jobs")),
    ("GET", ("api", "v1", "jobs", None)),
    ("GET", ("api", "v1", "jobs", None, "events")),
    ("GET", ("api", "v1", "jobs", None, "download")),
    ("GET", ("api", "v1", "sources", "image")),
    ("POST", ("api", "v1", "sources", "scan")),
    ("POST", ("api", "v1", "uploads")),
    ("POST", ("api", "v1", "jobs")),
    ("DELETE", ("api", "v1", "jobs", None)),
)


def match_route(method: str, parts: list[str]) -> tuple[str | None, ...] | None:
    """Return the declared route pattern matching this request, if any."""
    for route_method, pattern in ROUTES:
        if route_method != method or len(pattern) != len(parts):
            continue
        if all(
            expected is None or expected == actual
            for expected, actual in zip(pattern, parts)
        ):
            return pattern
    return None


def content_disposition(name: str) -> str:
    """Build an attachment header that survives non-ASCII output names.

    ``send_header`` encodes headers as latin-1, so a raw CJK filename would raise
    while writing the response.  Keep an ASCII fallback plus the RFC 5987 form.
    """
    fallback = name.encode("ascii", "replace").decode("ascii").replace('"', "_")
    return f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{quote(name)}"


class HttpRequestError(Exception):
    def __init__(self, status: int, message: str, details: list[str] | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.details = details or []


class AudioVTTForgeServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], manager: JobManager, static_root: Path) -> None:
        self.manager = manager
        self.static_root = static_root.resolve()
        super().__init__(address, AudioVTTForgeHandler)

    def server_close(self) -> None:
        self.manager.shutdown()
        super().server_close()


class AudioVTTForgeHandler(BaseHTTPRequestHandler):
    server: AudioVTTForgeServer
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: object) -> None:
        # Keep the console useful when the server is launched from a terminal.
        super().log_message(format, *args)

    @property
    def manager(self) -> JobManager:
        return self.server.manager

    def _send_bytes(self, body: bytes, content_type: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            self.wfile.write(body)
        except BrokenPipeError:
            pass
        self.close_connection = True

    def _send_json(self, data: Any, status: int = 200) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self._send_bytes(body, "application/json; charset=utf-8", status)

    def _send_error(self, status: int, message: str, details: list[str] | None = None) -> None:
        payload: dict[str, Any] = {"error": {"status": status, "message": message}}
        if details:
            payload["error"]["details"] = details
        self._send_json(payload, status)

    def _read_body(self, limit: int) -> bytes:
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            raise HttpRequestError(411, "Content-Length is required")
        try:
            length = int(raw_length)
        except ValueError as exc:
            raise HttpRequestError(400, "Content-Length is invalid") from exc
        if length < 0 or length > limit:
            raise HttpRequestError(413, "Request body is too large")
        body = self.rfile.read(length)
        if len(body) != length:
            raise HttpRequestError(400, "Request body ended before Content-Length")
        return body

    def _read_json(self) -> dict[str, Any]:
        content_type = self.headers.get_content_type()
        if content_type != "application/json":
            raise HttpRequestError(415, "Content-Type must be application/json")
        try:
            data = json.loads(self._read_body(MAX_JSON_BYTES).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HttpRequestError(400, "Request body is not valid JSON") from exc
        if not isinstance(data, dict):
            raise HttpRequestError(400, "Request body must be a JSON object")
        return data

    def _read_multipart(self) -> list[tuple[str, bytes]]:
        content_type = self.headers.get("Content-Type", "")
        if not content_type.lower().startswith("multipart/form-data"):
            raise HttpRequestError(415, "Content-Type must be multipart/form-data")
        body = self._read_body(MAX_UPLOAD_BYTES)
        header = f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode()
        message = BytesParser(policy=email_policy).parsebytes(header + body)
        if not message.is_multipart():
            raise HttpRequestError(400, "Multipart request could not be parsed")
        files: list[tuple[str, bytes]] = []
        for part in message.iter_parts():
            filename = part.get_filename()
            if filename is None:
                continue
            payload = part.get_payload(decode=True) or b""
            files.append((filename, payload))
        if not files:
            raise HttpRequestError(400, "No files were included in the upload")
        return files

    def _serve_static(self, relative: str) -> None:
        if relative in {"", "/"}:
            relative = "index.html"
        relative = unquote(relative).lstrip("/")
        candidate = (self.server.static_root / relative).resolve()
        try:
            candidate.relative_to(self.server.static_root)
        except ValueError as exc:
            raise HttpRequestError(404, "Static resource not found") from exc
        if not candidate.is_file():
            raise HttpRequestError(404, "Static resource not found")
        content_type = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
        self._send_bytes(candidate.read_bytes(), content_type)

    def _dispatch(self) -> None:
        parsed = urlsplit(self.path)
        path = parsed.path
        if self.command == "GET":
            if path == "/favicon.ico":
                self._send_bytes(b"", "image/x-icon", 204)
                return
            if path == "/" or path.startswith("/assets/"):
                relative = path.removeprefix("/assets/") if path.startswith("/assets/") else "index.html"
                self._serve_static(relative)
                return
        parts = [unquote(part) for part in path.split("/") if part]
        pattern = match_route(self.command, parts)
        if pattern is None:
            raise HttpRequestError(404, "Resource not found")
        if pattern == ("api", "v1", "health"):
            self._send_json({"status": "ok", "service": "AudioVTTForge", "api_version": "v1"})
        elif pattern == ("api", "v1", "capabilities"):
            self._send_json(capabilities())
        elif pattern == ("api", "v1", "sources", "scan"):
            self._send_json(self.manager.scan(self._read_json().get("path")).to_dict())
        elif pattern == ("api", "v1", "sources", "image"):
            self._serve_source_image(parsed.query)
        elif pattern == ("api", "v1", "uploads"):
            records = [
                self.manager.upload(name, content)
                for name, content in self._read_multipart()
            ]
            self._send_json({"items": [record.to_dict() for record in records]}, 201)
        elif pattern == ("api", "v1", "jobs"):
            if self.command == "GET":
                items = self.manager.list_jobs()
                self._send_json({"items": items, "total": len(items)})
            else:
                record = self.manager.submit(self._read_json())
                self._send_json(self.manager.describe(record.job_id), 202)
        elif pattern == ("api", "v1", "jobs", None):
            if self.command == "DELETE":
                record = self.manager.cancel(parts[3])
                self._send_json(self.manager.describe(record.job_id))
            else:
                self._send_json(self.manager.describe(parts[3]))
        elif pattern == ("api", "v1", "jobs", None, "events"):
            self._send_job_events(parts[3], parsed.query)
        elif pattern == ("api", "v1", "jobs", None, "download"):
            self._send_download(parts[3])
        else:
            raise HttpRequestError(404, "Resource not found")

    def _serve_source_image(self, query_string: str) -> None:
        query = parse_qs(query_string)
        source_dir = query.get("directory", [""])[0]
        name = query.get("name", [""])[0]
        if not source_dir or not name:
            raise HttpRequestError(400, "'directory' and 'name' are required")
        image = self.manager.source_image(source_dir, name)
        content_type = mimetypes.guess_type(image.name)[0] or "application/octet-stream"
        self._send_bytes(image.read_bytes(), content_type)

    def _send_job_events(self, job_id: str, query_string: str) -> None:
        query = parse_qs(query_string)
        try:
            after = int(query.get("after", ["0"])[0])
        except ValueError as exc:
            raise HttpRequestError(400, "'after' must be an integer") from exc
        if after < 0:
            raise HttpRequestError(400, "'after' must not be negative")
        items = self.manager.events(job_id, after)
        self._send_json({"items": items, "next_after": items[-1]["seq"] if items else after})

    def _send_download(self, job_id: str) -> None:
        path = self.manager.output_path(job_id)
        self.send_response(200)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Content-Length", str(path.stat().st_size))
        self.send_header("Content-Disposition", content_disposition(path.name))
        self.send_header("Connection", "close")
        self.end_headers()
        # Stream the finished file; a render can be far larger than memory.
        try:
            with path.open("rb") as source:
                shutil.copyfileobj(source, self.wfile, DOWNLOAD_CHUNK_BYTES)
        except (BrokenPipeError, ConnectionResetError):
            pass
        self.close_connection = True

    def _respond(self, failure: str) -> None:
        """Run one request and map service errors onto HTTP status codes."""
        try:
            self._dispatch()
        except HttpRequestError as exc:
            self._send_error(exc.status, exc.message, exc.details)
        except NotFoundError as exc:
            self._send_error(404, str(exc))
        except RequestValidationError as exc:
            self._send_error(422, str(exc), exc.details)
        except ServiceError as exc:
            self._send_error(409, str(exc))
        except OSError as exc:
            self._send_error(500, f"{failure}: {exc}")

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        self._respond("Could not read resource")

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
        self._respond("Could not store resource")

    def do_DELETE(self) -> None:  # noqa: N802 - stdlib handler API
        self._respond("Could not clean up cancelled job")


def static_root() -> Path:
    """Locate the browser assets in both source and PyInstaller layouts.

    A frozen build keeps them at ``<bundle>/audiovttforge/web_static`` (see the
    ``--add-data`` entry in build.ps1), which is not what ``__file__`` points at.
    """
    if getattr(sys, "frozen", False):
        bundle = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
        return bundle / "audiovttforge" / "web_static"
    return Path(__file__).with_name("web_static")


def create_server(
    host: str = "127.0.0.1",
    port: int = 8765,
    root: Path | None = None,
    manager: JobManager | None = None,
) -> AudioVTTForgeServer:
    return AudioVTTForgeServer(
        (host, port),
        manager or JobManager(root or default_data_root()),
        static_root(),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the AudioVTTForge local REST API and browser UI.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--data-root", type=Path, default=None)
    parser.add_argument(
        "--open-browser",
        action="store_true",
        help="open the UI in the default browser once the service is listening",
    )
    parser.add_argument(
        "--no-open-browser",
        action="store_true",
        help="never launch a browser, even if --open-browser is also given",
    )
    args = parser.parse_args(argv)
    manager = JobManager(args.data_root or default_data_root())
    try:
        server = create_server(args.host, args.port, manager=manager)
    except OSError as exc:
        if args.port == 0:
            raise
        # A second instance (or another program) already owns the port; keep the
        # packaged build usable instead of exiting with a bare traceback.
        print(f"Port {args.port} is unavailable ({exc}); using a free port instead.", flush=True)
        server = create_server(args.host, 0, manager=manager)
    port = server.server_address[1]
    url = f"http://{args.host}:{port}/"
    print(f"AudioVTTForge web UI: {url}", flush=True)
    if args.open_browser and not args.no_open_browser:
        # The socket is already listening, so the browser can connect immediately.
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
