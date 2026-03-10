# LughaBridge — Dev Log (March 3, 2026)

## How to Start the Stack

```bash
# Terminal 1 — ASGI server (handles HTTP + WebSockets)
cd Backend
source ../.venv/bin/activate
daphne -p 8000 lughabridge.asgi:application   # NOTE: colon, not dot

# Terminal 2 — Background task worker (ASR → Translation → TTS pipeline)
cd Backend
source ../.venv/bin/activate
python manage.py qcluster

# Terminal 3 — Frontend
cd Frontend
pnpm dev
```

> **DO NOT** use `python manage.py runserver` in production/WebSocket sessions.  
> It is HTTP-only and will return 404 for every `/ws/...` request.

---

## Issues Fixed Today

---

### 1. WebSocket 404 — `/ws/room/<code>/` Not Found

**Symptom:**
```
Not Found: /ws/room/AV7ZXC/
"GET /ws/room/AV7ZXC/ HTTP/1.1" 404 2369
```

**Root cause:**  
`python manage.py runserver` was running instead of Daphne. Django's dev server is HTTP-only — it cannot handle WebSocket upgrades and silently returns 404 for every `/ws/` path.

Additionally the Daphne command was wrong:
```bash
# WRONG (dot notation — breaks module lookup)
daphne -p 8000 lughabridge.asgi.application

# CORRECT (colon separates module from variable)
daphne -p 8000 lughabridge.asgi:application
```

Also: Daphne must be run **from inside the `Backend/` directory**, otherwise Python cannot find the `lughabridge` module (`ModuleNotFoundError`).

**Fix:** Kill whatever is on port 8000, then start Daphne correctly:
```bash
fuser -k 8000/tcp
cd Backend && daphne -p 8000 lughabridge.asgi:application
```

---

### 2. Translation not appearing until page refresh

**Symptom:** After speaking, the translated message only showed after manually refreshing the page.

**Root cause:** The translation pipeline runs as a background task via `django-q`. Without `qcluster` running, tasks sit in the queue forever — nothing processes them. On refresh, the frontend fetches message history from Redis (which is only written *after* a task completes), so nothing appeared live.

**Fix:** Always run `python manage.py qcluster` alongside Daphne.

---

### 3. Stop mic button not working on touch devices

**Symptom:** Tapping the stop button on mobile did nothing — recording kept going.

**Root cause 1 — Logic guard in `handleMicPress` (RoomChat.tsx):**
```tsx
// BUG: this guard fired when state === 'listening', blocking the stop branch entirely
if (systemState !== 'idle' && systemState !== 'completed') return;

if (isRecording) {
  stopRecording();   // ← UNREACHABLE when recording
}
```

**Root cause 2 — Touch events on mobile:**  
`onClick` has a ~300ms delay on touch screens and can be swallowed by scroll gestures.

**Fix:**
```tsx
// Check for active recording BEFORE the state guard
if (isRecording) {
  stopRecording();
  setSystemState('transcribing');
  return;
}
if (systemState !== 'idle' && systemState !== 'completed') return;
```
And in `MicButton.tsx` added `onTouchEnd` for instant mobile response:
```tsx
onTouchEnd={(e) => { e.preventDefault(); onPress(); }}
```

---

### 4. ASR (Speech Recognition) — `StopIteration` crash

**Symptom:**
```
StopIteration
huggingface_hub/inference/_providers/__init__.py, line 253
provider = next(iter(provider_mapping)).provider
```

**Root cause:**  
A breaking change in `huggingface_hub` — `InferenceClient.automatic_speech_recognition()` now tries to route calls through a provider registry (Together, Fal, etc.). Community fine-tuned models like `badrex/w2v-bert-2.0-kikuyu-asr` have **no provider mapping**, so the registry returns an empty set and `next(iter(empty_set))` explodes as `StopIteration`.

**First fix attempt:** Switched to direct `requests.post` to `router.huggingface.co` (same pattern the translator already used). Got `404` — model not registered on the router.

**Second fix attempt:** Tried classic `api-inference.huggingface.co` endpoint. Got `410 Gone` — HF removed free-tier serverless inference for community models permanently.

**Final fix:** Replaced HF ASR entirely with **Groq Whisper API** (`whisper-large-v3`).

New class `GroqASR` added to `translation/services/groq_translator.py`:
- Endpoint: `https://api.groq.com/openai/v1/audio/transcriptions`
- Model: `whisper-large-v3` (multilingual, handles Kikuyu via auto-detect)
- Free tier: 7,200 seconds of audio/day

Factory wired in `translation/services/factory.py`: when `USE_HF_INFERENCE=True`, `get_asr_service()` now returns `GroqASR` instead of dead `HFInferenceASR`.

---

### 5. TTS (Text-to-Speech) — Same `StopIteration` crash

**Symptom:**
```
StopIteration
huggingface_hub/inference/_client.py, line 2857
provider_helper = get_provider_helper(self.provider, task="text-to-speech", ...)
```

**Root cause:** Identical to issue #4 — `InferenceClient.text_to_speech()` has the same broken provider routing. MMS-TTS models (`facebook/mms-tts-kik`, `facebook/mms-tts-eng`, etc.) also return 410 on both HF endpoints.

**Fix — two parts:**

**Part A — non-fatal TTS in the pipeline** (`translation/tasks.py`):  
Wrapped the TTS step in `try/except` so a TTS failure no longer crashes the entire task. Text translation is still broadcast to the room:
```python
try:
    audio_path = tts_service.synthesize(translation['text'], target_lang)
    audio_base64 = base64.b64encode(open(audio_path, 'rb').read()).decode()
except Exception as tts_err:
    logger.warning(f"TTS failed (text translation will still be delivered): {tts_err}")
    audio_base64 = None   # frontend handles null gracefully
```

**Part B — browser TTS fallback** (`Frontend/src/hooks/useBrowserTTS.ts`):  
New hook added. When a `translation_complete` WebSocket message arrives:
1. If `audio_data` is present → decode base64 and play the server-generated audio
2. If `audio_data` is `null` → use `window.speechSynthesis` (browser built-in, free, no API needed)

Language mapping for browser TTS:
| Language | BCP-47 code |
|---|---|
| English | `en-US` |
| Swahili | `sw` (Chrome/Edge) |
| Kikuyu | `en` (no Kikuyu voice exists in any browser) |

Hook wired into `RoomChat.tsx` `translation_complete` handler.

---

## Current Working Pipeline (USE_HF_INFERENCE=True)

```
Microphone → audio/webm (base64)
    ↓
[qcluster task: translation.tasks.process_voice_message]
    ↓
ASR:         Groq Whisper API (whisper-large-v3)          ← was broken HF
    ↓
Translation: HybridTranslator (Groq LLaMA + HF NLLB)     ← unchanged
    ↓
TTS:         HF MMS-TTS via requests (410 fallback: null) ← non-fatal now
    ↓
WebSocket broadcast → chat_message → React state update → useBrowserTTS
    ↓
Audio playback: server audio OR window.speechSynthesis fallback
```

---

## Files Changed Today

| File | What changed |
|---|---|
| `Backend/lughabridge/asgi.py` | Was already correct — issue was how Daphne was invoked |
| `Backend/translation/services/hf_inference_services.py` | ASR: dropped broken `InferenceClient`, use `requests` directly. TTS: same fix + router/classic fallback |
| `Backend/translation/services/groq_translator.py` | Added `GroqASR` class using Groq Whisper API |
| `Backend/translation/services/factory.py` | `get_asr_service()` now returns `GroqASR` in HF mode; added `_groq_asr_service` singleton |
| `Backend/translation/tasks.py` | TTS step wrapped in try/except — failure is non-fatal |
| `Frontend/src/pages/RoomChat.tsx` | Fixed `handleMicPress` stop logic; wired `useBrowserTTS` on `translation_complete` |
| `Frontend/src/components/lugha/MicButton.tsx` | Added `onTouchEnd` for mobile stop reliability |
| `Frontend/src/hooks/useBrowserTTS.ts` | New hook — plays server audio or falls back to `speechSynthesis` |

---

## Environment Variables Required

```env
# Backend/.env
SECRET_KEY=...
DEBUG=True
DEMO_MODE=False
USE_HF_INFERENCE=True

HF_TOKEN=hf_...              # Still needed for HF translation (NLLB router)
GROQ_API_KEY=gsk_...         # Used for both ASR (Whisper) and translation (LLaMA)

REDIS_URL=redis://localhost:6379/0
ALLOWED_HOSTS=localhost,127.0.0.1
FRONTEND_URL=http://localhost:5173
```
