"""Local REST API and browser server for AudioVTTForge."""

from __future__ import annotations

import argparse
import json
import mimetypes
import threading
from email.parser import BytesParser
from email.policy import default as email_policy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from .job import SUBTITLE_MODES
from .service import (
    JobManager,
    NotFoundError,
    RequestValidationError,
    ServiceError,
    default_data_root,
)

MAX_JSON_BYTES = 2 * 1024 * 1024
MAX_UPLOAD_BYTES = 1024 * 1024 * 1024


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

    def _handle_get(self) -> None:
        parsed = urlsplit(self.path)
        path = parsed.path
        if path == "/favicon.ico":
            self._send_bytes(b"", "image/x-icon", 204)
            return
        if path == "/" or path.startswith("/assets/"):
            self._serve_static(path.removeprefix("/assets/") if path.startswith("/assets/") else "index.html")
            return
        parts = [unquote(part) for part in path.split("/") if part]
        api_roots = (
            ["api", "v1", "health"],
            ["api", "v1", "capabilities"],
            ["api", "v1", "jobs"],
            ["api", "v1", "sources"],
        )
        if parts[:3] not in api_roots:
            raise HttpRequestError(404, "Resource not found")
        if parts[:3] == ["api", "v1", "health"] and len(parts) == 3:
            self._send_json({"status": "ok", "service": "AudioVTTForge", "api_version": "v1"})
            return
        if parts[:3] == ["api", "v1", "capabilities"] and len(parts) == 3:
            self._send_json(
                {
                    "subtitle_modes": sorted(SUBTITLE_MODES),
                    "defaults": {
                        "fps": 2,
                        "width": 1920,
                        "workers": 2,
                        "subtitle": "burnin",
                        "font_name": "Microsoft YaHei",
                        "font_size": 42,
                        "font_color": "#FFFFFF",
                    },
                    "upload_kinds": {
                        "audio": sorted(self.manager.uploads.AUDIO_EXTENSIONS),
                        "image": sorted(self.manager.uploads.IMAGE_EXTENSIONS),
                        "subtitle": sorted(self.manager.uploads.SUBTITLE_EXTENSIONS),
                    },
                }
            )
            return
        if parts[:3] == ["api", "v1", "sources"] and len(parts) == 4 and parts[3] == "image":
            query = parse_qs(parsed.query)
            source_dir = query.get("directory", [""])[0]
            name = query.get("name", [""])[0]
            if not source_dir or not name:
                raise HttpRequestError(400, "'directory' and 'name' are required")
            image = self.manager.source_image(source_dir, name)
            content_type = mimetypes.guess_type(image.name)[0] or "application/octet-stream"
            self._send_bytes(image.read_bytes(), content_type)
            return
        if len(parts) < 4:
            raise HttpRequestError(404, "Resource not found")
        job_id = parts[3]
        if len(parts) == 4:
            self._send_json(self.manager.get(job_id).to_dict())
            return
        if len(parts) == 5 and parts[4] == "events":
            query = parse_qs(parsed.query)
            try:
                after = int(query.get("after", ["0"])[0])
            except ValueError as exc:
                raise HttpRequestError(400, "'after' must be an integer") from exc
            if after < 0:
                raise HttpRequestError(400, "'after' must not be negative")
            items = self.manager.events(job_id, after)
            self._send_json({"items": items, "next_after": items[-1]["seq"] if items else after})
            return
        if len(parts) == 5 and parts[4] == "download":
            path = self.manager.output_path(job_id)
            body = path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Content-Disposition", f'attachment; filename="{path.name}"')
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
            self.close_connection = True
            return
        raise HttpRequestError(404, "Resource not found")

    def _handle_post(self) -> None:
        parsed = urlsplit(self.path)
        if parsed.path == "/api/v1/sources/scan":
            payload = self._read_json()
            path = payload.get("path")
            self._send_json(self.manager.scan(path).to_dict())
            return
        if parsed.path == "/api/v1/uploads":
            records = [self.manager.upload(name, content) for name, content in self._read_multipart()]
            self._send_json({"items": [record.to_dict() for record in records]}, 201)
            return
        if parsed.path == "/api/v1/jobs":
            record = self.manager.submit(self._read_json())
            self._send_json(record.to_dict(), 202)
            return
        raise HttpRequestError(404, "Resource not found")

    def _handle_delete(self) -> None:
        parts = [unquote(part) for part in urlsplit(self.path).path.split("/") if part]
        if parts[:3] != ["api", "v1", "jobs"] or len(parts) != 4:
            raise HttpRequestError(404, "Resource not found")
        self._send_json(self.manager.cancel(parts[3]).to_dict())

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        try:
            self._handle_get()
        except HttpRequestError as exc:
            self._send_error(exc.status, exc.message, exc.details)
        except NotFoundError as exc:
            self._send_error(404, str(exc))
        except RequestValidationError as exc:
            self._send_error(422, str(exc), exc.details)
        except ServiceError as exc:
            self._send_error(409, str(exc))
        except OSError as exc:
            self._send_error(500, f"Could not read resource: {exc}")

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
        try:
            self._handle_post()
        except HttpRequestError as exc:
            self._send_error(exc.status, exc.message, exc.details)
        except RequestValidationError as exc:
            self._send_error(422, str(exc), exc.details)
        except NotFoundError as exc:
            self._send_error(404, str(exc))
        except ServiceError as exc:
            self._send_error(409, str(exc))
        except OSError as exc:
            self._send_error(500, f"Could not store resource: {exc}")

    def do_DELETE(self) -> None:  # noqa: N802 - stdlib handler API
        try:
            self._handle_delete()
        except HttpRequestError as exc:
            self._send_error(exc.status, exc.message, exc.details)
        except NotFoundError as exc:
            self._send_error(404, str(exc))
        except ServiceError as exc:
            self._send_error(409, str(exc))
        except OSError as exc:
            self._send_error(500, f"Could not clean up cancelled job: {exc}")


def create_server(
    host: str = "127.0.0.1",
    port: int = 8765,
    root: Path | None = None,
) -> AudioVTTForgeServer:
    static_root = Path(__file__).with_name("web_static")
    return AudioVTTForgeServer((host, port), JobManager(root or default_data_root()), static_root)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the AudioVTTForge local REST API and browser UI.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--data-root", type=Path, default=None)
    args = parser.parse_args(argv)
    server = create_server(args.host, args.port, args.data_root)
    print(f"AudioVTTForge web UI: http://{args.host}:{args.port}/", flush=True)
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
