#!/usr/bin/env python3
"""Production-quality fake scanner for local Windows development and testing.

This script provides:
- A threaded HTTP server with REST and eSCL-like endpoints.
- Random document selection from a watched images folder.
- Optional scan-like transformations for raster images.
- Bonjour / mDNS advertisement when Bonjour is installed (`dns-sd` on Windows/macOS).
- TWAIN-like and WIA-like HTTP/CLI bridges for list/select/acquire flows.

Primary runtime target: Windows.
No third-party Python packages are required.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import random
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import asdict, dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple
from xml.etree.ElementTree import Element, SubElement, tostring

LOGGER = logging.getLogger("fake_scanner")
SUPPORTED_RASTER_EXTENSIONS = {".jpg", ".jpeg", ".png"}
SUPPORTED_DOCUMENT_EXTENSIONS = SUPPORTED_RASTER_EXTENSIONS | {".pdf"}
DEFAULT_CONFIG_PATH = Path("scanner_config.json")
DEFAULT_SELECTION_PATH = Path.home() / ".fake_scanner_selected_device.json"
SCAN_DOCUMENT_TTL_SECONDS = 300


@dataclass
class ScannerConfig:
    host: str = "0.0.0.0"
    port: int = 80
    scanner_name: str = "DevScanner Pro"
    image_folder: str = "./images"
    enable_transforms: bool = False
    scan_delay_min: float = 1.0
    scan_delay_max: float = 3.0
    manufacturer: str = "DevLab Imaging"
    model: str = "ScanSim 2000"
    driver_platform: str = "WIA/TWAIN"
    mdns_service_type: str = "_uscan._tcp"
    mdns_domain: str = "local"
    enable_discovery: bool = True
    config_file: Optional[str] = None

    @classmethod
    def load(cls, config_file: Optional[str] = None) -> "ScannerConfig":
        file_data: Dict[str, Any] = {}
        chosen_file = config_file or os.environ.get("CONFIG_FILE")
        if chosen_file is None and DEFAULT_CONFIG_PATH.exists():
            chosen_file = str(DEFAULT_CONFIG_PATH)
        if chosen_file:
            file_data = json.loads(Path(chosen_file).read_text(encoding="utf-8"))

        def get_value(env_name: str, default: Any) -> Any:
            return os.environ.get(env_name, file_data.get(env_name.lower(), default))

        return cls(
            host=str(get_value("HOST", cls.host)),
            port=int(get_value("PORT", cls.port)),
            scanner_name=str(get_value("SCANNER_NAME", cls.scanner_name)),
            image_folder=str(get_value("IMAGE_FOLDER", cls.image_folder)),
            enable_transforms=str(get_value("ENABLE_TRANSFORMS", cls.enable_transforms)).lower() in {"1", "true", "yes", "on"},
            scan_delay_min=float(get_value("SCAN_DELAY_MIN", cls.scan_delay_min)),
            scan_delay_max=float(get_value("SCAN_DELAY_MAX", cls.scan_delay_max)),
            manufacturer=str(get_value("MANUFACTURER", cls.manufacturer)),
            model=str(get_value("MODEL", cls.model)),
            driver_platform=str(get_value("DRIVER_PLATFORM", cls.driver_platform)),
            mdns_service_type=str(get_value("MDNS_SERVICE_TYPE", cls.mdns_service_type)),
            mdns_domain=str(get_value("MDNS_DOMAIN", cls.mdns_domain)),
            enable_discovery=str(get_value("ENABLE_DISCOVERY", cls.enable_discovery)).lower() in {"1", "true", "yes", "on"},
            config_file=chosen_file,
        )


@dataclass
class DocumentRecord:
    path: Path
    extension: str
    modified_at: float
    size_bytes: int

    @property
    def name(self) -> str:
        return self.path.name

    @property
    def is_pdf(self) -> bool:
        return self.extension == ".pdf"

    @property
    def is_raster(self) -> bool:
        return self.extension in SUPPORTED_RASTER_EXTENSIONS


@dataclass
class ScanJob:
    job_id: str
    created_at: float
    source_file: str
    output_format: str
    size_bytes: int
    mime_type: str
    status: str = "completed"


class ImageRepository:
    """Keeps an up-to-date index of source files using lightweight polling."""

    def __init__(self, folder: str, poll_interval: float = 2.0) -> None:
        self.folder = Path(folder).expanduser().resolve()
        self.poll_interval = poll_interval
        self._documents: List[DocumentRecord] = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.refresh()

    def start(self) -> None:
        self.folder.mkdir(parents=True, exist_ok=True)
        self._thread = threading.Thread(target=self._watch_loop, name="image-repository", daemon=True)
        self._thread.start()
        LOGGER.info("Watching image folder: %s", self.folder)

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2)

    def _watch_loop(self) -> None:
        while not self._stop.wait(self.poll_interval):
            self.refresh()

    def refresh(self) -> None:
        self.folder.mkdir(parents=True, exist_ok=True)
        documents: List[DocumentRecord] = []
        for child in sorted(self.folder.iterdir()):
            if not child.is_file():
                continue
            extension = child.suffix.lower()
            if extension not in SUPPORTED_DOCUMENT_EXTENSIONS:
                continue
            stat = child.stat()
            documents.append(DocumentRecord(child, extension, stat.st_mtime, stat.st_size))
        with self._lock:
            self._documents = documents
        LOGGER.debug("Indexed %s documents", len(documents))

    def count(self) -> int:
        with self._lock:
            return len(self._documents)

    def documents(self) -> List[DocumentRecord]:
        with self._lock:
            return list(self._documents)

    def choose_random(self, output_format: str) -> Optional[DocumentRecord]:
        documents = self.documents()
        compatible = [doc for doc in documents if doc.is_raster] if output_format == "jpeg" else documents
        return random.choice(compatible) if compatible else None

    def ensure_demo_document(self, config: ScannerConfig, filename: str = "demo-scan.pdf") -> Optional[Path]:
        if self.count() > 0:
            return None
        output_path = self.folder / filename
        output_path.write_bytes(build_demo_pdf(config))
        self.refresh()
        LOGGER.info("Image folder was empty; created demo document at %s", output_path)
        return output_path


class BonjourBroadcaster:
    """Publishes Bonjour metadata when Bonjour's dns-sd utility is available."""

    def __init__(self, config: ScannerConfig) -> None:
        self.config = config
        self._process: Optional[subprocess.Popen[str]] = None

    def start(self) -> None:
        if not self.config.enable_discovery:
            LOGGER.info("Network discovery disabled by configuration")
            return
        dns_sd = shutil.which("dns-sd") or shutil.which("dns-sd.exe")
        if dns_sd is None:
            LOGGER.warning("Bonjour dns-sd utility not found; skipping discovery advertisement")
            return
        cmd = [
            dns_sd,
            "-R",
            self.config.scanner_name,
            self.config.mdns_service_type,
            self.config.mdns_domain,
            str(self.config.port),
            f"note=Development fake scanner ty={self.config.scanner_name} mfg={self.config.manufacturer} mdl={self.config.model} rs=/eSCL",
        ]
        self._process = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, text=True)
        LOGGER.info("Published Bonjour service %r via dns-sd", self.config.scanner_name)

    def stop(self) -> None:
        if self._process and self._process.poll() is None:
            self._process.terminate()
            try:
                self._process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._process.kill()
        self._process = None


class DocumentTransformer:
    """Handles format conversion and optional scan-like transforms.

    On macOS, uses the built-in `sips` utility for conversions/rotation. If ImageMagick is
    present, it may also apply grayscale or blur. The pipeline degrades gracefully when a tool
    is unavailable.
    """

    def __init__(self, enable_transforms: bool) -> None:
        self.enable_transforms = enable_transforms
        self.sips = shutil.which("sips")
        self.magick = shutil.which("magick") or shutil.which("convert")

    def render(self, record: DocumentRecord, output_format: str) -> Tuple[bytes, str]:
        if output_format == "pdf":
            return self._render_pdf(record)
        return self._render_jpeg(record)

    def _render_pdf(self, record: DocumentRecord) -> Tuple[bytes, str]:
        if record.is_pdf:
            return record.path.read_bytes(), "application/pdf"
        converted = self._convert_with_sips(record.path, target_format="pdf")
        return converted, "application/pdf"

    def _render_jpeg(self, record: DocumentRecord) -> Tuple[bytes, str]:
        if not record.is_raster:
            raise ScannerError(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "JPEG output requires a raster source image.")
        if record.extension in {".jpg", ".jpeg"} and not self.enable_transforms:
            return record.path.read_bytes(), "image/jpeg"

        temp_dir = Path(tempfile.mkdtemp(prefix="fake-scanner-"))
        try:
            working_source = temp_dir / record.path.name
            shutil.copy2(record.path, working_source)
            if self.enable_transforms:
                self._apply_transforms_in_place(working_source)
            if working_source.suffix.lower() not in {".jpg", ".jpeg"}:
                output_path = temp_dir / f"{working_source.stem}.jpg"
                self._run_sips(["-s", "format", "jpeg", str(working_source), "--out", str(output_path)])
                working_source = output_path
            return working_source.read_bytes(), "image/jpeg"
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    def _convert_with_sips(self, path: Path, target_format: str) -> bytes:
        temp_dir = Path(tempfile.mkdtemp(prefix="fake-scanner-"))
        try:
            output_path = temp_dir / f"{path.stem}.{target_format}"
            self._run_sips(["-s", "format", target_format, str(path), "--out", str(output_path)])
            return output_path.read_bytes()
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    def _apply_transforms_in_place(self, image_path: Path) -> None:
        if self.sips is None:
            LOGGER.warning("ENABLE_TRANSFORMS=true but sips is unavailable; serving original raster")
            return

        rotation = random.choice([-2, -1, 0, 1, 2])
        if rotation:
            self._run_sips(["-r", str(rotation), str(image_path)])

        if self.magick and random.random() < 0.5:
            grayscale_or_blur = [self.magick, str(image_path)]
            if random.random() < 0.5:
                grayscale_or_blur += ["-colorspace", "Gray"]
            if random.random() < 0.5:
                grayscale_or_blur += ["-blur", "0x0.6"]
            grayscale_or_blur.append(str(image_path))
            subprocess.run(grayscale_or_blur, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def _run_sips(self, args: List[str]) -> None:
        if self.sips is None:
            raise ScannerError(
                HTTPStatus.NOT_IMPLEMENTED,
                "The built-in macOS 'sips' utility is required for this conversion. Run on macOS or provide JPEG source files.",
            )
        cmd = [self.sips, *args]
        try:
            subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        except subprocess.CalledProcessError as exc:
            stderr = exc.stderr.strip() if exc.stderr else "unknown error"
            raise ScannerError(HTTPStatus.INTERNAL_SERVER_ERROR, f"Image conversion failed: {stderr}") from exc


class ScannerError(Exception):
    def __init__(self, status: HTTPStatus, message: str, *, extra: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.extra = extra or {}

    def to_payload(self) -> Dict[str, Any]:
        payload = {"error": self.status.phrase.lower().replace(" ", "_"), "message": self.message}
        payload.update(self.extra)
        return payload


class ScannerService:
    def __init__(self, config: ScannerConfig, repository: ImageRepository) -> None:
        self.config = config
        self.repository = repository
        self.transformer = DocumentTransformer(enable_transforms=config.enable_transforms)
        self._jobs: Dict[str, ScanJob] = {}
        self._documents: Dict[str, Tuple[bytes, str, float]] = {}
        self._lock = threading.Lock()

    def status_payload(self) -> Dict[str, Any]:
        return {
            "healthy": True,
            "scanner_name": self.config.scanner_name,
            "manufacturer": self.config.manufacturer,
            "model": self.config.model,
            "driver_platform": self.config.driver_platform,
            "host": self.config.host,
            "port": self.config.port,
            "image_folder": str(self.repository.folder),
            "documents_available": self.repository.count(),
            "transforms_enabled": self.config.enable_transforms,
            "recent_jobs": self.recent_jobs(),
        }

    def capabilities_payload(self) -> Dict[str, Any]:
        return {
            "scanner": {
                "name": self.config.scanner_name,
                "manufacturer": self.config.manufacturer,
                "model": self.config.model,
                "driver_platform": self.config.driver_platform,
                "protocols": ["rest", "escl", "twain-like-cli", "wia-like-http"],
            },
            "formats": ["jpeg", "pdf"],
            "color_modes": ["color", "grayscale"],
            "resolutions_dpi": [75, 150, 200, 300],
            "source": "flatbed",
            "adf": False,
            "folder_watch_enabled": True,
        }

    def device_descriptor(self) -> Dict[str, Any]:
        return {
            "device_id": f"{self.config.scanner_name.lower().replace(' ', '-')}-{self.config.port}",
            "name": self.config.scanner_name,
            "manufacturer": self.config.manufacturer,
            "model": self.config.model,
            "driver_platform": self.config.driver_platform,
            "transport": "http",
            "host": advertised_host(self.config),
            "port": self.config.port,
            "formats": ["jpeg", "pdf"],
            "sources": ["flatbed"],
        }

    def twain_payload(self) -> Dict[str, Any]:
        device = self.device_descriptor()
        return {
            "twain": {
                "default_source": device["device_id"],
                "sources": [
                    {
                        **device,
                        "source_name": self.config.scanner_name,
                        "twain_state": 4,
                        "supports_native_transfer": False,
                        "supports_file_transfer": True,
                    }
                ],
            }
        }

    def wia_payload(self) -> Dict[str, Any]:
        device = self.device_descriptor()
        return {
            "wia": {
                "default_device_id": device["device_id"],
                "devices": [
                    {
                        **device,
                        "wia_item_type": "ScannerDevice",
                        "properties": {
                            "Horizontal Resolution": 300,
                            "Vertical Resolution": 300,
                            "Current Intent": "ImageTypeColor",
                            "Document Handling Select": "FEEDER_DISABLED",
                        },
                    }
                ],
            }
        }

    def perform_scan(self, output_format: str) -> Tuple[bytes, str, ScanJob]:
        if output_format not in {"jpeg", "pdf"}:
            raise ScannerError(HTTPStatus.BAD_REQUEST, f"Unsupported output format: {output_format}")

        record = self.repository.choose_random(output_format)
        if record is None:
            raise ScannerError(
                HTTPStatus.SERVICE_UNAVAILABLE,
                f"No compatible source files found in {self.repository.folder}",
                extra={"image_folder": str(self.repository.folder)},
            )

        delay = random.uniform(self.config.scan_delay_min, self.config.scan_delay_max)
        LOGGER.info("Scan requested: source=%s output=%s delay=%.2fs", record.name, output_format, delay)
        time.sleep(delay)
        payload, mime_type = self.transformer.render(record, output_format)

        job = ScanJob(
            job_id=str(uuid.uuid4()),
            created_at=time.time(),
            source_file=record.name,
            output_format=output_format,
            size_bytes=len(payload),
            mime_type=mime_type,
        )
        with self._lock:
            self._jobs[job.job_id] = job
        return payload, mime_type, job

    def create_escl_job(self, output_format: str) -> ScanJob:
        payload, mime_type, job = self.perform_scan(output_format)
        with self._lock:
            self._documents[job.job_id] = (payload, mime_type, time.time())
            self._cleanup_expired_documents_locked()
        return job

    def get_escl_document(self, job_id: str) -> Tuple[bytes, str]:
        with self._lock:
            self._cleanup_expired_documents_locked()
            item = self._documents.get(job_id)
        if item is None:
            raise ScannerError(HTTPStatus.NOT_FOUND, f"Unknown or expired eSCL job: {job_id}")
        data, mime_type, _ = item
        return data, mime_type

    def recent_jobs(self) -> List[Dict[str, Any]]:
        with self._lock:
            jobs = sorted(self._jobs.values(), key=lambda entry: entry.created_at, reverse=True)
        return [asdict(job) for job in jobs[:10]]

    def _cleanup_expired_documents_locked(self) -> None:
        cutoff = time.time() - SCAN_DOCUMENT_TTL_SECONDS
        expired = [job_id for job_id, (_, _, created_at) in self._documents.items() if created_at < cutoff]
        for job_id in expired:
            self._documents.pop(job_id, None)


class ScannerHTTPRequestHandler(BaseHTTPRequestHandler):
    server_version = "FakeScanner/1.0"

    def do_GET(self) -> None:  # noqa: N802
        self._dispatch_request()

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch_request()

    def log_message(self, format: str, *args: Any) -> None:
        LOGGER.info("HTTP %s - %s", self.address_string(), format % args)

    @property
    def scanner_service(self) -> ScannerService:
        return self.server.scanner_service  # type: ignore[attr-defined]

    @property
    def scanner_config(self) -> ScannerConfig:
        return self.server.scanner_config  # type: ignore[attr-defined]

    def _dispatch_request(self) -> None:
        request_id = uuid.uuid4().hex[:8]
        client_ip = self.client_address[0] if self.client_address else "unknown"
        LOGGER.info("[%s] %s %s from %s", request_id, self.command, self.path, client_ip)
        try:
            parsed = urllib.parse.urlparse(self.path)
            if self.command == "GET" and parsed.path == "/":
                self._handle_root()
            elif self.command == "GET" and parsed.path == "/status":
                self._handle_status()
            elif self.command == "GET" and parsed.path == "/capabilities":
                self._handle_capabilities()
            elif self.command == "GET" and parsed.path == "/twain/devices":
                self._handle_twain_devices()
            elif self.command == "GET" and parsed.path == "/wia/devices":
                self._handle_wia_devices()
            elif self.command == "GET" and parsed.path == "/scan":
                self._handle_scan(parsed.query)
            elif self.command == "GET" and parsed.path == "/eSCL/ScannerCapabilities":
                self._handle_escl_capabilities()
            elif self.command == "POST" and parsed.path == "/twain/acquire":
                self._handle_twain_acquire()
            elif self.command == "POST" and parsed.path == "/wia/acquire":
                self._handle_wia_acquire()
            elif self.command == "POST" and parsed.path == "/eSCL/ScanJobs":
                self._handle_escl_scan_job()
            elif self.command == "GET" and parsed.path.startswith("/eSCL/ScanJobs/") and parsed.path.endswith("/NextDocument"):
                self._handle_escl_next_document(parsed.path)
            else:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found", "message": f"Unknown path: {parsed.path}"})
        except ScannerError as exc:
            LOGGER.warning("[%s] scanner error: %s", request_id, exc.message)
            self._send_json(exc.status, exc.to_payload())
        except Exception as exc:  # noqa: BLE001
            LOGGER.exception("[%s] unexpected error", request_id)
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "internal_server_error", "message": str(exc)})

    def _handle_root(self) -> None:
        self._send_json(
            HTTPStatus.OK,
            {
                "service": "fake-scanner",
                "scanner_name": self.scanner_config.scanner_name,
                "status_url": "/status",
                "capabilities_url": "/capabilities",
                "scan_url": "/scan",
                "twain_devices_url": "/twain/devices",
                "twain_acquire_url": "/twain/acquire",
                "wia_devices_url": "/wia/devices",
                "wia_acquire_url": "/wia/acquire",
                "escl_capabilities_url": "/eSCL/ScannerCapabilities",
            },
        )

    def _handle_status(self) -> None:
        self._send_json(HTTPStatus.OK, self.scanner_service.status_payload())

    def _handle_capabilities(self) -> None:
        self._send_json(HTTPStatus.OK, self.scanner_service.capabilities_payload())

    def _handle_twain_devices(self) -> None:
        self._send_json(HTTPStatus.OK, self.scanner_service.twain_payload())

    def _handle_wia_devices(self) -> None:
        self._send_json(HTTPStatus.OK, self.scanner_service.wia_payload())

    def _handle_scan(self, query: str) -> None:
        params = urllib.parse.parse_qs(query)
        output_format = params.get("output", ["jpeg"])[0].lower()
        payload, mime_type, job = self.scanner_service.perform_scan(output_format)
        extension = "jpg" if output_format == "jpeg" else output_format
        headers = {
            "X-Scanner-Name": self.scanner_config.scanner_name,
            "X-Scan-Job-Id": job.job_id,
            "Content-Disposition": f'inline; filename="scan-{job.job_id}.{extension}"',
        }
        self._send_bytes(HTTPStatus.OK, payload, mime_type, headers=headers)

    def _handle_escl_capabilities(self) -> None:
        root = Element("scan:ScannerCapabilities", {"xmlns:scan": "http://schemas.hp.com/imaging/escl/2011/05/03"})
        SubElement(root, "scan:MakeAndModel").text = f"{self.scanner_config.manufacturer} {self.scanner_config.model}"
        SubElement(root, "scan:ScannerName").text = self.scanner_config.scanner_name
        formats = SubElement(root, "scan:DocumentFormats")
        SubElement(formats, "scan:DocumentFormat").text = "image/jpeg"
        SubElement(formats, "scan:DocumentFormat").text = "application/pdf"
        SubElement(root, "scan:ColorModes")
        xml_payload = tostring(root, encoding="utf-8", xml_declaration=True)
        self._send_bytes(HTTPStatus.OK, xml_payload, "application/xml")

    def _read_json_body(self) -> Dict[str, Any]:
        content_length = int(self.headers.get("Content-Length", "0"))
        if content_length <= 0:
            return {}
        raw_body = self.rfile.read(content_length)
        if not raw_body:
            return {}
        try:
            return json.loads(raw_body.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise ScannerError(HTTPStatus.BAD_REQUEST, f"Invalid JSON body: {exc}") from exc

    def _handle_twain_acquire(self) -> None:
        payload = self._read_json_body()
        output_format = str(payload.get("output_format", "pdf")).lower()
        document, mime_type, job = self.scanner_service.perform_scan(output_format)
        response = {
            "protocol": "twain",
            "job_id": job.job_id,
            "device": self.scanner_service.device_descriptor(),
            "output_format": output_format,
            "mime_type": mime_type,
            "size_bytes": len(document),
            "source_file": job.source_file,
            "download_url": f"/scan?output={output_format}",
        }
        self._send_json(HTTPStatus.OK, response)

    def _handle_wia_acquire(self) -> None:
        payload = self._read_json_body()
        output_format = str(payload.get("output_format", "pdf")).lower()
        document, mime_type, job = self.scanner_service.perform_scan(output_format)
        response = {
            "protocol": "wia",
            "job_id": job.job_id,
            "device": self.scanner_service.device_descriptor(),
            "item_name": job.source_file,
            "output_format": output_format,
            "mime_type": mime_type,
            "size_bytes": len(document),
            "transfer_mode": "file",
            "download_url": f"/scan?output={output_format}",
        }
        self._send_json(HTTPStatus.OK, response)

    def _handle_escl_scan_job(self) -> None:
        content_length = int(self.headers.get("Content-Length", "0"))
        request_body = self.rfile.read(content_length) if content_length else b""
        output_format = "pdf" if b"pdf" in request_body.lower() else "jpeg"
        job = self.scanner_service.create_escl_job(output_format)
        response_body = {
            "job_id": job.job_id,
            "location": f"/eSCL/ScanJobs/{job.job_id}/NextDocument",
            "mime_type": job.mime_type,
            "source_file": job.source_file,
        }
        self._send_json(
            HTTPStatus.CREATED,
            response_body,
            headers={"Location": response_body["location"]},
        )

    def _handle_escl_next_document(self, path: str) -> None:
        parts = path.strip("/").split("/")
        if len(parts) < 4:
            raise ScannerError(HTTPStatus.BAD_REQUEST, f"Malformed eSCL path: {path}")
        job_id = parts[2]
        payload, mime_type = self.scanner_service.get_escl_document(job_id)
        self._send_bytes(HTTPStatus.OK, payload, mime_type)

    def _send_json(self, status: HTTPStatus, payload: Dict[str, Any], headers: Optional[Dict[str, str]] = None) -> None:
        body = json.dumps(payload, indent=2).encode("utf-8")
        self._send_bytes(status, body, "application/json", headers=headers)

    def _send_bytes(
        self,
        status: HTTPStatus,
        payload: bytes,
        content_type: str,
        headers: Optional[Dict[str, str]] = None,
    ) -> None:
        self.send_response(status.value)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Connection", "close")
        if headers:
            for key, value in headers.items():
                self.send_header(key, value)
        self.end_headers()
        self.wfile.write(payload)


class ScannerHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, server_address: Tuple[str, int], request_handler_class: type[BaseHTTPRequestHandler], scanner_config: ScannerConfig, scanner_service: ScannerService) -> None:
        super().__init__(server_address, request_handler_class)
        self.scanner_config = scanner_config
        self.scanner_service = scanner_service


def configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )


def advertised_host(config: ScannerConfig) -> str:
    if config.host not in {"0.0.0.0", "::"}:
        return config.host
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.connect(("8.8.8.8", 80))
        return probe.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        try:
            probe.close()
        except Exception:  # noqa: BLE001
            pass


def service_identity(config: ScannerConfig) -> Dict[str, Any]:
    host = advertised_host(config)
    return {
        "name": config.scanner_name,
        "manufacturer": config.manufacturer,
        "model": config.model,
        "host": host,
        "port": config.port,
        "base_url": f"http://{host}:{config.port}",
    }


def save_selected_device(config: ScannerConfig) -> None:
    DEFAULT_SELECTION_PATH.write_text(json.dumps(service_identity(config), indent=2), encoding="utf-8")


def load_selected_device() -> Optional[Dict[str, Any]]:
    if not DEFAULT_SELECTION_PATH.exists():
        return None
    return json.loads(DEFAULT_SELECTION_PATH.read_text(encoding="utf-8"))


def run_cli_command(args: argparse.Namespace, config: ScannerConfig) -> int:
    if args.command == "list-devices":
        print(json.dumps({"devices": [service_identity(config)]}, indent=2))
        return 0
    if args.command == "twain-list-sources":
        print(json.dumps({"sources": [service_identity(config) | {"driver": "TWAIN"}]}, indent=2))
        return 0
    if args.command == "wia-list-devices":
        print(json.dumps({"devices": [service_identity(config) | {"driver": "WIA"}]}, indent=2))
        return 0
    if args.command == "select-device":
        save_selected_device(config)
        print(f"Selected device: {config.scanner_name}")
        return 0
    if args.command == "twain-select-source":
        save_selected_device(config)
        print(f"Selected TWAIN source: {config.scanner_name}")
        return 0
    if args.command == "acquire-image":
        selected = load_selected_device() or service_identity(config)
        url = f"{selected['base_url']}/scan?{urllib.parse.urlencode({'output': args.output_format})}"
        try:
            with urllib.request.urlopen(url, timeout=args.timeout) as response:
                payload = response.read()
        except urllib.error.URLError as exc:
            raise SystemExit(f"Failed to acquire image from {url}: {exc}") from exc
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(payload)
        print(f"Saved scan to {output_path}")
        return 0
    if args.command in {"twain-acquire", "wia-acquire"}:
        selected = load_selected_device() or service_identity(config)
        endpoint = "twain/acquire" if args.command == "twain-acquire" else "wia/acquire"
        request_payload = json.dumps({"output_format": args.output_format}).encode("utf-8")
        request = urllib.request.Request(
            f"{selected['base_url']}/{endpoint}",
            data=request_payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=args.timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.URLError as exc:
            raise SystemExit(f"Failed to acquire image metadata from {request.full_url}: {exc}") from exc
        print(json.dumps(payload, indent=2))
        return 0
    raise SystemExit(f"Unknown command: {args.command}")


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Fake network scanner for local development and testing")
    parser.add_argument("--config", dest="config_file", default=None, help="Optional JSON config file")
    parser.add_argument("--verbose", action="store_true", help="Enable verbose logging")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("serve", help="Run the scanner HTTP service")
    subparsers.add_parser("list-devices", help="List discoverable fake scanner devices")
    subparsers.add_parser("twain-list-sources", help="List simulated TWAIN sources")
    subparsers.add_parser("wia-list-devices", help="List simulated WIA devices")
    subparsers.add_parser("select-device", help="Select the local fake scanner device")
    subparsers.add_parser("twain-select-source", help="Select the local fake scanner as the TWAIN source")
    acquire = subparsers.add_parser("acquire-image", help="Acquire an image through the local scanner API")
    acquire.add_argument("--output", required=True, help="Output file path")
    acquire.add_argument("--output-format", choices=["jpeg", "pdf"], default="jpeg")
    acquire.add_argument("--timeout", type=int, default=15)
    twain_acquire = subparsers.add_parser("twain-acquire", help="Acquire scan metadata through the simulated TWAIN bridge")
    twain_acquire.add_argument("--output-format", choices=["jpeg", "pdf"], default="pdf")
    twain_acquire.add_argument("--timeout", type=int, default=15)
    wia_acquire = subparsers.add_parser("wia-acquire", help="Acquire scan metadata through the simulated WIA bridge")
    wia_acquire.add_argument("--output-format", choices=["jpeg", "pdf"], default="pdf")
    wia_acquire.add_argument("--timeout", type=int, default=15)
    return parser


def _pdf_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def build_demo_pdf(config: ScannerConfig) -> bytes:
    title = _pdf_escape(f"{config.scanner_name} Demo Scan")
    subtitle = _pdf_escape(f"Manufacturer: {config.manufacturer} | Model: {config.model}")
    details = _pdf_escape("Generated locally so the repo stays text-only and PR-safe.")
    content_stream = "\n".join(
        [
            "BT",
            "/F1 24 Tf",
            "72 720 Td",
            f"({title}) Tj",
            "0 -36 Td",
            "/F1 14 Tf",
            f"({subtitle}) Tj",
            "0 -24 Td",
            f"({details}) Tj",
            "ET",
        ]
    ).encode("utf-8")
    prefix = (
        "%PDF-1.4\n"
        "1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n"
        "2 0 obj\n<< /Type /Pages /Count 1 /Kids [3 0 R] >>\nendobj\n"
        "3 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>\nendobj\n"
    ).encode("utf-8")
    stream_header = f"4 0 obj\n<< /Length {len(content_stream)} >>\nstream\n".encode("utf-8")
    stream_footer = b"\nendstream\nendobj\n"
    font_obj = b"5 0 obj\n<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>\nendobj\n"
    xref_offset = len(prefix) + len(stream_header) + len(content_stream) + len(stream_footer) + len(font_obj)
    font_offset = len(prefix) + len(stream_header) + len(content_stream) + len(stream_footer)
    xref = (
        "xref\n"
        "0 6\n"
        "0000000000 65535 f \n"
        "0000000009 00000 n \n"
        "0000000058 00000 n \n"
        "0000000115 00000 n \n"
        "0000000241 00000 n \n"
        f"{font_offset:010d} 00000 n \n"
        "trailer\n"
        "<< /Size 6 /Root 1 0 R >>\n"
        "startxref\n"
        f"{xref_offset}\n"
        "%%EOF\n"
    ).encode("utf-8")
    return prefix + stream_header + content_stream + stream_footer + font_obj + xref


def serve(config: ScannerConfig, verbose: bool) -> int:
    configure_logging(verbose)
    repository = ImageRepository(config.image_folder)
    repository.start()
    repository.ensure_demo_document(config)
    service = ScannerService(config, repository)
    broadcaster = BonjourBroadcaster(config)
    server = ScannerHTTPServer((config.host, config.port), ScannerHTTPRequestHandler, config, service)

    stop_event = threading.Event()

    def shutdown_handler(signum: int, _frame: Any) -> None:
        LOGGER.info("Received signal %s; shutting down", signum)
        stop_event.set()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, shutdown_handler)
    signal.signal(signal.SIGTERM, shutdown_handler)

    try:
        broadcaster.start()
        LOGGER.info(
            "Starting %s at http://%s:%s serving files from %s",
            config.scanner_name,
            config.host,
            config.port,
            repository.folder,
        )
        server.serve_forever(poll_interval=0.5)
        return 0
    finally:
        broadcaster.stop()
        repository.stop()
        server.server_close()
        if stop_event.is_set():
            LOGGER.info("Server stopped")


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    config = ScannerConfig.load(args.config_file)

    if args.command == "serve":
        return serve(config, args.verbose)

    configure_logging(args.verbose)
    return run_cli_command(args, config)


if __name__ == "__main__":
    sys.exit(main())
