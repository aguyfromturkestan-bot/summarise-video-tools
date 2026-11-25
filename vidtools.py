#!/usr/bin/env python3
"""vidtools: get the words (and, when useful, the pictures) out of a video.

  fetch SOURCE       metadata + existing captions (+ media if needed); URL or local file
  transcribe MEDIA   faster-whisper speech-to-text -> transcript.txt
  frames VIDEO       scene-change keyframes -> contact sheets with burned-in timestamps
  normalize INPUT    .vtt/.srt/json3/segments.json/pasted panel text -> transcript.txt
  check SUMMARY TRANSCRIPT   verify quotes and timestamps in a finished summary
  mentions TRANSCRIPT TERMS  where actors/themes appear and which appear together (terms.json)

Every command prints a JSON report on stdout. Transcript lines look like
"[HH:MM:SS] text", merged into ~20 s blocks.
"""
import argparse, glob, html, json, os, re, shutil, subprocess, sys, time, unicodedata

# ----------------------------------------------------------------- helpers
def hms(t):
    t = max(0, int(t or 0)); return "%02d:%02d:%02d" % (t // 3600, t % 3600 // 60, t % 60)

def secs(t):
    if t in (None, ""): return None
    s = 0.0
    for p in str(t).split(":"): s = s * 60 + float(p)
    return s

def run(cmd, timeout=300):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout, r.stderr
    except subprocess.TimeoutExpired: return 124, "", "timeout after %ss" % timeout
    except FileNotFoundError as e: return 127, "", str(e)

def slug(s, n=60):
    s = re.sub(r"[^\w\-]+", "-", s or "video", flags=re.U).strip("-").lower()
    return s[:n] or "video"

def out(obj):
    print(json.dumps(obj, ensure_ascii=False, indent=1)); return 0 if obj.get("ok", True) else 1

def probe_duration(path):
    c, so, _ = run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", path], 60)
    try: return float(so.strip())
    except ValueError: return None

# ----------------------------------------------------------------- normalize
TS = re.compile(r"(?:(\d+):)?(\d{1,2}):(\d{2})[.,](\d{1,3})\s*-->")
PANEL = re.compile(r"^\s*(?:(\d{1,2}):)?(\d{1,2}):(\d{2})\s*$")
INLINE = re.compile(r"^\s*[\[(]?(?:(\d{1,2}):)?(\d{1,2}):(\d{2})(?:[.,]\d+)?[\])]?\s*[-–—:]?\s+(.*\S)\s*$")

def _s(h, m, s, ms="0"): return int(h or 0) * 3600 + int(m) * 60 + int(s) + int((ms or "0").ljust(3, "0")[:3]) / 1000
def clean(t):
    t = html.unescape(re.sub(r"<[^>]+>", "", t)).replace("\u200b", "").replace("\xa0", " ")
    return re.sub(r"\s+", " ", t).strip()

def parse_subs(raw):
    """VTT/SRT -> [(start, line)], line by line (some sites omit blank lines),
    dropping the repeats of YouTube's rolling auto-captions."""
    cues, start, cur = [], None, []
    lines = raw.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    for i, line in enumerate(lines):
        m = TS.search(line) if "-->" in line else None
        if m:
            if start is not None and cur: cues.append((start, cur))
            start, cur = _s(*m.groups()), []; continue
        if start is None: continue
        s = line.strip(); nxt = lines[i + 1] if i + 1 < len(lines) else ""
        if not s or (s.isdigit() and "-->" in nxt):
            if not s and cur: cues.append((start, cur)); start, cur = None, []
            continue
        if clean(s): cur.append(clean(s))
    if start is not None and cur: cues.append((start, cur))
    res, seen = [], []
    for st, tl in cues:
        for t in tl:
            if seen and t in seen[-3:]: continue
            if res and t.startswith(res[-1][1]) and len(t) > len(res[-1][1]):
                res[-1] = (res[-1][0], t); seen.append(t); continue
            res.append((st, t)); seen.append(t)
    return res

def parse_text(raw):
    res, pending, stamped = [], None, 0
    for l in raw.replace("\r\n", "\n").split("\n"):
        if not l.strip(): continue
        m = PANEL.match(l)
        if m: pending = _s(*m.groups()); stamped += 1; continue
        m = INLINE.match(l)
        if m: res.append((_s(m.group(1), m.group(2), m.group(3)), clean(m.group(4)))); stamped += 1; pending = None; continue
        if re.fullmatch(r"\s*(\d+\s+(hours?|minutes?|seconds?|sati?|minuta?|sekundi?),?\s*)+", l, re.I): continue
        if pending is not None: res.append((pending, clean(l))); pending = None
        elif res and stamped: res[-1] = (res[-1][0], res[-1][1] + " " + clean(l))
        else: res.append((None, clean(l)))
    return res, stamped > 0

def load_any(path):
    raw = open(path, encoding="utf-8", errors="replace").read()
    ext = os.path.splitext(path)[1].lower()
    if ext in (".json", ".json3"):
        d = json.loads(raw)
        if isinstance(d, dict) and "events" in d:
            return [(e.get("tStartMs", 0) / 1000, clean("".join(s.get("utf8", "") for s in e["segs"])))
                    for e in d["events"] if e.get("segs") and clean("".join(s.get("utf8", "") for s in e["segs"]))], True
        segs = d.get("segments", d) if isinstance(d, dict) else d
        return [(float(s["start"]), clean(s["text"])) for s in segs if clean(s.get("text", ""))], True
    if ext in (".vtt", ".srt") or "-->" in raw[:5000]: return parse_subs(raw), True
    return parse_text(raw)

def blocks_of(cues, sec):
    if sec <= 0: return cues
    res, st, cur = [], None, []
    for s, t in cues:
        st = s if st is None else st; cur.append(t); j = " ".join(cur)
        if s - st >= sec or (j.endswith((".", "?", "!")) and s - st >= sec * 0.6):
            res.append((st, j)); st, cur = None, []
    if cur: res.append((st, " ".join(cur)))
    return res

def normalize(inp, outp, block=20.0, chunk_minutes=0, chapters=None):
    cues, has_ts = load_any(inp)
    if has_ts:
        bl = blocks_of([(s or 0.0, t) for s, t in cues], block)
        text = "\n".join("[%s] %s" % (hms(s), t) for s, t in bl)
    else:
        bl = cues; text = "\n".join(t for _, t in cues)
    open(outp, "w", encoding="utf-8").write(text + "\n")
    chunks = []
    if has_ts and chunk_minutes > 0 and bl:
        step = chunk_minutes * 60
        starts = sorted(float(c.get("start_time", 0)) for c in (chapters or []))
        if len(starts) >= 2:
            merged = [starts[0]]
            for s in starts[1:]:
                if s - merged[-1] >= step / 3: merged.append(s)
            starts = merged
        else:
            starts = [i * step for i in range(int(bl[-1][0] // step) + 1)]
        d = os.path.join(os.path.dirname(os.path.abspath(outp)), "chunks"); os.makedirs(d, exist_ok=True)
        for i, st in enumerate(starts):
            en = starts[i + 1] if i + 1 < len(starts) else float("inf")
            part = [(s, t) for s, t in bl if st <= s < en]
            if part:
                p = os.path.join(d, "part_%02d_%s.txt" % (i + 1, hms(st).replace(":", "-")))
                open(p, "w", encoding="utf-8").write("\n".join("[%s] %s" % (hms(s), t) for s, t in part) + "\n")
                chunks.append(p)
    w = len(text.split())
    return {"ok": True, "output": outp, "timestamps": has_ts, "blocks": len(bl), "words": w,
            "approx_tokens": int(w * 1.4), "last_timestamp": hms(bl[-1][0]) if has_ts and bl else None, "chunks": chunks}

# ----------------------------------------------------------------- fetch
YT_CLIENTS = ["default", "mweb", "tv_simply", "web_safari", "android_vr"]

def classify(e):
    e = e.lower()
    for key, label in [("confirm you", "youtube_bot_check"), ("po token", "youtube_po_token"),
                       ("did not get any data blocks", "empty_response_po_token"), ("429", "rate_limited_429"),
                       ("too many requests", "rate_limited_429"), ("private video", "private_or_login"),
                       ("members-only", "private_or_login"), ("login", "private_or_login"),
                       ("not available in your country", "geo_blocked"), ("unsupported url", "unsupported_site"),
                       ("requested format is not available", "no_downloadable_format"),
                       ("agent-proxy", "network_blocked"), ("403", "http_403")]:
        if key in e: return label
    return "unknown"

def ytdlp(a):
    c = ["yt-dlp", "--no-warnings", "--no-playlist", "--retries", "3", "--socket-timeout", "30"]
    if a.cookies: c += ["--cookies", a.cookies]
    if a.cookies_from_browser: c += ["--cookies-from-browser", a.cookies_from_browser]
    return c

def client_args(is_yt, c): return ["--extractor-args", "youtube:player_client=%s" % c] if is_yt and c and c != "default" else []

def fetch_info(url, a, is_yt):
    best, score_best, client, errs = None, -1, None, []
    for c in (YT_CLIENTS if is_yt else ["default"]):
        code, so, se = run(ytdlp(a) + ["-J", "--skip-download", "--ignore-no-formats-error"] + client_args(is_yt, c) + [url], 120)
        try: info = json.loads(so) if so.strip() not in ("", "null") else None
        except json.JSONDecodeError: info = None
        if not info:
            errs.append("%s: %s" % (c, classify(se)))
            if "429" in se: time.sleep(6)
            continue
        sc = 2 * bool(info.get("subtitles") or info.get("automatic_captions")) + bool(info.get("formats")) + bool(info.get("duration"))
        if sc > score_best: best, score_best, client = info, sc, c
        if sc >= 3: break
        errs.append("%s: partial info" % c)
    return best, client, errs

def choose_track(info, want):
    man = {k: v for k, v in (info.get("subtitles") or {}).items() if k != "live_chat"}
    auto = info.get("automatic_captions") or {}
    orig = [k for k in auto if k.endswith("-orig")]
    spoken = want or info.get("language") or (orig[0][:-5] if orig else None)
    def match(keys, lang):
        if not lang: return None
        for k in keys:
            if k.lower() == lang.lower(): return k
        for k in keys:
            if k.lower().split("-")[0] == lang.lower().split("-")[0]: return k
    k = match(man, spoken)
    if k: return k, "manual", spoken
    if spoken and spoken + "-orig" in auto: return spoken + "-orig", "auto", spoken
    k = match(orig, spoken) or (orig[0] if orig else None)
    if k: return k, "auto", k[:-5]
    k = match(auto, spoken)
    if k: return k, "auto (may be machine-translated)", spoken
    if man:
        k = match(man, "en") or sorted(man)[0]; return k, "manual", spoken or k
    return None, None, spoken

def fetch_local(path, wd, a):
    meta = {"source_type": "local", "path": os.path.abspath(path), "title": os.path.splitext(os.path.basename(path))[0]}
    st, streams = {"media": os.path.abspath(path)}, []
    c, so, _ = run(["ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", path], 60)
    if c == 0:
        j = json.loads(so); f = j.get("format", {}); tags = {k.lower(): v for k, v in (f.get("tags") or {}).items()}
        meta.update({"duration": float(f.get("duration") or 0) or None, "title": tags.get("title") or meta["title"],
                     "creation_time": tags.get("creation_time") or tags.get("date"), "artist": tags.get("artist")})
        streams = j.get("streams", [])
    meta["has_video"] = any(s.get("codec_type") == "video" and not s.get("disposition", {}).get("attached_pic") for s in streams)
    side = [p for p in glob.glob(glob.escape(os.path.splitext(path)[0]) + "*") if p.lower().endswith((".srt", ".vtt"))]
    tpath = os.path.join(wd, "transcript.txt")
    if side:
        st["transcript_report"] = normalize(side[0], tpath, a.block_seconds); st["transcript_source"] = "sidecar " + os.path.basename(side[0])
    else:
        subs = [s for s in streams if s.get("codec_type") == "subtitle" and s.get("codec_name") in ("subrip", "mov_text", "webvtt", "ass", "ssa")]
        if subs:
            dest = os.path.join(wd, "captions.srt")
            run(["ffmpeg", "-loglevel", "error", "-y", "-i", path, "-map", "0:%d" % subs[0]["index"], dest], 120)
            if os.path.exists(dest):
                st["transcript_report"] = normalize(dest, tpath, a.block_seconds); st["transcript_source"] = "embedded subtitles"
    return meta, st

def cmd_fetch(a):
    src = a.source.strip(); st = {"ok": False, "source": src, "problems": []}
    if os.path.exists(src):
        wd = a.workdir or os.path.join(os.getcwd(), "video_work", slug(os.path.basename(src))); os.makedirs(wd, exist_ok=True)
        meta, s2 = fetch_local(src, wd, a); st.update(s2); st["source_type"] = "local"
    else:
        if not shutil.which("yt-dlp"):
            st["problems"].append("yt-dlp missing: pip install -U yt-dlp"); return out(st)
        is_yt = bool(re.search(r"(youtube\.com|youtu\.be|youtube-nocookie\.com)", src))
        st["source_type"] = "youtube" if is_yt else "url"
        info, client, errs = fetch_info(src, a, is_yt)
        if not info:
            wd = a.workdir or os.path.join(os.getcwd(), "video_work", "blocked"); os.makedirs(wd, exist_ok=True)
            meta = {"source_url": src}; st["problems"] += errs; st["blocked"] = True
        else:
            vid = str(info.get("id") or "")
            name = slug(info.get("title"), 50) if (vid.isdigit() or len(vid) < 6) and info.get("title") else slug(vid or info.get("title"))
            wd = a.workdir or os.path.join(os.getcwd(), "video_work", name); os.makedirs(wd, exist_ok=True)
            ip = os.path.join(wd, "info.json"); json.dump(info, open(ip, "w"))
            keys = ["id", "title", "uploader", "uploader_url", "channel", "channel_url", "creator", "upload_date",
                    "release_date", "duration", "language", "webpage_url", "extractor_key", "tags", "chapters", "license"]
            meta = {k: info.get(k) for k in keys if info.get(k) not in (None, "", [])}
            d = info.get("description") or ""; meta["description"] = d[:3000] + ("..." if len(d) > 3000 else "")
            meta["source_url"] = src; meta["manual_caption_langs"] = sorted((info.get("subtitles") or {}).keys())[:60]
            meta["auto_caption_original"] = [k for k in (info.get("automatic_captions") or {}) if k.endswith("-orig")]
            st["client"] = client
            key, kind, spoken = choose_track(info, a.lang); meta["spoken_language_guess"] = spoken
            json.dump(meta, open(os.path.join(wd, "metadata.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
            if key:
                for f in glob.glob(os.path.join(wd, "captions.*")): os.remove(f)
                cmd = ytdlp(a) + ["--load-info-json", ip, "--skip-download", "--ignore-no-formats-error",
                                  "--write-subs" if kind == "manual" else "--write-auto-subs", "--sub-langs", "^%s$" % re.escape(key),
                                  "--sub-format", "json3/vtt/srt/best", "-o", os.path.join(wd, "captions.%(ext)s")] + client_args(is_yt, client)
                err = ""
                for attempt in range(2):
                    c, so, se = run(cmd, 120)
                    caps = [f for f in glob.glob(os.path.join(wd, "captions.*")) if not f.endswith(".part")]
                    if caps or "429" not in se: break
                    time.sleep(8)
                if caps:
                    st["transcript_report"] = normalize(caps[0], os.path.join(wd, "transcript.txt"), a.block_seconds)
                    st["transcript_source"] = "%s captions (%s)" % (kind, key)
                else:
                    st["problems"].append("caption download failed: %s" % classify(se)); st["blocked"] = is_yt
            else:
                st["problems"].append("no captions published")
            if a.media in ("audio", "video") or (a.media == "auto" and "transcript_source" not in st):
                h = a.max_height
                fmt = "ba/b[height<=%d]/w" % h if a.media != "video" else "b[height<=%d][vcodec!=none][acodec!=none]/bv*[height<=%d]+ba/b/w" % (h, h)
                base = ytdlp(a) + ["-f", fmt, "-o", os.path.join(wd, "media.%(ext)s"), "--no-part"] + client_args(is_yt, client)
                for srcarg in (["--load-info-json", ip], [src]):
                    c, so, se = run(base + srcarg, 1800)
                    media = [f for f in glob.glob(os.path.join(wd, "media.*")) if not f.endswith((".part", ".ytdl"))]
                    if media: st["media"] = media[0]; break
                else:
                    st["problems"].append("media download failed: %s" % classify(se)); st["blocked"] = st.get("blocked") or is_yt
    mp = os.path.join(wd, "metadata.json")
    json.dump(meta, open(mp, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    t = os.path.join(wd, "transcript.txt")
    st.update({"workdir": wd, "metadata": mp, "title": meta.get("title"), "duration": meta.get("duration"),
               "chapters": len(meta.get("chapters") or []), "transcript": t if os.path.exists(t) else None})
    st["needs"] = [] if st["transcript"] else (["transcribe"] if st.get("media") else ["transcript_from_browser_or_user"])
    st["ok"] = bool(st["transcript"] or st.get("media"))
    return out(st)

# ----------------------------------------------------------------- transcribe
# x realtime on 2 cores, int8. large-v3-turbo: large-v3 accuracy at small-model cost; best default when it fits.
SPEED = {"tiny": 20.0, "base": 12.0, "small": 5.0, "large-v3-turbo": 3.0, "medium": 0.8, "large-v3": 0.3}

def pick_model(span, lang, cores, budget_min=20):
    k, mins = max(1.0, cores / 2.0), (span or 600) / 60.0
    for name in ["large-v3-turbo", "small", "base"]:
        if mins / (SPEED[name] * k) <= budget_min:
            return "small" if name == "base" and lang not in (None, "en") else name
    return "base" if lang in (None, "en") else "small"

def cmd_transcribe(a):
    """Transcribes in 10-minute windows and saves after each one, so a killed run
    (memory, timeout) resumes where it stopped when rerun with the same arguments."""
    try:
        import numpy as np
        from faster_whisper import WhisperModel
    except ImportError:
        return out({"ok": False, "error": "pip install faster-whisper numpy"})
    wd = a.out or os.path.join(os.path.dirname(os.path.abspath(a.media)), "video_work"); os.makedirs(wd, exist_ok=True)
    log = open(os.path.join(wd, "transcribe.log"), "a", encoding="utf-8")
    def say(m): log.write(m + "\n"); log.flush(); print(m, file=sys.stderr, flush=True)
    dur = probe_duration(a.media)
    start, end = secs(a.start) or 0.0, secs(a.end) or dur
    if not end: return out({"ok": False, "error": "cannot read duration"})
    span, cores = end - start, os.cpu_count() or 2
    model = a.model if a.model != "auto" else pick_model(span, a.lang, cores)
    est = span / 60 / (SPEED.get(model, 2) * max(1, cores / 2) * a.speed)
    partial = os.path.join(wd, "segments.partial.json")
    key = {"media": os.path.abspath(a.media), "model": model, "start": start, "end": end, "speed": a.speed}
    st = {"key": key, "done_until": start, "language": a.lang, "lang_p": None, "segments": []}
    if os.path.exists(partial):
        old = json.load(open(partial, encoding="utf-8"))
        if old.get("key") == key: st = old
    say("model=%s cores=%d audio=%s est_minutes=%.0f resume_from=%s" % (model, cores, hms(span), est, hms(st["done_until"])))
    t0 = time.time()
    m = WhisperModel(model, device="cpu", compute_type="int8", cpu_threads=cores)
    window = 600.0
    while st["done_until"] < end - 0.5:
        pos = st["done_until"]; length = min(window, end - pos)
        raw = subprocess.run(["ffmpeg", "-loglevel", "error", "-nostdin", "-ss", str(pos), "-t", str(length), "-i", a.media,
                              "-vn", *(["-af", "atempo=%s" % a.speed] if a.speed != 1 else []),
                              "-ac", "1", "-ar", "16000", "-f", "f32le", "-"], capture_output=True).stdout
        audio = np.frombuffer(raw, np.float32)
        if audio.size >= 16000:
            it, info = m.transcribe(audio, language=st["language"], beam_size=1 if model in ("medium", "large-v3") else 2,
                                    vad_filter=not a.no_vad, condition_on_previous_text=False, initial_prompt=a.prompt)
            if not st["language"]:
                st["language"], st["lang_p"] = info.language, round(info.language_probability, 2)
                say("language=%s p=%.2f" % (info.language, info.language_probability))
            for s in it:
                st["segments"].append({"start": round(s.start * a.speed + pos, 2), "end": round(s.end * a.speed + pos, 2), "text": s.text.strip(),
                                       "avg_logprob": round(s.avg_logprob, 3)})
        elif pos == start:
            return out({"ok": False, "error": "no decodable audio"})
        st["done_until"] = pos + length
        json.dump(st, open(partial, "w", encoding="utf-8"), ensure_ascii=False)
        say("progress %s / %s (%.1f min elapsed)" % (hms(st["done_until"]), hms(end), (time.time() - t0) / 60))
    segs = st["segments"]
    sp = os.path.join(wd, "segments.json")
    json.dump({"model": model, "language": st["language"], "segments": segs}, open(sp, "w", encoding="utf-8"), ensure_ascii=False)
    os.remove(partial)
    rep = normalize(sp, os.path.join(wd, "transcript.txt"), a.block_seconds)
    texts = [s["text"] for s in segs]
    loops = sum(1 for i in range(2, len(texts)) if texts[i] and texts[i] == texts[i - 1] == texts[i - 2])
    edges = [start] + [x for s in segs for x in (s["start"], s["end"])] + [end]
    gaps = ["%s-%s" % (hms(edges[i]), hms(edges[i + 1])) for i in range(0, len(edges) - 1, 2) if edges[i + 1] - edges[i] > 20]
    say("done in %.1f min" % ((time.time() - t0) / 60))
    return out({"ok": True, "model": model, "language": st["language"], "language_probability": st["lang_p"],
                "segments": len(segs), "low_confidence_segments": sum(1 for s in segs if s["avg_logprob"] < -1.0),
                "repetition_loops": loops, "silent_gaps_over_20s": gaps, "minutes": round((time.time() - t0) / 60, 1),
                "transcript": rep["output"], "words": rep["words"], "last_timestamp": rep["last_timestamp"]})

# ----------------------------------------------------------------- frames
def cmd_frames(a):
    from PIL import Image, ImageDraw, ImageFont
    wd = a.out or os.path.join(os.path.dirname(os.path.abspath(a.video)), "video_work")
    fd = os.path.join(wd, "frames"); os.makedirs(fd, exist_ok=True)
    for f in glob.glob(os.path.join(fd, "*.jpg")): os.remove(f)
    dur = probe_duration(a.video)
    if not dur: return out({"ok": False, "error": "no readable video"})
    st, en = secs(a.start) or 0.0, secs(a.end) or dur; span = max(1.0, en - st)
    pinned = [secs(x.strip()) for x in a.at.split(",") if x.strip()]
    times, mode = [], a.mode
    if a.mode == "scene":
        c, so, se = run(["ffmpeg", "-nostdin", "-hide_banner", "-ss", str(st), "-i", a.video, "-t", str(span), "-an",
                         "-vf", "fps=1,select='gt(scene\\,%s)',showinfo" % a.threshold, "-f", "null", "-"], 3600)
        times = [float(x) + st for x in re.findall(r"pts_time:([0-9.]+)", se)]
        if len(times) < min(6, a.max // 3): mode, times = "uniform (static video: %d scene changes)" % len(times), []
    if not times:
        n = max(1, min(a.max, int(span // 10) or 1)); times = [st + span / n * (i + 0.5) for i in range(n)]
    if len(times) > a.max: times = [times[int(i * len(times) / a.max)] for i in range(a.max)]
    items, last = [], None
    for i, t in enumerate(sorted(set([round(x, 1) for x in times] + [round(p, 1) for p in pinned if p is not None]))):
        p = os.path.join(fd, "frame_%04d.jpg" % (i + 1))
        run(["ffmpeg", "-loglevel", "error", "-nostdin", "-y", "-ss", "%.2f" % t, "-i", a.video, "-frames:v", "1",
             "-vf", "scale=%d:-2" % a.width, "-q:v", "3", p], 120)
        if not os.path.exists(p): continue
        sig = list(Image.open(p).convert("L").resize((16, 16)).tobytes())
        if last and sum(abs(x - y) for x, y in zip(sig, last)) / 256 < 6 and round(t, 1) not in [round(q or -1, 1) for q in pinned]:
            os.remove(p); continue
        last = sig; items.append({"file": os.path.basename(p), "t": t, "hms": hms(t)})
    try:
        fonts = glob.glob("/usr/share/fonts/**/DejaVuSans-Bold.ttf", recursive=True) or glob.glob("/usr/share/fonts/**/*.ttf", recursive=True) \
            or glob.glob("/System/Library/Fonts/*.ttc") or glob.glob("C:/Windows/Fonts/arial*.ttf")
        font = ImageFont.truetype(fonts[0], 22)
    except Exception:
        font = ImageFont.load_default()
    per, sheets = a.cols * a.rows, []
    for si in range(0, len(items), per):
        grp = items[si:si + per]; ims = [Image.open(os.path.join(fd, g["file"])).convert("RGB") for g in grp]
        w, h = max(i.width for i in ims), max(i.height for i in ims)
        sh = Image.new("RGB", (a.cols * w, ((len(grp) + a.cols - 1) // a.cols) * h)); d = ImageDraw.Draw(sh)
        for k, (g, im) in enumerate(zip(grp, ims)):
            x, y = (k % a.cols) * w, (k // a.cols) * h; sh.paste(im, (x, y))
            bb = d.textbbox((0, 0), g["hms"], font=font); d.rectangle([x, y, x + bb[2] + 12, y + bb[3] + 10], fill="black")
            d.text((x + 6, y + 4), g["hms"], fill="yellow", font=font)
        name = os.path.join(fd, "sheet_%02d.jpg" % (si // per + 1)); sh.save(name, quality=85); sheets.append(name)
        for g in grp: g["sheet"] = os.path.basename(name)
    json.dump(items, open(os.path.join(fd, "index.json"), "w"), indent=1)
    return out({"ok": True, "mode": mode, "frames": len(items), "sheets": sheets, "frames_dir": fd, "duration": hms(dur)})

# ----------------------------------------------------------------- check
CYR = dict(zip("абвгдђежзијклљмнњопрстћуфхцчџш", ["a","b","v","g","d","đ","e","ž","z","i","j","k","l","lj","m","n","nj","o","p","r","s","t","ć","u","f","h","c","č","dž","š"]))
def fold(s):
    s = "".join(CYR.get(ch, ch) for ch in s.lower())
    s = unicodedata.normalize("NFKD", s).replace("đ", "d")
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    return re.sub(r"[^\w ]+", " ", s).split()

def cmd_check(a):
    import difflib
    summ = open(a.summary, encoding="utf-8").read()
    lines = [l for l in open(a.transcript, encoding="utf-8").read().split("\n") if l.strip()]
    tl = []  # (seconds, words)
    for l in lines:
        m = re.match(r"\s*[\[(]?((?:\d{1,2}:)?\d{1,2}:\d{2})(?:[.,]\d+)?(?:\s*(?:-->|[-–])\s*(?:\d{1,2}:)?\d{1,2}:\d{2}(?:[.,]\d+)?)?[\])]?\s+(.*)", l)
        tl.append((secs(m.group(1)), fold(m.group(2))) if m else (None, fold(l)))
    words, owner = [], []
    for t, w in tl: words += w; owner += [t] * len(w)
    last_t = max([t for t, _ in tl if t is not None] or [0]); limit = a.duration or last_t + 60
    quotes = [(m.group(1), summ[m.end():m.end() + 90].split("\n")[0]) for m in re.finditer(r"[\"“„«]([^\"“”„«»\n]{1,600})[\"”“»]", summ)
              if len(m.group(1)) >= 12]
    quotes += [(m.group(1), m.group(1)) for m in re.finditer(r"^>\s*(.{12,600})$", summ, re.M)
               if not re.search(r"[\"“„«]", m.group(1))]
    results, bad = [], 0
    for q, region in quotes:
        cited = re.search(r"(?<![\d:])(\d{1,2}:\d{2}(?::\d{2})?)(?![\d:])", region)
        q = re.split(r"\s[—–]\s|\s[—–]\(", q)[0]
        body = re.sub(r"\([^)]*\d+:\d+[^)]*\)", " ", q)
        parts = [fold(re.sub(r"\[[^\]]*\]", " ", p)) for p in re.split(r"\[?(?:\.\.\.|…)\]?", body)]
        parts = [p for p in parts if len(p) >= 2] or [fold(body)]
        if sum(len(p) for p in parts) < 3: continue
        scores, at = [], None
        for k, qw in enumerate(parts):  # a quote cut with "..." must match part by part
            n, best, pos = len(qw), 0.0, None
            target = " ".join(qw)
            for i in range(0, max(1, len(words) - n + 1)):
                if words[i] not in qw[:3] and i % 3: continue
                r = difflib.SequenceMatcher(None, target, " ".join(words[i:i + n])).ratio()
                if r > best: best, pos = r, i
            scores.append(best)
            if k == 0: at = pos
        best = min(scores)
        found = owner[at] if at is not None else None
        verdict = "ok" if best >= 0.85 else ("paraphrased/edited - fix or unquote" if best >= 0.6 else "NOT IN TRANSCRIPT")
        if verdict == "ok" and cited and found is not None and abs(secs(cited.group(1)) - found) > 60:
            verdict = "timestamp off: cited %s, transcript block starts %s" % (cited.group(1), hms(found))
        bad += verdict != "ok"
        results.append({"quote": q[:90], "match": round(best, 2), "cited": cited.group(1) if cited else None,
                        "found_at": hms(found) if found is not None else None, "verdict": verdict})
    stamps = re.findall(r"(?<![\d:])(\d{1,2}:\d{2}(?::\d{2})?)(?![\d:])", summ)
    over = sorted({s for s in stamps if secs(s) is not None and secs(s) > limit})
    return out({"ok": bad == 0 and not over, "quotes_checked": len(results), "quote_problems": bad, "quotes": results,
                "timestamps_found": len(stamps), "timestamps_beyond_end": over, "transcript_ends": hms(last_t)})

# ----------------------------------------------------------------- mentions
def cmd_mentions(a):
    """Count where each actor/theme term group appears and which groups share a passage.
    terms.json: {"Label": ["stem", "multi word stem", ...]}. Stems match word starts after folding
    (case, diacritics and Cyrillic->Latin), so "vučić" also catches "Vučića", "Вучићу"."""
    import itertools
    groups = {k: [fold(s) for s in v if fold(s)] for k, v in json.load(open(a.terms, encoding="utf-8")).items()}
    def count(words, stem):  # every word of a multi-word stem must start the matching word in sequence
        n = len(stem)
        return sum(1 for i in range(len(words) - n + 1) if all(words[i + j].startswith(stem[j]) for j in range(n)))
    blocks = []
    for l in open(a.transcript, encoding="utf-8").read().split("\n"):
        if not l.strip(): continue
        m = re.match(r"\s*\[((?:\d{1,2}:)?\d{1,2}:\d{2})\]\s*(.*)", l)
        blocks.append((secs(m.group(1)), m.group(2)) if m else (None, l))
    wins, cur, key = [], [], None
    for i, (t, txt) in enumerate(blocks):
        k = int(t // a.window) if t is not None else i
        if key is not None and k != key: wins.append(cur); cur = []
        key = k; cur.append((t, txt))
    if cur: wins.append(cur)
    end = max([t for t, _ in blocks if t is not None] or [0]) or 1
    stats = {g: {"passages": 0, "hits": 0, "at": []} for g in groups}
    pairs, bins = {}, {g: [0] * 10 for g in groups}
    for w in wins:
        present = {}  # group -> [time of first hit in this passage, hits]
        for t, txt in w:
            words = fold(txt)
            for g, stems in groups.items():
                n = sum(count(words, s) for s in stems)
                if n: present.setdefault(g, [t, 0])[1] += n
        for g, (t, n) in present.items():
            st = stats[g]; st["passages"] += 1; st["hits"] += n
            if t is not None:
                st["at"].append(hms(t)); bins[g][min(9, int(t / end * 10))] += 1
        for x, y in itertools.combinations(sorted(present), 2):
            p = pairs.setdefault((x, y), {"passages": 0, "at": []}); p["passages"] += 1
            tt = [present[x][0], present[y][0]]
            if None not in tt and len(p["at"]) < 6: p["at"].append(hms(max(tt)))
    bar = "▁▂▃▄▅▆▇█"
    def spark(v):
        mx = max(v) or 1
        return "".join(bar[min(7, int(x / mx * 7))] if x else "·" for x in v)
    actors = sorted(({"term": g, "passages": s["passages"], "hits": s["hits"], "first": s["at"][0] if s["at"] else None,
                      "last": s["at"][-1] if s["at"] else None, "timeline": spark(bins[g])} for g, s in stats.items()),
                    key=lambda d: -d["passages"])
    co = sorted(({"a": x, "b": y, **v} for (x, y), v in pairs.items()), key=lambda d: -d["passages"])[:a.top]
    return out({"ok": True, "window_seconds": a.window, "passages": len(wins), "terms": actors, "together": co,
                "note": "Co-mention = both appear in the same %d s passage. It shows proximity, not the relation: read the passages." % a.window})

# ----------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = ap.add_subparsers(dest="cmd", required=True)
    f = sp.add_parser("fetch"); f.add_argument("source"); f.add_argument("-o", "--workdir")
    f.add_argument("--lang"); f.add_argument("--media", choices=["none", "auto", "audio", "video"], default="auto")
    f.add_argument("--max-height", type=int, default=480); f.add_argument("--cookies"); f.add_argument("--cookies-from-browser")
    f.add_argument("--block-seconds", type=float, default=20)
    t = sp.add_parser("transcribe"); t.add_argument("media"); t.add_argument("-o", "--out"); t.add_argument("--lang")
    t.add_argument("--model", default="auto"); t.add_argument("--start"); t.add_argument("--end"); t.add_argument("--prompt")
    t.add_argument("--block-seconds", type=float, default=20)
    t.add_argument("--no-vad", action="store_true", help="keep audio the voice-activity filter would skip")
    t.add_argument("--speed", type=float, default=1.0, help="feed audio this much faster (max useful 1.5); timestamps stay true")
    r = sp.add_parser("frames"); r.add_argument("video"); r.add_argument("-o", "--out")
    r.add_argument("--mode", choices=["scene", "uniform"], default="scene"); r.add_argument("--max", type=int, default=48)
    r.add_argument("--threshold", type=float, default=0.25); r.add_argument("--start"); r.add_argument("--end")
    r.add_argument("--width", type=int, default=640); r.add_argument("--at", default=""); r.add_argument("--cols", type=int, default=4)
    r.add_argument("--rows", type=int, default=3)
    n = sp.add_parser("normalize"); n.add_argument("input"); n.add_argument("-o", "--output")
    n.add_argument("--block-seconds", type=float, default=20); n.add_argument("--chunk-minutes", type=float, default=0)
    n.add_argument("--chapters", help="metadata.json with chapters")
    c = sp.add_parser("check"); c.add_argument("summary"); c.add_argument("transcript"); c.add_argument("--duration", type=float)
    mn = sp.add_parser("mentions"); mn.add_argument("transcript"); mn.add_argument("terms", help="terms.json {label: [stems]}")
    mn.add_argument("--window", type=float, default=60); mn.add_argument("--top", type=int, default=30)
    a = ap.parse_args()
    if a.cmd == "fetch": return cmd_fetch(a)
    if a.cmd == "transcribe": return cmd_transcribe(a)
    if a.cmd == "frames": return cmd_frames(a)
    if a.cmd == "check": return cmd_check(a)
    if a.cmd == "mentions": return cmd_mentions(a)
    chap = (json.load(open(a.chapters)) or {}).get("chapters") if a.chapters and os.path.exists(a.chapters) else None
    return out(normalize(a.input, a.output or os.path.join(os.path.dirname(os.path.abspath(a.input)), "transcript.txt"),
                         a.block_seconds, a.chunk_minutes, chap))

if __name__ == "__main__":
    sys.exit(main())
