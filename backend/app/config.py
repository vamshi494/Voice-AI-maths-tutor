# app/config.py
from dotenv import load_dotenv

load_dotenv()

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # LiveKit
    LIVEKIT_URL: str = Field(default="", description="LiveKit server WebSocket URL")
    LIVEKIT_API_KEY: str = Field(default="", description="LiveKit API Key")
    LIVEKIT_API_SECRET: str = Field(default="", description="LiveKit API Secret")

    # LLM Providers (OpenCode Go, Groq & OpenRouter)
    OPENCODE_API_KEY: str = Field(default="", description="OpenCode Go API Key")
    OPENCODE_BASE_URL: str = Field(default="https://opencode.ai/zen/go/v1", description="OpenCode Go Base URL")
    GROQ_API_KEY: str = Field(default="", description="GroqCloud API Key")
    GROQ_API_KEYS: str = Field(default="", description="Comma-separated Groq API keys for round-robin")
    OPENROUTER_API_KEY: str = Field(default="", description="OpenRouter API Key")
    MODEL_MAIN: str = Field(default="opencode-go/deepseek-v4.1-flash", description="TurnPlan, ProblemIR, scene, lesson teaching (OpenCode, Groq or OpenRouter)")
    MODEL_FAST: str = Field(default="openai/gpt-oss-20b", description="Doubt teaching (fallback: MODEL_MAIN)")
    MODEL_CLASSIFIER: str = Field(default="openai/gpt-oss-safeguard-20b", description="Interrupt and figure-need classifiers")
    MODEL_VISION: str = Field(default="qwen/qwen3.8-27b", description="Photo -> question text (OCR/Vision)")

    # OpenCode Zen tuning. Zen's chat-completions models (deepseek-v4.1-flash) reason by default;
    # "none" turns thinking off for lower latency. Zen accepts
    # none|minimal|low|medium|high|xhigh|max and silently ignores `thinking`.
    OPENCODE_REASONING_EFFORT: str = Field(
        default="none", description="reasoning_effort for OpenCode Zen chat-completions models"
    )
    # OpenCode Zen /responses models (muse-spark) reject "none"; "minimal" is their lowest
    # accepted effort (verified live 2026-09-27: 1.4 s / 20 reasoning tokens vs 4.2 s / 178 at low).
    OPENCODE_RESPONSES_REASONING_EFFORT: str = Field(
        default="minimal", description="reasoning.effort for OpenCode Zen /responses models"
    )
    # Groq openai/gpt-oss-* cannot turn reasoning off: reasoning_effort must be low|medium|high
    # ("none" is HTTP 400). Without the parameter Groq uses medium, which on long teaching prompts
    # spent the whole output budget on reasoning and returned no content. Any other
    # value is clamped to "low". Groq qwen3 models always get reasoning_effort "none".
    GROQ_REASONING_EFFORT: str = Field(
        default="low", description="reasoning_effort for Groq openai/gpt-oss-* models (low|medium|high)"
    )
    # One retry target when a reply carries ONLY reasoning and no usable output. Empty = MODEL_MAIN
    # (OpenCode deepseek with reasoning off). Same model as the failing one -> retried once with
    # the provider's minimum reasoning.
    LLM_REASONING_FALLBACK_MODEL: str = Field(
        default="", description="model for the single reasoning-only retry (empty = MODEL_MAIN)"
    )
    # Upper bound on generated tokens. Not a target: a generous guard against a runaway reply
    # that would blow the latency budget. OpenAI-compatible field name is `max_tokens`.
    LLM_MAX_TOKENS: int = Field(default=8192, description="Max generated tokens per LLM call")

    # Voice Providers
    DEEPGRAM_API_KEY: str = Field(default="", description="Deepgram STT API Key")
    DEEPGRAM_MODEL: str = Field(default="nova-2", description="Deepgram model")
    DEEPGRAM_LANGUAGE: str = Field(default="en-IN", description="Tune for Indian English")

    STT_PROVIDER: str = Field(default="deepgram", description="STT provider")
    TTS_PROVIDER: str = Field(default="elevenlabs", description="TTS provider: elevenlabs or deepgram")
    ELEVENLABS_API_KEY: str = Field(default="", description="ElevenLabs API Key")
    ELEVENLABS_VOICE_ID: str = Field(default="", description="ElevenLabs voice ID")
    ELEVENLABS_MODEL: str = Field(default="eleven_multilingual_v2", description="ElevenLabs model")

    # Persistence
    DATABASE_URL: str = Field(
        default="postgresql+asyncpg://postgres:postgres@localhost:5432/math_tutor",
        description="Async PostgreSQL database URL",
    )
    SQLITE_FALLBACK_PATH: str = Field(
        default="",
        description="Absolute path of the shared SQLite fallback file (default: repo-root math_tutor.db)",
    )

    # Observability
    LOGFIRE_TOKEN: str = Field(default="", description="Logfire token")
    LANGSMITH_API_KEY: str = Field(default="", description="LangSmith API Key")
    LANGSMITH_PROJECT: str = Field(default="ai-math-tutor", description="LangSmith project")
    LANGSMITH_TRACING: bool = Field(default=True, description="Enable LangSmith tracing")
    LANGSMITH_USE_DAEMON: bool = Field(default=True, description="Use daemon threads for LangSmith background worker")

    # Pipeline & Tuning
    TURN_PLAN_DUAL_LANE: bool = Field(
        default=False,
        description="When false, run a single primary lane; when true, race the two TurnPlan lanes",
    )
    TURN_PLAN_PEER_GRACE_MS: int = Field(default=3000, description="Grace window in milliseconds for the second TurnPlan lane")
    TURN_PLAN_LANE_TIMEOUT_S: float = Field(default=25.0, description="TurnPlan lane timeout in seconds")
    SCENE_MAX_CANDIDATES: int = Field(default=3, description="Maximum scene repair candidates")
    CLASSIFIER_TIMEOUT_S: float = Field(default=3.0, description="Classifier timeout in seconds")
    INTERRUPT_MIN_WORDS: int = Field(
        default=3,
        description="Barge-in needs at least 3 words; backchannels never interrupt",
    )
    ALLOW_SUB3_INTERRUPT: bool = Field(
        default=False,
        description="Explicit escape hatch for owner experiments: allow values below 3",
    )
    VAD_MIN_SPEECH_DURATION: float = Field(
        default=0.1,
        description="Minimum speech duration in seconds for VAD (filters clicks/breath)",
    )
    VAD_MIN_SILENCE_DURATION: float = Field(
        default=1.5,
        description="Minimum silence duration in seconds for VAD before marking turn completion",
    )
    IDLE_QUESTION_SETTLE_MS: int = Field(
        default=2000,
        description="Debounce window in ms to accumulate spoken question chunks in IDLE before starting turn",
    )
    DOUBT_SETTLE_TIMEOUT_MS: int = Field(default=2500, description="Doubt audio/pen settle timeout in milliseconds")
    SETTLE_CAP_MS: int = Field(default=4000, description="settle hard cap from the first doubt input")
    HISTORY_EXCHANGES: int = 3
    STEP_BUDGET_PROBLEM: int = Field(default=10, description="steps asked of a single-problem lesson")
    STREAM_IDLE_TIMEOUT_S: float = Field(default=20.0, description="teaching stream: max gap between chunks")
    STREAM_TOTAL_TIMEOUT_S: float = Field(default=90.0, description="teaching stream: max total")
    IMAGE_MAX_EDGE_PX: int = 1024
    IMAGE_JPEG_QUALITY: int = 80
    TTS_SPEEDS: list[float] = Field(default_factory=lambda: [0.8, 1.0, 1.2])

    # Feature flags: each defaults to False and is enabled when its feature ships.
    FEATURE_OUTBOX: bool = Field(default=False, description="ring-buffer resync for tutor.events")
    FEATURE_PARKED_RESUME: bool = Field(default=False, description="park lesson runs across doubts")
    FEATURE_CAPTIONS: bool = Field(default=False, description="caption mode when TTS fails")
    FEATURE_MEMORY: bool = Field(default=False, description="session memory + persisted board state")
    FEATURE_REHYDRATE: bool = Field(default=False, description="restore the board, memory and checkpoint on join")
    FEATURE_PAGE_COMMIT: bool = Field(default=False, description="publish page_commit instead of the legacy diagram_commit")
    FEATURE_CHAPTERS: bool = Field(default=False, description="outline + multi-page chapter lessons")
    # Chapter pipeline.
    MAX_CHAPTER_PAGES: int = Field(default=6, description="outline clamp on pages per lesson")
    PLANNER_BLOCK_BUDGET: int = Field(default=2, description="figure/table/text blocks the outline may declare per page")
    MAX_STICKY_BLOCKS: int = Field(default=2, description="sticky blocks per lesson")
    STEP_BUDGET_PAGE_MIN: int = Field(default=6, description="chapter page step budget lower bound")
    STEP_BUDGET_PAGE_MAX: int = Field(default=10, description="chapter page step budget upper bound")
    STEP_BUDGET_PAGE_DEFAULT: int = Field(default=8, description="chapter page step budget default")
    OUTLINE_TIMEOUT_S: float = Field(default=6.0, description="outline LLM call timeout")
    PAGE_GAP_MS: int = Field(default=1200, description="pause between chapter pages")
    # Parked runs, producers, outbox, captions and scene limits.
    MAX_PARKED_RUNS: int = Field(default=2, description="parked PageRuns kept (oldest discarded)")
    MAX_CONCURRENT_PRODUCERS: int = Field(default=2, description="LLM teaching streams at once")
    OUTBOX_CAPACITY: int = Field(default=256, description="server event ring buffer entries")
    OUTBOX_MAX_BYTES: int = Field(default=4_194_304, description="server event ring buffer bytes")
    CAPTION_WPS: float = Field(default=2.6, description="caption clock words per second at speed 1.0")
    SCENE_CANDIDATE_TIMEOUT_S: float = Field(default=35.0, description="one scene LLM call")
    SCENE_JOIN_BUDGET_S: float = Field(default=45.0, description="teaching starts without the figure after this")
    BOARD_PREP_LINE_AFTER_MS: int = Field(default=4000, description="the consumer speaks BOARD_PREP_LINE if no step exists yet")
    # Heard progress flushes the board document mid-turn (at most once per window) so a
    # refresh mid-lesson finds the board and a resumable checkpoint.
    BOARD_FLUSH_THROTTLE_MS: int = Field(default=1500, description="min ms between mid-turn board_state flushes")
    # Board lease.
    LEASE_TTL_S: int = Field(default=30, description="board lease lifetime in seconds")
    LEASE_RENEW_S: int = Field(default=10, description="board lease renew interval in seconds")
    LEASE_WAIT_S: int = Field(default=3, description="seconds a new job waits before stealing the lease")
    # Layout engine.
    MAX_BLOCKS_PER_PAGE: int = Field(default=4, description="layout engine hard cap on blocks per page")
    MIN_CELL_W: int = Field(default=240, description="legibility minimum cell width (px)")
    MIN_CELL_H: int = Field(default=200, description="legibility minimum cell height (px)")
    ASPECT_MIN: float = Field(default=0.5, description="cell width:height clamp lower bound")
    ASPECT_MAX: float = Field(default=2.0, description="cell width:height clamp upper bound")
    LAYOUT_PADDING: int = Field(default=24, description="px between the region edge and the block grid")
    LAYOUT_GUTTER: int = Field(default=24, description="px between grid cells")

    @model_validator(mode="after")
    def _enforce_min_words(self):
        if self.INTERRUPT_MIN_WORDS < 3 and not self.ALLOW_SUB3_INTERRUPT:
            object.__setattr__(self, "INTERRUPT_MIN_WORDS", 3)
        return self


settings = Settings()
