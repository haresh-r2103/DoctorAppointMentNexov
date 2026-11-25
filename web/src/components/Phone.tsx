/**
 * The phone that pops up over the dashboard.
 * ringing → connected → ended (or failed), each with its own screen and a
 * cross-fade between them. Everything shown comes from useCall.
 */
import { useEffect, useState } from 'react'
import type { CallState, Stage } from '../lib/useCall'
import { clock } from '../lib/format'
import { Trace } from './Trace'
import { Transcript } from './Transcript'

const STAGE_TEXT: Record<Stage, string> = {
  idle: '',
  listening: 'Listening',
  hearing: 'Hearing you',
  thinking: 'Mira is thinking',
  tool: 'Checking the clinic system',
  speaking: 'Mira is speaking',
}

function useTicker(on: boolean) {
  const [, set] = useState(0)
  useEffect(() => {
    if (!on) return
    const id = setInterval(() => set((n) => n + 1), 500)
    return () => clearInterval(id)
  }, [on])
}

export function Phone({ call }: { call: CallState }) {
  const { phase, stage, startedAt, endedAt } = call
  useTicker(phase === 'connected')
  const [statusTime, setStatusTime] = useState(() => new Date())
  useEffect(() => {
    const id = setInterval(() => setStatusTime(new Date()), 15000)
    return () => clearInterval(id)
  }, [])

  // Esc ends or closes; M toggles mute. Handy while recording a demo.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.target instanceof HTMLInputElement) return
      if (e.key === 'Escape') phase === 'connected' || phase === 'ringing' ? call.end() : call.dismiss()
      if (e.key.toLowerCase() === 'm' && phase === 'connected') call.toggleMute()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [phase, call])

  if (phase === 'idle') return null

  const elapsed = startedAt ? ((endedAt ?? Date.now()) - startedAt) / 1000 : 0
  const actions = call.toolCalls.filter((c) => c.result?.ok && (c.name === 'book_appointment' || c.name === 'cancel_appointment'))

  return (
    <div className={`phone-layer phase-${phase}`} role="dialog" aria-label="Call with Mira">
      <div className="phone">
        <div className="phone-notch" />
        <div className="phone-status">
          <span>{statusTime.toLocaleTimeString('en-IN', { hour: '2-digit', minute: '2-digit', hour12: false })}</span>
          <span className="phone-bars" aria-hidden="true">
            <i /> <i /> <i /> <i />
          </span>
        </div>

        {phase === 'ringing' && (
          <section className="screen screen-ringing">
            <div className="ring-avatar">
              <span className="ring-wave" />
              <span className="ring-wave" />
              <span className="ring-wave" />
              <span className="avatar avatar-lg">M</span>
            </div>
            <h2 className="callee">Mira</h2>
            <p className="callee-sub">Nexov Clinic front desk</p>
            <p className="call-state">Calling</p>
            <div className="controls">
              <button className="btn-round btn-end" onClick={call.end} aria-label="Cancel call">
                <HangupIcon />
              </button>
            </div>
          </section>
        )}

        {phase === 'connected' && (
          <section className="screen screen-live">
            <header className="live-head">
              <span className={`avatar ${stage === 'speaking' ? 'avatar-speaking' : ''}`}>M</span>
              <div className="live-who">
                <h2 className="callee callee-sm">Mira</h2>
                <p className="callee-sub">
                  {call.callerName ? `Caller ${call.callerName}` : 'Nexov Clinic front desk'}
                </p>
              </div>
              <span className="timer">{clock(elapsed)}</span>
            </header>

            <div className="monitor">
              <div className="monitor-labels">
                <span className="lead lead-bot">Mira</span>
                <span className="lead lead-user">{call.muted ? 'You, muted' : 'You'}</span>
              </div>
              <Trace bot={call.botAnalyser} user={call.userAnalyser} active muted={call.muted} />
              <div className="monitor-readout">
                <Readout label="Hear" v={call.latency.stt} />
                <Readout label="Think" v={call.latency.llm} />
                <Readout label="Speak" v={call.latency.tts} />
              </div>
            </div>

            <Transcript turns={call.turns} version={call.version} />

            {call.notice && <p className="notice">{call.notice}</p>}
            <p className={`stage stage-${stage}`}>
              <span className="stage-dot" /> {STAGE_TEXT[stage]}
            </p>

            <div className="controls">
              <button
                className={`btn-round btn-mute ${call.muted ? 'is-on' : ''}`}
                onClick={call.toggleMute}
                aria-pressed={call.muted}
                aria-label={call.muted ? 'Unmute' : 'Mute'}
              >
                <MicIcon off={call.muted} />
                <span className="btn-label">{call.muted ? 'Unmute' : 'Mute'}</span>
              </button>
              <button className="btn-round btn-end" onClick={call.end} aria-label="End call">
                <HangupIcon />
                <span className="btn-label">End</span>
              </button>
            </div>
          </section>
        )}

        {(phase === 'ended' || phase === 'failed') && (
          <section className="screen screen-ended">
            <span className="avatar avatar-lg avatar-off">M</span>
            <h2 className="callee">{phase === 'failed' ? 'Call didn’t connect' : 'Call ended'}</h2>
            {phase === 'ended' ? (
              <p className="callee-sub">
                {clock(elapsed)}
                {call.endedBy === 'mira' ? ', Mira hung up' : ''}
              </p>
            ) : (
              <p className="fail-msg">{call.error}</p>
            )}
            {actions.length > 0 && (
              <ul className="summary">
                {actions.map((a) => {
                  const appt = a.result.appointment ?? a.result.cancelled
                  return (
                    <li key={a.id}>
                      {a.name === 'book_appointment' ? 'Booked' : 'Cancelled'} {appt.ref} with {appt.doctor}
                    </li>
                  )
                })}
              </ul>
            )}
            <div className="controls controls-text">
              <button className="btn-pill btn-call" onClick={call.start}>
                Call again
              </button>
              <button className="btn-pill btn-ghost" onClick={call.dismiss}>
                Close
              </button>
            </div>
          </section>
        )}
        <div className="phone-home" />
      </div>
    </div>
  )
}

function Readout({ label, v }: { label: string; v?: number }) {
  return (
    <span className="readout">
      <span className="readout-v">{v ?? '—'}</span>
      <span className="readout-l">{label} ms</span>
    </span>
  )
}

export function HangupIcon() {
  return (
    <svg viewBox="0 0 24 24" width="26" height="26" aria-hidden="true">
      <path
        fill="currentColor"
        d="M12 9c-1.6 0-3.15.25-4.6.72v3.1c0 .39-.23.74-.56.9-.98.49-1.87 1.12-2.66 1.85-.18.18-.43.28-.7.28-.28 0-.53-.11-.71-.29L.29 13.08a.996.996 0 0 1 0-1.41C3.34 8.78 7.46 7 12 7s8.66 1.78 11.71 4.67c.18.18.29.43.29.71 0 .28-.11.53-.29.71l-2.48 2.48c-.18.18-.43.29-.71.29-.27 0-.52-.11-.7-.28a11.27 11.27 0 0 0-2.67-1.85.996.996 0 0 1-.56-.9v-3.1C15.15 9.25 13.6 9 12 9z"
      />
    </svg>
  )
}

function MicIcon({ off }: { off: boolean }) {
  return (
    <svg viewBox="0 0 24 24" width="24" height="24" aria-hidden="true" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round">
      <rect x="9" y="3" width="6" height="11" rx="3" />
      <path d="M5.5 11a6.5 6.5 0 0 0 13 0M12 17.5V21" />
      {off && <path d="M4 4l16 16" />}
    </svg>
  )
}
