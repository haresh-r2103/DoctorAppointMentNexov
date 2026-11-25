# Pipeline description

The browser connects to a FastAPI server over WebRTC (Pipecat's SmallWebRTC transport). Every call gets its own Pipecat pipeline: microphone audio → ElevenLabs Realtime STT → a context aggregator where Silero VAD and Smart Turn decide when the caller has finished speaking → Gemini → ElevenLabs TTS → back to the browser. A second aggregator records what was actually spoken, so if the caller interrupts, the context only holds the part Mira said.

Gemini has four tools registered through Pipecat function calling: check availability, book, cancel, and look up my appointments. Each one is a plain Python function over SQLite. The caller is identified by the name and phone number they say; the number is stored only as a keyed hash plus its last four digits. The prompt tells Mira to read the name back, and the tools also refuse to book or cancel unless `patient_name_confirmed` is true, so the rule doesn't rely on the model behaving. Booking is a single conditional update, so the same slot can't be taken twice.

The dashboard learns what is happening from Pipecat's RTVI events on the WebRTC data channel (transcripts, speaking state, tool calls, latency). When the database changes, the server pushes a Server-Sent Event so the appointment list and rota update and highlight immediately.

LLM: Google Gemini (free tier), the default model in Pipecat 1.17, which can be overridden with `GOOGLE_MODEL`.
