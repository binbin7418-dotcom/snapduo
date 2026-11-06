#!/usr/bin/env python3
"""SnapDuo generation agent (runs on the local VM).

Polls the EC2 web app for queued jobs, generates the composite through the
Kie API (Nano Banana 2.1, credential via the custom.kie-ai connection),
builds a watermarked preview, and posts results back. Mirrors the
pitchreel-agent pattern: the API key never leaves this machine.
"""
import base64
import io
import json
import os
import sys
import time
import urllib.request

sys.path.insert(0, "/opt/muse/skills/custom-connector-setup/bin")
from dynamic_credentials import add_surrogate_to_request, read_json_response

from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "agent_config.json")
KIE_BASE = "https://api.kie.ai"
KIE_HOSTS = ["api.kie.ai", "kieai.redpandaai.co"]
POLL_SECONDS = 8

TEMPLATES = {
    "christmas-couple": {
        "aspect_ratio": "4:5",
        "prompt": (
            "Create a festive Christmas couple portrait of the two people in the reference photos. "
            "Keep both faces clearly recognizable and faithful to the reference photos. "
            "The couple wears matching red Christmas sweaters with white reindeer patterns, standing close "
            "together and smiling in front of a decorated Christmas tree with warm string lights, holding "
            "mugs of hot cocoa, cozy living room, soft warm golden lighting, photorealistic portrait "
            "photography, natural skin texture."
        ),
    },
    "zombie-choice": {
        "aspect_ratio": "4:5",
        "prompt": (
            "Create a cinematic zombie apocalypse couple portrait of the two people in the reference photos. "
            "Keep both faces clearly recognizable and faithful to the reference photos. "
            "The two stand back to back as survivors in torn, dirty clothes, determined expressions, "
            "a ruined city street with fog behind them, dramatic teal-and-orange movie lighting, "
            "photorealistic movie still, gritty realistic detail, natural skin texture."
        ),
    },
    "tiny-boyfriend": {
        "aspect_ratio": "4:5",
        "prompt": (
            "Create a playful photorealistic picture: the woman from the first reference photo holds her open "
            "palm up, and standing on her palm is a tiny miniature version of the man from the second reference "
            "photo, about 15 cm tall, full body, wearing his normal clothes, smiling up at her. "
            "Keep both faces clearly recognizable and faithful to the reference photos. "
            "Bright soft daylight, shallow depth of field, whimsical and cute mood, natural skin texture."
        ),
    },
}


def load_config():
    with open(CONFIG_PATH) as f:
        return json.load(f)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args):
        raise RuntimeError("redirect refused")


def http_json(method, url, payload=None, headers=None, timeout=60):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data:
        req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    opener = urllib.request.build_opener(NoRedirect())
    with opener.open(req, timeout=timeout) as resp:
        return read_json_response(resp)


def http_bytes(url, timeout=120):
    req = urllib.request.Request(url, method="GET")
    opener = urllib.request.build_opener(NoRedirect())
    with opener.open(req, timeout=timeout) as resp:
        return resp.read()


def kie_call(method, path, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(KIE_BASE + path, data=data, method=method)
    if data:
        req.add_header("Content-Type", "application/json")
    add_surrogate_to_request(req, "custom.kie-ai", allowed_hosts=KIE_HOSTS)
    opener = urllib.request.build_opener(NoRedirect())
    try:
        with opener.open(req, timeout=60) as resp:
            return resp.status, read_json_response(resp)
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8", "replace")
        except Exception:
            body = ""
        return e.code, body


def kie_generate(template_id, image_urls):
    tpl = TEMPLATES[template_id]
    payload = {
        "model": "nano-banana-2-1",
        "input": {
            "prompt": tpl["prompt"],
            "image_input": image_urls,
            "aspect_ratio": tpl["aspect_ratio"],
            "resolution": "1K",
            "output_format": "jpg",
        },
    }
    status, body = kie_call("POST", "/api/v1/jobs/createTask", payload)
    if not isinstance(body, dict) or body.get("code") != 200:
        raise RuntimeError("kie_create_failed: %s %s" % (status, str(body)[:200]))
    task_id = body["data"]["taskId"]
    for _ in range(72):  # up to ~6 minutes
        time.sleep(5)
        status, body = kie_call("GET", "/api/v1/jobs/recordInfo?taskId=%s" % task_id)
        if not isinstance(body, dict):
            continue
        data = body.get("data") or {}
        state = data.get("state")
        if state == "success":
            rj = data.get("resultJson")
            if isinstance(rj, str):
                rj = json.loads(rj)
            urls = (rj or {}).get("resultUrls") or []
            if not urls:
                raise RuntimeError("kie_no_result_url")
            return urls[0]
        if state == "fail":
            raise RuntimeError("kie_task_failed: %s" % json.dumps(data)[:200])
    raise RuntimeError("kie_timeout")


def make_preview(clean_bytes):
    img = Image.open(io.BytesIO(clean_bytes)).convert("RGB")
    target_w = 620
    if img.width > target_w:
        img = img.resize((target_w, int(img.height * target_w / img.width)), Image.LANCZOS)
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 30)
    except Exception:
        font = ImageFont.load_default()
    text = "SnapDuo Preview"
    text_w = draw.textlength(text, font=font)
    step_x, step_y = int(text_w) + 70, 200
    row = 0
    for y in range(-40, img.height + 40, step_y):
        offset = (step_x // 2) if row % 2 else 0
        for x in range(-80 - offset, img.width + 80, step_x):
            draw.text((x + offset, y), text, font=font, fill=(255, 255, 255, 110))
            draw.text((x + offset + 2, y + 2), text, font=font, fill=(0, 0, 0, 60))
        row += 1
    out = Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")
    buf = io.BytesIO()
    out.save(buf, "JPEG", quality=82)
    return buf.getvalue()


def process_job(cfg, job):
    base = cfg["web_base"].rstrip("/")
    token = cfg["agent_token"]
    headers = {"X-Agent-Token": token}
    try:
        result_url = kie_generate(job["template"], [job["input_a_url"], job["input_b_url"]])
        clean = http_bytes(result_url)
        preview = make_preview(clean)
        http_json("POST", "%s/duo/agent/complete/%s" % (base, job["id"]), {
            "clean_b64": base64.b64encode(clean).decode(),
            "preview_b64": base64.b64encode(preview).decode(),
        }, headers=headers, timeout=180)
        print("done job %s" % job["id"], flush=True)
    except Exception as e:
        print("job %s failed: %s" % (job["id"], e), flush=True)
        try:
            http_json("POST", "%s/duo/agent/complete/%s" % (base, job["id"]),
                      {"error": str(e)[:180]}, headers=headers, timeout=60)
        except Exception as e2:
            print("could not report failure: %s" % e2, flush=True)


def main():
    cfg = load_config()
    base = cfg["web_base"].rstrip("/")
    headers = {"X-Agent-Token": cfg["agent_token"]}
    print("duo agent started, polling %s" % base, flush=True)
    while True:
        try:
            data = http_json("GET", base + "/duo/agent/next", headers=headers, timeout=30)
            job = (data or {}).get("job")
            if job:
                process_job(cfg, job)
                continue
        except Exception as e:
            print("poll error: %s" % e, flush=True)
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
