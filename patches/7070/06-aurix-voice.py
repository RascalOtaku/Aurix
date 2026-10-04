"""
Aurix Voice Assistant — Say "Aurix" and ask anything.

Pipeline:
1. Porcupine wake word detection (custom "Aurix" model, or built-in fallback)
2. Record audio until silence
3. Faster-Whisper transcription (GPU-accelerated on the GPU PC)
4. Send to Aurix API on the 7070
5. Speak response via Windows TTS

Setup:
- Train "Aurix" wake word at https://console.picovoice.ai/ (free)
- Save as aurix.ppn in the same directory as this script
- Set PORCUPINE_ACCESS_KEY environment variable (free from Picovoice console)

Usage:
    python aurix_voice.py
"""
import os
import sys
import time
import wave
import audioop
import threading
import requests
import numpy as np

# Configuration
PORCUPINE_ACCESS_KEY = os.environ.get("PORCUPINE_ACCESS_KEY", "")
CUSTOM_WAKE_WORD_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "aurix.ppn")
AURIX_API_URL = os.environ.get("AURIX_API_URL", "http://<server-tailscale-ip>:8000/api/chat")  # the 7070's chat endpoint
AURIX_API_KEY = os.environ.get("AURIX_API_KEY", "")

# Audio settings
SAMPLE_RATE = 16000
CHANNELS = 1
CHUNK_SIZE = 512
SILENCE_THRESHOLD = 500  # RMS threshold for silence detection
SILENCE_DURATION = 1.5   # seconds of silence to stop recording
MAX_RECORD_SECONDS = 30


class AurixVoice:
    def __init__(self):
        self.porcupine = None
        self.whisper_model = None
        self.tts_engine = None
        self.running = False
        self._init_wake_word()
        self._init_whisper()
        self._init_tts()

    def _init_wake_word(self):
        """Initialize Porcupine wake word detection."""
        import pvporcupine
        if not PORCUPINE_ACCESS_KEY:
            print("WARNING: PORCUPINE_ACCESS_KEY not set. Get one free at https://console.picovoice.ai/")
            print("Falling back to built-in 'porcupine' wake word for testing.")
            self.porcupine = pvporcupine.create(
                access_key="",
                keywords=["porcupine"],
            )
            print("Say 'porcupine' to activate (replace with custom 'aurix.ppn' when trained)")
        elif os.path.exists(CUSTOM_WAKE_WORD_PATH):
            self.porcupine = pvporcupine.create(
                access_key=PORCUPINE_ACCESS_KEY,
                keyword_paths=[CUSTOM_WAKE_WORD_PATH],
            )
            print("Wake word 'Aurix' loaded from aurix.ppn")
        else:
            print(f"Custom wake word not found at {CUSTOM_WAKE_WORD_PATH}")
            print("Train 'Aurix' at https://console.picovoice.ai/ and save as aurix.ppn")
            print("Falling back to built-in 'jarvis' for now.")
            self.porcupine = pvporcupine.create(
                access_key=PORCUPINE_ACCESS_KEY,
                keywords=["jarvis"],
            )

    def _init_whisper(self):
        """Initialize Faster-Whisper for transcription (GPU accelerated)."""
        from faster_whisper import WhisperModel
        print("Loading Whisper model (this takes a moment)...")
        # Use small model for speed; base for accuracy. GPU via CUDA.
        self.whisper_model = WhisperModel("small", device="cuda", compute_type="float16")
        print("Whisper ready (GPU)")

    def _init_tts(self):
        """Initialize Windows TTS."""
        import pyttsx3
        self.tts_engine = pyttsx3.init()
        # Slightly slower, more natural rate
        self.tts_engine.setProperty("rate", 175)
        print("TTS ready")

    def speak(self, text):
        """Speak text via Windows TTS."""
        print(f"Aurix: {text}")
        self.tts_engine.say(text)
        self.tts_engine.runAndWait()

    def listen_for_wake_word(self):
        """Block until wake word is detected."""
        import pyaudio
        pa = pyaudio.PyAudio()
        stream = pa.open(
            rate=self.porcupine.sample_rate,
            channels=1,
            format=pyaudio.paInt16,
            input=True,
            frames_per_buffer=self.porcupine.frame_length,
        )
        print("\n🎤 Listening for wake word... (say 'Aurix')")
        try:
            while self.running:
                pcm = stream.read(self.porcupine.frame_length, exception_on_overflow=False)
                pcm = np.frombuffer(pcm, dtype=np.int16)
                result = self.porcupine.process(pcm)
                if result >= 0:
                    print("✨ Wake word detected!")
                    stream.stop_stream()
                    stream.close()
                    pa.terminate()
                    return True
        except Exception as e:
            print(f"Wake word error: {e}")
        finally:
            try:
                stream.stop_stream(); stream.close()
            except: pass
            pa.terminate()
        return False

    def record_until_silence(self):
        """Record audio until silence or max duration."""
        import pyaudio
        pa = pyaudio.PyAudio()
        stream = pa.open(
            rate=SAMPLE_RATE, channels=CHANNELS,
            format=pyaudio.paInt16, input=True,
            frames_per_buffer=CHUNK_SIZE,
        )
        print("🎙️ Recording... (speak now, pause when done)")
        frames = []
        silent_chunks = 0
        silence_limit = int(SILENCE_DURATION * SAMPLE_RATE / CHUNK_SIZE)
        max_chunks = int(MAX_RECORD_SECONDS * SAMPLE_RATE / CHUNK_SIZE)

        for _ in range(max_chunks):
            data = stream.read(CHUNK_SIZE, exception_on_overflow=False)
            frames.append(data)
            # Check volume
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
        """Transcribe audio using Whisper."""
        # Save to temp WAV for Whisper
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            with wave.open(f.name, "wb") as wf:
                wf.setnchannels(CHANNELS)
                wf.setsampwidth(2)
                wf.setframerate(SAMPLE_RATE)
                wf.writeframes(audio_data)
            segments, _ = self.whisper_model.transcribe(f.name, language="en")
            text = " ".join(seg.text for seg in segments).strip()
        os.unlink(f.name)
        return text

    def ask_aurix(self, text):
        """Send text to Aurix API and get response."""
        headers = {}
        if AURIX_API_KEY:
            headers["Authorization"] = f"Bearer {AURIX_API_KEY}"
        try:
            resp = requests.post(
                AURIX_API_URL,
                json={"message": text, "stream": False},
                headers=headers,
                timeout=120,
            )
            resp.raise_for_status()
            data = resp.json()
            # Try common response formats
            return data.get("response") or data.get("reply") or data.get("text") or str(data)
        except Exception as e:
            print(f"Aurix API error: {e}")
            return "Sorry, I couldn't reach the Aurix brain right now."

    def run(self):
        """Main loop: wake → listen → transcribe → ask → speak."""
        self.running = True
        self.speak("Aurix voice assistant ready.")
        while self.running:
            try:
                if not self.listen_for_wake_word():
                    break
                # Wake word detected — play activation chime (short beep via TTS)
                self.speak("Yes?")
                audio = self.record_until_silence()
                if not audio or len(audio) < 1000:
                    continue
                text = self.transcribe(audio)
                if not text:
                    self.speak("I didn't catch that.")
                    continue
                print(f"You: {text}")
                response = self.ask_aurix(text)
                self.speak(response)
            except KeyboardInterrupt:
                print("\nShutting down...")
                break
            except Exception as e:
                print(f"Error: {e}")
                time.sleep(1)
        self.running = False
        if self.porcupine:
            self.porcupine.delete()


if __name__ == "__main__":
    voice = AurixVoice()
    voice.run()
