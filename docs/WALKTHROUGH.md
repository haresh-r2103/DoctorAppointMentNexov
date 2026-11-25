# Nexov, step by step

This document explains how the project was built, in the order it was built, and what happens at each step. Read it top to bottom once and you should be able to explain any line of the code.

---

## The big picture

```
 Browser (React)                                   Python server (FastAPI + Pipecat)
┌───────────────────────────┐                     ┌──────────────────────────────────────────┐
│ Dashboard                 │  GET /api/doctors   │ app.py                                   │
│  Rota, Appointments  ◀────┼──GET /api/appts ────┤   REST routes ─── db.py (SQLite)         │
│                      ◀────┼──SSE /api/events ───┤   events.py (pub/sub) ◀── tools.py       │
│                           │                     │                                          │
│ Phone (useCall.ts)        │  POST /api/offer    │   WebRTC signalling ──▶ bot.py           │
│  PipecatClient  ──────────┼──(SDP offer/answer)─┤      one Pipecat pipeline per call       │
│  mic audio      ══════════╪══ WebRTC audio ═════╪══▶  STT → VAD/turn → Gemini → TTS ══╗    │
│  Mira's voice   ◀═════════╪═════════════════════╪════════════════════════════════════╝    │
│  RTVI events    ◀─────────┼── data channel ─────┤      (transcripts, tool calls, metrics)  │
└───────────────────────────┘                     └──────────────────────────────────────────┘
```

There are two separate channels to the browser, and it helps to keep them apart:

1. **WebRTC** carries the call: audio both ways, plus a data channel for RTVI messages (Pipecat's protocol for telling a client what the bot is doing).
2. **HTTP + Server-Sent Events** carry the dashboard data. This channel doesn't depend on the call, so the dashboard also updates in a second tab that isn't on a call.

---

## Step 1: The data layer (`server/db.py`)

**What it does:** stores doctors, slots and appointments in SQLite.

- **Tables:** `doctors`, `slots` (one row per 30-minute opening, with an `is_booked` flag) and `appointments` (a patient holding a slot, identified by name plus a keyed phone hash and the last four digits, with status `confirmed` or `cancelled`).
- **Seeding:** on startup, if the rota doesn't begin today, it is rebuilt for the next 7 days from each doctor's working days and hours. About 16% of slots are pre-booked by fake "walk-in" patients so the board looks like a real clinic day. The random generator is seeded with the date, so the same day always looks the same.
- **Booking is atomic:** `UPDATE slots SET is_booked=1 WHERE id=? AND is_booked=0`. If `rowcount` is 0, someone else got the slot first, so we roll back. This is how double-booking is prevented without a separate "check then write" race.
- **Cancelling** flips the appointment to `cancelled` and reopens the slot in the same transaction.
- **Booking references** like `NX-7K2P` use an alphabet without 0/O/1/I/L, because Mira reads them aloud.

**Why SQLite?** The brief allowed in-memory data, JSON or SQLite. SQLite gives real transactions, survives restarts, and is still a single file.

---

## Step 2: The tools (`server/tools.py`)

Each tool has three layers, and the layering is deliberate:

1. **A plain Python function** with the business logic, for example `book_appointment(patient_name, patient_name_confirmed, phone_number, phone_number_confirmed, doctor, date, time, ...)`. It returns a dict. It knows nothing about audio or LLMs, which is why `tests/test_tools.py` can test it directly.
2. **A `FunctionSchema`:** the tool's name, a description and a JSON schema for its arguments. Pipecat converts this into Gemini's function-declaration format. Gemini reads the descriptions to decide *when* to call a tool and *what* to pass.
3. **A Pipecat handler** (`_handler`) that receives `FunctionCallParams`, calls the function with `params.arguments`, and returns the result with `await params.result_callback(result)`. Pipecat adds the result to the conversation context and asks Gemini to continue. That second LLM pass is where Mira turns `{"ok": true, ...}` into a spoken sentence.

**The tools:**

| Tool | What it does |
|---|---|
| `check_doctor_availability(doctor?, date?)` | Free slots, grouped by day (max 3 days × 8 slots so the answer stays short enough to speak) |
| `book_appointment(patient_name, patient_name_confirmed, phone_number, phone_number_confirmed, doctor, date, time, reason?)` | Validates and books, then publishes an `appointment.booked` event |
| `cancel_appointment(patient_name, patient_name_confirmed, phone_number, phone_number_confirmed, appointment_ref?, doctor?, date?)` | Finds the caller's booking by name; if there are several, it returns the list so Mira asks which one |
| `find_my_appointments(...)` | Extra: "when is my appointment?" |

**Being forgiving with voice input.** Speech-to-text is messy, so the tools normalise what they get:
- `normalize_name("  asha   KAPOOR ")` gives `Asha Kapoor`; `normalize_phone("+91 98765-43210")` gives `9876543210`.
- `parse_time` accepts `"2 pm"`, `"14:00"`, `"2:30 PM"` and a bare `"3"` (treated as 3 pm in clinic hours). The prompt asks Gemini for 24-hour `HH:MM`, so this is only a safety net.
- `match_doctor` fuzzy-matches names *and* specialties/keywords, so "Dr. Mehra" (an STT typo), "heart doctor" and "skin specialist" all resolve correctly.

**The identity guardrail.** The brief says the agent must read the name back before booking or cancelling. The prompt asks for this, but prompts can be ignored, so the code also enforces it. Each patient tool has `patient_name_confirmed` and `phone_number_confirmed` booleans. If either is false, the tool returns `identity_not_confirmed` with the exact name and digit-by-digit number to read back (`"Asha Kapoor"`, `"9 8 7 6 5, 4 3 2 1 0"`), and Gemini has to ask the caller before it can try again.

**Errors are data, not exceptions.** Every failure returns `{"ok": false, "error": "...", "message": "..."}`, often with alternatives (`nearest_free_slots_that_day`). Mira can then say "That one's gone, but 11 or 11:30 are free" instead of failing. Unexpected exceptions are caught in `_handler`, so a bug can never kill the call.

---

## Step 3: The prompt (`server/prompts.py`)

The prompt is built **per call** with today's date, time and weekday, plus the roster generated from `db.DOCTORS`. That's why "this Friday" resolves to the right date and the prompt can never disagree with the database. The rules cover:
- **Speech style:** one or two short sentences, no lists or markdown (it's all spoken), times said naturally, at most three slots offered.
- **The identity procedure**, step by step.
- **Flow:** always check availability before offering times, sum up before booking, read the reference letter by letter, and no medical advice (emergency → 112).

---

## Step 4: The pipeline (`server/bot.py`)

`run_bot(connection)` runs once per call. In Pipecat, everything is a **frame** (audio chunk, transcript, text token, function call…) flowing through a chain of **processors**:

```python
Pipeline([
    transport.input(),     # 1
    stt,                   # 2
    user_aggregator,       # 3
    llm,                   # 4
    tts,                   # 5
    transport.output(),    # 6
    assistant_aggregator,  # 7
])
```

1. **`transport.input()`** receives the caller's audio from WebRTC as raw PCM `InputAudioRawFrame`s.
2. **`ElevenLabsRealtimeSTTService`** streams that audio over a websocket to ElevenLabs Scribe and emits interim and final `TranscriptionFrame`s. By default, Pipecat's VAD decides when to "commit" a segment, so the final transcript arrives right when you stop talking.
3. **`user_aggregator`** (from `LLMContextAggregatorPair`) is where **Silero VAD** runs (the bonus requirement). VAD detects speech start and stop. Pipecat also loads its Smart Turn model, which looks at the audio to judge whether you've actually *finished your turn* or just paused. When the turn ends, the aggregator adds your words to the `LLMContext` and triggers the LLM. VAD also enables **barge-in**: if you start talking while Mira speaks, her audio is cut and the pipeline is interrupted.
4. **`GoogleLLMService`** (Gemini) streams text tokens. When it decides to call a tool, Pipecat runs our handler, adds the result to the context and runs Gemini again.
5. **`ElevenLabsTTSService`** streams text in and audio out over a websocket. It also returns **word timestamps**, which is what lets the UI highlight each word as Mira says it.
6. **`transport.output()`** sends Mira's audio back over WebRTC.
7. **`assistant_aggregator`** records what Mira *actually said* into the context. If you interrupted her halfway, only the spoken half is stored, so the model doesn't think you heard everything.

**Other pieces in `bot.py`:**
- `tools.register_tools(llm)` links each tool name to its handler, and `LLMContext(tools=tools.TOOLS)` gives Gemini the schemas.
- `on_function_calls_started` queues a short filler ("Let me check the diary.") straight into TTS, so there's no dead air during the extra LLM round trip.
- `PipelineWorker(..., rtvi_observer_params=...FULL)`: RTVI is on by default in Pipecat 1.x. By default, tool-call events hide their arguments and results for security. We raise the level to `FULL` because our own UI shows them in the tool inspector.
- `on_client_connected` adds a "greet the caller" instruction and pushes an `LLMRunFrame`, so Mira speaks first, like a real receptionist.
- `on_client_disconnected` cancels the runner, which tears down the STT and TTS websockets.
- `idle_timeout_secs=180` ends abandoned calls.

---

## Step 5: The server (`server/app.py`)

- **`POST /api/offer`**: WebRTC signalling. The browser sends an SDP offer. `SmallWebRTCRequestHandler` creates a `SmallWebRTCConnection` (built on aiortc), returns the SDP answer, and calls our callback, which starts `run_bot(connection)` as a background task. One request means one call means one pipeline. Before any of this, it checks that the API keys exist and returns a readable error if they don't.
- **`PATCH /api/offer`**: trickle ICE candidates for that connection.
- **`GET /api/doctors`** and **`/api/appointments`**: dashboard data. Slots earlier than now today are filtered out.
- **`GET /api/events`**: Server-Sent Events. Each open dashboard gets an `asyncio.Queue` in `events.py`. When `tools.py` books or cancels, `events.publish()` puts one JSON message in every queue, and the stream sends it as `data: {...}`. A keep-alive comment every 15 s stops proxies from closing idle streams. SSE was chosen over WebSockets because the data only flows server → browser and SSE reconnects automatically.
- **`POST /api/reset`**: re-seed the rota (handy before recording the demo).
- **Static files:** if `web/dist` exists, the built dashboard is served on the same port.

---

## Step 6: One call, start to finish

This is what happens, in order, when you press **Call Mira**:

1. `useCall.start()` resets state, shows the phone in the **ringing** state and starts the synthesised Indian double-ring.
2. It calls `/api/health`. If a key is missing, the phone shows exactly which one.
3. A `PipecatClient` with a `SmallWebRTCTransport` is created and `connect({webrtcRequestParams: {endpoint: '/api/offer'}})` runs. The browser asks for the mic, creates the offer and POSTs it.
4. The server answers. `run_bot` builds the pipeline and audio starts flowing.
5. The bot signals ready (RTVI `bot-ready`). After at least one full ring (1.3 s), the ring stops, a connect chirp plays and the phone switches to **connected**: the timer starts and the monitor traces start moving.
6. `on_client_connected` makes Mira greet you. Her text arrives as `bot-output` messages *before* the audio, and word-progress messages arrive as each word is spoken, so the bubble fills in bright as she talks.
7. You speak. VAD fires `user-started-speaking` (the pipeline view lights "Your mic / Turn detection / Speech to text"), interim transcripts appear faint in your bubble, and then the final text arrives.
8. Your turn ends. Gemini decides to call `check_doctor_availability`. The client receives `llm-function-call-started`, then `-in-progress` (with arguments), then `-stopped` (with the result). The phone shows a receipt ("Checked the diary for Dr. Mehta") with the times it found, and the tool inspector logs the raw JSON. Meanwhile TTS says "Let me check the diary."
9. Gemini speaks the options. You choose one and say your number. Mira reads it back and you say yes.
10. Gemini calls `book_appointment(..., patient_name_confirmed=true)`. The slot is booked in SQLite and `events.publish('appointment.booked')` runs.
11. Two things happen in parallel:
    - **In the phone:** the `-stopped` event carries the result, so a booking card drops in and a chime plays. The caller's number appears in the phone header.
    - **On the dashboard:** the SSE message arrives. `useClinic` refetches, the rota jumps to that day and pulses the slot, and the Appointments list switches to "Booked by Mira" and flashes the new row.
12. You press End (or `Esc`). The client disconnects, a hang-up tone plays and the phone shows **Call ended** with the duration and a summary ("Booked NX-7K2P with Dr. Kabir Mehta"). On the server, `on_client_disconnected` cancels the pipeline.

---

## Step 7: The frontend, piece by piece

### `lib/useCall.ts`: RTVI events → UI state
This is the only file that talks to Pipecat. It maps callbacks to state:

| RTVI callback | Used for |
|---|---|
| `onBotReady` | ringing → connected |
| `onTrackStarted` | plays Mira's audio track in an `<audio>` element; creates Web Audio analysers for both voices |
| `onUserStartedSpeaking` / `Stopped` | stage = hearing / thinking |
| `onBotStartedSpeaking` / `Stopped` | stage = speaking / listening |
| `onUserInput` | caller transcript (interim and final) |
| `onBotOutput` | Mira's transcript segments + `spoken_progress` for word highlighting |
| `onBotInterrupted` | strike out what she didn't get to say |
| `onLLMFunctionCall{Started,InProgress,Stopped}` | tool receipts, cards, inspector, chimes |
| `onMetrics` | time-to-first-byte for STT, LLM and TTS |
| `onError` | plain-language errors ("ElevenLabs rejected the API key") |

Two details worth knowing:
- The transcript updates many times per second, so it lives in a `useRef` with a version counter that triggers re-renders, instead of copying arrays on every word.
- `llm-function-call-started` has no `tool_call_id`, so the call is parked in a pending queue and the following `in-progress` event (which has the id) claims it.

### `components/Phone.tsx`: the call screen
Three screens (ringing, connected, ended/failed) cross-fade in with a blur. The connected screen has the header (avatar glows while Mira speaks, caller number once confirmed, timer), the monitor, the transcript, a status line ("Mira is thinking") and the mute and end buttons. Keyboard: `M` mutes and `Esc` ends the call.

### `components/Trace.tsx`: the voice monitor
Each animation frame it reads the time-domain samples from each analyser, takes the strongest signed sample, and draws it at a sweeping cursor that erases a gap ahead of itself, like a bedside ECG monitor. Lead 1 is Mira (mint) and lead 2 is you (blue). The readout on the right shows live Hear/Think/Speak latency in ms from Pipecat's metrics.

### `components/Transcript.tsx`
Caller bubbles, Mira bubbles (spoken part bright, rest faint, interrupted rest struck out), tool receipts, and the **appointment card**: the doctor's line colour, big date, time, room, reference, and a dashed tear line like a paper appointment slip.

### `lib/api.ts`: `useClinic`
Fetches doctors and appointments, opens an `EventSource('/api/events')`, and on each event refetches and records a `flash` `{kind, apptId, slotId, at}`. The `at` timestamp goes into React `key`s, so the CSS highlight animation restarts even if the same row changes twice.

### `components/Rota.tsx` and `Ledger.tsx`
- **Rota:** each doctor is a horizontal "floor-line" (the idea comes from colour-coded hospital wayfinding lines) on a 9 am–7 pm axis. Open slots are solid tiles in the doctor's colour and taken slots are hatched. A red line marks "now" on today's view.
- **Ledger:** Upcoming / Booked by Mira / Cancelled views. Cancelled rows are struck through. On mobile the table turns into stacked rows.

### `lib/sounds.ts`
Everything is synthesised with oscillators: a 400+450 Hz double ring, a two-note connect chirp, a three-tone hang-up, a rising chime for booked and a falling one for cancelled. There are no audio files.

### Design choices
- **Theme:** hospital wayfinding. Each doctor owns a colour used on the rota line, the ledger dot, the availability receipt and the booking card, so you can follow "Dr. Mehta = red" everywhere.
- **Type:** Archivo at expanded width for headings (signage feel) and **Atkinson Hyperlegible Next** for text. It was designed by the Braille Institute for low-vision readers, which suits a clinic, and its distinct 0/O and 1/l help with phone numbers and reference codes. Both are self-hosted, with no font CDN.
- **The phone is the one bold element** (dark monitor-green screen with a grid); the rest of the page stays quiet.
- Respects `prefers-reduced-motion` and has visible keyboard focus. On narrow screens the phone goes full-screen.

---

## Step 8: Testing

- `server/tests/test_tools.py` covers phone normalisation, time parsing, fuzzy doctor matching (including an STT typo), availability filters, the confirmation guardrail, the full book → double-book refused → look up → cancel → slot reopened round trip, cancel disambiguation, and the "same caller in two places at once" rule.
- The server path was exercised with a real WebRTC client: the offer is answered, a pipeline is built per call, the greeting is triggered, RTVI messages arrive on the data channel, and hanging up tears everything down.
- Live SSE highlights were verified in a headless browser: a booking made in the server flashed the right row and slot.

---

## Questions you'll likely be asked (and short answers)

**Why Pipecat instead of calling the APIs directly?**
Real-time voice needs streaming at every stage, interruption handling, turn detection, audio resampling and transport. Pipecat gives you those as composable processors, so the app code is mostly the tools and the prompt.

**Where exactly is VAD, and what does it do?**
`SileroVADAnalyzer` sits in the user aggregator. It classifies each audio chunk as speech or not. With `stop_secs=0.5`, half a second of silence ends speech, Smart Turn confirms the turn is complete, and then the LLM runs. VAD also triggers barge-in interruptions.

**How do you stop the agent booking without confirming the number?**
Twice: the prompt tells it to read the name back, and the tool refuses unless `patient_name_confirmed=true`, returning the exact read-back to use.

**What if two callers grab the same slot at once?**
The booking is a conditional `UPDATE ... WHERE is_booked = 0` inside a transaction. Only one succeeds; the other gets `slot_taken` and Mira offers another time.

**How does the dashboard know about a booking?**
The tool publishes an event, the server pushes it over SSE to every open dashboard, and the dashboard refetches and highlights. The phone itself learns from the RTVI function-call result on the WebRTC data channel.

**How is latency kept low?**
Streaming STT (websocket), VAD-driven commits, streaming LLM tokens straight into streaming TTS (Mira starts speaking before the sentence is finished being generated), a spoken filler during tool calls, and peer-to-peer WebRTC. The phone shows the live time-to-first-byte numbers.

**What would you do for production?**
Real telephony (Twilio or Daily dial-in through Pipecat's other transports), Postgres, authenticated dashboard routes, OTP or caller-ID verification on top of the spoken number, and observability (Pipecat metrics to a tracing backend).
