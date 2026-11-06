#!/usr/bin/env python3
"""SnapDuo web app (EC2 side). Stdlib only.

Serves the static site under /duo/, accepts photo-pair jobs, serves input
photos at unguessable URLs (the local agent passes them to the image API),
receives finished results from the agent, and unlocks full-resolution
downloads after Gumroad license verification.

Data layout (next to this file):
  data/jobs/<job_id>/job.json, input-a.jpg, input-b.jpg, preview.jpg, clean.jpg
  data/licenses.json  {license_key: {"uses": n}}
  duo_config.json     {gumroad_product_id, gumroad_buy_url, agent_token}
"""
import base64
import json
import os
import secrets
import shutil
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SITE_DIR = os.path.join(BASE_DIR, "site")
DATA_DIR = os.path.join(BASE_DIR, "data")
JOBS_DIR = os.path.join(DATA_DIR, "jobs")
CONFIG_PATH = os.path.join(BASE_DIR, "duo_config.json")
LICENSES_PATH = os.path.join(DATA_DIR, "licenses.json")
MAX_UNLOCKS_PER_KEY = 3
DAILY_JOB_CAP_PER_IP = 20
JOB_TTL_SECONDS = 7 * 24 * 3600
MAX_PHOTO_BYTES = 10 * 1024 * 1024
TEMPLATES = {"christmas-couple", "santa-market", "tiny-boyfriend"}

_lock = threading.Lock()


def load_config():
    with open(CONFIG_PATH) as f:
        return json.load(f)


def job_dir(job_id):
    return os.path.join(JOBS_DIR, job_id)


def read_job(job_id):
    p = os.path.join(job_dir(job_id), "job.json")
    if not os.path.exists(p):
        return None
    with open(p) as f:
        return json.load(f)


def write_job(job):
    d = job_dir(job["id"])
    os.makedirs(d, exist_ok=True)
    tmp = os.path.join(d, "job.json.tmp")
    with open(tmp, "w") as f:
        json.dump(job, f)
    os.replace(tmp, os.path.join(d, "job.json"))


def load_licenses():
    if not os.path.exists(LICENSES_PATH):
        return {}
    with open(LICENSES_PATH) as f:
        return json.load(f)


def save_licenses(data):
    tmp = LICENSES_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.replace(tmp, LICENSES_PATH)


def cleanup_old_jobs():
    now = time.time()
    try:
        for name in os.listdir(JOBS_DIR):
            d = job_dir(name)
            if os.path.isdir(d) and now - os.path.getmtime(d) > JOB_TTL_SECONDS:
                shutil.rmtree(d, ignore_errors=True)
    except FileNotFoundError:
        pass


def verify_gumroad_license(product_id, license_key):
    """Returns (ok, info). Uses Gumroad's public license verification API."""
    body = urllib.parse.urlencode({
        "product_id": product_id,
        "license_key": license_key,
        "increment_uses_count": "false",
    }).encode()
    req = urllib.request.Request(
        "https://api.gumroad.com/v2/licenses/verify", data=body, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        try:
            data = json.loads(e.read().decode())
        except Exception:
            return False, {"error": "verify_http_%s" % e.code}
    except Exception as e:
        return False, {"error": "verify_failed: %s" % e}
    if not data.get("success"):
        return False, data
    purchase = data.get("purchase") or {}
    if purchase.get("refunded") or purchase.get("disputed") or purchase.get("chargebacked"):
        return False, {"error": "purchase_not_valid"}
    return True, data


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def send_json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_file(self, path, content_type, download_name=None):
        if not os.path.exists(path):
            self.send_json(404, {"error": "not_found"})
            return
        with open(path, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "public, max-age=86400")
        if download_name:
            self.send_header("Content-Disposition",
                             'attachment; filename="%s"' % download_name)
        self.end_headers()
        self.wfile.write(body)

    def read_json_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        return json.loads(raw.decode())

    def agent_authorized(self):
        try:
            token = load_config().get("agent_token", "")
        except Exception:
            return False
        return bool(token) and self.headers.get("X-Agent-Token") == token

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)

        if path in ("/duo", "/duo/", "/duo/index.html"):
            self.send_file(os.path.join(SITE_DIR, "index.html"), "text/html; charset=utf-8")
            return
        if path.startswith("/duo/assets/"):
            name = os.path.basename(path)
            ctype = "image/jpeg" if name.endswith((".jpg", ".jpeg")) else \
                    "image/png" if name.endswith(".png") else "application/octet-stream"
            self.send_file(os.path.join(SITE_DIR, name), ctype)
            return
        if path == "/duo/health":
            self.send_json(200, {"ok": True})
            return
        if path.startswith("/duo/api/config"):
            try:
                cfg = load_config()
                self.send_json(200, {"buy_url": cfg.get("gumroad_buy_url", "")})
            except Exception:
                self.send_json(200, {"buy_url": ""})
            return
        if path.startswith("/duo/files/"):
            parts = path.split("/")
            # /duo/files/<job_id>/<name>
            if len(parts) == 5:
                job_id, name = parts[3], parts[4]
                if name in ("input-a.jpg", "input-b.jpg", "preview.jpg"):
                    self.send_file(os.path.join(job_dir(job_id), name), "image/jpeg")
                    return
                if name == "clean.jpg":
                    job = read_job(job_id)
                    token = (query.get("t") or [""])[0]
                    if job and job.get("unlocked") and token and token == job.get("download_token"):
                        self.send_file(os.path.join(job_dir(job_id), "clean.jpg"),
                                       "image/jpeg", download_name="snapduo-full.jpg")
                        return
                    self.send_json(403, {"error": "locked"})
                    return
            self.send_json(404, {"error": "not_found"})
            return
        if path.startswith("/duo/api/jobs/"):
            job_id = path.rsplit("/", 1)[-1]
            job = read_job(job_id)
            if not job:
                self.send_json(404, {"error": "not_found"})
                return
            out = {"id": job["id"], "status": job["status"], "template": job["template"]}
            if job["status"] == "done":
                out["preview_url"] = "/duo/files/%s/preview.jpg" % job_id
                out["unlocked"] = bool(job.get("unlocked"))
                if job.get("unlocked"):
                    out["clean_url"] = "/duo/files/%s/clean.jpg?t=%s" % (
                        job_id, job.get("download_token"))
            if job["status"] == "failed":
                out["error"] = job.get("error", "generation_failed")
            self.send_json(200, out)
            return
        if path == "/duo/agent/next":
            if not self.agent_authorized():
                self.send_json(403, {"error": "forbidden"})
                return
            with _lock:
                nxt = None
                try:
                    ids = sorted(os.listdir(JOBS_DIR))
                except FileNotFoundError:
                    ids = []
                for jid in ids:
                    job = read_job(jid)
                    if not job:
                        continue
                    # Requeue jobs orphaned by an agent restart mid-generation.
                    if job["status"] == "processing" and \
                            time.time() - job.get("claimed_at", 0) > 600:
                        job["status"] = "queued"
                        write_job(job)
                    if job["status"] == "queued":
                        job["status"] = "processing"
                        job["claimed_at"] = time.time()
                        write_job(job)
                        nxt = job
                        break
            if not nxt:
                self.send_json(200, {"job": None})
                return
            host = self.headers.get("Host", "")
            scheme = "https"
            base = "%s://%s" % (scheme, host)
            self.send_json(200, {"job": {
                "id": nxt["id"],
                "template": nxt["template"],
                "input_a_url": "%s/duo/files/%s/input-a.jpg" % (base, nxt["id"]),
                "input_b_url": "%s/duo/files/%s/input-b.jpg" % (base, nxt["id"]),
            }})
            return
        self.send_json(404, {"error": "not_found"})

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path == "/duo/api/jobs":
            try:
                payload = self.read_json_body()
            except Exception:
                self.send_json(400, {"error": "bad_json"})
                return
            template = payload.get("template")
            if template not in TEMPLATES:
                self.send_json(400, {"error": "bad_template"})
                return
            ip = self.headers.get("X-Forwarded-For", self.client_address[0]).split(",")[0].strip()
            today = time.strftime("%Y-%m-%d")
            with _lock:
                counts_path = os.path.join(DATA_DIR, "ip_counts.json")
                counts = {}
                if os.path.exists(counts_path):
                    with open(counts_path) as f:
                        counts = json.load(f)
                key = "%s:%s" % (today, ip)
                if counts.get(key, 0) >= DAILY_JOB_CAP_PER_IP:
                    self.send_json(429, {"error": "daily_cap"})
                    return
                counts[key] = counts.get(key, 0) + 1
                with open(counts_path, "w") as f:
                    json.dump(counts, f)
            try:
                a = base64.b64decode(payload["photo_a_b64"])
                b = base64.b64decode(payload["photo_b_b64"])
            except Exception:
                self.send_json(400, {"error": "bad_photo_data"})
                return
            if not (0 < len(a) <= MAX_PHOTO_BYTES and 0 < len(b) <= MAX_PHOTO_BYTES):
                self.send_json(400, {"error": "photo_too_large"})
                return
            if not (a[:3] == b"\xff\xd8\xff" or a[:8] == b"\x89PNG\r\n\x1a\n") or \
               not (b[:3] == b"\xff\xd8\xff" or b[:8] == b"\x89PNG\r\n\x1a\n"):
                self.send_json(400, {"error": "photo_must_be_jpg_or_png"})
                return
            note = payload.get("note") or ""
            if not isinstance(note, str):
                note = ""
            note = " ".join(note.split())[:140]
            cleanup_old_jobs()
            job_id = secrets.token_urlsafe(12)
            d = job_dir(job_id)
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, "input-a.jpg"), "wb") as f:
                f.write(a)
            with open(os.path.join(d, "input-b.jpg"), "wb") as f:
                f.write(b)
            job = {"id": job_id, "template": template, "status": "queued",
                   "note": note,
                   "created_at": time.time(), "unlocked": False,
                   "download_token": secrets.token_urlsafe(18)}
            write_job(job)
            self.send_json(200, {"id": job_id, "status": "queued"})
            return

        if path.startswith("/duo/api/unlock"):
            try:
                payload = self.read_json_body()
            except Exception:
                self.send_json(400, {"error": "bad_json"})
                return
            job = read_job(payload.get("job_id", ""))
            if not job:
                self.send_json(404, {"error": "job_not_found"})
                return
            if job["status"] != "done":
                self.send_json(400, {"error": "job_not_ready"})
                return
            if job.get("unlocked"):
                self.send_json(200, {"unlocked": True,
                                     "clean_url": "/duo/files/%s/clean.jpg?t=%s" % (
                                         job["id"], job["download_token"])})
                return
            license_key = (payload.get("license_key") or "").strip().upper()
            if not license_key:
                self.send_json(400, {"error": "missing_license_key"})
                return
            try:
                cfg = load_config()
            except Exception:
                self.send_json(500, {"error": "config_missing"})
                return
            with _lock:
                licenses = load_licenses()
                rec = licenses.get(license_key) or {"uses": 0}
                if rec["uses"] >= MAX_UNLOCKS_PER_KEY:
                    self.send_json(403, {"error": "license_fully_used"})
                    return
                ok, info = verify_gumroad_license(cfg.get("gumroad_product_id", ""), license_key)
                if not ok:
                    self.send_json(403, {"error": "license_invalid", "detail": info.get("message", "")})
                    return
                rec["uses"] += 1
                licenses[license_key] = rec
                save_licenses(licenses)
                job["unlocked"] = True
                write_job(job)
            self.send_json(200, {"unlocked": True,
                                 "clean_url": "/duo/files/%s/clean.jpg?t=%s" % (
                                     job["id"], job["download_token"])})
            return

        if path.startswith("/duo/agent/complete/"):
            if not self.agent_authorized():
                self.send_json(403, {"error": "forbidden"})
                return
            job_id = path.rsplit("/", 1)[-1]
            job = read_job(job_id)
            if not job:
                self.send_json(404, {"error": "not_found"})
                return
            try:
                payload = self.read_json_body()
            except Exception:
                self.send_json(400, {"error": "bad_json"})
                return
            if payload.get("error"):
                job["status"] = "failed"
                job["error"] = str(payload["error"])[:200]
                write_job(job)
                self.send_json(200, {"ok": True})
                return
            try:
                clean = base64.b64decode(payload["clean_b64"])
                preview = base64.b64decode(payload["preview_b64"])
            except Exception:
                self.send_json(400, {"error": "bad_result_data"})
                return
            d = job_dir(job_id)
            with open(os.path.join(d, "clean.jpg"), "wb") as f:
                f.write(clean)
            with open(os.path.join(d, "preview.jpg"), "wb") as f:
                f.write(preview)
            job["status"] = "done"
            job["finished_at"] = time.time()
            write_job(job)
            self.send_json(200, {"ok": True})
            return

        self.send_json(404, {"error": "not_found"})


def main():
    os.makedirs(JOBS_DIR, exist_ok=True)
    port = int(os.environ.get("DUO_PORT", "8790"))
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print("duo app listening on 127.0.0.1:%d" % port)
    server.serve_forever()


if __name__ == "__main__":
    main()
