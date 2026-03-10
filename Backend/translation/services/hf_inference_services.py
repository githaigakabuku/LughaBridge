"""
Hugging Face Inference API service implementations.
Uses cloud-hosted models via HF Inference API instead of local models.
"""

import logging
from typing import Dict, Any
from django.conf import settings
import os
import uuid

try:
    from huggingface_hub import InferenceClient
    HF_HUB_AVAILABLE = True
except ImportError:
    HF_HUB_AVAILABLE = False
    logging.warning("huggingface_hub not installed. Run: pip install huggingface_hub>=0.20")

from .base import ASRService, TranslationService, TTSService

logger = logging.getLogger(__name__)


class HFInferenceASR(ASRService):
    """
    ASR service using Hugging Face Inference API.
    Calls cloud-hosted Wav2Vec2/W2V-BERT models for speech recognition.
    """
    
    def __init__(self):
        """Initialize HF Inference ASR service."""
        if not HF_HUB_AVAILABLE:
            raise ImportError("huggingface_hub is required. Run: pip install huggingface_hub>=0.20")
        
        self.model_configs = settings.MODELS['asr']
        self.token = settings.HF_TOKEN
        
        if not self.token:
            logger.warning(
                "HF_TOKEN not set. HF Inference API requests may be rate-limited. "
                "Get a token from https://huggingface.co/settings/tokens"
            )
        # Note: InferenceClient is NOT used here — automatic_speech_recognition() raises
        # StopIteration for community models that have no provider mapping.
        # We call router.huggingface.co directly via requests instead.

    def transcribe(self, audio_path: str, language: str) -> Dict[str, Any]:
        """
        Transcribe audio file using HF Inference API.
        
        Args:
            audio_path: Path to audio file (wav, mp3, etc.)
            language: Source language code (kikuyu, swahili, english)
            
        Returns:
            dict: {"text": str, "confidence": float}
        """
        model_name = self.model_configs.get(language)
        if not model_name:
            raise ValueError(f"No ASR model configured for language: {language}")
        
        logger.info(f"Transcribing audio via HF API for {language}: {model_name}")
        
        try:
            import requests as req

            # Read audio file as bytes
            with open(audio_path, 'rb') as audio_file:
                audio_data = audio_file.read()

            # Call HF Inference API directly.
            # router.huggingface.co returns 404 for community fine-tuned models not
            # registered on the provider router. Use the classic serverless inference
            # endpoint instead, which supports any public model on the Hub.
            api_url = f"https://api-inference.huggingface.co/models/{model_name}"
            headers = {
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "audio/webm",
            }

            response = req.post(api_url, headers=headers, data=audio_data, timeout=60)
            response.raise_for_status()
            data = response.json()

            # Extract text
            if isinstance(data, dict):
                if "error" in data:
                    raise RuntimeError(f"HF ASR API error: {data['error']}")
                transcribed_text = data.get("text", "")
            elif isinstance(data, str):
                transcribed_text = data
            else:
                logger.error(f"Unexpected ASR API response format: {data}")
                transcribed_text = ""

            logger.info(f"ASR successful: {transcribed_text[:50]}...")

            return {
                "text": transcribed_text,
                "confidence": 0.95  # HF API doesn't return confidence scores
            }

        except Exception as e:
            logger.error(f"HF Inference API ASR error for {language}: {str(e)}")
            raise


class HFInferenceTranslator(TranslationService):
    """
    Translation service using Hugging Face Inference API.
    Calls cloud-hosted NLLB model for translation.
    """
    
    def __init__(self):
        """Initialize HF Inference translation service."""
        if not HF_HUB_AVAILABLE:
            raise ImportError("huggingface_hub is required. Run: pip install huggingface_hub>=0.20")
        
        self.model_name = settings.MODELS['translation']['model']
        self.lang_codes = settings.MODELS['translation']['lang_codes']
        self.token = settings.HF_TOKEN
        
        if not self.token:
            logger.warning(
                "HF_TOKEN not set. HF Inference API requests may be rate-limited."
            )
        
        # Initialize Inference Client
        self.client = InferenceClient(token=self.token)
    
    def translate(self, text: str, source_lang: str, target_lang: str) -> Dict[str, Any]:
        """
        Translate text using HF Inference API.
        
        Args:
            text: Text to translate
            source_lang: Source language (kikuyu, swahili, english)
            target_lang: Target language (kikuyu, swahili, english)
            
        Returns:
            dict: {"text": str, "confidence": float}
        """
        src_code = self.lang_codes.get(source_lang)
        tgt_code = self.lang_codes.get(target_lang)
        
        if not src_code or not tgt_code:
            raise ValueError(f"Unsupported language pair: {source_lang} -> {target_lang}")
        
        logger.info(f"Translating via HF API (direct): {source_lang}({src_code}) -> {target_lang}({tgt_code})")

        try:
            import requests
            import json

            # Call HF Inference API directly with requests.
            # Uses new router.huggingface.co endpoint (old api-inference.huggingface.co is 410 Gone).
            api_url = f"https://router.huggingface.co/hf-inference/models/{self.model_name}"
            headers = {
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
            }
            payload = {
                "inputs": text,
                "parameters": {
                    "src_lang": src_code,
                    "tgt_lang": tgt_code,
                },
            }

            response = requests.post(api_url, headers=headers, json=payload, timeout=30)
            response.raise_for_status()
            data = response.json()

            # NLLB returns: [{"translation_text": "..."}]
            if isinstance(data, list) and data:
                translated_text = data[0].get("translation_text", "")
            elif isinstance(data, dict):
                # Model still loading — {"error": "...", "estimated_time": ...}
                if "error" in data:
                    raise RuntimeError(f"HF API error: {data['error']}")
                translated_text = data.get("translation_text", "")
            else:
                logger.error(f"Unexpected HF API response: {data}")
                translated_text = ""

            logger.info(f"HF NLLB translation OK: {translated_text[:60]}")

            return {
                "text": translated_text,
                "confidence": 0.92,
                "service_used": "hf_nllb",
            }

        except Exception as e:
            logger.error(f"HF Inference API translation error: {str(e)}")
            raise


class HFInferenceTTS(TTSService):
    """
    Text-to-Speech service using Hugging Face Inference API.
    Calls cloud-hosted MMS-TTS models for speech synthesis.
    """
    
    def __init__(self):
        """Initialize HF Inference TTS service."""
        if not HF_HUB_AVAILABLE:
            raise ImportError("huggingface_hub is required. Run: pip install huggingface_hub>=0.20")
        
        self.model_configs = settings.MODELS['tts']
        self.token = settings.HF_TOKEN
        
        if not self.token:
            logger.warning(
                "HF_TOKEN not set. HF Inference API requests may be rate-limited."
            )
        # Note: InferenceClient.text_to_speech() raises StopIteration for models
        # with no provider mapping. Use requests directly instead.

        # Create temp directory for audio files
        self.temp_dir = os.path.join(settings.MEDIA_ROOT, 'tts_temp')
        os.makedirs(self.temp_dir, exist_ok=True)

    def synthesize(self, text: str, language: str, gender: str = "neutral") -> str:
        """
        Synthesize speech from text using HF Inference API.

        Args:
            text: Text to synthesize
            language: Target language code (kikuyu, swahili, english)
            gender: Voice gender preference (not used by HF API)

        Returns:
            str: Path to generated audio file
        """
        import requests as req

        model_name = self.model_configs.get(language)
        if not model_name:
            raise ValueError(f"No TTS model configured for language: {language}")

        logger.info(f"Synthesizing speech via HF API for {language}: {text[:50]}...")

        try:
            # Call HF router endpoint directly (avoids broken InferenceClient provider mapping).
            # MMS-TTS models are Facebook-hosted and available on the router.
            api_url = f"https://router.huggingface.co/hf-inference/models/{model_name}"
            headers = {
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
            }
            response = req.post(api_url, headers=headers, json={"inputs": text}, timeout=30)

            if response.status_code == 404:
                # Fall back to classic endpoint if model isn't on the router
                api_url = f"https://api-inference.huggingface.co/models/{model_name}"
                response = req.post(api_url, headers=headers, json={"inputs": text}, timeout=30)

            response.raise_for_status()
            audio_bytes = response.content

            # Save audio to file
            audio_filename = f"tts_{language}_{uuid.uuid4().hex[:8]}.flac"
            audio_path = os.path.join(self.temp_dir, audio_filename)
            
            with open(audio_path, 'wb') as audio_file:
                audio_file.write(audio_bytes)
            
            logger.info(f"TTS successful: {audio_path}")
            
            return audio_path
            
        except Exception as e:
            logger.error(f"HF Inference API TTS error for {language}: {str(e)}")
            raise
