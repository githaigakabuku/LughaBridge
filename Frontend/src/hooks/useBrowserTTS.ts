/**
 * Browser TTS hook — plays translated audio.
 *
 * Priority:
 *  1. If the backend returned base64 audio_data, decode and play it (original HF MMS-TTS).
 *  2. Otherwise, fall back to window.speechSynthesis (free, built-in, works for English/Swahili).
 *
 * Kikuyu has no browser speech synthesis voice, so it falls back to English voice
 * reading the text (still useful to hear the translation).
 */

import { useCallback, useRef } from 'react';

// Maps our language codes to BCP-47 tags accepted by SpeechSynthesis
const SPEECH_LANG: Record<string, string> = {
  english: 'en-US',
  swahili: 'sw',    // supported in Chrome/Edge; silently ignored on others
  kikuyu: 'en',     // no Kikuyu voice exists — read text in English accent
};

export function useBrowserTTS() {
  const audioRef = useRef<HTMLAudioElement | null>(null);

  const speak = useCallback((text: string, language: string, audioData?: string | null) => {
    // --- Option 1: play server-generated audio ---
    if (audioData) {
      try {
        // Stop any currently playing audio
        if (audioRef.current) {
          audioRef.current.pause();
          audioRef.current = null;
        }
        const audio = new Audio(`data:audio/flac;base64,${audioData}`);
        audioRef.current = audio;
        audio.play().catch((err) => {
          console.warn('Audio playback failed, falling back to speechSynthesis:', err);
          _synthesize(text, language);
        });
        return;
      } catch {
        // fall through to speechSynthesis
      }
    }

    // --- Option 2: browser Web Speech API ---
    _synthesize(text, language);
  }, []);

  const stop = useCallback(() => {
    if (audioRef.current) {
      audioRef.current.pause();
      audioRef.current = null;
    }
    if ('speechSynthesis' in window) {
      window.speechSynthesis.cancel();
    }
  }, []);

  return { speak, stop };
}

function _synthesize(text: string, language: string) {
  if (!('speechSynthesis' in window)) {
    console.warn('speechSynthesis not supported in this browser');
    return;
  }

  window.speechSynthesis.cancel(); // stop previous utterance

  const utterance = new SpeechSynthesisUtterance(text);
  utterance.lang = SPEECH_LANG[language] ?? 'en-US';
  utterance.rate = 0.9;
  utterance.pitch = 1;

  // Try to pick a matching installed voice; fall back to browser default
  const voices = window.speechSynthesis.getVoices();
  const match = voices.find((v) => v.lang.startsWith(utterance.lang.split('-')[0]));
  if (match) utterance.voice = match;

  window.speechSynthesis.speak(utterance);
}
