"""Photo moderation for RouteMind's own place photos (docs/place-ugc-2026-10.md §3).

Runs in the PUBLIC repository ibrahim212-wq/routemind-ugc on GitHub Actions (free minutes) every three
hours — `.github/workflows/moderate.yml` there is this repository's tools/ugc/ci/moderate.yml.

    SUPABASE_URL=… SUPABASE_SERVICE_ROLE_KEY=… GITHUB_TOKEN=… GITHUB_REPOSITORY=owner/routemind-ugc \
        python moderate.py [--dry-run]

What a photo goes through (the upload function put it in the DRAFT release `pending`, whose assets are
not public, with a `pending` row in public.ugc_photos):
  1. it must decode as an image, at least 320 px on its short side;
  2. NUDITY — NudeNet's detector (ONNX, CPU); any exposed-body class at ≥ 0.45 rejects it;
  3. PRIVACY — every face (OpenCV YuNet) and every licence plate (OpenCV's plate cascade, best effort)
     is blurred beyond recognition; EXIF and every other metadata block are dropped by re-encoding;
  4. size — ≤ 1280 px (long side) JPEG q82 + a 320 px thumbnail;
  5. a place shows at most MAX_PER_PLACE photos (newest win; the community flag hides bad ones).
Published photos are assets of `photos-NNNN` releases (≤ 960 assets each, a new one when full). A photo
the community hid (3 flags) or that was rejected has its assets deleted — nothing lingers in git history,
because nothing is ever committed. Rows the upload left without a file are closed as `rejected/lost`.
"""
from __future__ import annotations

import argparse
import calendar
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

MAX_PER_PLACE = 10
MIN_SHORT_SIDE = 320
LONG_SIDE = 1280
THUMB_SIDE = 320
NSFW_SCORE = 0.45
NSFW_CLASSES = {"FEMALE_BREAST_EXPOSED", "FEMALE_GENITALIA_EXPOSED", "MALE_GENITALIA_EXPOSED",
                "ANUS_EXPOSED", "BUTTOCKS_EXPOSED"}
RELEASE_ASSETS = 960
PENDING_TAG = "pending"
YUNET_URL = ("https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/"
             "face_detection_yunet_2023mar.onnx")


def log(*a):
    print("[moderate]", *a, flush=True)


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ── GitHub ─────────────────────────────────────────────────────────────────────────────────────
class Gh:
    def __init__(self, repo: str, token: str, dry: bool):
        self.repo, self.token, self.dry = repo, token, dry

    def call(self, method, path, body=None, data=None, ctype=None, accept="application/vnd.github+json"):
        url = path if path.startswith("http") else "https://api.github.com" + path
        hdr = {"Authorization": f"Bearer {self.token}", "Accept": accept,
               "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "routemind-ugc-moderation"}
        if body is not None:
            data, ctype = json.dumps(body).encode(), "application/json"
        if ctype:
            hdr["Content-Type"] = ctype
        req = urllib.request.Request(url, data=data, method=method, headers=hdr)
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                raw = r.read()
                return r.status, raw
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def json(self, method, path, body=None):
        code, raw = self.call(method, path, body)
        return code, (json.loads(raw) if raw and raw[:1] in (b"{", b"[") else None)

    def releases(self):
        code, rels = self.json("GET", f"/repos/{self.repo}/releases?per_page=100")
        if code != 200:
            raise SystemExit(f"releases: {code} {rels}")
        return rels

    def assets(self, rel_id):
        out, page = [], 1
        while True:
            _, chunk = self.json("GET", f"/repos/{self.repo}/releases/{rel_id}/assets?per_page=100&page={page}")
            out += chunk or []
            if not chunk or len(chunk) < 100:
                return out
            page += 1

    def download(self, asset_id) -> bytes:
        code, raw = self.call("GET", f"/repos/{self.repo}/releases/assets/{asset_id}", accept="application/octet-stream")
        if code != 200:
            raise IOError(f"asset {asset_id}: {code}")
        return raw

    def upload(self, rel_id, name, data, ctype="image/jpeg"):
        if self.dry:
            return
        code, raw = self.call("POST", f"https://uploads.github.com/repos/{self.repo}/releases/{rel_id}/assets?"
                              + urllib.parse.urlencode({"name": name}), data=data, ctype=ctype)
        if code not in (200, 201):
            raise IOError(f"upload {name}: {code} {raw[:200]!r}")

    def delete_asset(self, asset_id):
        if not self.dry:
            self.call("DELETE", f"/repos/{self.repo}/releases/assets/{asset_id}")

    def ensure(self, tag, draft):
        for r in self.releases():
            if r["tag_name"] == tag:
                return r
        if self.dry:
            return {"id": 0, "tag_name": tag, "draft": draft}
        code, r = self.json("POST", f"/repos/{self.repo}/releases",
                            {"tag_name": tag, "name": tag, "draft": draft,
                             "body": "RouteMind user photos (CC BY-SA 4.0, credited in the app). Moderated." if not draft
                             else "Photos waiting for moderation (draft: not public)."})
        if code not in (200, 201):
            raise SystemExit(f"create release {tag}: {code} {r}")
        return r


# ── Supabase (service role) ────────────────────────────────────────────────────────────────────
class Db:
    def __init__(self, url: str, key: str, dry: bool):
        self.base, self.key, self.dry = url.rstrip("/") + "/rest/v1", key, dry

    def call(self, method, path, body=None):
        hdr = {"apikey": self.key, "Authorization": f"Bearer {self.key}", "Content-Type": "application/json",
               "Prefer": "return=minimal"}
        if self.key.startswith("sb_secret_"):
            hdr.pop("Authorization")  # new secret keys are not JWTs
        req = urllib.request.Request(self.base + path, method=method, headers=hdr,
                                     data=json.dumps(body).encode() if body is not None else None)
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read()
            return json.loads(raw) if raw else None

    def rows(self, query):
        return self.call("GET", "/ugc_photos?" + query) or []

    def update(self, pid, fields):
        if not self.dry:
            self.call("PATCH", f"/ugc_photos?id=eq.{pid}", fields)


# ── the image work ─────────────────────────────────────────────────────────────────────────────
class Inspector:
    def __init__(self, work: str):
        import cv2
        from nudenet import NudeDetector
        self.cv2 = cv2
        self.nude = NudeDetector()
        model = os.path.join(work, "yunet.onnx")
        if not os.path.exists(model):
            urllib.request.urlretrieve(YUNET_URL, model)
        self.faces = cv2.FaceDetectorYN.create(model, "", (320, 320), 0.6, 0.3, 5000)
        self.plates = cv2.CascadeClassifier(os.path.join(cv2.data.haarcascades, "haarcascade_russian_plate_number.xml"))

    def nsfw(self, path) -> bool:
        return any(d["class"] in NSFW_CLASSES and d["score"] >= NSFW_SCORE for d in self.nude.detect(path))

    def blur(self, img):
        """Faces and plates blurred in place; returns how many regions."""
        cv2 = self.cv2
        h, w = img.shape[:2]
        boxes = []
        self.faces.setInputSize((w, h))
        _, faces = self.faces.detect(img)
        for f in (faces if faces is not None else []):
            x, y, fw, fh = (int(v) for v in f[:4])
            boxes.append((x - fw // 4, y - fh // 4, fw + fw // 2, fh + fh // 2))
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        for (x, y, pw, ph) in self.plates.detectMultiScale(gray, 1.1, 4, minSize=(40, 12)):
            boxes.append((x - pw // 10, y - ph // 4, pw + pw // 5, ph + ph // 2))
        for (x, y, bw, bh) in boxes:
            x0, y0, x1, y1 = max(0, x), max(0, y), min(w, x + bw), min(h, y + bh)
            if x1 - x0 < 4 or y1 - y0 < 4:
                continue
            k = max(31, (min(x1 - x0, y1 - y0) // 2) | 1)
            img[y0:y1, x0:x1] = cv2.GaussianBlur(img[y0:y1, x0:x1], (k, k), 0)
        return len(boxes)


def encode(img, side, q, cv2) -> bytes:
    h, w = img.shape[:2]
    s = min(1.0, side / max(h, w))
    if s < 1.0:
        img = cv2.resize(img, (int(w * s), int(h * s)), interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, q])  # a fresh JPEG: no EXIF, no GPS
    if not ok:
        raise IOError("encode")
    return buf.tobytes()


def run(dry: bool, work: str) -> int:
    import cv2
    import numpy as np
    repo = os.environ["GITHUB_REPOSITORY"]
    gh = Gh(repo, os.environ["GITHUB_TOKEN"], dry)
    db = Db(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_ROLE_KEY"], dry)
    os.makedirs(work, exist_ok=True)
    pending = gh.ensure(PENDING_TAG, draft=True)
    rels = gh.releases()
    photo_rels = sorted((r for r in rels if r["tag_name"].startswith("photos-") and not r["draft"]),
                        key=lambda r: r["tag_name"])
    current = photo_rels[-1] if photo_rels else gh.ensure("photos-0001", draft=False)
    current_n = len(gh.assets(current["id"])) if current.get("id") else 0
    insp = Inspector(work)
    stats = {"visible": 0, "rejected": 0, "lost": 0, "removed": 0}

    queue = gh.assets(pending["id"]) if pending.get("id") else []
    by_id = {a["name"].rsplit(".", 1)[0]: a for a in queue}
    rows = db.rows("select=id,place_key,status,created_at&status=eq.pending&order=created_at.asc&limit=500")
    for row in rows:
        pid = row["id"]
        asset = by_id.pop(pid, None)
        if asset is None:
            # the upload never landed (or an older run handled the file): close it after an hour
            made = calendar.timegm(time.strptime(row["created_at"][:19], "%Y-%m-%dT%H:%M:%S"))
            if time.time() - made > 3600:
                db.update(pid, {"status": "rejected", "reason": "lost", "decided_at": now()})
                stats["lost"] += 1
            continue
        reason = None
        try:
            raw = gh.download(asset["id"])
            path = os.path.join(work, asset["name"])
            with open(path, "wb") as f:
                f.write(raw)
            img = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
            if img is None:
                reason = "decode"
            elif min(img.shape[:2]) < MIN_SHORT_SIDE:
                reason = "small"
            elif insp.nsfw(path):
                reason = "nsfw"
            else:
                visible = db.rows(f"select=id&place_key=eq.{urllib.parse.quote(row['place_key'])}&status=eq.visible")
                if len(visible) >= MAX_PER_PLACE:
                    reason = "place_full"
            if reason is None:
                n = insp.blur(img)
                full, thumb = encode(img, LONG_SIDE, 82, cv2), encode(img, THUMB_SIDE, 75, cv2)
                if current_n + 2 > RELEASE_ASSETS:
                    nxt = "photos-%04d" % (int(current["tag_name"].split("-")[1]) + 1)
                    current, current_n = gh.ensure(nxt, draft=False), 0
                gh.upload(current["id"], f"{pid}.jpg", full)
                gh.upload(current["id"], f"{pid}_t.jpg", thumb)
                current_n += 2
                h, w = img.shape[:2]
                s = min(1.0, LONG_SIDE / max(h, w))
                db.update(pid, {"status": "visible", "path": f"{current['tag_name']}/{pid}.jpg",
                                "thumb": f"{current['tag_name']}/{pid}_t.jpg", "width": int(w * s),
                                "height": int(h * s), "decided_at": now()})
                stats["visible"] += 1
                log(pid, "visible", f"{n} region(s) blurred")
            else:
                db.update(pid, {"status": "rejected", "reason": reason, "decided_at": now()})
                stats["rejected"] += 1
                log(pid, "rejected", reason)
            gh.delete_asset(asset["id"])
        except Exception as e:  # noqa: BLE001 — one bad photo never stops the run; it is retried next run
            log(pid, "error", repr(e)[:200])
    for name_id, asset in by_id.items():  # files with no pending row (a failed insert, a decided row)
        gh.delete_asset(asset["id"])
        stats["lost"] += 1

    # hidden by the community / removed: the published files go, the row keeps the decision
    for row in db.rows("select=id,path,thumb&status=eq.hidden&path=not.is.null&limit=200"):
        tag = row["path"].split("/")[0]
        rel = next((r for r in photo_rels + [current] if r.get("tag_name") == tag), None)
        if rel and rel.get("id"):
            for a in gh.assets(rel["id"]):
                if a["name"] in (row["path"].split("/")[-1], (row.get("thumb") or "").split("/")[-1]):
                    gh.delete_asset(a["id"])
        db.update(row["id"], {"path": None, "thumb": None})
        stats["removed"] += 1
    log("done", json.dumps(stats))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--work", default="/tmp/ugc-moderation")
    a = ap.parse_args(argv)
    for k in ("SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY", "GITHUB_TOKEN", "GITHUB_REPOSITORY"):
        if not os.environ.get(k):
            print(f"::warning::{k} is not set — nothing to moderate yet (docs/place-ugc-2026-10.md §6)")
            return 0
    return run(a.dry_run, a.work)


if __name__ == "__main__":
    sys.exit(main())
