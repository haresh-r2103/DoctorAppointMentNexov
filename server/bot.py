"""
The voice pipeline — one instance runs per phone call.

    browser mic ──WebRTC──▶ transport.input()
                               │  raw 16-bit PCM audio frames
                               ▼
                    ElevenLabs Realtime STT   (speech → text, streaming)
                               │  TranscriptionFrames
                               ▼
                    user context aggregator   (Silero VAD decides when the caller
                               │               has finished their turn)
                               ▼
                         Gemini LLM  ◀──────▶  tools.py (availability / book / cancel)
                               │  streamed text tokens
                               ▼
                    ElevenLabs TTS            (text → speech, streaming)
                               │  audio frames
                               ▼
                    transport.output() ──WebRTC──▶ browser speaker
                               │
                    assistant context aggregator (stores what Mira actually said)

RTVI (Pipecat's client protocol) rides along on the WebRTC data channel and
tells the browser what's happening: transcripts, who is speaking, tool calls,
latency metrics. The dashboard is built on those events.
"""

from __future__ import annotations

import os

from loguru import logger

import prompts
import tools
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.frames.frames import LLMRunFrame, TTSSpeakFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.processors.frameworks.rtvi import RTVIObserverParams
from pipecat.processors.frameworks.rtvi.observer import RTVIFunctionCallReportLevel
from pipecat.services.elevenlabs.stt import ElevenLabsRealtimeSTTService
from pipecat.services.elevenlabs.tts import ElevenLabsTTSService
from pipecat.services.google.llm import GoogleLLMService
from pipecat.transports.base_transport import TransportParams
from pipecat.transports.smallwebrtc.connection import SmallWebRTCConnection
from pipecat.transports.smallwebrtc.transport import SmallWebRTCTransport
from pipecat.workers.runner import WorkerRunner

# Short fillers so the caller never hears dead air while a tool runs.
FILLERS = {
    "check_doctor_availability": "Let me check the diary.",
    "book_appointment": "Booking that now.",
    "cancel_appointment": "One moment.",
}


async def run_bot(webrtc_connection: SmallWebRTCConnection) -> None:
    logger.info("☎  New call — building pipeline")

    # 1. Transport: audio in/out over a peer-to-peer WebRTC connection with the browser.
    transport = SmallWebRTCTransport(
        webrtc_connection=webrtc_connection,
        params=TransportParams(audio_in_enabled=True, audio_out_enabled=True),
    )

    # 2. Speech-to-text. Realtime (websocket) Scribe; by default Pipecat's VAD
    #    tells it when to commit a segment, so transcripts land the moment you stop talking.
    stt = ElevenLabsRealtimeSTTService(api_key=os.environ["ELEVENLABS_API_KEY"])

    # 3. The brain. Gemini with our tools; the system prompt is rebuilt per call.
    llm_settings = {"system_instruction": prompts.build_system_prompt()}
    if os.getenv("GOOGLE_MODEL"):
        llm_settings["model"] = os.environ["GOOGLE_MODEL"]
    llm = GoogleLLMService(
        api_key=os.environ["GOOGLE_API_KEY"],
        settings=GoogleLLMService.Settings(**llm_settings),
    )
    tools.register_tools(llm)

    # 4. Text-to-speech. Streaming websocket TTS that also returns word timings,
    #    which is what lets the phone UI highlight each word as Mira says it.
    tts = ElevenLabsTTSService(
        api_key=os.environ["ELEVENLABS_API_KEY"],
        settings=ElevenLabsTTSService.Settings(
            voice=os.getenv("ELEVENLABS_VOICE_ID", "21m00Tcm4TlvDQ8ikWAM"),
        ),
    )

    @llm.event_handler("on_function_calls_started")
    async def on_function_calls_started(service, function_calls):
        name = function_calls[0].function_name if function_calls else ""
        if filler := FILLERS.get(name):
            await tts.queue_frame(TTSSpeakFrame(filler))

    # If Gemini hangs or is overloaded, don't leave the caller in silence.
    @llm.event_handler("on_completion_timeout")
    async def on_completion_timeout(service):
        logger.warning("LLM completion timed out; apologising to the caller")
        await tts.queue_frame(TTSSpeakFrame("Sorry, I lost my train of thought for a moment. Could you say that again?"))

    # 5. Conversation memory. The context holds every turn plus the tool list.
    #    Silero VAD (bonus requirement) runs inside the user aggregator: it
    #    detects speech start/stop, which drives turn-taking and barge-in
    #    (talk over Mira and she stops).
    context = LLMContext(tools=tools.TOOLS)
    user_aggregator, assistant_aggregator = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(
            vad_analyzer=SileroVADAnalyzer(params=VADParams(stop_secs=0.5)),
        ),
    )

    # 6. Wire it together. Frames flow top to bottom.
    pipeline = Pipeline(
        [
            transport.input(),
            stt,
            user_aggregator,
            llm,
            tts,
            transport.output(),
            assistant_aggregator,
        ]
    )

    # 7. The worker runs the pipeline. RTVI is on by default; we raise the
    #    function-call report level so the dashboard can show tool arguments and
    #    results live (it's our own demo UI, so exposing them is fine).
    worker = PipelineWorker(
        pipeline,
        params=PipelineParams(enable_metrics=True, enable_usage_metrics=True),
        rtvi_observer_params=RTVIObserverParams(
            function_call_report_level={"*": RTVIFunctionCallReportLevel.FULL},
        ),
        idle_timeout_secs=180,
    )
    runner = WorkerRunner(handle_sigint=False)
    await runner.add_workers(worker)

    @transport.event_handler("on_client_connected")
    async def on_client_connected(transport, client):
        logger.info("☎  Caller connected — Mira greets")
        context.add_message({"role": "developer", "content": prompts.GREETING_INSTRUCTION})
        await worker.queue_frames([LLMRunFrame()])

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(transport, client):
        logger.info("☎  Caller hung up")
        await runner.cancel()

    await runner.run()
    logger.info("☎  Call finished")
