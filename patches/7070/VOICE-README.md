# Aurix Voice Assistant — "Say Aurix"

Voice-activate Aurix from Steammachine's mic and speakers. Say "Aurix", ask anything.

## How it works

1. **Porcupine** listens for the wake word (custom "Aurix" model, or built-in fallback)
2. **Faster-Whisper** transcribes your speech (GPU-accelerated on the GTX 1660)
3. Text goes to **Aurix API** on the 7070
4. Response plays through **Windows TTS** on Steammachine's speakers

## Setup

### 1. Train the "Aurix" wake word (5 minutes, free)

1. Go to https://console.picovoice.ai/ (free account)
2. Go to Porcupine → Train custom wake word
3. Enter "Aurix" as the phrase
4. Download the `.ppn` file for Windows
5. Save as `aurix.ppn` in the same folder as `aurix_voice.py`
6. Copy your Access Key from the console

Without this step, it falls back to the built-in "jarvis" wake word for testing.

### 2. Set environment variables

```powershell
# In PowerShell (or set permanently via System Properties)
$env:PORCUPINE_ACCESS_KEY = "your-picovoice-access-key"
$env:AURIX_API_KEY = "your-aurix-api-key"  # if Aurix requires auth
```

### 3. Install dependencies

```powershell
pip install pvporcupine faster-whisper requests pyttsx3 PyAudio
```

### 4. Run it

```powershell
python aurix_voice.py
```

Say "Aurix" (or "jarvis" if using fallback), wait for "Yes?", then ask your question.

### 5. Run on startup (optional)

Create a scheduled task or place a shortcut in the Startup folder:
- Target: `pythonw.exe C:\path\to\aurix_voice.py`
- Use `pythonw.exe` (not `python.exe`) to run without a console window

## Configuration

Edit the top of `aurix_voice.py`:

| Variable | Default | Description |
|----------|---------|-------------|
| `AURIX_API_URL` | `http://100.112.82.10:8000/api/chat` | 7070's chat endpoint (Tailscale) |
| `SILENCE_THRESHOLD` | `500` | RMS level for silence detection (lower = more sensitive) |
| `SILENCE_DURATION` | `1.5` | Seconds of silence before stopping recording |
| `MAX_RECORD_SECONDS` | `30` | Max recording length |

## Troubleshooting

- **"No mic found"**: Check Windows Sound Settings → Input. The Realtek mic must be enabled.
- **Wake word not triggering**: Speak clearly. Porcupine needs ~1 second of audio. Check mic levels in Windows.
- **Whisper slow**: Should use GPU automatically. If it's on CPU, check CUDA is installed.
- **Aurix API unreachable**: Verify the 7070 is up and Tailscale is connected. Test with `curl http://100.112.82.10:8000/api/chat`.
