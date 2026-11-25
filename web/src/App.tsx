import { useEffect, useState } from 'react'
import { Ledger } from './components/Ledger'
import { Phone } from './components/Phone'
import { Pipeline } from './components/Pipeline'
import { Rota } from './components/Rota'
import { useClinic } from './lib/api'
import { useCall } from './lib/useCall'

export default function App() {
  const clinic = useClinic()
  const call = useCall()
  const inCall = call.phase === 'ringing' || call.phase === 'connected'

  const openWeek = clinic.doctors.reduce((n, d) => n + d.slots.filter((s) => !s.booked).length, 0)
  const byMira = clinic.appointments.filter((a) => a.source === 'voice' && a.status === 'confirmed').length

  return (
    <div className={`app ${call.phase !== 'idle' ? 'has-phone' : ''}`}>
      <header className="topbar">
        <div className="brand">
          <span className="wordmark">nexov</span>
          <span className="brand-sub">Clinic front desk</span>
        </div>
        <div className="topbar-right">
          <ServerState online={clinic.online} />
          <Clock />
        </div>
      </header>

      <main>
        <section className="intro">
          <h1>Mira answers the clinic phone.</h1>
          <div className="intro-side">
            <p>
              Ask her for a doctor, a day or a symptom. She checks the rota, books or cancels, and confirms your number
              before she touches anything.
            </p>
            <div className="intro-actions">
              <button className="btn-pill btn-call" onClick={call.start} disabled={inCall}>
                <PhoneIcon /> {inCall ? 'On a call' : 'Call Mira'}
              </button>
              <dl className="facts">
                <div>
                  <dt>Open this week</dt>
                  <dd>{openWeek}</dd>
                </div>
                <div>
                  <dt>Booked by Mira</dt>
                  <dd>{byMira}</dd>
                </div>
              </dl>
            </div>
          </div>
        </section>

        <Rota doctors={clinic.doctors} today={clinic.today} flash={clinic.flash} />

        <div className="lower">
          <Ledger appointments={clinic.appointments} today={clinic.today} flash={clinic.flash} />
          <Pipeline call={call} />
        </div>
      </main>

      <footer className="foot">
        Built with Pipecat, ElevenLabs speech, Gemini and Silero VAD. Mock clinic data, reset daily.
      </footer>

      <Phone call={call} />
    </div>
  )
}

function ServerState({ online }: { online: boolean | null }) {
  if (online === null) return null
  return (
    <span className={`server ${online ? 'is-up' : 'is-down'}`}>
      <span className="server-dot" /> {online ? 'Line open' : 'Server offline'}
    </span>
  )
}

function Clock() {
  const [now, setNow] = useState(() => new Date())
  useEffect(() => {
    const id = setInterval(() => setNow(new Date()), 10000)
    return () => clearInterval(id)
  }, [])
  return (
    <time className="clock">
      {now.toLocaleString('en-IN', { weekday: 'short', day: 'numeric', month: 'short', hour: 'numeric', minute: '2-digit', timeZone: 'Asia/Kolkata' })}
    </time>
  )
}

function PhoneIcon() {
  return (
    <svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true" fill="currentColor">
      <path d="M6.6 10.8a15.1 15.1 0 0 0 6.6 6.6l2.2-2.2c.3-.3.7-.4 1-.2 1.1.4 2.3.6 3.6.6.6 0 1 .4 1 1V20c0 .6-.4 1-1 1A17 17 0 0 1 3 4c0-.6.4-1 1-1h3.5c.6 0 1 .4 1 1 0 1.3.2 2.5.6 3.6.1.3 0 .7-.2 1l-2.3 2.2z" />
    </svg>
  )
}
