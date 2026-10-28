import logging
import time
from typing import List, Optional, Any

try:
    from google import genai
    from google.genai import types
    from google.genai.errors import APIError
    HAS_GENAI = True
except ImportError:
    HAS_GENAI = False
    genai = None
    types = None
    class APIError(Exception):
        pass

logger = logging.getLogger("GeminiPool")

class KeyState:
    def __init__(self, key: str):
        self.key = key
        self.client: Optional[genai.Client] = None
        self.is_exhausted: bool = False
        self.exhausted_until: float = 0.0
        self.request_count: int = 0
        self.error_count: int = 0
        self._init_client()

    def _init_client(self):
        if not HAS_GENAI:
            logger.info(f"Key ...{self.key[-4:] if len(self.key)>=4 else 'MOCK'} loaded in offline/mock mode (google-genai not installed).")
            self.client = "MOCK_CLIENT"
            return

        try:
            self.client = genai.Client(api_key=self.key)
        except Exception as e:
            logger.error(f"Failed to initialize Gemini client for key ending in ...{self.key[-4:]}: {e}")
            self.client = None

    def mark_exhausted(self, cooldown_seconds: float = 60.0):
        self.is_exhausted = True
        self.exhausted_until = time.time() + cooldown_seconds
        self.error_count += 1
        masked = f"...{self.key[-4:]}" if len(self.key) >= 4 else "UNKNOWN"
        logger.warning(f"Key {masked} marked exhausted for {cooldown_seconds}s (HTTP 429/RateLimit)")

    def is_available(self) -> bool:
        if not self.client:
            return False
        if self.is_exhausted:
            if time.time() >= self.exhausted_until:
                self.is_exhausted = False
                masked = f"...{self.key[-4:]}" if len(self.key) >= 4 else "UNKNOWN"
                logger.info(f"Cooldown expired for key {masked}. Restoring to pool.")
                return True
            return False
        return True


class GeminiKeyPoolManager:
    """
    Manages a pool of free-tier Gemini API keys with active failover,
    round-robin rotation, 429 rate-limit backoff, and token constraints.
    """
    def __init__(self, keys: List[str], model: str = "gemini-2.5-flash", max_output_tokens: int = 1024, temperature: float = 0.0):
        self.model = model
        self.max_output_tokens = max_output_tokens
        self.temperature = temperature
        self.keys = [KeyState(k) for k in keys if k]
        self._current_index = 0

        if not self.keys:
            logger.warning("No Gemini API keys provided in pool! Running in mock/offline mode.")

    def add_key(self, key: str):
        if key and not any(k.key == key for k in self.keys):
            self.keys.append(KeyState(key))
            logger.info(f"Added new key ending in ...{key[-4:]} to pool.")

    def _get_next_available_key(self) -> Optional[KeyState]:
        total_keys = len(self.keys)
        if total_keys == 0:
            return None

        # Check round-robin
        for _ in range(total_keys):
            candidate = self.keys[self._current_index]
            self._current_index = (self._current_index + 1) % total_keys
            if candidate.is_available():
                return candidate

        return None

    def analyze_document_in_memory(self, pdf_bytes: bytes, prompt: str) -> str:
        """
        Sends in-memory PDF bytes to Gemini Flash.
        Retries across available keys on 429 RESOURCE_EXHAUSTED.
        """
        if not self.keys:
            raise RuntimeError("Cannot analyze document: No Gemini API keys configured.")

        max_attempts = len(self.keys) * 2
        attempts = 0

        while attempts < max_attempts:
            key_state = self._get_next_available_key()
            if not key_state:
                # All keys in cooldown, wait briefly for earliest cooldown or fail
                logger.error("All Gemini API keys are currently exhausted or cooling down!")
                time.sleep(2.0)
                attempts += 1
                continue

            try:
                key_state.request_count += 1
                masked = f"...{key_state.key[-4:]}"
                logger.info(f"Dispatching AI Vision request to {self.model} using key {masked}...")

                response = key_state.client.models.generate_content(
                    model=self.model,
                    contents=[
                        types.Part.from_bytes(data=pdf_bytes, mime_type="application/pdf"),
                        prompt
                    ],
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json",
                        temperature=self.temperature,
                        max_output_tokens=self.max_output_tokens,
                    )
                )

                if response and response.text:
                    return response.text
                else:
                    raise ValueError("Gemini returned empty response text.")

            except Exception as e:
                err_msg = str(e).lower()
                if "429" in err_msg or "resource_exhausted" in err_msg or "quota" in err_msg:
                    key_state.mark_exhausted(cooldown_seconds=60.0)
                    logger.warning(f"Failover: rotating to next key due to rate limit: {e}")
                    attempts += 1
                    continue
                else:
                    key_state.error_count += 1
                    logger.error(f"Gemini API Error with key {key_state.key[-4:]}: {e}")
                    raise e

        raise RuntimeError("Exceeded maximum retries across all Gemini API keys in pool.")

    def get_pool_status(self) -> List[dict]:
        return [
            {
                "key_ending": f"...{k.key[-4:]}" if len(k.key) >= 4 else "UNKNOWN",
                "is_available": k.is_available(),
                "is_exhausted": k.is_exhausted,
                "requests": k.request_count,
                "errors": k.error_count,
                "cooldown_remaining_sec": max(0, int(k.exhausted_until - time.time()))
            }
            for k in self.keys
        ]
