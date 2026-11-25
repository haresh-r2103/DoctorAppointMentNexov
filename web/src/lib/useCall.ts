/**
 * useCall — everything about one phone call with Mira.
 *
 * It owns a PipecatClient (Pipecat's browser SDK) using the SmallWebRTC
 * transport. `start()` grabs the mic, POSTs a WebRTC offer to /api/offer,
 * and the Python server answers and spins up a fresh pipeline for this call.
 * From then on audio flows both ways over WebRTC, and RTVI messages on the
 * data channel tell us what the pipeline is doing. We turn those messages
 * into: a call phase, a live transcript, tool-call cards, a "which stage is
 * active" signal and latency numbers.
 */
import { PipecatClient } from '@pipecat-ai/client-js'
import type {
  BotOutputData,
  LLMFunctionCallInProgressData,
  LLMFunctionCallStartedData,
  LLMFunctionCallStoppedData,
  Participant,
  PipecatMetricsData,
  UserInputData,
} from '@pipecat-ai/client-js'
import { SmallWebRTCTransport } from '@pipecat-ai/small-webrtc-transport'
import { useCallback, useEffect, useRef, useState } from 'react'
import { fetchHealth } from './api'
import { playBooked, playCancelled, playConnected, playHangup, startRinging } from './sounds'

export type Phase = 'idle' | 'ringing' | 'connected' | 'ended' | 'failed'
export type Stage = 'idle' | 'listening' | 'hearing' | 'thinking' | 'tool' | 'speaking'

export type Seg = { key: string; text: string; spoken: number; cut?: boolean }
export type ToolCall = {
  id: string
  name: string
  status: 'running' | 'done' | 'cancelled'
  args?: Record<string, unknown>
  result?: any
  startedAt: number
  ms?: number
}
export type Turn =
  | { id: string; role: 'user'; text: string; interim: string }
  | { id: string; role: 'bot'; segs: Seg[] }
  | { id: string; role: 'tool'; call: ToolCall }

export type Latency = { stt?: number; llm?: number; tts?: number }

const MIN_RING_MS = 1300
let uid = 0
const nextId = () => `t${++uid}`

export function useCall() {
  const [phase, setPhase] = useState<Phase>('idle')
  const [stage, setStage] = useState<Stage>('idle')
  const [error, setError] = useState<string | null>(null)
  const [muted, setMuted] = useState(false)
  const [startedAt, setStartedAt] = useState<number | null>(null)
  const [endedAt, setEndedAt] = useState<number | null>(null)
  const [endedBy, setEndedBy] = useState<'you' | 'mira' | null>(null)
  const [callerName, setCallerName] = useState<string | null>(null)
  const [latency, setLatency] = useState<Latency>({})
  const [notice, setNotice] = useState<string | null>(null)
  const [version, setVersion] = useState(0)

  // The transcript changes many times per second (word-by-word), so we keep
  // it in a ref and bump a counter to re-render instead of copying arrays.
  const turns = useRef<Turn[]>([])
  const calls = useRef<Map<string, ToolCall>>(new Map())
  const segIndex = useRef<Map<string, Seg>>(new Map())
  const pending = useRef<ToolCall[]>([])
  const bump = () => setVersion((v) => v + 1)

  const client = useRef<PipecatClient | null>(null)
  const audioEl = useRef<HTMLAudioElement | null>(null)
  const audioCtx = useRef<AudioContext | null>(null)
  const botAnalyser = useRef<AnalyserNode | null>(null)
  const userAnalyser = useRef<AnalyserNode | null>(null)
  const stopRing = useRef<(() => void) | null>(null)
  const phaseRef = useRef<Phase>('idle')
  phaseRef.current = phase

  const analyse = (track: MediaStreamTrack) => {
    const ctx = (audioCtx.current ??= new AudioContext())
    const src = ctx.createMediaStreamSource(new MediaStream([track]))
    const an = ctx.createAnalyser()
    an.fftSize = 1024
    an.smoothingTimeConstant = 0.6
    src.connect(an)
    return an
  }

  const last = () => turns.current[turns.current.length - 1]

  const finish = useCallback((by: 'you' | 'mira') => {
    stopRing.current?.()
    stopRing.current = null
    if (phaseRef.current === 'ended' || phaseRef.current === 'idle') return
    playHangup()
    setEndedBy(by)
    setEndedAt(Date.now())
    setStage('idle')
    setPhase(phaseRef.current === 'failed' ? 'failed' : 'ended')
  }, [])

  const start = useCallback(async () => {
    // Reset everything from any previous call.
    turns.current = []
    calls.current = new Map()
    segIndex.current = new Map()
    pending.current = []
    setError(null)
    setMuted(false)
    setCallerName(null)
    setLatency({})
    setNotice(null)
    setStartedAt(null)
    setEndedAt(null)
    setEndedBy(null)
    setPhase('ringing')
    setStage('idle')
    bump()
    stopRing.current = startRinging()
    const ringStarted = Date.now()

    const fail = (msg: string) => {
      stopRing.current?.()
      stopRing.current = null
      playHangup()
      setError(msg)
      setPhase('failed')
      client.current?.disconnect().catch(() => {})
    }

    try {
      const h = await fetchHealth()
      const missing = [!h.google && 'GOOGLE_API_KEY', !h.elevenlabs && 'ELEVENLABS_API_KEY'].filter(Boolean)
      if (missing.length) return fail(`The server is missing ${missing.join(' and ')}. Add it to .env and restart.`)
    } catch {
      return fail('Can’t reach the clinic server on port 7860. Start it with python server/app.py.')
    }

    const pc = new PipecatClient({
      transport: new SmallWebRTCTransport(),
      enableMic: true,
      enableCam: false,
      callbacks: {
        onBotReady: () => {
          const wait = Math.max(0, MIN_RING_MS - (Date.now() - ringStarted))
          window.setTimeout(() => {
            if (phaseRef.current !== 'ringing') return
            stopRing.current?.()
            stopRing.current = null
            playConnected()
            setStartedAt(Date.now())
            setPhase('connected')
            setStage('listening')
          }, wait)
        },
        onDisconnected: () => finish('mira'),
        onError: (m) => {
          const d = (m?.data as any) ?? {}
          const msg = d.message ?? d.error ?? 'The call dropped.'
          if (phaseRef.current === 'ringing' && d.fatal !== false) fail(msg)
          else setNotice(explain(String(msg)))
        },
        onTrackStarted: (track: MediaStreamTrack, p?: Participant) => {
          if (track.kind !== 'audio') return
          if (p?.local) {
            userAnalyser.current = analyse(track)
          } else {
            // Mira's voice: play it, and tap it for the trace.
            if (!audioEl.current) {
              audioEl.current = new Audio()
              audioEl.current.autoplay = true
            }
            audioEl.current.srcObject = new MediaStream([track])
            audioEl.current.play().catch(() => {})
            botAnalyser.current = analyse(track)
          }
        },

        // ---- pipeline stage (drives the pipeline strip + phone status) ----
        onUserStartedSpeaking: () => setStage('hearing'),
        onUserStoppedSpeaking: () => setStage('thinking'),
        onBotLlmStarted: () => setStage((s) => (s === 'speaking' ? s : 'thinking')),
        onBotStartedSpeaking: () => setStage('speaking'),
        onBotStoppedSpeaking: () => {
          // Anything left unhighlighted has now been said.
          const t = last()
          if (t?.role === 'bot') t.segs.forEach((s) => !s.cut && (s.spoken = s.text.length))
          setStage('listening')
          bump()
        },
        onBotInterrupted: () => {
          const t = last()
          if (t?.role === 'bot') t.segs.forEach((s) => s.spoken < s.text.length && (s.cut = true))
          bump()
        },

        // ---- transcript: caller ----
        onUserInput: (d: UserInputData) => {
          if (d.input_type !== 'transcription' || !d.text?.trim()) return
          let t = last()
          if (t?.role !== 'user') {
            t = { id: nextId(), role: 'user', text: '', interim: '' }
            turns.current.push(t)
          }
          if (d.final) {
            t.text = (t.text + ' ' + d.text).trim()
            t.interim = ''
          } else {
            t.interim = d.text
          }
          bump()
        },

        // ---- transcript: Mira, word by word as the TTS speaks ----
        onBotOutput: (d: BotOutputData) => {
          const type = String(d.output_type ?? d.aggregated_by ?? '')
          if (type.startsWith('expression') || !d.text) return
          const key = d.segment_id != null ? `s${d.segment_id}` : nextId()
          const existing = segIndex.current.get(key)
          if (!existing) {
            let t = last()
            if (t?.role !== 'bot') {
              t = { id: nextId(), role: 'bot', segs: [] }
              turns.current.push(t)
            }
            const seg: Seg = { key, text: d.text, spoken: d.will_be_spoken === false ? d.text.length : 0 }
            if (d.spoken_status === 'completed') seg.spoken = d.text.length
            t.segs.push(seg)
            segIndex.current.set(key, seg)
          } else if (d.spoken_status === 'in-progress' && d.spoken_progress) {
            existing.spoken = Math.max(existing.spoken, d.spoken_progress.accumulated_text.length)
          } else if (d.spoken_status === 'completed') {
            existing.spoken = existing.text.length
          }
          bump()
        },

        // ---- tools ----
        onLLMFunctionCallStarted: (d: LLMFunctionCallStartedData) => {
          setStage('tool')
          // "started" carries no tool_call_id, so park it until "in-progress" claims it.
          const call: ToolCall = { id: nextId(), name: d.function_name ?? 'tool', status: 'running', startedAt: performance.now() }
          pending.current.push(call)
          turns.current.push({ id: nextId(), role: 'tool', call })
          bump()
        },
        onLLMFunctionCallInProgress: (d: LLMFunctionCallInProgressData) => {
          let call = calls.current.get(d.tool_call_id)
          if (!call && pending.current.length) {
            call = pending.current.shift()!
            call.id = d.tool_call_id
            calls.current.set(call.id, call)
          }
          if (!call) {
            call = { id: d.tool_call_id, name: d.function_name ?? 'tool', status: 'running', startedAt: performance.now() }
            calls.current.set(call.id, call)
            turns.current.push({ id: nextId(), role: 'tool', call })
          }
          call.args = d.arguments as Record<string, unknown>
          if (d.function_name) call.name = d.function_name
          const a = call.args ?? {}
          if (a.patient_name && a.patient_name_confirmed && a.phone_number_confirmed) setCallerName(String(a.patient_name))
          bump()
        },
        onLLMFunctionCallStopped: (d: LLMFunctionCallStoppedData) => {
          const call = calls.current.get(d.tool_call_id) ?? pending.current.shift()
          if (!call) return
          call.status = d.cancelled ? 'cancelled' : 'done'
          call.result = typeof d.result === 'string' ? safeJSON(d.result) : d.result
          call.ms = Math.round(performance.now() - call.startedAt)
          if (call.result?.ok && call.name === 'book_appointment') playBooked()
          if (call.result?.ok && call.name === 'cancel_appointment') playCancelled()
          setStage('thinking')
          bump()
        },

        // ---- latency: time-to-first-byte per service ----
        onMetrics: (m: PipecatMetricsData) => {
          const next: Latency = {}
          for (const x of m.ttfb ?? []) {
            const p = x.processor.toLowerCase()
            const ms = Math.round(x.value * 1000)
            if (!ms) continue
            if (p.includes('stt')) next.stt = ms
            else if (p.includes('llm')) next.llm = ms
            else if (p.includes('tts')) next.tts = ms
          }
          if (Object.keys(next).length) setLatency((l) => ({ ...l, ...next }))
        },
      },
    })
    client.current = pc

    try {
      await pc.connect({ webrtcRequestParams: { endpoint: '/api/offer' } })
    } catch (e: any) {
      const msg = String(e?.message ?? e)
      fail(/permission|notallowed/i.test(msg) ? 'Microphone access was blocked. Allow it and call again.' : msg)
    }
  }, [finish])

  const end = useCallback(async () => {
    finish('you')
    await client.current?.disconnect().catch(() => {})
  }, [finish])

  const toggleMute = useCallback(() => {
    setMuted((m) => {
      client.current?.enableMic(m) // m is the old value: muted → enable
      return !m
    })
  }, [])

  const dismiss = useCallback(() => {
    setPhase('idle')
    setError(null)
  }, [])

  useEffect(() => () => void client.current?.disconnect().catch(() => {}), [])

  // Dev-only: /?preview=live|ringing|ended renders the phone with a scripted
  // conversation so the UI can be styled without API keys. Not in production builds.
  useEffect(() => {
    if (!import.meta.env.DEV) return
    const kind = new URLSearchParams(location.search).get('preview')
    if (!kind) return
    const appt = {
      ref: 'NX-7K2P', status: 'confirmed', doctor_id: 'mehta', doctor: 'Dr. Kabir Mehta', specialty: 'Cardiologist',
      room: 'Room 4', date: '2026-11-25', time: '11:30', patient_name: 'Asha K.', phone_masked: '•••••3210',
    }
    const avail: ToolCall = {
      id: 'a', name: 'check_doctor_availability', status: 'done', startedAt: 0, ms: 14, args: { doctor: 'Dr. Mehta' },
      result: { ok: true, results: [{ doctor_id: 'mehta', doctor: 'Dr. Kabir Mehta', availability: [{ date_spoken: 'Wednesday, 25 November', free_slots: ['10:00 AM', '11:30 AM', '1:00 PM'] }] }] },
    }
    const book: ToolCall = {
      id: 'b', name: 'book_appointment', status: 'done', startedAt: 0, ms: 21,
      args: { patient_name: 'Asha Kapoor', patient_name_confirmed: true, phone_number: '98765 43210', phone_number_confirmed: true }, result: { ok: true, appointment: appt },
    }
    turns.current = [
      { id: '1', role: 'bot', segs: [{ key: 'a', text: 'Hi, this is Mira at Nexov Clinic. How can I help?', spoken: 99 }] },
      { id: '2', role: 'user', text: 'I need to see a heart doctor this week.', interim: '' },
      { id: '3', role: 'tool', call: avail },
      { id: '4', role: 'bot', segs: [{ key: 'b', text: 'Dr. Mehta is free Wednesday at ten, half past eleven or one. Which suits you?', spoken: 99 }] },
      { id: '5', role: 'user', text: 'Half eleven. My number is 98765 43210.', interim: '' },
      { id: '6', role: 'bot', segs: [{ key: 'c', text: 'That’s nine eight seven six five, four three two one zero. Is that right?', spoken: 99 }] },
      { id: '7', role: 'user', text: 'Yes.', interim: '' },
      { id: '8', role: 'tool', call: book },
      { id: '9', role: 'bot', segs: [{ key: 'd', text: 'You’re booked with Dr. Mehta on Wednesday at half past eleven. Your reference is N X 7 K 2 P.', spoken: 46 }] },
    ]
    setCallerName('Asha Kapoor')
    setLatency({ stt: 212, llm: 684, tts: 241 })
    setStartedAt(Date.now() - 83000)
    if (kind === 'ended') {
      setEndedAt(Date.now())
      setEndedBy('you')
    }
    setStage('speaking')
    setPhase(kind as Phase)
    bump()
  }, [])

  return {
    phase,
    stage,
    error,
    muted,
    startedAt,
    endedAt,
    endedBy,
    callerName,
    latency,
    notice,
    turns: turns.current,
    version,
    toolCalls: turns.current.flatMap((t) => (t.role === 'tool' ? [t.call] : [])),
    botAnalyser,
    userAnalyser,
    start,
    end,
    toggleMute,
    dismiss,
  }
}

export type CallState = ReturnType<typeof useCall>

/** Turn a raw service error into something a person can act on. */
function explain(msg: string) {
  if (/elevenlabs/i.test(msg) && /401|403|unauthor|invalid/i.test(msg)) return 'ElevenLabs rejected the API key. Check ELEVENLABS_API_KEY.'
  if (/elevenlabs/i.test(msg)) return 'Can’t reach ElevenLabs. Check the key and your connection.'
  if (/google|gemini/i.test(msg)) return 'Gemini didn’t answer. Check GOOGLE_API_KEY and GOOGLE_MODEL.'
  return msg
}

function safeJSON(s: string) {
  try {
    return JSON.parse(s)
  } catch {
    return s
  }
}
