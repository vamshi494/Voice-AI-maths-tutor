# AI Math Tutor (CBSE Grades 6–10)

An intelligent, real-time multimodal AI Math Tutor tailored for CBSE curriculum. Features voice-driven interaction, dual-lane turn planning, dynamic whiteboard rendering, diagram generation, handwritten pen math, step-by-step Socratic teaching, and interactive doubt clearing.

> **How it works (Zero-Hallucination Multi-LLM Pipeline):** Instead of asking a single model to speak and draw simultaneously (which leads to visual hallucinations), responsibilities are strictly split across specialized agents: **one LLM plans** the pedagogical strategy, **another compiles & validates** geometric diagrams deterministically into verified canvas primitives, and **another teaches** step-by-step with synchronized voice and whiteboard writing.

New here? Start with the [User Guide](docs/USER_GUIDE.md).

---


## Deploy (Docker, one command)

```bash
docker compose -f docker-compose.deploy.yml up -d --build
```

Starts LiveKit, Postgres, the API, the voice worker and the web app (Caddy, HTTPS when
`SITE_ADDRESS` is a domain). Visitors sign in with just their name; the same name restores
their board and lessons. Settings, ports and the microphone/HTTPS rule: `deploy/README.md`.

## Architecture Overview

- **Frontend (`frontend/`)**: React 18, Vite, TypeScript, Konva / React-Konva, RoughJS, Tegaki handwriting canvas, and Lucide icons.
- **Backend (`backend/`)**:
  - **LiveKit Agent Server (`app/main.py`)**: Real-time voice agent worker using LiveKit Agents, Deepgram STT (`nova-2`), ElevenLabs TTS (`eleven_multilingual_v2`), and Silero VAD.
  - **FastAPI HTTP Service (`app/api/server.py`)**: Board persistence, session token generation, and REST endpoints.
  - **Pipeline & Agents (`app/agents/`)**: LangGraph state machine, Groq LLM inference, Dual-Lane Race (`TurnPlan`), and Scene Engine (`compile_scene`).
  - **Database (`app/persistence/`)**: PostgreSQL with SQLAlchemy (asyncpg) storing boards, turns, segments, and scene artifacts.

### Core Agents & Responsibilities

The system coordinates a multi-agent pipeline orchestrated via LangGraph:

1. **Voice Agent (LiveKit Session)** — Manages real-time bidirectional audio streaming, user speech interruption (VAD / barge-in), and plays audio cues.
2. **Turn Planner Agent (`turn_plan.py`)** — Determines conversational strategy for the current turn (e.g. Socratic question, hint, explanation) and issues board operation intents.
3. **Teaching Agent (`teaching.py`)** — Generates spoken pedagogical dialogue, math explanations, and whiteboard writing instructions (`[WRITE]`, `[FOCUS]`).
4. **Scene / Diagram Agent (`scene.py`)** — Compiles geometric diagrams and blackboard layouts into verified canvas operators using a deterministic 28-operator catalog.
5. **Doubt & Context Agent (`doubt_context.py`)** — Resolves student interruptions, gesture annotations ("Mark & Ask"), and photo-based math questions.
6. **Curriculum Outline Agent (`outline.py`)** — Structures multi-page lesson outlines aligned with the CBSE syllabus.

### Architecture & Flow Diagrams

Interactive diagrams of the tutor's architecture, turn lifecycle, and lesson flow.

- 🌐 **Interactive Diagram Hub (GitHub Pages)**: [https://vamshi494.github.io/Voice-AI-maths-tutor/](https://vamshi494.github.io/Voice-AI-maths-tutor/)
- 🖼️ **Visual Gallery & Previews**: [`docs/diagrams/README.md`](docs/diagrams/README.md)

#### Direct Interactive Links
- **System Architecture**: [Web Interactive](https://vamshi494.github.io/Voice-AI-maths-tutor/diagrams/01-system-architecture.html) · [Source File](docs/diagrams/01-system-architecture.html)
- **Turn Lifecycle Sequence**: [Web Interactive](https://vamshi494.github.io/Voice-AI-maths-tutor/diagrams/02-turn-lifecycle-sequence.html) · [Source File](docs/diagrams/02-turn-lifecycle-sequence.html)
- **Chapter Page Flow**: [Web Interactive](https://vamshi494.github.io/Voice-AI-maths-tutor/diagrams/04-chapter-page-flow.html) · [Source File](docs/diagrams/04-chapter-page-flow.html)

---

## Design improvements to be done

- **Scene generation cost** — the scene planner sends the full 28-operator catalogue and rulebook to
  the heavy model for every figure. → A lightweight decision model (TypeSafe Jev / System One) picks
  the operators and a figure template, so the heavy model gets only what it needs, or is skipped.
- **Speech–board sync** — visuals are cued by fuzzy word-transcript matching, which drifts when the
  spoken text and the transcript differ. → Cue board ops from TTS word/character timestamps on the
  audio clock; use the transcript only for captions.
- **Lesson scope** — free-form LLM outlines can drift off-syllabus and vary run to run. → Assemble
  lessons from a reviewed curriculum catalogue (objectives, page/figure templates, practice), with
  the LLM as a fallback.
- **Student interaction** — the board is one-way; the tutor cannot read the student's work. → Add a
  student ink layer; recognised writing is checked symbolically, then answered like a doubt.
- **Reliability and cost** — hallucinated working, repeated calls and no cost visibility. → Verify
  each written line symbolically before showing it, cache repeats, and log tokens/latency per turn.

---

## Prerequisites

- **Python**: 3.12+
- **Node.js**: 18+ or 20+ (with `npm`)
- **Docker & Docker Compose**: For running PostgreSQL
- **API Keys**:
  - [GroqCloud API Key](https://console.groq.com/) (Required for LLM inference)
  - [LiveKit Cloud or Self-Hosted](https://livekit.io/) (URL, API Key, API Secret)
  - [Deepgram API Key](https://deepgram.com/) (For Speech-to-Text)
  - [ElevenLabs API Key](https://elevenlabs.io/) (For Text-to-Speech)

---

## Setup Instructions

### 1. Clone & Configure Environment

Copy `.env.example` to `.env` in the project root:

```bash
cp .env.example .env
```

Open `.env` and fill in your API keys and credentials:

```ini
LIVEKIT_URL=ws://localhost:7880
LIVEKIT_API_KEY=devkey
LIVEKIT_API_SECRET=secret

OPENCODE_API_KEY=oc_sk_...
OPENCODE_BASE_URL=https://opencode.ai/zen/go/v1

GROQ_API_KEYS=gsk_...,gsk_...,gsk_...
GROQ_API_KEY=gsk_...
OPENROUTER_API_KEY=sk-or-v1-...

MODEL_MAIN=opencode-go/deepseek-v4.1-flash
OPENCODE_REASONING_EFFORT=none
LLM_MAX_TOKENS=8192
MODEL_FAST=openai/gpt-oss-20b
MODEL_CLASSIFIER=openai/gpt-oss-safeguard-20b
MODEL_VISION=llama-3.2-11b-vision-preview

DEEPGRAM_API_KEY=your_deepgram_key
DEEPGRAM_MODEL=nova-3
DEEPGRAM_LANGUAGE=en-IN

ELEVENLABS_API_KEY=your_elevenlabs_key
ELEVENLABS_VOICE_ID=your_voice_id
ELEVENLABS_MODEL=eleven_multilingual_v2

DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5434/math_tutor

# Toggle dual-lane turn planning (true = dual race, false = single primary lane)
TURN_PLAN_DUAL_LANE=false
```

---

### 2. Start PostgreSQL Database

Start the PostgreSQL database container via Docker Compose:

```bash
docker compose up -d
```

Verify that PostgreSQL is healthy:

```bash
docker compose ps
```

---

### 3. Backend Setup

Activate your virtual environment and install dependencies:

```bash
# From project root
source .venv/bin/activate
```

#### Run the FastAPI REST Server
The FastAPI server handles board persistence and LiveKit token generation:

```bash
source .venv/bin/activate
PYTHONPATH=backend uvicorn app.api.server:app --host 0.0.0.0 --port 8000 --reload
```

The API docs will be available at: `http://localhost:8000/docs`.

#### Run the LiveKit Voice Agent Worker
The agent worker connects to your LiveKit room, performs Silero VAD prewarming, and handles real-time voice sessions:

```bash
source .venv/bin/activate
python backend/app/main.py dev
```

---

### 4. Frontend Setup

In a new terminal window:

```bash
cd frontend
npm install
npm run dev
```

The frontend development server will launch at:
`http://localhost:3000`

---

## Running Tests

### Backend Tests (Pytest)
Run the offline unit and integration tests:

```bash
source .venv/bin/activate
PYTHONPATH=backend python -m pytest backend/tests -q --ignore=backend/tests/spikes
```

### Integration Spikes
Run the LiveKit and pipeline integration verification scripts:

```bash
source .venv/bin/activate
PYTHONPATH=backend python backend/tests/spikes/spike_a.py
PYTHONPATH=backend python backend/tests/spikes/spike_b.py
```

### Frontend Tests (Vitest)
Run the React and whiteboard unit tests:

```bash
cd frontend
npm test
```

### Frontend Production Build
Validate TypeScript types and build bundle:

```bash
cd frontend
npm run build
```

---

## Project Structure

```
├── backend/
│   ├── app/
│   │   ├── agents/           # LangGraph orchestration, dual-lane race, nodes
│   │   ├── api/              # FastAPI server & routes (/token, /boards)
│   │   ├── contracts/        # Pydantic schemas (TurnPlan, SceneDocument, IR)
│   │   ├── persistence/      # SQLAlchemy models, async DB engine, repository
│   │   ├── scene_engine/     # Scene compiler, repair loop, chalk/draw ops
│   │   ├── state_machine/    # Conversation state manager (ConvState, ConvEvent)
│   │   ├── tutor/            # Arithmetic reconciler, SymPy solver, classifiers
│   │   ├── voice/            # LiveKit agent session & voice integration
│   │   ├── config.py         # App configuration & load_dotenv()
│   │   └── main.py           # LiveKit CLI worker entrypoint
│   └── tests/                # Unit, e2e, and spike tests
├── frontend/
│   ├── src/
│   │   ├── whiteboard/       # Canvas, layout engine, layers, animations
│   │   ├── components/       # InputBar, AudioWaveform, PhotoUploadModal
│   │   ├── livekit/          # RPCs, data sync, and event handlers
│   │   ├── types/            # TypeScript interfaces and contracts
│   │   └── App.tsx           # Main application view
│   └── package.json
├── docker-compose.yml        # Local PostgreSQL container configuration
└── .env.example              # Sample environment variables
```

---

## Common Troubleshooting

1. **`ModuleNotFoundError: No module named 'sqlalchemy'`**:
   Ensure you activate the project virtual environment before running python commands: `source .venv/bin/activate`.
2. **PostgreSQL connection refused**:
   Ensure the docker container is running via `docker compose up -d`.
3. **Microphone or Audio Permissions**:
   When using voice features in the browser, ensure microphone permissions are granted for `http://localhost:3000`.
