# FakeScanner

A production-quality fake scanner for macOS development and testing.

## What it does

- Runs a threaded HTTP server that behaves like a network scanner.
- Binds to a configurable IP and port, with defaults of `0.0.0.0:80`.
- Serves realistic scan results from files stored in `./images`.
- Exposes both simple REST endpoints and eSCL / AirScan-style endpoints.
- Advertises itself over Bonjour / mDNS using macOS's built-in `dns-sd` tool.
- Includes a TWAIN-like CLI simulation for `list-devices`, `select-device`, and `acquire-image`.
- Auto-generates a demo PDF at startup when `./images` is empty, so the repo can stay text-only.
- Watches the source folder continuously and handles empty folders gracefully.

## Files

- `fake_scanner.py` — main script
- `requirements.txt` — dependency note

## Supported source files

Put any of these into `./images`:

- `.jpg`
- `.jpeg`
- `.png`
- `.pdf`

## Installation

No third-party Python packages are required.

```bash
mkdir -p images
chmod +x fake_scanner.py
```

Optional: create a virtual environment if you want an isolated Python runtime.

```bash
python3 -m venv .venv
source .venv/bin/activate
```

## Run

Default run on port 80:

```bash
python3 fake_scanner.py serve
```

On macOS, binding to port `80` usually requires elevated privileges. For local testing on a non-privileged port:

```bash
PORT=8080 python3 fake_scanner.py serve
```

## Configuration

Set configuration via environment variables or an optional JSON config file.

### Environment variables

- `HOST` — default `0.0.0.0`
- `PORT` — default `80`
- `SCANNER_NAME` — default `DevScanner Pro`
- `IMAGE_FOLDER` — default `./images`
- `ENABLE_TRANSFORMS` — default `false`
- `SCAN_DELAY_MIN` — default `1.0`
- `SCAN_DELAY_MAX` — default `3.0`
- `MANUFACTURER` — default `DevLab Imaging`
- `MODEL` — default `ScanSim 2000`
- `MDNS_SERVICE_TYPE` — default `_uscan._tcp`
- `MDNS_DOMAIN` — default `local`
- `CONFIG_FILE` — optional JSON config file

### Example config file

```json
{
  "host": "0.0.0.0",
  "port": 8080,
  "scanner_name": "DevScanner Pro",
  "image_folder": "./images",
  "enable_transforms": true,
  "scan_delay_min": 1.0,
  "scan_delay_max": 3.0,
  "manufacturer": "DevLab Imaging",
  "model": "ScanSim 2000"
}
```

Run with a config file:

```bash
python3 fake_scanner.py --config scanner_config.json serve
```

## Endpoints

### REST

- `GET /status`
- `GET /capabilities`
- `GET /scan?output=jpeg`
- `GET /scan?output=pdf`

### eSCL-like

- `GET /eSCL/ScannerCapabilities`
- `POST /eSCL/ScanJobs`
- `GET /eSCL/ScanJobs/{job_id}/NextDocument`

## Test with curl

Start on a test port:

```bash
PORT=8080 python3 fake_scanner.py serve
```

Check status:

```bash
curl http://127.0.0.1:8080/status
```

Check capabilities:

```bash
curl http://127.0.0.1:8080/capabilities
curl http://127.0.0.1:8080/eSCL/ScannerCapabilities
```

Request a scan:

```bash
curl -o scan.jpg "http://127.0.0.1:8080/scan?output=jpeg"
curl -o scan.pdf "http://127.0.0.1:8080/scan?output=pdf"
```

Minimal eSCL flow:

```bash
curl -i -X POST http://127.0.0.1:8080/eSCL/ScanJobs \
  -H 'Content-Type: application/xml' \
  -d '<scan:ScanSettings xmlns:scan="http://schemas.hp.com/imaging/escl/2011/05/03"><scan:DocumentFormatExt>application/pdf</scan:DocumentFormatExt></scan:ScanSettings>'

curl -o scan-from-escl.pdf http://127.0.0.1:8080/eSCL/ScanJobs/<job_id>/NextDocument
```

## TWAIN-like CLI bridge

List devices:

```bash
python3 fake_scanner.py list-devices
```

Select the local device:

```bash
python3 fake_scanner.py select-device
```

Acquire a scan through the local API:

```bash
python3 fake_scanner.py acquire-image --output ./output/scan.jpg --output-format jpeg
python3 fake_scanner.py acquire-image --output ./output/scan.pdf --output-format pdf
```

## Notes

- The server is built on `ThreadingHTTPServer`, so it can handle concurrent requests.
- If `./images` is empty when the server starts, it seeds the folder with a demo PDF automatically; if there are still no compatible documents for a requested format, the scan endpoints return a structured `503` response instead of crashing.
- JPEG output uses raster files only.
- PDF output can come directly from a source PDF or be generated from a raster file using macOS `sips`.
- Optional transforms are scan-like and best-effort. Rotation uses built-in macOS tooling; grayscale/blur are applied only if ImageMagick is present.
- Bonjour advertisement is available on macOS through the built-in `dns-sd` command.
- No sample binary assets are committed, which keeps Codex PR creation compatible with text-only diffs while still giving you a startup document automatically.
