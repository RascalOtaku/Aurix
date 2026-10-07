# Aurix Voice

Talk to Aurix from the GPU PC's mic and speakers. Say "Aurix", ask anything, hear the answer.

**Use `aurix_voice.py`**: 100% open source, no accounts.
- **Wake word:** openWakeWord.
- **Speech-to-text:** Faster-Whisper.
- **Text-to-speech:** Piper.

`aurix_voice_porcupine.py` is the older Porcupine version, which needs a Picovoice access key; it works the same way otherwise.

## How it works

1. **openWakeWord** listens for the wake word (your trained `aurix.onnx`, or the built-in "hey jarvis" for testing).
2. **Faster-Whisper** transcribes what you say until you pause.
3. The text goes to Aurix's chat API on the 7070 (`POST /api/chat`), into a chat called **Voice**.
4. **Piper** speaks the answer.

**Interrupting:** say the wake word while Aurix is talking and it stops mid-sentence and listens. It uses a stricter
threshold while speaking, so its own voice from the speakers does not cut itself off.

## Setup (once)

1. In the Aurix web UI, create an API token under **Settings → API tokens**. It starts with `ody_`.
2. Start a chat named **Voice**; voice turns land there. Or set `AURIX_SESSION` to a chat's id.
3. On the GPU PC:

```powershell
pip install -r voice/requirements.txt
$env:AURIX_URL = "http://<server-tailscale-ip>:7000"
$env:AURIX_API_KEY = "ody_..."
python voice/aurix_voice.py
```

To run it at login, create a shortcut with target `pythonw.exe C:\path\to\Aurix\voice\aurix_voice.py` and put it in
`shell:startup`.

## Settings (environment variables)

| Variable | Default | What |
|---|---|---|
| `AURIX_URL` | `http://<server-tailscale-ip>:7000` | Aurix on the 7070 (port 7000) |
| `AURIX_API_KEY` | | API token (`ody_...`) |
| `AURIX_SESSION` | | Chat id for voice turns; empty = the newest chat named `AURIX_SESSION_NAME` |
| `AURIX_SESSION_NAME` | `Voice` | Name of that chat |

## Training an "Aurix" wake word

- **openWakeWord's training pipeline:** about 50 two-second recordings of you saying "Aurix", then a few hours of
  training on the GPU. Save the result as `aurix.onnx` next to the script.
- **Faster option:** [ivnsell/custom-wake-word](https://github.com/ivnsell/custom-wake-word) trains from fewer samples.
- **License note:** openWakeWord's code is Apache-2.0, but its bundled pretrained wake words (like "hey jarvis") are for
  non-commercial use. A model you train yourself is yours.

## Troubleshooting

- **"I need a chat to talk in"**: start a chat named Voice in the web UI, or set `AURIX_SESSION`.
- **"Aurix refused the API token"**: make a new token in Settings → API tokens.
- **Aurix unreachable**: check that the 7070 is up and Tailscale is connected:
  `curl http://<server-tailscale-ip>:7000/api/health`.
- **Wake word not triggering**: speak clearly and check the mic level in Windows. `WAKE_THRESHOLD` at the top of the
  script trades misses for false triggers.
