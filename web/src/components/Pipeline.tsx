/**
 * What the Pipecat pipeline is doing right now: each stage lights up as the
 * RTVI events arrive, with each service's latest time-to-first-byte, and every
 * tool call is logged with the exact arguments Gemini sent and what it got back.
 */
import type { CallState, Stage } from '../lib/useCall'

const NODES: { id: string; name: string; by: string; on: Stage[]; metric?: 'stt' | 'llm' | 'tts' }[] = [
  { id: 'mic', name: 'Your mic', by: 'WebRTC', on: ['hearing'] },
  { id: 'vad', name: 'Turn detection', by: 'Silero VAD', on: ['hearing', 'listening'] },
  { id: 'stt', name: 'Speech to text', by: 'ElevenLabs', on: ['hearing'], metric: 'stt' },
  { id: 'llm', name: 'Reasoning', by: 'Gemini', on: ['thinking'], metric: 'llm' },
  { id: 'tools', name: 'Clinic tools', by: 'SQLite', on: ['tool'] },
  { id: 'tts', name: 'Text to speech', by: 'ElevenLabs', on: ['speaking'], metric: 'tts' },
  { id: 'out', name: 'Speaker', by: 'WebRTC', on: ['speaking'] },
]

const TOOL_NAME: Record<string, string> = {
  check_doctor_availability: 'check_doctor_availability',
  book_appointment: 'book_appointment',
  cancel_appointment: 'cancel_appointment',
  find_my_appointments: 'find_my_appointments',
}

export function Pipeline({ call }: { call: CallState }) {
  const live = call.phase === 'connected'
  return (
    <section className="pipeline" aria-labelledby="pipe-h">
      <div className="section-head">
        <h2 id="pipe-h">Inside the call</h2>
        <span className={`live-pill ${live ? 'is-live' : ''}`}>{live ? 'Live' : 'No call'}</span>
      </div>

      <ol className="chain">
        {NODES.map((n) => {
          const on = live && n.on.includes(call.stage)
          const ms = n.metric ? call.latency[n.metric] : undefined
          return (
            <li key={n.id} className={`node ${on ? 'is-on' : ''} ${n.id === 'vad' && call.stage === 'listening' && live ? 'is-idle' : ''}`}>
              <span className="node-pin" aria-hidden="true" />
              <span className="node-name">{n.name}</span>
              <span className="node-by">{n.by}</span>
              {ms != null && <span className="node-ms">{ms} ms</span>}
            </li>
          )
        })}
      </ol>

      <div className="toollog">
        {call.toolCalls.length === 0 ? (
          <p className="empty">Tool calls appear here with their arguments and results.</p>
        ) : (
          [...call.toolCalls].reverse().map((c) => (
            <details key={c.id} className={`tool-entry tool-${c.status} ${c.result?.ok === false ? 'tool-refused' : ''}`}>
              <summary>
                <code>{TOOL_NAME[c.name] ?? c.name}</code>
                <span className="tool-outcome">
                  {c.status === 'running' ? 'running' : c.result?.ok ? 'ok' : (c.result?.error ?? c.status)}
                </span>
                {c.ms != null && <span className="tool-ms">{c.ms} ms</span>}
              </summary>
              <div className="tool-io">
                <p>Arguments</p>
                <pre>{JSON.stringify(c.args ?? {}, null, 2)}</pre>
                {c.result && (
                  <>
                    <p>Result</p>
                    <pre>{JSON.stringify(c.result, null, 2)}</pre>
                  </>
                )}
              </div>
            </details>
          ))
        )}
      </div>
    </section>
  )
}
