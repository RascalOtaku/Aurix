#!/usr/bin/env python3
"""transcribe.py - audio/video -> subtitles + transcript, offline (faster-whisper + ffmpeg).

    python3 transcribe.py --input talk.mp4 --outdir out/ [--model base] [--language en]
                          [--formats srt,vtt,txt,json] [--glossary terms.txt]
    python3 transcribe.py --selftest

Pipeline: ffprobe (validate) -> ffmpeg (16 kHz mono WAV) -> faster-whisper (CPU, int8) ->
post-process (glossary, merge tiny segments) -> write files + a QA report that FLAGS
low-confidence spans for a human spot-check.

A glossary file has one `wrong => right` per line (names, jargon).
Only process media you own or have written permission to transcribe.
The model directory is baked into the image (/models); the sandbox has no internet.
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

MODEL_DIR = os.environ.get("WHISPER_MODEL_DIR", "/models")
LOW_LOGPROB = -1.0          # segments below this average log-prob are flagged
HIGH_NO_SPEECH = 0.6


# ---------------------------------------------------------------------------
# pure helpers (unit-tested)
# ---------------------------------------------------------------------------

def _ts(seconds, sep):
    seconds = max(0.0, float(seconds))
    ms = int(round(seconds * 1000))
    h, ms = divmod(ms, 3600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}{sep}{ms:03d}"


def fmt_srt_ts(seconds):
    return _ts(seconds, ",")


def fmt_vtt_ts(seconds):
    return _ts(seconds, ".")


def parse_glossary(text):
    """`wrong => right` lines -> list of (compiled regex, replacement); '#' comments ignored."""
    rules = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=>" not in line:
            continue
        wrong, right = (p.strip() for p in line.split("=>", 1))
        if wrong:
            # (?<!\w)/(?!\w) instead of \b: \b never matches next to punctuation ("C++", "(old)")
            rules.append((re.compile(r"(?<!\w)" + re.escape(wrong) + r"(?!\w)", re.IGNORECASE), right.replace("\\", "\\\\")))
    return rules


def apply_glossary(text, rules):
    for rx, right in rules:
        text = rx.sub(right, text)
    return text


def merge_short_segments(segments, min_chars=12, max_gap=0.6, max_chars=84):
    """Merge tiny fragments into their neighbour so subtitles are readable."""
    out = []
    for seg in segments:
        seg = dict(seg)
        if out:
            prev = out[-1]
            gap = seg["start"] - prev["end"]
            if gap <= max_gap and (len(prev["text"]) < min_chars or len(seg["text"]) < min_chars) \
                    and len(prev["text"]) + 1 + len(seg["text"]) <= max_chars:
                prev["text"] = (prev["text"] + " " + seg["text"]).strip()
                prev["end"] = seg["end"]
                prev["avg_logprob"] = min(prev.get("avg_logprob", 0.0), seg.get("avg_logprob", 0.0))
                prev["no_speech_prob"] = max(prev.get("no_speech_prob", 0.0), seg.get("no_speech_prob", 0.0))
                continue
        out.append(seg)
    return out


def flag_low_confidence(segments):
    flags = []
    for i, s in enumerate(segments, 1):
        reasons = []
        if s.get("avg_logprob", 0.0) < LOW_LOGPROB:
            reasons.append(f"low confidence ({s['avg_logprob']:.2f})")
        if s.get("no_speech_prob", 0.0) > HIGH_NO_SPEECH:
            reasons.append(f"probably not speech ({s['no_speech_prob']:.2f})")
        if reasons:
            flags.append({"index": i, "start": round(s["start"], 2), "end": round(s["end"], 2),
                          "text": s["text"][:80], "reasons": reasons})
    return flags


def to_srt(segments):
    return "".join(f"{i}\n{fmt_srt_ts(s['start'])} --> {fmt_srt_ts(s['end'])}\n{s['text'].strip()}\n\n"
                   for i, s in enumerate(segments, 1))


def to_vtt(segments):
    return "WEBVTT\n\n" + "".join(
        f"{fmt_vtt_ts(s['start'])} --> {fmt_vtt_ts(s['end'])}\n{s['text'].strip()}\n\n" for s in segments)


def to_txt(segments, gap_para=2.0):
    """Plain transcript; a pause longer than gap_para starts a new paragraph."""
    paras, cur, last_end = [], [], None
    for s in segments:
        if last_end is not None and s["start"] - last_end > gap_para and cur:
            paras.append(" ".join(cur))
            cur = []
        cur.append(s["text"].strip())
        last_end = s["end"]
    if cur:
        paras.append(" ".join(cur))
    return "\n\n".join(paras) + "\n"


WRITERS = {"srt": to_srt, "vtt": to_vtt, "txt": to_txt}


def build_report(src, duration_s, elapsed_s, model, language, segments, flags):
    rtf = round(elapsed_s / duration_s, 3) if duration_s else None
    return {"source": os.path.basename(src), "duration_s": round(duration_s, 1),
            "processing_s": round(elapsed_s, 1), "realtime_factor": rtf, "model": model,
            "language": language, "segments": len(segments), "flagged": len(flags),
            "flagged_spans": flags[:50],
            "needs_spot_check": bool(flags) or not segments}


# ---------------------------------------------------------------------------
# media + model (run inside the sandbox image)
# ---------------------------------------------------------------------------

def probe_duration(path):
    exe = shutil.which("ffprobe")
    if not exe:
        raise RuntimeError("ffprobe not found (is this the sandbox image?)")
    r = subprocess.run([exe, "-v", "error", "-show_entries", "format=duration", "-of", "json", path],
                       capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise RuntimeError(f"not readable media: {r.stderr.strip()[:200]}")
    dur = float(json.loads(r.stdout)["format"]["duration"])
    if dur <= 0:
        raise RuntimeError("media has zero duration")
    return dur


def to_wav16k(src, dst):
    r = subprocess.run(["ffmpeg", "-y", "-i", src, "-vn", "-ac", "1", "-ar", "16000", "-f", "wav", dst],
                       capture_output=True, text=True, timeout=3600)
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {r.stderr.strip()[-300:]}")


def run_model(wav, model_name, language):
    from faster_whisper import WhisperModel      # lazy: only present in the sandbox image
    model = WhisperModel(model_name, device="cpu", compute_type="int8", download_root=MODEL_DIR)
    seg_iter, info = model.transcribe(wav, language=None if language in (None, "", "auto") else language,
                                      vad_filter=True, beam_size=5)
    segments = [{"start": s.start, "end": s.end, "text": s.text.strip(),
                 "avg_logprob": s.avg_logprob, "no_speech_prob": s.no_speech_prob} for s in seg_iter]
    return segments, info.language


def transcribe(src, outdir, model_name="base", language="auto", formats=("srt", "vtt", "txt", "json"),
               glossary_text=""):
    os.makedirs(outdir, exist_ok=True)
    duration = probe_duration(src)
    started = time.time()
    with tempfile.TemporaryDirectory() as tmp:
        wav = os.path.join(tmp, "audio.wav")
        to_wav16k(src, wav)
        segments, lang = run_model(wav, model_name, language)
    rules = parse_glossary(glossary_text)
    for s in segments:
        s["text"] = apply_glossary(s["text"], rules)
    segments = merge_short_segments(segments)
    flags = flag_low_confidence(segments)
    base = os.path.splitext(os.path.basename(src))[0]
    written = []
    for fmt in formats:
        if fmt in WRITERS:
            path = os.path.join(outdir, f"{base}.{fmt}")
            open(path, "w", encoding="utf-8").write(WRITERS[fmt](segments))
            written.append(path)
    report = build_report(src, duration, time.time() - started, model_name, lang, segments, flags)
    if "json" in formats:
        path = os.path.join(outdir, f"{base}.report.json")
        json.dump({"report": report, "segments": segments}, open(path, "w", encoding="utf-8"), indent=2)
        written.append(path)
    return report, written


def _selftest():
    segs = [{"start": 0.0, "end": 1.2, "text": "Hello", "avg_logprob": -0.2, "no_speech_prob": 0.01},
            {"start": 1.3, "end": 3.0, "text": "world of whispr", "avg_logprob": -1.4, "no_speech_prob": 0.02},
            {"start": 9.0, "end": 10.0, "text": "A new paragraph.", "avg_logprob": -0.3, "no_speech_prob": 0.9}]
    assert fmt_srt_ts(3661.5) == "01:01:01,500" and fmt_vtt_ts(0.0004) == "00:00:00.000"
    merged = merge_short_segments(segs)
    assert len(merged) == 2 and merged[0]["text"] == "Hello world of whispr"
    rules = parse_glossary("whispr => Whisper\n# comment\n")
    assert apply_glossary("about whispr!", rules) == "about Whisper!"
    assert len(flag_low_confidence(merged)) == 2
    assert to_srt(merged).startswith("1\n00:00:00,000 --> 00:00:03,000\n")
    assert to_vtt(merged).startswith("WEBVTT")
    assert to_txt(merged).count("\n\n") == 1
    for tool in ("ffmpeg", "ffprobe"):
        assert shutil.which(tool), f"{tool} missing from this image"
    try:
        import faster_whisper  # noqa: F401
    except ImportError:
        raise AssertionError("faster_whisper missing from this image")
    print("transcribe selftest OK (formatting + tools present; run a real file to test the model)")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--input")
    ap.add_argument("--outdir", default=".")
    ap.add_argument("--model", default="base")
    ap.add_argument("--language", default="auto")
    ap.add_argument("--formats", default="srt,vtt,txt,json")
    ap.add_argument("--glossary", help="file of `wrong => right` lines")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)
    if a.selftest:
        return _selftest()
    if not a.input:
        ap.error("--input is required")
    glossary = open(a.glossary, encoding="utf-8").read() if a.glossary else ""
    report, written = transcribe(a.input, a.outdir, a.model, a.language,
                                 tuple(f.strip() for f in a.formats.split(",")), glossary)
    print(json.dumps(report, indent=2))
    for p in written:
        print("wrote", p)
    return 0


if __name__ == "__main__":
    sys.exit(main())
