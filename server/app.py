"""
Nexov HTTP server (FastAPI).

Routes
  POST  /api/offer          WebRTC signalling: browser sends an SDP offer, we
                            answer and start a fresh voice pipeline (bot.py).
  PATCH /api/offer          Trickle-ICE candidates for an existing connection.
  GET   /api/doctors        Doctors + every slot in the rolling 7-day rota.
  GET   /api/appointments   The appointment ledger.
  GET   /api/events         Server-Sent Events: live booked/cancelled updates.
  GET   /api/health         Which keys are configured (never the keys themselves).
  GET   /*                  The built dashboard (web/dist) when present.

Run:  python server/app.py      (http://localhost:7860)
"""

from __future__ import annotations

import asyncio
import os
import secrets
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env", override=True)
sys.path.insert(0, str(Path(__file__).parent))

import aiohttp  # noqa: E402
import uvicorn  # noqa: E402
from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from fastapi.responses import FileResponse, StreamingResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from loguru import logger  # noqa: E402

import db  # noqa: E402
import events  # noqa: E402
from tools import mask_name, mask_phone  # noqa: E402

from pipecat.transports.smallwebrtc.connection import IceServer, SmallWebRTCConnection  # noqa: E402
from pipecat.transports.smallwebrtc.request_handler import (  # noqa: E402
    SmallWebRTCPatchRequest,
    SmallWebRTCRequest,
    SmallWebRTCRequestHandler,
)

webrtc = SmallWebRTCRequestHandler(ice_servers=[IceServer(urls="stun:stun.l.google.com:19302")])


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.connect()  # create + seed the rota before the first request
    logger.info(f"Nexov ready · rota seeded from {db.today_local()} · db={db.DB_PATH}")
    yield
    await webrtc.close()


app = FastAPI(title="Nexov Clinic", lifespan=lifespan)
ALLOWED_ORIGINS = [
    o.strip()
    for o in os.getenv("ALLOWED_ORIGINS", "http://localhost:5173,http://localhost:7860,http://127.0.0.1:5173,http://127.0.0.1:7860").split(",")
    if o.strip()
]
app.add_middleware(CORSMiddleware, allow_origins=ALLOWED_ORIGINS, allow_methods=["*"], allow_headers=["*"])


# ---------------------------------------------------------------------------
# Voice: WebRTC signalling
# ---------------------------------------------------------------------------
@app.post("/api/offer")
async def offer(request: SmallWebRTCRequest, background_tasks: BackgroundTasks):
    missing = [k for k in ("ELEVENLABS_API_KEY", "GOOGLE_API_KEY") if not os.getenv(k)]
    if missing:
        return {"error": f"Missing {', '.join(missing)} in .env"}

    async def start_call(connection: SmallWebRTCConnection):
        from bot import run_bot  # imported lazily: loads Silero + service SDKs

        background_tasks.add_task(run_bot, connection)

    return await webrtc.handle_web_request(request=request, webrtc_connection_callback=start_call)


@app.patch("/api/offer")
async def ice_candidate(request: SmallWebRTCPatchRequest):
    await webrtc.handle_patch_request(request)
    return {"status": "success"}


# ---------------------------------------------------------------------------
# Dashboard data
# ---------------------------------------------------------------------------
def _appt_row(a: dict) -> dict:
    return {
        "id": a["id"],
        "ref": a["ref"],
        "patient_name": mask_name(a["patient_name"]),  # "Asha K." — the dashboard is not a place for full names
        "phone_masked": mask_phone(a["phone_last4"]),  # "•••••3210" — the full number is never stored
        "reason": a["reason"],
        "doctor_id": a["doctor_id"],
        "doctor_name": a["doctor_name"],
        "specialty": a["specialty"],
        "slot_id": a["slot_id"],
        "day": a["day"],
        "start": a["start"],
        "status": a["status"],
        "source": a["source"],
        "created_at": a["created_at"],
        "cancelled_at": a["cancelled_at"],
    }


@app.get("/api/doctors")
async def doctors():
    out = []
    for d in db.list_doctors():
        slots = db.list_slots(d["id"])
        out.append(
            {
                **{k: d[k] for k in ("id", "name", "specialty", "room")},
                "slots": [{"id": s["id"], "day": s["day"], "start": s["start"], "booked": bool(s["is_booked"])} for s in slots],
            }
        )
    return {"today": db.today_local().isoformat(), "now": db.now_local().isoformat(), "doctors": out}


@app.get("/api/appointments")
async def appointments():
    return {"appointments": [_appt_row(a) for a in db.list_appointments()]}


@app.get("/api/events")
async def stream(request: Request):
    """Server-Sent Events. Each booking/cancellation is pushed as one `data:` line."""
    q = events.subscribe()

    async def gen():
        try:
            yield "event: ready\ndata: {}\n\n"
            while not await request.is_disconnected():
                try:
                    payload = await asyncio.wait_for(q.get(), timeout=15)
                    yield f"data: {payload}\n\n"
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"  # stops proxies from closing an idle stream
        finally:
            events.unsubscribe(q)

    return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})


@app.get("/api/health")
async def health():
    return {
        "ok": True,
        "elevenlabs": bool(os.getenv("ELEVENLABS_API_KEY")),
        "google": bool(os.getenv("GOOGLE_API_KEY")),
        "model": os.getenv("GOOGLE_MODEL") or "pipecat default",
    }


async def _probe(session: aiohttp.ClientSession, url: str, headers: dict) -> dict:
    t0 = time.perf_counter()
    try:
        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=8)) as r:
            return {"ok": r.status == 200, "status": r.status, "ms": round((time.perf_counter() - t0) * 1000)}
    except Exception as e:  # network down, DNS, timeout...
        return {"ok": False, "error": type(e).__name__, "ms": round((time.perf_counter() - t0) * 1000)}


@app.get("/api/health/deep")
async def health_deep():
    """Actually call the providers: checks the keys work, the voice exists and the model answers."""
    eleven_key, google_key = os.getenv("ELEVENLABS_API_KEY", ""), os.getenv("GOOGLE_API_KEY", "")
    voice = os.getenv("ELEVENLABS_VOICE_ID", "")
    model = os.getenv("GOOGLE_MODEL") or "gemini-3.6-flash"
    async with aiohttp.ClientSession() as s:
        eleven, google = await asyncio.gather(
            _probe(s, f"https://api.elevenlabs.io/v1/voices/{voice}", {"xi-api-key": eleven_key}),
            _probe(s, f"https://generativelanguage.googleapis.com/v1beta/models/{model}", {"x-goog-api-key": google_key}),
        )
    return {"ok": eleven["ok"] and google["ok"], "elevenlabs_voice": eleven, "gemini_model": {"model": model, **google}}


@app.post("/api/reset")
async def reset(x_admin_token: str | None = Header(default=None)):
    """Wipe and re-seed the demo rota. DESTRUCTIVE, so it needs ADMIN_TOKEN (disabled when unset)."""
    expected = os.getenv("ADMIN_TOKEN")
    if not expected or not x_admin_token or not secrets.compare_digest(x_admin_token, expected):
        raise HTTPException(status_code=403, detail="Set ADMIN_TOKEN and send it as the X-Admin-Token header.")
    db.seed(db.connect())
    events.publish("rota.reset", {})
    return {"ok": True}


# ---------------------------------------------------------------------------
# Serve the built frontend (npm run build → web/dist) from the same origin.
# ---------------------------------------------------------------------------
DIST = ROOT / "web" / "dist"
if DIST.exists():
    app.mount("/assets", StaticFiles(directory=DIST / "assets"), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    async def spa(path: str):
        f = DIST / path
        return FileResponse(f if path and f.is_file() else DIST / "index.html")


if __name__ == "__main__":
    uvicorn.run(app, host=os.getenv("HOST", "0.0.0.0"), port=int(os.getenv("PORT", "7860")))
