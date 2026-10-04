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
3. python aurix_voice.py

To train a custom "Aurix" model:
- Record ~50 samples of you saying "Aurix" (2 sec each)
- Use openWakeWord's training pipeline (takes 4-8 hours on GPU)
- Or use https://github.com/ivnsell/custom-wake-word for faster training from fewer samples
"""
import os
import sys
import time
import wave
import audioop
import requests
import numpy as np

# Configuration
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CUSTOM_WAKE_MODEL = os.path.join(SCRIPT_DIR, "aurix.onnx")
AURIX_API_URL = "http://100.112.82.10:8000/api/chat"  # 7070 Tailscale IP
AURIX_API_KEY = os.environ.get("AURIX_API_KEY", "")

# Audio settings
SAMPLE_RATE = 16000
CHANNELS = 1
CHUNK_SIZE = 1280  # openWakeWord uses 1280 samples (80ms at 16kHz)
SILENCE_THRESHOLD = 500
SILENCE_DURATION = 1.5
MAX_RECORD_SECONDS = 30
WAKE_THRESHOLD = 0.5  # openWakeWord detection threshold (0.0-1.0)


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

    def speak(self, text):
        """Speak text via Piper TTS."""
        print(f"Aurix: {text}")
        import io
        import pyaudio
        # Synthesize to WAV in memory
        wav_io = io.BytesIO()
        with wave.open(wav_io, "wb") as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(22050)
            self.piper_voice.synthesize(text, wav_file)
        # Play it
        wav_io.seek(0)
        with wave.open(wav_io, "rb") as wf:
            pa = pyaudio.PyAudio()
            stream = pa.open(
                format=pa.get_format_from_width(wf.getsampwidth()),
                channels=wf.getnchannels(),
                rate=wf.getframerate(),
                output=True,
            )
            data = wf.readframes(1024)
            while data:
                stream.write(data)
                data = wf.readframes(1024)
            stream.stop_stream(); stream.close(); pa.terminate()

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
            rms = audioop.rms(data, 2)
            if rms < SILENCE_THRESHOLD:
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

    def ask_aurix(self, text):
        """Send to Aurix API."""
        headers = {"Authorization": f"Bearer {AURIX_API_KEY}"} if AURIX_API_KEY else {}
        try:
            resp = requests.post(AURIX_API_URL, json={"message": text, "stream": False}, headers=headers, timeout=120)
            resp.raise_for_status()
            data = resp.json()
            return data.get("response") or data.get("reply") or data.get("text") or str(data)
        except Exception as e:
            print(f"Aurix API error: {e}")
            return "Sorry, I couldn't reach the Aurix brain right now."

    def run(self):
        self.running = True
        self.speak("Aurix voice assistant ready. All open source.")
        while self.running:
            try:
                if not self.listen_for_wake_word():
                    break
                self.speak("Yes?")
                audio = self.record_until_silence()
                if not audio or len(audio) < 1000:
                    continue
                text = self.transcribe(audio)
                if not text:
                    self.speak("I didn't catch that.")
                    continue
                print(f"You: {text}")
                self.speak(self.ask_aurix(text))
            except KeyboardInterrupt:
                print("\nShutting down..."); break
            except Exception as e:
                print(f"Error: {e}"); time.sleep(1)
        self.running = False


if __name__ == "__main__":
    AurixVoice().run()
