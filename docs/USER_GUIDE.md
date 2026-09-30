# AI Math Tutor — User Guide

## What it is

A voice tutor, **Teacher Vamshi**, for CBSE/NCERT mathematics, classes 6–10. It talks you through a
problem while writing the working on a chalkboard (left) and drawing checked figures (right). It
speaks Indian English and is meant to be used one student at a time.

## Getting started

1. Open the app and enter your name. There is no password — the name is what identifies you, and the
   same name next time brings back your board, your session memory and an unfinished lesson.
2. Allow the microphone when the browser asks. If sound is blocked, tap the banner to enable audio.
3. Ask a question by **voice**, by **typing** (press *Ask*), or from a **photo** (camera button). To
   have a topic taught as a multi-page chapter, type it and press *Teach a topic*.

## What a student can do

- **Speak** a question or a doubt at any time (Indian English). Short sounds such as "hmm" or "okay"
  do not interrupt the tutor; three or more words do.
- **Type** a question in the input bar and press *Ask*. While a lesson is on the board, typed text is
  treated like speech: a question about what is on the board is a doubt, and a new problem starts a
  new lesson.
- **Mark the board with the pen**: circle, underline, strike, scribble, or point at a line or a
  figure part, then press *Explain this* — optionally adding what is unclear in the text box.
  Touching the board pauses the tutor while you write.
- **Upload a photo** of a textbook question: the recognised text appears for you to check and edit
  before it is sent.
- **Pause and resume** with the pause button or the **Space** bar. Use **Continue lesson** after a
  doubt to pick up at the exact step where the lesson stopped.
- **Change the speech speed** between 0.8x, 1.0x and 1.2x. This works with the ElevenLabs voice;
  with a different voice the control reports that it cannot and reverts.
- **Mute your microphone** and keep typing.
- **Start a new board** or **End class** (the tutor says goodbye and closes the session).
- **Lesson notes**: open the notes drawer to list earlier pages, preview any page read-only, and
  **Export notes** as a PDF.

## What the tutor does

- Teaches one question in about ten short steps: one or two spoken sentences per step, each with one
  line of working written on the left.
- Draws checked figures on the right — points, segments, lines, rays, triangles, polygons,
  rectangles, circles, arcs, angle and right-angle marks, equal-length ticks, dimensions, coordinate
  axes, graphs of functions and number lines — and **reveals each part in step with its speech**,
  highlighting the part it is naming.
- Answers doubts on the same board, or draws a new figure on a fresh page when the doubt needs a
  different case; your lesson page comes back when you continue.
- Redirects politely when you go off topic, then repeats the sentence it was saying.
- Remembers the session — recent exchanges plus page and lesson summaries — so doubts about earlier
  parts are answered in context. The board, memory and *Continue* are restored after a reload or a
  new connection.
- Teaches a whole chapter topic page by page, carrying key figures forward and pausing briefly
  between pages.
- Lays out up to four figures or tables on one page.
- Recovers from reloads, blocked audio and voice failures: if speech synthesis fails, the turn
  switches to captions instead of stalling.
- If the figure planner cannot produce a usable figure, the tutor teaches the turn in words
  alone.

## User flow

1. Open the app, enter your name, allow the microphone, and tap the banner if audio is blocked.
   Teacher Vamshi greets you.
2. Ask by voice, type and press *Ask*, upload a photo and confirm the recognised text, or type a
   topic and press *Teach a topic*.
3. The tutor shows a thinking indicator, then speaks step by step; each line of working appears on
   the left as it is said, and figure parts appear as they are named.
4. At any time: say "wait, why…", mark the board and press *Explain this*, or type your doubt. The
   tutor pauses, answers, then waits.
5. Say "got it, continue" or press *Continue lesson* to resume.
6. For a topic, the tutor moves through its pages with a short pause between them; use *Lesson notes*
   to look back at any page.
7. Press *End class* (or say "end the class") to finish. Sign in with the same name later to restore
   your board and unfinished lesson; *New board* starts a fresh one.

## What it cannot do

- **Subjects and grades**: mathematics only, CBSE/NCERT classes 6–10, taught in English.
- **Figures**: no 3D solids (cones, cylinders and spheres are explained in words), no statistics
  charts (bar graphs, histograms, pie charts), and no free-hand tutor sketches. Up to four figures or
  tables per page, each at least 240×200 px.
- **Pen input** understands only the marking gestures above; it does not read handwriting or
  recognise shapes you draw.
- **Sign-in** is a display name only — no passwords, accounts or teacher dashboard — and one student
  per session.
- **Connectivity**: an internet connection is required; speech, voice and reasoning run on cloud
  services, so there is no offline mode.
- **Photos**: one question per photo; blurry photos are rejected with a request to type the question.

## Known limitations

- If the tab is muted or the output device changes, the board keeps drawing; the browser gives no
  signal that audio is not being heard.
- Opening the same board in two tabs: the newest tab takes over and the older one is told so.
- A page reload restores the board, memory and *Continue*, but the live audio reconnects fresh.
- Voice quality and latency depend on the configured cloud providers.

## Running it yourself

Copy `.env.example` to `.env` and fill in your provider keys (LiveKit, an LLM gateway, Deepgram for
speech-to-text, ElevenLabs for text-to-speech). The feature flags in `.env` enable the chapter
pipeline, session memory and restore, captions, parked-run resume, outbox resync and page commits;
they are on in the shipped example. For a one-command Docker deployment, see `deploy/README.md`.
