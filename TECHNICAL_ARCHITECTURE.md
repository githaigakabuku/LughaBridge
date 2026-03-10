# LughaBridge - TECHNICAL ARCHITECTURE & FILE CONNECTIONS

**Deep Dive Into System Design**

---

## 1. COMPLETE FILE DEPENDENCY MAP

### 1.1 Request Flow: Frontend → Backend → Database

```
USER INTERACTION:
├─ Landing Page (pages/Landing.tsx)
│  └─ Calls: services/api.ts → createRoom()
│             └─ HTTP POST /api/rooms/create/
│                └─ Returns: room_code, ws_url
│
├─ RoomChat Page (pages/RoomChat.tsx)
│  ├─ Initializes: services/websocket.ts → RoomWebSocket.connect()
│  │  └─ Connects to: /ws/room/{code}/
│  │     └─ Handled by: rooms/consumers.py → ChatConsumer
│  │
│  └─ Sends message: MicButton.tsx → captures audio → sends via websocket
│     └─ WebSocket message type: "voice_message"
│        └─ Handler: consumers.py → receive() → sends to channel layer
│           └─ Background task: translation/tasks.py → process_voice_message_task()
│              └─ Services used (from translation/services/):
│                 ├─ factory.py → selects based on DEMO_MODE/USE_HF_INFERENCE
│                 ├─ mock_services.py (DEMO_MODE=True)
│                 │  ├─ STT: Predefined transcriptions
│                 │  ├─ Translation: Hardcoded phrase pairs
│                 │  └─ TTS: Mock audio URLs
│                 │
│                 └─ hf_inference_services.py (USE_HF_INFERENCE=True)
│                    ├─ STT: HuggingFace Inference API
│                    ├─ Translation: NLLB-200 model
│                    └─ TTS: MMS TTS model
│
└─ Receives response: MessageBubble.tsx displays with ConfidenceRing.tsx


DATA PERSISTENCE:
├─ Message saved to: rooms/models.py → Message (Django ORM)
│  └─ Database: db.sqlite3 (dev) or PostgreSQL (production)
│     └─ Fields: sender, original_text, translated_text, confidence, timestamp
│
├─ Translation cache: translation/models.py → TranslationCache
│  └─ Stored for: 24-hour Redis TTL + permanent database backup
│     └─ Key format: trans:{src_lang}:{tgt_lang}:{text_hash}
│
└─ Audio files: AWS S3 (production) or Django storage (dev)
   └─ Path pattern: s3://bucket/rooms/{room_code}/{message_id}.mp3
```

---

## 2. BACKEND FILE STRUCTURE WITH FUNCTION MAPPING

### 2.1 Django App: `rooms/` (Chat Management)

```
rooms/
├── models.py
│   ├── Room
│   │   ├── room_code (unique identifier)
│   │   ├── source_lang / target_lang
│   │   ├── created_at / expires_at
│   │   ├── is_demo (flag for mock data)
│   │   └── save() → auto-generates room_code if not exists
│   │
│   └── Message
│       ├── room (FK to Room)
│       ├── sender (CharField: "A" or "B")
│       ├── original_text / translated_text
│       ├── stt_confidence / translation_confidence
│       ├── audio_url (S3 path)
│       ├── processing_status (pending/processing/completed/failed)
│       ├── created_at
│       └── __str__() → returns formatted message summary
│
├── views.py
│   ├── @api_view(['POST'])
│   │   └── create_room(request)
│   │       ├─ Validate source_lang, target_lang
│   │       ├─ Check language pair is not same language
│   │       ├─ Create Room object with generate_room_code()
│   │       ├─ Return: {room_code, source_lang, target_lang, ws_url, expiry_hours}
│   │       └─ Status: 201 Created (or 400 Bad Request on error)
│   │
│   ├── @api_view(['GET'])
│   │   └── join_room(request, room_code)
│   │       ├─ Lookup Room by room_code
│   │       ├─ Check if room is not expired
│   │       ├─ Return room metadata
│   │       └─ Status: 200 OK (or 404 Not Found)
│   │
│   ├── @api_view(['GET'])
│   │   └── room_messages(request, room_code)
│   │       ├─ Get all messages for room (ordered by created_at DESC)
│   │       ├─ Normalize to frontend format
│   │       └─ Return: {messages: [{id, sender, originalText, ...}]}
│   │
│   └── @api_view(['GET'])
│       └── health_check(request)
│           ├─ Check Django status
│           ├─ Check DEMO_MODE flag
│           └─ Return: {status, demo_mode, supported_languages}
│
├── consumers.py
│   └── ChatConsumer(AsyncWebsocketConsumer)
│       ├── connect()
│       │   ├─ room_code = scope['url_route']['kwargs']['room_code']
│       │   ├─ Verify room exists, not expired
│       │   ├─ channel_layer.group_add(f'room_{room_code}', channel_name)
│       │   ├─ await send({"type": "connection_established"})
│       │   └─ Log user joined
│       │
│       ├── disconnect(close_code)
│       │   ├─ channel_layer.group_discard(f'room_{room_code}', channel_name)
│       │   ├─ Broadcast "user_left" event
│       │   └─ Log user disconnected
│       │
│       ├── receive(text_data)
│       │   ├─ Parse JSON message
│       │   ├─ Route to handler based on message['type']:
│       │   │  ├─ 'voice_message' → handle_voice_message()
│       │   │  ├─ 'text_message' → handle_text_message()
│       │   │  ├─ 'read_aloud' → handle_read_aloud()
│       │   │  └─ 'get_history' → handle_get_history()
│       │   └─ Send processing status to client
│       │
│       ├── handle_voice_message(message_data)
│       │   ├─ Extract: audio_data (base64), language
│       │   ├─ Create Message object (status="processing")
│       │   ├─ Queue background task: process_voice_message_task()
│       │   │  └─ tasks.py (see below)
│       │   ├─ Send "processing" status to WebSocket client
│       │   └─ On task completion, broadcast via group_send()
│       │
│       ├── group_receive(event)
│       │   └─ Called when channel_layer broadcasts to group
│       │       ├─ Happens in background task after translation
│       │       ├─ Convert event to JSON
│       │       └─ await send(JSON to WebSocket)
│       │
│       └── [Other message handlers]
│
├── routing.py
│   └── websocket_urlpatterns = [
│       └─ path('ws/room/<str:room_code>/', ChatConsumer.as_asgi())
│           └─ Mounted in: lughabridge/routing.py
│
├── room_manager.py
│   ├── generate_room_code()
│   │   ├─ Generate 6-character alphanumeric (A-Z, 0-9)
│   │   ├─ Check uniqueness against existing Room objects
│   │   └─ Retry if collision
│   │
│   ├── cleanup_expired_rooms()
│   │   ├─ Query Room objects where expires_at < now()
│   │   ├─ Delete associated messages (cascade)
│   │   └─ Delete audio files from S3 (if bucket configured)
│   │
│   └── get_room_participant_count(room_code)
│       └─ Count active WebSocket connections in room_{room_code} group
│
├── admin.py
│   └─ Register Room, Message models for Django admin UI
│       └─ Allows manual inspection/management via /admin/
│
├── apps.py
│   └─ RoomsConfig(AppConfig)
│       └─ ready() → register signal handlers (if any)
│
├── tests.py
│   ├─ TestCreateRoom
│   ├─ TestJoinRoom
│   ├─ TestWebSocketConnection
│   └─ TestMessageBroadcasting
│
├── migrations/
│   └─ Auto-generated by: python manage.py makemigrations rooms
│       └─ Tracks schema changes for Room, Message models
│
└── urls.py
    └─ urlpatterns = [
        path('create/', create_room),
        path('<str:room_code>/join/', join_room),
        path('<str:room_code>/messages/', room_messages),
    ]
    └─ Mounted in: lughabridge/urls.py → path('api/rooms/', include('rooms.urls'))
```

### 2.2 Django App: `translation/` (Translation Services)

```
translation/
├── services/
│   ├── factory.py
│   │   ├── TranslationServiceFactory(object)
│   │   │   ├── get_stt_service(mode='live')
│   │   │   │   └─ Return: HuggingFaceASR | MockSTT (based on settings)
│   │   │   │
│   │   │   ├── get_translation_service(mode='live')
│   │   │   │   └─ Return: HybridTranslator | MockTranslator
│   │   │   │
│   │   │   └── get_tts_service(mode='live')
│   │   │       └─ Return: MMSTTSService | MockTTS
│   │   │
│   │   └── SERVICE SELECTION LOGIC:
│   │       if settings.DEMO_MODE:
│   │           Use mock_services.py (fast, no API calls)
│   │       elif settings.USE_HF_INFERENCE:
│   │           Use hf_inference_services.py (cloud-hosted models)
│   │       else:
│   │           Use local models (requires GPU, loads in memory)
│   │
│   ├── base.py
│   │   ├── STTService (ABC)
│   │   │   └── abstract transcribe(audio_bytes) -> (text, confidence)
│   │   │
│   │   ├── TranslationService (ABC)
│   │   │   └── abstract translate(text, source_lang, target_lang) -> (text, confidence)
│   │   │
│   │   └── TTSService (ABC)
│   │       └── abstract synthesize(text, language, voice_params) -> audio_bytes
│   │
│   ├── mock_services.py
│   │   ├── MOCK_CONVERSATIONS = {
│   │   │   'kikuyu': ['Habari gani?', 'Nĩ mwega', ...],
│   │   │   'english': ['How are you?', 'I am fine', ...],
│   │   │   ...
│   │   ├── MockSTT(STTService)
│   │   │   ├── transcribe(audio_bytes)
│   │   │   │   └─ Return: random phrase, confidence=0.92
│   │   │   │
│   │   │   └── Simulates: 0.1-0.5s processing delay
│   │   │
│   │   ├── MockTranslator(TranslationService)
│   │   │   ├── translate(text, src_lang, tgt_lang)
│   │   │   │   ├─ Lookup in KIKUYU_ENGLISH_PAIRS dict
│   │   │   │   ├─ Return: translated text, confidence=0.95
│   │   │   │   └─ If no match: use Google Translate API (fallback)
│   │   │   │
│   │   │   └── Simulates: instant response (no delay)
│   │   │
│   │   └── MockTTS(TTSService)
│   │       ├── synthesize(text, language, voice_params)
│   │       │   └─ Return: mock audio file path or base64
│   │       │
│   │       └── Simulates: instant response (pre-recorded segments)
│   │
│   ├── huggingface_asr.py
│   │   ├── HuggingFaceASR(STTService)
│   │   │   ├── __init__()
│   │   │   │   └─ Load HF Inference API client (needs HF_TOKEN)
│   │   │   │
│   │   │   └── transcribe(audio_bytes)
│   │   │       ├─ Send audio to HF API
│   │   │       ├─ Receive: {text, confidence}
│   │   │       ├─ Handles: Opus, MP3, WAV formats
│   │   │       └─ Supports: Kikuyu, Swahili, English models
│   │   │
│   │   └── ERROR HANDLING:
│   │       ├─ APIError → Return empty string, confidence=0.0
│   │       └─ Retry: up to 3 times with exponential backoff
│   │
│   ├── nllb_translator.py
│   │   ├── NLLBTranslator(TranslationService) [Local Model]
│   │   │   ├── __init__()
│   │   │   │   ├─ Load NLLB-200 from Hugging Face Hub
│   │   │   │   ├─ Tokenizer: NllbTokenizer (supports Kikuyu)
│   │   │   │   └─ GPU: Auto-detect and use if available
│   │   │   │
│   │   │   └── translate(text, src_lang, tgt_lang)
│   │   │       ├─ Tokenize input text
│   │   │       ├─ Forward pass through model
│   │   │       ├─ Beam search decoding (3 beams)
│   │   │       └─ Return: translated text, confidence from logits
│   │   │
│   │   └── MEMORY: ~2GB (Q4 quantized) / ~8GB (full precision)
│   │
│   ├── hf_inference_services.py
│   │   ├── HFInferenceTranslator(TranslationService) [Cloud]
│   │   │   ├── __init__(hf_token)
│   │   │   │   └─ Initialize HF Inference API client
│   │   │   │
│   │   │   ├── translate(text, src_lang, tgt_lang)
│   │   │   │   ├─ Call: inference_api.translation(text, model_id)
│   │   │   │   ├─ Model mapping: src_lang → tgt_lang → model_id
│   │   │   │   └─ Return: {translation_text, confidence}
│   │   │   │
│   │   │   └── Supported model IDs:
│   │   │       ├─ nllb-200-distilled-600M (fast, less accurate)
│   │   │       └─ nllb-200-1.3B (slower, more accurate)
│   │   │
│   │   └── LATENCY: 2-5 seconds (cloud inference)
│   │
│   ├── groq_translator.py
│   │   ├── GroqTranslator(TranslationService) [LLM]
│   │   │   ├── __init__(api_key, model='llama-3.3-70b-versatile')
│   │   │   │   └─ Initialize Groq API client (fast inference)
│   │   │   │
│   │   │   ├── translate(text, src_lang, tgt_lang)
│   │   │   │   ├─ Call: chat.completions.create() with prompt
│   │   │   │   ├─ Prompt: "Translate Kikuyu to English: [text]"
│   │   │   │   ├─ Return: translated text from model output
│   │   │   │   └─ Confidence: Estimated from probability tokens
│   │   │   │
│   │   │   └── USE CASE: Faster alternative for Swahili (if enabled)
│   │   │
│   │   └── LATENCY: 1-2 seconds (faster than HF Inference)
│   │
│   ├── hybrid_translator.py
│   │   ├── HybridTranslator(TranslationService)
│   │   │   ├── __init__()
│   │   │   │   ├─ Instantiate multiple translators based on config
│   │   │   │   └─ Priority order: Groq > HF Inference > Local
│   │   │   │
│   │   │   └── translate(text, src_lang, tgt_lang)
│   │   │       └─ DECISION TREE:
│   │   │           if src_lang or tgt_lang == 'swahili' AND USE_GROQ_FOR_SWAHILI:
│   │   │               Use GroqTranslator (fast)
│   │   │           elif src_lang == 'kikuyu' or tgt_lang == 'kikuyu':
│   │   │               Use HF Inference (better Kikuyu model)
│   │   │           else:
│   │   │               Use GoogleTranslate (fallback for other languages)
│   │   │
│   │   └── LOGIC: Optimize for speed (Groq) + quality (HF for Kikuyu)
│   │
│   ├── mms_tts.py
│   │   ├── MMSTTSService(TTSService)
│   │   │   ├── __init__()
│   │   │   │   ├─ Load Meta MMS TTS model (multilingual)
│   │   │   │   └─ Supports: Kikuyu, Swahili, English, 1000+ languages
│   │   │   │
│   │   │   └── synthesize(text, language, voice_params)
│   │   │       ├─ Parameters:
│   │   │       │  ├─ voice_gender: 'male', 'female', 'neutral'
│   │   │       │  ├─ pitch: -5 to +5 (relative shift)
│   │   │       │  └─ speed: 0.5 to 2.0 (rate)
│   │   │       │
│   │   │       ├─ Forward pass through TTS vocoder
│   │   │       ├─ Generate: audio_numpy (22050 Hz sample rate)
│   │   │       └─ Output: WAV file or base64 string
│   │   │
│   │   └── MEMORY: ~500MB (uses quantized weights)
│   │
│   └── cached_translator.py (cached_translate decorator)
│       ├── @cache_translation_result(ttl=86400)
│       │   └─ Wraps translate() methods
│       │
│       └── CACHING LOGIC:
│           1. Check Redis cache: trans:{src}:{tgt}:{hash(text)}
│           2. On hit: Return cached translation + mark as cache_hit=true
│           3. On miss: Call wrapped service
│           4. Store both Redis (24h TTL) + DB (persistent)
│           5. Increment hit_count in TranslationCache model
│
├── models.py
│   ├── TranslationCache (Django model)
│   │   ├── source_hash (CharField, indexed)
│   │   ├── source_lang (CharField)
│   │   ├── target_lang (CharField)
│   │   ├── translated_text (TextField)
│   │   ├── confidence_score (FloatField)
│   │   ├── hit_count (IntegerField) → tracks cache efficiency
│   │   ├── created_at (DateTimeField)
│   │   ├── expires_at (DateTimeField)
│   │   └── Meta.unique_together = ('source_hash', 'source_lang', 'target_lang')
│   │
│   ├── APIUsageLog (Django model)
│   │   ├── service_type (CharField: 'stt', 'translation', 'tts')
│   │   ├── provider (CharField: 'mock', 'hf_inference', 'local', 'groq')
│   │   ├── user_id (ForeignKey to User)
│   │   ├── input_tokens (IntegerField)
│   │   ├── output_tokens (IntegerField)
│   │   ├── cost_estimate (DecimalField)
│   │   ├── success (BooleanField)
│   │   ├── error_message (TextField)
│   │   ├── timestamp (DateTimeField)
│   │   └── duration_ms (IntegerField) → for latency monitoring
│   │
│   └── AudioFile (Django model)
│       ├── file_path (CharField: S3 path or local path)
│       ├── file_size (IntegerField: bytes)
│       ├── format (CharField: 'mp3', 'wav', 'opus')
│       ├── duration (FloatField: seconds)
│       ├── room (ForeignKey to Room)
│       ├── message (ForeignKey to Message)
│       ├── created_at (DateTimeField)
│       ├── expires_at (DateTimeField)
│       └── is_uploaded (BooleanField) → whether in S3 vs /tmp/
│
├── tasks.py
│   ├── process_voice_message_task(conversation_id, sender_id, audio_data, original_language, mode='live')
│   │   │   [Triggered by: rooms/consumers.py → handle_voice_message()]
│   │   │
│   │   ├── Step 1: SERVICE INITIALIZATION
│   │   │   services = TranslationServiceFactory()
│   │   │   stt = services.get_stt_service(mode)
│   │   │   translator = services.get_translation_service(mode)
│   │   │   tts = services.get_tts_service(mode)
│   │   │
│   │   ├── Step 2: SPEECH-TO-TEXT
│   │   │   audio_bytes = base64.decode(audio_data)
│   │   │   original_text, stt_confidence = stt.transcribe(audio_bytes)
│   │   │
│   │   ├── Step 3: DETECT TARGET LANGUAGE
│   │   │   if original_language == 'kikuyu':
│   │   │       target_language = 'english'
│   │   │   else:
│   │   │       target_language = 'kikuyu'
│   │   │
│   │   ├── Step 4: TRANSLATE
│   │   │   translated_text, trans_confidence = translator.translate(
│   │   │       original_text, original_language, target_language
│   │   │   )
│   │   │
│   │   ├── Step 5: OPTIONAL - TEXT-TO-SPEECH
│   │   │   If TTS enabled:
│   │   │       audio_bytes = tts.synthesize(translated_text, target_language)
│   │   │       S3_upload(audio_bytes, message_id)
│   │   │       translated_audio_url = S3_path
│   │   │   Else:
│   │   │       translated_audio_url = None
│   │   │
│   │   ├── Step 6: SAVE TO DATABASE
│   │   │   message = Message.objects.get(id=message_id)
│   │   │   message.original_text = original_text
│   │   │   message.translated_text = translated_text
│   │   │   message.stt_confidence = stt_confidence
│   │   │   message.translation_confidence = trans_confidence
│   │   │   message.audio_url = translated_audio_url
│   │   │   message.processing_status = 'completed'
│   │   │   message.save()
│   │   │
│   │   ├── Step 7: LOG API USAGE
│   │   │   APIUsageLog.objects.create(
│   │   │       service_type='stt|translation|tts',
│   │   │       provider=stt.provider,
│   │   │       success=True,
│   │   │       duration_ms=(end_time - start_time)
│   │   │   )
│   │   │
│   │   └── Step 8: BROADCAST TO WEBSOCKET
│   │       channel_layer.group_send(f'room_{room_code}', {
│   │           "type": "chat_message",
│   │           "message_id": message_id,
│   │           "original_text": original_text,
│   │           "translated_text": translated_text,
│   │           ...
│   │       })
│   │
│   ├── cleanup_expired_translations()
│   │   │   [Scheduled daily @ 2 AM via django-q]
│   │   │
│   │   ├─ Query TranslationCache where expires_at < now()
│   │   ├─ Delete from cache
│   │   └─ Keep in database for analytics
│   │
│   └── cleanup_expired_audio_files()
│       │   [Scheduled weekly via django-q]
│       │
│       ├─ Query AudioFile where expires_at < now()
│       ├─ Delete from S3 bucket
│       └─ Delete database records
│
├── tests.py
│   ├─ TestMockServices
│   ├─ TestTranslationCaching
│   ├─ TestHybridTranslator
│   └─ TestBackgroundTasks
│
├── management/
│   └─ commands/
│       └─ download_models.py
│           └─ python manage.py download_models
│               ├─ Downloads NLLB-200 (if not using HF Inference)
│               ├─ Downloads MMS TTS
│               └─ Caches to HF_CACHE_DIR
│
├── migrations/
│   └─ Auto-generated schema changes for models
│
├── apps.py
│   └─ TranslationConfig
│       └─ ready() → setup scheduled background tasks via django-q
│
├── admin.py
│   └─ Register TranslationCache, APIUsageLog for admin UI
│
└── urls.py (if needed)
    └─ Can expose translation API endpoints separately
```

### 2.3 Core Django Configuration Files

```
lughabridge/
├── settings.py [KEY FILE - SYSTEM CONFIGURATION]
│   ├── DEBUG = env.bool('DEBUG', default=False)
│   │   └─ Controls error pages, static files, security settings
│   │
│   ├── SECRET_KEY = env('SECRET_KEY')
│   │   └─ Django session/CSRF token signing key
│   │
│   ├── ALLOWED_HOSTS = env.list('ALLOWED_HOSTS')
│   │   └─ Prevents Host header attacks
│   │       └─ Dev: ['localhost', '127.0.0.1']
│   │       └─ Prod: ['lughabridge-backend.railway.app']
│   │
│   ├── INSTALLED_APPS = [
│   │   'django.contrib.admin',
│   │   'django.contrib.auth',
│   │   'django.contrib.contenttypes',
│   │   'rest_framework',        # DRF for REST APIs
│   │   'corsheaders',            # CORS for frontend domain
│   │   'channels',               # WebSocket support
│   │   'django_q',               # Background task queue
│   │   'rooms',                  # Our chat app
│   │   'translation',            # Translation services
│   │]
│   │
│   ├── MIDDLEWARE = [
│   │   'corsheaders.middleware.CorsMiddleware',  # Must be first!
│   │   'django.middleware.security.SecurityMiddleware',
│   │   'django.contrib.sessions.middleware.SessionMiddleware',
│   │   # ... more middleware
│   │]
│   │
│   ├── REST_FRAMEWORK = {
│   │   'DEFAULT_PAGINATION_CLASS': 'rest_framework.pagination.PageNumberPagination',
│   │   'PAGE_SIZE': 20,
│   │   'DEFAULT_FILTER_BACKENDS': ['rest_framework.filters.OrderingFilter'],
│   │}
│   │
│   ├── CORS_ALLOWED_ORIGINS = [
│   │   env('FRONTEND_URL', default='http://localhost:8080'),
│   │   # Dev: http://localhost:8080
│   │   # Prod: https://lughabridge.vercel.app
│   │]
│   │
│   ├── CHANNEL_LAYERS = {
│   │   "default": {
│   │       "BACKEND": "channels.layers.InMemoryChannelLayer"  # Dev
│   │       # or "channels_redis.core.RedisChannelLayer"       # Prod
│   │   }
│   │}
│   │
│   ├── ASGI_APPLICATION = 'lughabridge.asgi.application'
│   │   └─ Entry point for Daphne ASGI server
│   │
│   ├── DATABASES = {
│   │   'default': env.db('DATABASE_URL', default='sqlite:///db.sqlite3')
│   │   # Dev: sqlite3 (local file)
│   │   # Prod: postgresql://user:pass@host/db
│   │}
│   │
│   ├── Q_CLUSTER = {
│   │   'name': 'lughabridge',
│   │   'workers': 4,
│   │   'timeout': 500,
│   │   'retry': 600,
│   │   'save_limit': 250,
│   │   'queue_limit': 500,
│   │   'broker': 'default',  # Uses Django cache/ORM
│   │   'orm': 'default',      # Uses Django ORM as task storage
│   │}
│   │   └─ Configures django-q background task processor
│   │
│   ├── LOGGING = {
│   │   'version': 1,
│   │   'handlers': {
│   │       'console': { 'class': 'logging.StreamHandler' },
│   │       'file': { 'filename': '/var/log/lughabridge.log' },
│   │   },
│   │   'loggers': {
│   │       'django': {'handlers': ['console', 'file'], 'level': 'INFO'},
│   │       'translation': {'handlers': ['file'], 'level': 'DEBUG'},
│   │   }
│   │}
│   │
│   ├── CACHES = {
│   │   'default': {
│   │       'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'  # Dev
│   │       # 'BACKEND': 'django_redis.cache.RedisCache'                 # Prod
│   │   }
│   │}
│   │   └─ For translation caching (cache_translation_result decorator)
│   │
│   └── DEMO_MODE = env.bool('DEMO_MODE', default=False)
│       ├─ True: Use MockSTT, MockTranslator, MockTTS
│       ├─ False: Use HF Inference or local models
│       └─ Affects entire system behavior
│
├── asgi.py [WEBSOCKET ENTRY POINT]
│   ├── os.environ.setdefault("DJANGO_SETTINGS_MODULE", "lughabridge.settings")
│   │   └─ Tells Django which settings file to use
│   │
│   ├── django_asgi_app = get_asgi_application()
│   │   └─ Django ASGI application for HTTP requests
│   │
│   ├── from rooms.routing import websocket_urlpatterns
│   │   └─ Import WebSocket URL patterns
│   │
│   └── application = ProtocolTypeRouter({
│       "http": django_asgi_app,
│       "websocket": URLRouter(websocket_urlpatterns),
│   })
│       └─ Routes HTTP → Django, WebSocket → rooms.routing
│           └─ Daphne runs this application
│
├── urls.py [HTTP ROUTING]
│   └─ urlpatterns = [
│       path("admin/", admin.site.urls),
│       path("api/health/", health_check),
│       path("api/rooms/", include('rooms.urls')),
│   ]
│       └─ All regex match → views.py functions
│           └─ Returns JSON responses
│
├── routing.py [WEBSOCKET ROUTING]
│   └─ websocket_urlpatterns = [
│       path('ws/room/<str:room_code>/', ChatConsumer.as_asgi())
│   ]
│       └─ All /ws/room/{code}/ → ChatConsumer
│           └─ Daphne routes via asgi.py
│
├── wsgi.py
│   └─ Legacy WSGI app (not used, Daphne handles ASGI)
│
└── __init__.py
    └─ Empty, marks directory as Python package
```

### 2.4 Environment Configuration

```
Backend/.env
├── # Django security
├─ SECRET_KEY=long-random-string-for-session-signing
├─ DEBUG=False (or True for dev)
├─ ALLOWED_HOSTS=localhost,127.0.0.1,lughabridge-backend.railway.app
│
├── # Database
├─ DATABASE_URL=sqlite:///db.sqlite3 OR postgresql://...
│
├── # Cache & Channels
├─ REDIS_URL=redis://localhost:6379/0
│
├── # Frontend CORS
├─ FRONTEND_URL=http://localhost:8080 OR https://lughabridge.vercel.app
│
├── # Model settings
├─ DEMO_MODE=True  # Controls which services to use
├─ USE_HF_INFERENCE=False
├─ HF_TOKEN=<hugging-face-api-token>
├─ HF_CACHE_DIR=/tmp/huggingface_cache
│
├── # Groq API (optional)
├─ GROQ_API_KEY=<groq-api-key>
├─ USE_GROQ_FOR_SWAHILI=False
│
└── # AWS S3 (production)
   ├─ AWS_ACCESS_KEY_ID=...
   ├─ AWS_SECRET_ACCESS_KEY=...
   ├─ AWS_STORAGE_BUCKET_NAME=lughabridge-audio
   └─ AWS_S3_REGION_NAME=us-east-1
```

---

## 3. FRONTEND FILE STRUCTURE WITH FUNCTION MAPPING

### 3.1 Core Application Files

```
Frontend/src/
├── main.tsx [ENTRY POINT]
│   ├── import React from 'react'
│   ├── import ReactDOM from 'react-dom/client'
│   ├── ReactDOM.createRoot(document.getElementById('root')!).render(
│   │   <React.StrictMode>
│   │       <App />
│   │   </React.StrictMode>
│   └── )
│
├── App.tsx [ROOT COMPONENT]
│   ├── Defines routing structure:
│   │   <BrowserRouter>
│   │       <Routes>
│   │           <Route path="/" element={<Landing />} />
│   │           <Route path="/rooms/:code" element={<RoomChat />} />
│   │           <Route path="*" element={<NotFound />} />
│   │       </Routes>
│   │   </BrowserRouter>
│   │
│   ├── Provides context/global state (if needed)
│   └── App.css - Global styles
│
├── vite-env.d.ts
│   └─ TypeScript declarations for Vite environment variables
│       │  (VITE_API_BASE_URL, VITE_WS_BASE_URL)
│
├── index.css [GLOBAL STYLES]
│   └─ Tailwind @import, base styles, animations
│
└── [Additional config files below]
```

### 3.2 Pages Router

```
src/pages/
├── Landing.tsx
│   ├── Component: AOS for animations
│   ├── Form inputs:
│   │   ├─ source_lang: select (kikuyu, swahili, english)
│   │   └─ target_lang: select (english, kikuyu, swahili)
│   │
│   ├── On submit:
│   │   ├─ API call: api.createRoom({ source_lang, target_lang })
│   │   ├─ Response: { room_code, ws_url, ... }
│   │   └─ Navigate: /rooms/{room_code}
│   │
│   └── Error handling:
│       ├─ Network error → show toast
│       └─ Invalid language pair → show validation error
│
├── RoomChat.tsx [MAIN CHAT INTERFACE]
│   ├── useParams() → extract room_code from URL
│   │
│   ├── useState() hooks:
│   │   ├─ messages: ChatMessage[]
│   │   ├─ roomCode: string
│   │   ├─ connected: boolean
│   │   ├─ roomData: RoomData
│   │   └─ recordingState: 'idle' | 'recording' | 'processing'
│   │
│   ├── useEffect() hooks:
│   │   ├─ Join room on mount:
│   │   │   ├─ api.joinRoom(room_code)
│   │   │   ├─ Get room metadata (languages, expiry)
│   │   │   ├─ Initialize WebSocket: new RoomWebSocket(room_code)
│   │   │   └─ ws.connect() → establishes connection
│   │   │
│   │   └─ Listen to WebSocket events:
│   │       ├─ ws.on('chat_message', (data) => {
│   │       │   setMessages([...messages, normalizeMessage(data)])
│   │       │ })
│   │       │
│   │       └─ ws.on('error', (error) => {
│   │           showErrorToast()
│   │       })
│   │
│   ├── Render:
│   │   ├─ <ChatLayout>
│   │   │   ├─ Header: room code + participant count
│   │   │   ├─ Message list:
│   │   │   │   └─ {messages.map(msg => <MessageBubble {...msg} />)}
│   │   │   │
│   │   │   ├─ MicButton:
│   │   │   │   ├─ onClick → startRecording()
│   │   │   │   └─ onRelease → stopRecording() + sendAudio()
│   │   │   │
│   │   │   └─ Footer: language info + timer
│   │   │
│   │   └─ Modal: ConnectingSpinner (while WebSocket connects)
│   │
│   └─ Send audio to backend:
│       ├─ ws.sendVoiceMessage({
│       │   audio_data: base64(audioBlob),
│       │   language: roomData.source_lang
│       │ })
│       │
│       └─ Backend processes → broadcast → receive response
│
├── CreateRoom.tsx
│   └─ Same as Landing.tsx (alternative entry point)
│
├── JoinRoom.tsx
│   ├─ Input: room_code (text field)
│   ├─ Submit: api.joinRoom(room_code)
│   └─ Navigate to RoomChat on success
│
└── NotFound.tsx
    └─ 404 page, link back to Landing
```

### 3.3 Services Layer

```
src/services/
├── api.ts [REST CLIENT]
│   ├── const API_BASE = import.meta.env.VITE_API_BASE_URL || 'http://localhost:8000/api'
│   │   └─ Environment variable: VITE_API_BASE_URL
│   │
│   ├── export const api = {
│   │   baseUrl: API_BASE,
│   │   wsBase: WS_BASE,
│   │
│   │   async createRoom(payload: CreateRoomPayload): Promise<CreateRoomResponse> {
│   │       ├─ POST /api/rooms/create/
│   │       ├─ Body: { source_lang, target_lang }
│   │       ├─ Headers: { Content-Type: application/json }
│   │       └─ Response: { room_code, source_lang, target_lang, ws_url, expiry_hours }
│   │
│   │   async joinRoom(code: string): Promise<JoinRoomResponse> {
│   │       ├─ GET /api/rooms/{code}/join/
│   │       └─ Response: { room_code, source_lang, target_lang, participant_count, expires_at }
│   │
│   │   async getRoomMessages(code: string): Promise<MessagesResponse> {
│   │       ├─ GET /api/rooms/{code}/messages/
│   │       └─ Response: { messages: ChatMessage[] }
│   │
│   │   async healthCheck(): Promise<HealthCheckResponse> {
│   │       ├─ GET /api/health/
│   │       └─ Response: { status, demo_mode, supported_languages }
│   │
│   │   getWsUrl(roomCode: string): string {
│   │       └─ Construct: ws://{WS_BASE}/ws/room/{roomCode}/
│   │
│   │   normalizeMessages(raw: any[]): ChatMessage[] {
│   │       └─ Convert backend format → frontend format
│   │       └─ Handle snake_case → camelCase conversion
│   │
│   └── }
│
└── websocket.ts [WEBSOCKET CLIENT]
    ├── export type WSMessageType =
    │   └─ 'connection_established' | 'chat_message' | 'processing' | 'error' | ...
    │
    ├── export interface WSMessage { }
    │   └─ Generic message container
    │
    ├── export interface VoiceMessagePayload {
    │   ├─ type: 'voice_message'
    │   ├─ message_id: string
    │   ├─ audio_data: string (base64)
    │   └─ language: string
    │
    ├── export interface TextMessagePayload {
    │   ├─ type: 'text_message'
    │   ├─ message_id: string
    │   ├─ text: string
    │   └─ language: string
    │
    └── export class RoomWebSocket {
        ├── private ws: WebSocket | null
        ├── private listeners: Map<WSMessageType | 'open' | 'close', Set<WSEventCallback>>
        ├── private roomCode: string
        ├── private wsUrl: string
        ├── private reconnectAttempts: number
        ├── private maxReconnectAttempts: number = 5
        ├── private reconnectDelay: number = 2000
        │
        ├── constructor(roomCode: string, wsBaseUrl?: string)
        │   └─ Construct WebSocket URL, initialize listeners map
        │
        ├── connect(): Promise<void>
        │   ├─ new WebSocket(this.wsUrl)
        │   │
        │   ├─ this.ws.onopen = () => {
        │   │   └─ emit('open') → notify listeners
        │   │
        │   ├─ this.ws.onmessage = (event) => {
        │   │   ├─ JSON.parse(event.data) → WSMessage
        │   │   ├─ handleMessage(data) → route to handler
        │   │   └─ emit(data.type) → notify listeners
        │   │
        │   ├─ this.ws.onerror = (error) => {
        │   │   └─ emit('error')
        │   │
        │   └─ this.ws.onclose = (event) => {
        │       ├─ emit('close')
        │       └─ Attempt reconnect (up to 5 times)
        │
        ├── on(type: WSMessageType | 'open' | 'close', callback: WSEventCallback): void
        │   ├─ listeners.get(type).add(callback)
        │   └─ Example usage:
        │       │  ws.on('chat_message', (data) => {
        │       │    setMessages([...messages, data])
        │       │  })
        │
        ├── emit(type: string, data?: any): void
        │   └─ Call all callbacks registered for type
        │
        ├── send(payload: any): void
        │   └─ this.ws!.send(JSON.stringify(payload))
        │       └─ Sends JSON message to Backend
        │
        ├── sendVoiceMessage(audio: Blob, language: string): string
        │   ├─ const reader = new FileReader()
        │   ├─ reader.readAsDataURL(audio) → base64
        │   ├─ const message_id = generateUUID()
        │   ├─ this.send({
        │   │   type: 'voice_message',
        │   │   message_id,
        │   │   audio_data: dataUrl.split(',')[1],  // strip data:audio/...
        │   │   language
        │   │ })
        │   └─ return message_id
        │
        ├── sendTextMessage(text: string, language: string): string
        │   ├─ Similar to sendVoiceMessage
        │   └─ type: 'text_message'
        │
        ├── close(): void
        │   ├─ isIntentionallyClosed = true
        │   └─ this.ws!.close()
        │
        └── [Additional helpers]
```

### 3.4 UI Components

```
src/components/
├── lugha/  [CUSTOM COMPONENTS]
│   ├── ChatLayout.tsx
│   │   ├─ Props: { children, roomCode, participantCount }
│   │   └─ Layout structure:
│   │       <div className="flex flex-col h-screen">
│   │           <Header />
│   │           <MessageList>{children}</MessageList>
│   │           <Footer />
│   │       </div>
│   │
│   ├── MessageBubble.tsx
│   │   ├─ Props: ChatMessage (id, sender, original, translated, confidence, timestamp)
│   │   ├─ Render:
│   │   │   <div className={sender === 'A' ? 'mr-auto' : 'ml-auto'}>
│   │   │       <p>{originalText}</p>
│   │   │       <p className="text-sm text-gray-500">{translatedText}</p>
│   │   │       <ConfidenceRing confidence={confidence} />
│   │   │       <p className="text-xs text-gray-400">{timestamp}</p>
│   │   │   </div>
│   │   │
│   │   └─ Styling: Tailwind + custom CSS for bubble shape
│   │
│   ├── MicButton.tsx
│   │   ├─ State: { recording: boolean }
│   │   ├─ useMediaRecorder() hook → capture audio
│   │   │   ├─ navigator.mediaDevices.getUserMedia({ audio: true })
│   │   │   ├─ Create MediaRecorder
│   │   │   └─ Collect audio chunks into Blob
│   │   │
│   │   ├─ onClick:
│   │   │   └─ if (recording) stop() else start()
│   │   │
│   │   ├─ onStop:
│   │   │   ├─ audioBlob = new Blob(audioChunks)
│   │   │   ├─ onAudioReady(audioBlob)  ← callback to parent
│   │   │   └─ RoomChat.tsx: ws.sendVoiceMessage(audioBlob, language)
│   │   │
│   │   └─ Render:
│   │       <button className={recording ? 'bg-red-500' : 'bg-blue-500'}>
│   │           🎤 {recording ? 'Recording...' : 'Click to Record'}
│   │       </button>
│   │
│   ├── ConfidenceRing.tsx
│   │   ├─ Props: { confidence: number (0-1) }
│   │   ├─ Visual: Circular progress indicator
│   │   │   ├─ Color: red (0-0.3), yellow (0.3-0.7), green (0.7-1.0)
│   │   │   └─ SVG or Canvas circle
│   │   │
│   │   └─ Tooltip: "95% confidence"
│   │
│   └── DemoModeToggle.tsx
│       ├─ Props: { demoMode: boolean, onChange: (bool) => void }
│       ├─ Switch component with label
│       └─ Affects: api.healthCheck() → updates UI
│
└── ui/  [SHADCN/RADIX UI COMPONENTS]
    ├── card.tsx    → <Card>, <CardHeader>, <CardContent>
    ├── button.tsx  → <Button variant="..." />
    ├── input.tsx   → <Input type="..." />
    ├── select.tsx  → <Select><SelectItem>...</SelectSelect>
    ├── dialog.tsx  → <Dialog><DialogContent>...</DialogContent></Dialog>
    ├── toast.tsx   → <Toast /> (notifications)
    ├── progress.tsx → <Progress value={75} />
    ├── badge.tsx   → <Badge>Label</Badge>
    ├── avatar.tsx  → <Avatar><AvatarImage /><AvatarFallback/></Avatar>
    ├── tabs.tsx    → <Tabs><TabsList><TabsTrigger>...</TabsTrigger>...
    ├── form.tsx    → Custom form wrapper (uses react-hook-form)
    └── ... (20+ more pre-built components)
```

### 3.5 Data & Types

```
src/data/
├── mockMessages.ts
│   ├── export const mockMessages: ChatMessage[] = [
│   │   {
│   │       id: 'msg-1',
│   │       sender: 'A',
│   │       originalText: 'Habari gani?',
│   │       translatedText: 'How are you?',
│   │       originalLanguage: 'Kikuyu',
│   │       timestamp: new Date(),
│   │       confidence: 0.94
│   │   },
│   │   ...
│   │ ]
│   │
│   └─ Used for demo/offline testing
│
└── mockRooms.ts
    ├── export const mockRooms: RoomData[] = [...]
    │
    └─ Used for demo room list
```

```
src/types/
└── index.ts
    ├── export interface ChatMessage {
    │   id: string
    │   sender: 'A' | 'B'
    │   originalText: string
    │   translatedText: string
    │   originalLanguage: string
    │   timestamp: Date
    │   confidence: number
    │ }
    │
    ├── export interface CreateRoomPayload {
    │   source_lang: string
    │   target_lang: string
    │ }
    │
    ├── export interface CreateRoomResponse {
    │   room_code: string
    │   source_lang: string
    │   target_lang: string
    │   ws_url: string
    │   expiry_hours: number
    │ }
    │
    ├── export interface RoomData {
    │   room_code: string
    │   source_lang: string
    │   target_lang: string
    │   created_at: string
    │   expires_at: string
    │   participant_count: number
    │ }
    │
    ├── export interface HealthCheckResponse {
    │   status: 'healthy' | 'unhealthy'
    │   demo_mode: boolean
    │   supported_languages: string[]
    │ }
    │
    └── ... (other interfaces)
```

```
src/hooks/
├── useVoiceRecording.ts
│   ├── Hook for managing audio recording state
│   ├── Returns: {
│   │   isRecording,
│   │   startRecording,
│   │   stopRecording,
│   │   audioBlob
│   │ }
│   │
│   └─ Uses: MediaRecorder API
│
├── use-toast.ts (shadcn)
│   ├── Hook for toast notifications
│   └─ Usage: const { toast } = useToast()
│           toast({ title: 'Success', description: '...' })
│
└── use-mobile.tsx (shadcn)
    └─ Responsive hook for mobile detection
```

### 3.6 Build & Configuration

```
Frontend/
├── vite.config.ts
│   ├── export default defineConfig({
│   │   server: {
│   │       host: '::',  # Listen on all interfaces
│   │       port: 8080,
│   │       hmr: { overlay: false }  # No error overlay
│   │   },
│   │
│   │   plugins: [
│   │       react(),  # React Fast Refresh
│   │       componentTagger()  # Dev tools integration
│   │   ],
│   │
│   │   resolve: {
│   │       alias: {
│   │           '@': path.resolve(__dirname, './src')  # @/ import alias
│   │       }
│   │   }
│   │ })
│   │
│   └─ Builds to: dist/ (optimized static files)
│
├── tsconfig.json
│   ├── compilerOptions:
│   │   ├─ target: 'ES2020'
│   │   ├─ module: 'ESNext'
│   │   ├─ strict: true  # Strict type checking
│   │   ├─ skipLibCheck: true
│   │   └─ jsx: 'react-jsx'
│   │
│   └─ Enforces type safety across codebase
│
├── package.json
│   ├── "scripts": {
│   │   "dev": "vite",  # Start dev server
│   │   "build": "vite build",  # Production build
│   │   "lint": "eslint .",  # Type/style checking
│   │   "test": "vitest run",  # Run unit tests
│   │ }
│   │
│   ├── "dependencies": {
│   │   "react": "^18.3.1",
│   │   "react-router-dom": "^6.30.1",  # Client-side routing
│   │   "react-hook-form": "^7.61.1",  # Form management
│   │   "@tanstack/react-query": "^5.83.0",  # Data fetching (optional)
│   │   "zod": "^3.25.76",  # Type-safe validation
│   │   "tailwindcss": "^3.4.17",  # Styling
│   │   "@radix-ui/*": "...",  # Headless UI components
│   │   ... (more dependencies)
│   │ }
│   │
│   └─ Manages all 60+ packages
│
├── tailwind.config.ts
│   ├── Extends Tailwind with custom colors, fonts
│   └─ Processes CSS: index.css → output.css
│
├── tsconfig.app.json
│   └─ App-specific TypeScript config
│
└── eslint.config.js
    └─ Code quality rules, React hooks validation
```

---

## 4. DATA FLOW DIAGRAMS

### 4.1 Complete Request-Response Cycle

```
┌──────────────────────┐
│   USER SPEAKS        │
│   (RoomChat.tsx)     │
└──────────┬───────────┘
           │ MicButton captures audio
           ↓
┌──────────────────────────────────────────┐
│ sendVoiceMessage() (websocket.ts)        │
│ - Convert audio blob to base64           │
│ - Create message_id                      │
│ - Send WSMessage: {                      │
│   type: 'voice_message',                 │
│   audio_data: base64,                    │
│   language: 'kikuyu'                     │
│ }                                        │
└──────────────────────┬───────────────────┘
                       │ WebSocket send
                       ↓
┌──────────────────────────────────────────┐
│ BACKEND → consumers.py                   │
│ ChatConsumer.receive(text_data)          │
│ - Parse JSON message                     │
│ - Create Message(status='processing')    │
│ - Send "processing" event back           │
│ - Queue task: process_voice_message_task │
└──────────────────────┬───────────────────┘
                       │ Django-Q task
                       ↓
┌──────────────────────────────────────────┐
│ TRANSLATION PIPELINE (tasks.py)          │
│ 1. STT: audio → text + confidence        │
│    services/huggingface_asr.py           │
│                                          │
│ 2. Translate: text → translated_text    │
│    services/hybrid_translator.py         │
│                                          │
│ 3. TTS: translated_text → audio (opt)   │
│    services/mms_tts.py                   │
│                                          │
│ 4. Save to DB: Message.save()           │
│    models.py                             │
│                                          │
│ 5. Log: APIUsageLog.create()             │
│    models.py                             │
└──────────────────────┬───────────────────┘
                       │ channel_layer.group_send()
                       ↓
┌──────────────────────────────────────────┐
│ BROADCAST to WebSocket group             │
│ room_{room_code}                         │
│ - All connected clients receive message  │
│ - Format: {                              │
│   type: 'chat_message',                  │
│   original_text: '...',                  │
│   translated_text: '...',                │
│   confidence: 0.94,                      │
│   timestamp: '...'                       │
│ }                                        │
└──────────────────────┬───────────────────┘
                       │ WebSocket send
                       ↓
┌──────────────────────────────────────────┐
│ FRONTEND → websocket.ts                  │
│ on('chat_message', (data) => {           │
│   setMessages([...messages, data])       │
│ })                                       │
│                                          │
│ Triggers re-render of RoomChat.tsx       │
└──────────────────────┬───────────────────┘
                       │ React renders
                       ↓
┌──────────────────────────────────────────┐
│ DISPLAY MESSAGE (RoomChat.tsx)           │
│ {messages.map(msg =>                     │
│   <MessageBubble {...msg} />             │
│ )}                                       │
│                                          │
│ Components:                              │
│ - MessageBubble: Original text           │
│ - Styled bubble: Translated text         │
│ - ConfidenceRing: Confidence % circle    │
│ - Timestamp: Message time                │
└──────────────────────────────────────────┘
```

### 4.2 File Dependency Graph

```
FRONTEND ENTRYPOINT:
main.tsx → App.tsx → React Router

PAGES:
Landing.tsx ──→ api.createRoom()
   ↓
   Create room, get room_code  ← views.py (Backend)
   ↓
RoomChat.tsx ←─ Join room, get Room object  ← views.py
   │
   ├─→ websocket.ts (RoomWebSocket)  ← consumers.py (Backend)
   │   ├─ connect()  → ChatConsumer.connect()
   │   ├─ on('chat_message', callback)
   │   └─ sendVoiceMessage()  → ChatConsumer.receive()
   │
   ├─→ MicButton.tsx (useVoiceRecording hook)
   │   └─ Captures audio
   │
   ├─→ MessageBubble.tsx
   │   └─ Displays messages
   │
   └─→ ConfidenceRing.tsx
       └─ Shows translation confidence


BACKEND ENTRYPOINT:
asgi.py → ProtocolTypeRouter
   │
   ├─ HTTP → django_asgi_app → urls.py → views.py
   │  ├─ create_room()
   │  ├─ join_room()
   │  └─ room_messages()
   │
   └─ WebSocket → URLRouter → routing.py → consumers.py
      └─ ChatConsumer:
         ├─ connect()
         ├─ disconnect()
         └─ receive()
            └─ handle_voice_message()
               └─ tasks.py: process_voice_message_task()
                  ├─ factory.py (get services)
                  │  ├─ huggingface_asr.py
                  │  ├─ hybrid_translator.py
                  │  ├─ mms_tts.py
                  │  └─ mock_services.py
                  │
                  ├─ models.py (save message)
                  │
                  └─ channel_layer.group_send()
                     └─ Broadcast back to consumers
```

---

## 5. ENVIRONMENT VARIABLE IMPACT MAP

```
DEMO_MODE=True
│
├─→ settings.py: DEMO_MODE flag set
├─→ factory.py: Always return Mock* services
├─→ mock_services.py: Used for STT, Translation, TTS
├─→ Translation immediate (no API calls)
└─→ No external API authentication needed


DEMO_MODE=False + USE_HF_INFERENCE=True
│
├─→ factory.py: Return HF Inference services
├─→ hf_inference_services.py: Requires HF_TOKEN env var
├─→ Calls HuggingFace API for STT, Translation, TTS
├─→ Latency: 2-5 seconds
└─→ Works on limited RAM (512MB acceptable)


USE_GROQ_FOR_SWAHILI=True + GROQ_API_KEY= set
│
├─→ hybrid_translator.py: Uses Groq for Swahili
├─→ groq_translator.py: Initialized with GROQ_API_KEY
├─→ Faster alternative to HF Inference (1-2s)
└─→ Falls back to HF if Groq unavailable


FRONTEND_URL environment variable
│
├─→ settings.py: CORS_ALLOWED_ORIGINS configured
├─→ Must match Frontend deployment URL
├─→ Requests from other domains will be blocked
└─→ Dev: http://localhost:8080
    Prod: https://lughabridge.vercel.app


DATABASE_URL
│
├─→ settings.py: DATABASES['default'] configuration
├─→ Dev: sqlite:///db.sqlite3 (file-based, local)
└─→ Prod: postgresql://user:pass@host/db (cloud database)
```

---

## 6. TESTING STRATEGY

```
UNIT TESTS:
├─ Backend/translation/tests.py
│  ├─ Test mock_services.py methods
│  ├─ Test factory pattern
│  └─ Test TranslateCache model
│
├─ Backend/rooms/tests.py
│  ├─ Test create_room() view
│  ├─ Test room_code generation
│  └─ Test room expiry logic
│
└─ Frontend/src/test/
   ├─ api.test.ts: Test API client methods
   └─ websocket.test.ts: Test WebSocket connection


INTEGRATION TESTS:
├─ E2E: Create Room API
│  └─ POST /api/rooms/create/ → Check response format
│
├─ E2E: WebSocket Connection
│  └─ Connect to /ws/room/{code}/ → Check connected event
│
└─ E2E: Translation Pipeline
   └─ Send voice message → Receive processed translation


LOAD TESTS:
└─ Simulate 100+ concurrent WebSocket connections
   └─ Measure response times, memory usage
```

---

**Final Notes**:

- Every file has a specific purpose in the system
- Data flows from Frontend → Backend → Services → Database → Frontend
- Environment variables control behavior without code changes
- Services are interchangeable via factory pattern (easy to swap providers)
- WebSocket enables real-time, low-latency translation updates

_Last Updated: February 28, 2026_
