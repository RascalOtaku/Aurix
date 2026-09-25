"""The transcription pipeline: a real income workstream AURIX can finish end to end with a local, free Whisper model. No test needs
faster-whisper installed or touches the network - the model and the Telegram download are both faked."""
import json
import os
import sys
import tempfile
import time
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.foundation import audit, earnings, transcription as tr  # noqa: E402


class _FakeResp:
    def __init__(self, body: bytes, headers=None):
        self.body, self.headers = body, headers or {}

    def read(self, n=-1):
        return self.body if n < 0 else self.body[:n]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"AURIX_PROJECT_ROOT": self.tmp.name, "TELEGRAM_BOT_TOKEN": "123:abc"})
        self.env.start()
        audit._heads.clear()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()
        audit._heads.clear()

    def _mock_urlopen(self, get_file_ok=True, body=b"fake-audio-bytes", content_length=None):
        def fake(req_or_url, timeout=None):
            url = req_or_url if isinstance(req_or_url, str) else req_or_url.full_url
            if "getFile" in url:
                payload = {"ok": True, "result": {"file_path": "voice/file_1.oga"}} if get_file_ok else {"ok": False}
                return _FakeResp(json.dumps(payload).encode())
            headers = {"Content-Length": str(content_length)} if content_length is not None else {}
            return _FakeResp(body, headers)
        return fake


class DownloadTests(_Base):
    def test_downloads_and_saves_the_file(self):
        dest = Path(self.tmp.name) / "x.oga"
        with mock.patch.object(tr.urllib.request, "urlopen", side_effect=self._mock_urlopen()):
            err = tr._download("file123", dest)
        self.assertIsNone(err)
        self.assertEqual(dest.read_bytes(), b"fake-audio-bytes")

    def test_missing_token_is_a_clean_error(self):
        with mock.patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": ""}):
            err = tr._download("file123", Path(self.tmp.name) / "x.oga")
        self.assertIn("TELEGRAM_BOT_TOKEN", err)

    def test_expired_file_id_is_a_clean_error(self):
        with mock.patch.object(tr.urllib.request, "urlopen", side_effect=self._mock_urlopen(get_file_ok=False)):
            err = tr._download("gone", Path(self.tmp.name) / "x.oga")
        self.assertIn("could not locate", err.lower())

    def test_oversize_by_content_length_is_rejected_before_reading_the_body(self):
        with mock.patch.object(tr.urllib.request, "urlopen", side_effect=self._mock_urlopen(content_length=tr.MAX_BYTES + 1)):
            err = tr._download("file123", Path(self.tmp.name) / "x.oga")
        self.assertIn("MB", err)

    def test_oversize_by_actual_body_is_rejected_even_without_a_content_length_header(self):
        with mock.patch.object(tr.urllib.request, "urlopen", side_effect=self._mock_urlopen(body=b"x" * (tr.MAX_BYTES + 1))):
            err = tr._download("file123", Path(self.tmp.name) / "x.oga")
        self.assertIn("MB", err)

    def test_network_failure_is_a_clean_error_never_a_crash(self):
        def boom(req_or_url, timeout=None):
            url = req_or_url if isinstance(req_or_url, str) else req_or_url.full_url
            if "getFile" in url:                                    # the metadata lookup still succeeds; only the actual download fails
                return _FakeResp(json.dumps({"ok": True, "result": {"file_path": "voice/file_1.oga"}}).encode())
            raise urllib.error.URLError("down")
        with mock.patch.object(tr.urllib.request, "urlopen", side_effect=boom):
            err = tr._download("file123", Path(self.tmp.name) / "x.oga")
        self.assertIn("download failed", err)


def _fake_model(text="hello world this is a test"):
    seg = mock.Mock(text=text)
    info = mock.Mock(duration=12.0, language="en")

    def factory():
        m = mock.Mock()
        m.transcribe.return_value = ([seg], info)
        return m
    return factory


class RequestTests(_Base):
    def test_a_job_over_the_duration_cap_is_refused_before_any_download(self):
        with mock.patch.object(tr, "_download") as dl:
            msg = tr.request("f1", "long.mp3", duration_sec=tr.MAX_MINUTES * 60 + 1)
        self.assertIn("cap", msg)
        dl.assert_not_called()

    def test_a_failed_download_never_starts_a_job(self):
        with mock.patch.object(tr, "_download", return_value="no TELEGRAM_BOT_TOKEN configured on the server"):
            msg = tr.request("f1", "x.mp3")
        self.assertIn("Could not fetch", msg)
        self.assertEqual(tr.jobs(), [])

    def test_a_successful_job_transcribes_saves_text_and_notifies(self):
        notified, sent_files = [], []
        with mock.patch.object(tr, "_download", return_value=None):
            msg = tr.request("f1", "memo.oga", duration_sec=30, notify=notified.append,
                              send_file=lambda path, cap: sent_files.append((path, cap)),
                              model_factory=_fake_model())
        self.assertIn("Got it", msg)
        for _ in range(50):                                                     # the job runs in a background thread
            if tr.jobs() and tr.jobs()[0]["status"] != "running":
                break
            time.sleep(0.05)
        job = tr.jobs()[0]
        self.assertEqual(job["status"], "done")
        self.assertEqual(job["words"], 6)
        self.assertTrue(notified and "Transcript ready" in notified[0])
        self.assertEqual(len(sent_files), 1)
        self.assertTrue(Path(sent_files[0][0]).read_text(encoding="utf-8").startswith("hello world"))
        self.assertIn("transcription_done", [r["event"] for r in audit.recent(10)])
        self.assertEqual(earnings.by_source(), {"transcription": round((tr.RATE_PER_MIN[0] + tr.RATE_PER_MIN[1]) / 2 * 12.0 / 60, 2)})  # the fake model reports 12s

    def test_a_model_failure_becomes_a_failed_job_not_a_crash(self):
        def boom():
            raise RuntimeError("no model weights")
        notified = []
        with mock.patch.object(tr, "_download", return_value=None):
            tr.request("f1", "x.mp3", notify=notified.append, model_factory=boom)
        for _ in range(50):
            if tr.jobs() and tr.jobs()[0]["status"] != "running":
                break
            time.sleep(0.05)
        job = tr.jobs()[0]
        self.assertEqual(job["status"], "failed")
        self.assertIn("RuntimeError", job["error"])
        self.assertTrue(notified and "failed" in notified[0])
        self.assertIn("transcription_failed", [r["event"] for r in audit.recent(10)])

    def test_the_audio_file_is_deleted_after_the_job_whether_it_succeeds_or_fails(self):
        seen_path = {}

        def download(file_id, dest):
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"x")
            seen_path["p"] = dest
            return None
        with mock.patch.object(tr, "_download", side_effect=download):
            tr.request("f1", "x.mp3", model_factory=_fake_model())
        for _ in range(50):
            if tr.jobs() and tr.jobs()[0]["status"] != "running":
                break
            time.sleep(0.05)
        self.assertFalse(seen_path["p"].exists())


class RenderAndPanelTests(_Base):
    def test_rate_estimate_scales_with_minutes(self):
        self.assertIn("$5.00-15.00", tr.rate_estimate(10))
        self.assertIn("$0.50-1.50", tr.rate_estimate(1))

    def test_status_text_and_panel_are_never_empty(self):
        self.assertIn("No transcription jobs", tr.status_text())
        self.assertEqual(tr.panel()["jobs"], [])
        with mock.patch.object(tr, "_download", return_value=None):
            tr.request("f1", "memo.oga", model_factory=_fake_model())
        for _ in range(50):
            if tr.jobs() and tr.jobs()[0]["status"] != "running":
                break
            time.sleep(0.05)
        self.assertIn("memo.oga", tr.status_text())
        self.assertEqual(tr.panel()["jobs"][0]["status"], "done")

    def test_a_failed_job_renders_plainly(self):
        job = {"id": "t-abc123", "status": "failed", "error": "boom"}
        self.assertIn("failed", tr.render_result(job))
        self.assertIn("boom", tr.render_result(job))


if __name__ == "__main__":
    unittest.main()
