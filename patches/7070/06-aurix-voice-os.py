"""
Aurix Voice Assistant — 100% Open Source. Say "Aurix" and ask anything.

Stack (all open source, all local, no accounts, no cloud):
- Wake word: openWakeWord (https://github.com/dscripka/openWakeWord)
- Speech-to-text: Faster-Whisper (GPU-accelerated)
- Text-to-speech: Piper (local neural voices)
- Brain: Aurix API on the 7070

Setup:
1. pip install openwakeword faster-whisper piper-tts requests pyaudio numpy
2. Train "Aurix" wake word OR use built-in "hey_jarvis" for testing
   - Custom training: https://github.com/dscripka/openWakeWord#training-new-models
   - Save as aurix.onnx in the same directory as this script
3. In the Aurix web UI: Settings -> API tokens -> create one; start a chat named "Voice" (replies land there)
4. Set AURIX_URL=http://<server-tailscale-ip>:7000 and AURIX_API_KEY=ody_... (environment variables), then
   python aurix_voice.py

While Aurix is talking, say the wake word again to interrupt it: it stops mid-sentence and listens (barge-in).

To train a custom "Aurix" model:
- Record ~50 samples of you saying "Aurix" (2 sec each)
- Use openWakeWord's training pipeline (takes 4-8 hours on GPU)
- Or use https://github.com/ivnsell/custom-wake-word for faster training from fewer samples
"""
import os
import sys
import time
import wave
import requests
import numpy as np

# Configuration
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CUSTOM_WAKE_MODEL = os.path.join(SCRIPT_DIR, "aurix.onnx")
AURIX_URL = os.environ.get("AURIX_URL", "http://<server-tailscale-ip>:7000").rstrip("/")   # the 7070's Aurix (port 7000)
AURIX_API_URL = os.environ.get("AURIX_API_URL", AURIX_URL + "/api/chat")
AURIX_API_KEY = os.environ.get("AURIX_API_KEY", "")         # an Aurix API token (ody_...), Settings -> API tokens
AURIX_SESSION = os.environ.get("AURIX_SESSION", "")         # a chat session id; or leave empty and name a chat "Voice"
AURIX_SESSION_NAME = os.environ.get("AURIX_SESSION_NAME", "Voice")

# Audio settings
SAMPLE_RATE = 16000
CHANNELS = 1
CHUNK_SIZE = 1280  # openWakeWord uses 1280 samples (80ms at 16kHz)
SILENCE_THRESHOLD = 500
SILENCE_DURATION = 1.5
MAX_RECORD_SECONDS = 30
WAKE_THRESHOLD = 0.5  # openWakeWord detection threshold (0.0-1.0)
BARGE_IN_THRESHOLD = 0.7  # stricter while Aurix is talking, so its own voice from the speakers does not interrupt it


def rms(pcm_bytes):
    """Loudness of 16-bit mono PCM (audioop.rms replacement: audioop is gone in Python 3.13)."""
    samples = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float64)
    return float(np.sqrt(np.mean(samples * samples))) if samples.size else 0.0


def pick_session(sessions, name):
    """The id of the newest chat called `name` (case-insensitive) from GET /api/sessions, else None."""
    hits = [s for s in sessions or [] if str(s.get("name", "")).strip().lower() == name.strip().lower()]
    hits.sort(key=lambda s: str(s.get("last_message_at") or s.get("updated_at") or ""), reverse=True)
    return hits[0]["id"] if hits else None


class AurixVoice:
    def __init__(self):
        self.oww = None
        self.whisper_model = None
        self.piper_voice = None
        self.running = False
        self._init_wake_word()
        self._init_whisper()
        self._init_tts()

    def _init_wake_word(self):
        """Initialize openWakeWord."""
        from openwakeword.model import Model
        if os.path.exists(CUSTOM_WAKE_MODEL):
            print(f"Loading custom wake word from {CUSTOM_WAKE_MODEL}")
            self.oww = Model(wakeword_models=[CUSTOM_WAKE_MODEL])
            self.wake_word_name = "aurix"
        else:
            print("Custom 'aurix.onnx' not found. Using built-in 'hey_jarvis' for testing.")
            print("Train 'Aurix' with openWakeWord's training pipeline when ready.")
            # Download default models on first run
            self.oww = Model(wakeword_models=["hey_jarvis"])
            self.wake_word_name = "hey_jarvis"
        print(f"Wake word: say '{self.wake_word_name}' to activate")

    def _init_whisper(self):
        """Initialize Faster-Whisper (GPU)."""
        from faster_whisper import WhisperModel
        print("Loading Whisper model...")
        self.whisper_model = WhisperModel("small", device="cuda", compute_type="float16")
        print("Whisper ready (GPU)")

    def _init_tts(self):
        """Initialize Piper TTS."""
        from piper import PiperVoice
        # Download a good English voice on first run
        # en_US-lessac-medium is a solid default (~63MB)
        voice_name = "en_US-lessac-medium"
        print(f"Loading Piper voice '{voice_name}' (downloads on first run)...")
        self.piper_voice = PiperVoice.load(voice_name)
        print("Piper TTS ready")

    def speak(self, text, interruptible=True):
        """Speak text via Piper TTS. Returns True if the owner interrupted it with the wake word (barge-in)."""
        print(f"Aurix: {text}")
        import io
        import pyaudio
        wav_io = io.BytesIO()
        with wave.open(wav_io, "wb") as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(22050)
            self.piper_voice.synthesize(text, wav_file)
        wav_io.seek(0)
        pa = pyaudio.PyAudio()
        mic = None
        if interruptible:
            mic = pa.open(rate=SAMPLE_RATE, channels=CHANNELS, format=pyaudio.paInt16, input=True, frames_per_buffer=CHUNK_SIZE)
            self.oww.reset()
        interrupted = False
        with wave.open(wav_io, "rb") as wf:
            out = pa.open(format=pa.get_format_from_width(wf.getsampwidth()), channels=wf.getnchannels(),
                          rate=wf.getframerate(), output=True)
            data = wf.readframes(1024)
            while data and not interrupted:
                out.write(data)
                while mic is not None and mic.get_read_available() >= CHUNK_SIZE:
                    pcm = np.frombuffer(mic.read(CHUNK_SIZE, exception_on_overflow=False), dtype=np.int16)
                    if any(score >= BARGE_IN_THRESHOLD for score in self.oww.predict(pcm).values()):
                        print("✋ Interrupted")
                        interrupted = True
                        break
                data = wf.readframes(1024)
            out.stop_stream(); out.close()
        if mic is not None:
            mic.stop_stream(); mic.close()
        pa.terminate()
        self.oww.reset()
        return interrupted

    def listen_for_wake_word(self):
        """Block until wake word detected via openWakeWord."""
        import pyaudio
        pa = pyaudio.PyAudio()
        stream = pa.open(
            rate=SAMPLE_RATE, channels=CHANNELS,
            format=pyaudio.paInt16, input=True,
            frames_per_buffer=CHUNK_SIZE,
        )
        print(f"\n🎤 Listening... (say '{self.wake_word_name}')")
        try:
            while self.running:
                pcm = stream.read(CHUNK_SIZE, exception_on_overflow=False)
                pcm = np.frombuffer(pcm, dtype=np.int16)
                prediction = self.oww.predict(pcm)
                for model_name, score in prediction.items():
                    if score >= WAKE_THRESHOLD:
                        print(f"✨ Wake word detected! ({model_name}: {score:.2f})")
                        stream.stop_stream(); stream.close(); pa.terminate()
                        return True
        except Exception as e:
            print(f"Wake word error: {e}")
        finally:
            try: stream.stop_stream(); stream.close()
            except: pass
            pa.terminate()
        return False

    def record_until_silence(self):
        """Record until silence or max duration."""
        import pyaudio
        pa = pyaudio.PyAudio()
        stream = pa.open(
            rate=SAMPLE_RATE, channels=CHANNELS,
            format=pyaudio.paInt16, input=True,
            frames_per_buffer=CHUNK_SIZE,
        )
        print("🎙️ Recording... (speak now)")
        frames = []
        silent_chunks = 0
        silence_limit = int(SILENCE_DURATION * SAMPLE_RATE / CHUNK_SIZE)
        max_chunks = int(MAX_RECORD_SECONDS * SAMPLE_RATE / CHUNK_SIZE)
        for _ in range(max_chunks):
            data = stream.read(CHUNK_SIZE, exception_on_overflow=False)
            frames.append(data)
            if rms(data) < SILENCE_THRESHOLD:
                silent_chunks += 1
                if silent_chunks > silence_limit and len(frames) > 10:
                    break
            else:
                silent_chunks = 0
        stream.stop_stream(); stream.close(); pa.terminate()
        print(f"Recorded {len(frames) * CHUNK_SIZE / SAMPLE_RATE:.1f}s")
        return b"".join(frames)

    def transcribe(self, audio_data):
        """Transcribe with Whisper."""
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            with wave.open(f.name, "wb") as wf:
                wf.setnchannels(CHANNELS); wf.setsampwidth(2); wf.setframerate(SAMPLE_RATE)
                wf.writeframes(audio_data)
            segments, _ = self.whisper_model.transcribe(f.name, language="en")
            text = " ".join(s.text for s in segments).strip()
        os.unlink(f.name)
        return text

    def _session(self, headers):
        """The chat that voice turns go to: AURIX_SESSION, else the newest chat named AURIX_SESSION_NAME ("Voice")."""
        if AURIX_SESSION:
            return AURIX_SESSION
        if getattr(self, "_session_id", None):
            return self._session_id
        resp = requests.get(AURIX_URL + "/api/sessions", headers=headers, timeout=15)
        resp.raise_for_status()
        self._session_id = pick_session(resp.json(), AURIX_SESSION_NAME)
        return self._session_id

    def ask_aurix(self, text):
        """Send to Aurix's chat API (POST /api/chat needs a session id; the reply is {"response": ...})."""
        headers = {"Authorization": f"Bearer {AURIX_API_KEY}"} if AURIX_API_KEY else {}
        try:
            session = self._session(headers)
            if not session:
                return f"I need a chat to talk in. Open Aurix and start a chat named {AURIX_SESSION_NAME}."
            resp = requests.post(AURIX_API_URL, json={"message": text, "session": session}, headers=headers, timeout=180)
            if resp.status_code == 401:
                return "Aurix refused the API token. Make a new one in Settings, API tokens."
            resp.raise_for_status()
            return resp.json().get("response") or "I got an empty answer."
        except Exception as e:
            print(f"Aurix API error: {e}")
            return "Sorry, I couldn't reach the Aurix brain right now."

    def run(self):
        self.running = True
        self.speak("Aurix voice assistant ready. All open source.", interruptible=False)
        while self.running:
            try:
                if not getattr(self, "_barged_in", False) and not self.listen_for_wake_word():
                    break
                self._barged_in = False
                self.speak("Yes?", interruptible=False)
                audio = self.record_until_silence()
                if not audio or len(audio) < 1000:
                    continue
                text = self.transcribe(audio)
                if not text:
                    self.speak("I didn't catch that.")
                    continue
                print(f"You: {text}")
                self._barged_in = self.speak(self.ask_aurix(text))     # wake word mid-answer: skip straight to listening
            except KeyboardInterrupt:
                print("\nShutting down..."); break
            except Exception as e:
                print(f"Error: {e}"); time.sleep(1)
        self.running = False


if __name__ == "__main__":
    AurixVoice().run()
