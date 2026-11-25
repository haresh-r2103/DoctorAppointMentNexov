# Nexov: voice AI front desk for a doctor's clinic

Call **Mira**, Nexov Clinic's AI receptionist, from your browser. She checks a doctor's availability, books an appointment and cancels one, all by voice in a single conversation. You're identified by the name and phone number you say out loud, and she reads both back for you to confirm before she books or cancels. Your phone number is never stored: the database keeps only a keyed hash and the last four digits. The dashboard around the call shows the rota, the appointment list and what the pipeline is doing, and it updates live as Mira works.

| | |
|---|---|
| Voice pipeline | **Pipecat** 1.17 (Python) |
| Speech-to-text | **ElevenLabs** Scribe Realtime (streaming, websocket) |
| Text-to-speech | **ElevenLabs** (streaming, websocket, word timestamps) |
| LLM | **Google Gemini** (free tier), via Pipecat's `GoogleLLMService` with function calling |
| VAD / turn-taking | **Silero VAD** + Pipecat Smart Turn |
| Transport | Pipecat SmallWebRTC (peer-to-peer WebRTC, no third-party room service) |
| Data | SQLite, seeded with a rolling 7-day mock rota |
| Frontend | React + TypeScript + Vite, `@pipecat-ai/client-js` |

## Requirements checklist

**Core**
- [x] Pipecat pipeline: caller audio → STT → LLM → TTS → caller audio (`server/bot.py`)
- [x] Tools registered in the pipeline via function calling: `check_doctor_availability`, `book_appointment`, `cancel_appointment` (+ `find_my_appointments`)
- [x] Caller identified by spoken name + phone number; the agent reads both back first. This is **enforced in code**: tools refuse to run unless `patient_name_confirmed` and `phone_number_confirmed` are both true. Phone numbers are stored only as an HMAC-SHA256 hash plus the last four digits, so a copy of the database leaks no numbers.
- [x] Mock doctors, specialties and slots in SQLite
- [x] All three actions end to end in one conversation
- [x] Dashboard: start/end a call, live transcript, doctors + slots, appointments that update after booking or cancelling

**Bonus**
- [x] Silero VAD in the pipeline
- [x] Phone-style call screen: ringing → connected → ended, caller name, timer, mute, end call, transcript inside the phone
- [x] Live waveform that reacts to voice (two-lead "patient monitor" trace: Mira and you)
- [x] Smooth transitions between call states
- [x] Booking and cancellation confirmation cards inside the phone
- [x] Dashboard rows and rota slots highlight when the agent books or cancels (pushed over Server-Sent Events)
- [x] Ring, connect, hang-up, booked and cancelled sounds (synthesised with Web Audio, no files)
- [x] Mobile-friendly: the phone goes full-screen and the tables collapse
- [x] Extras: word-by-word transcript highlight that follows Mira's voice (with strike-through when you interrupt), a live pipeline view with per-service latency, a tool-call inspector showing exact arguments and results, spoken filler while tools run, keyboard shortcuts (`M` mute, `Esc` end), and plain-language errors for a missing or wrong API key.

## Run it

Prerequisites: Python 3.11+ (tested on 3.13), Node 20+, a Chrome or Edge browser and a microphone.

```bash
# 1. keys
cp .env.example .env          # add ELEVENLABS_API_KEY and GOOGLE_API_KEY

# 2. backend
python -m venv .venv
source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r server/requirements.txt
python server/app.py          # → http://localhost:7860

# 3. frontend (new terminal)
cd web
npm install
npm run dev                   # → http://localhost:5173
```

Open http://localhost:5173, press **Call Mira** and allow the microphone.

**Single-port mode:** run `npm run build` in `web/`, then just `python server/app.py`. FastAPI serves the built dashboard at http://localhost:7860.

The first call takes a few seconds longer because Silero VAD and Smart Turn load their models.

### Try this script

> "Hi, I'd like to see a heart specialist this week."
> Mira checks Dr. Mehta's diary and offers three times.
> "Wednesday at half eleven, please. My number is 98765 43210."
> Mira reads your name and number back. "Yes."
> The booking card drops into the phone, the slot pulses on the rota, and the row flashes in Appointments.
> "Actually, can you cancel that?" Mira cancels it and the slot reopens.

## Project layout

```
server/
  app.py        FastAPI: WebRTC signalling, REST, SSE, serves web/dist
  bot.py        The Pipecat pipeline (one per call)
  tools.py      Tool logic + JSON schemas + Pipecat handlers
  prompts.py    Mira's system prompt (built per call with today's date + roster)
  db.py         SQLite schema, seeding, atomic book/cancel
  events.py     In-process pub/sub feeding the SSE stream
  tests/        pytest for every tool (no audio/LLM needed)
web/src/
  lib/useCall.ts      PipecatClient + RTVI events → call state
  lib/api.ts          REST + SSE hook, change highlights
  lib/sounds.ts       Web Audio ring / hang-up / chimes
  components/Phone.tsx       Call screen and its states
  components/Trace.tsx       The two-lead voice monitor
  components/Transcript.tsx  Live transcript, tool receipts, booking card
  components/Rota.tsx        Doctors' floor-lines and slots
  components/Ledger.tsx      Appointments
  components/Pipeline.tsx    Live pipeline stages + tool inspector
docs/
  WALKTHROUGH.md   Step-by-step explanation of every part
  PIPELINE.md      Short pipeline description (for the submission form)
```

## Tests

```bash
cd server && python -m pytest -q
```

## Troubleshooting

- **"The server is missing GOOGLE_API_KEY"** in the phone: fill `.env` and restart `app.py`.
- **Gemini model errors:** set `GOOGLE_MODEL` in `.env` to a model your key can use, such as `gemini-2.5-flash`.
- **No audio from Mira:** check the tab isn't muted; some browsers need a click on the page first (pressing Call Mira counts).
- **Want a fresh rota:** `curl -X POST localhost:7860/api/reset`, or start with `RESET_DB=1`.
- **UI preview without keys (dev only):** http://localhost:5173/?preview=connected (also `ringing`, `ended`) shows the phone with a scripted conversation.
