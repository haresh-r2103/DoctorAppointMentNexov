/**
 * The live transcript inside the phone.
 *
 * Caller lines arrive from STT (interim text shown faint until final).
 * Mira's lines arrive as text segments ahead of the audio; the part already
 * spoken is bright and the rest faint, so the highlight follows her voice
 * word by word. If the caller talks over her, the unsaid rest is struck out.
 * Tool calls show up inline as compact receipts, and a booking or
 * cancellation becomes a full appointment card.
 */
import { useEffect, useRef } from 'react'
import type { Seg, ToolCall, Turn } from '../lib/useCall'
import { lineColor, shortName, time12 } from '../lib/format'

export function Transcript({ turns, version }: { turns: Turn[]; version: number }) {
  const box = useRef<HTMLDivElement>(null)
  const pinned = useRef(true)

  useEffect(() => {
    const el = box.current
    if (el && pinned.current) el.scrollTop = el.scrollHeight
  }, [version])

  return (
    <div
      className="transcript"
      ref={box}
      aria-live="polite"
      onScroll={(e) => {
        const el = e.currentTarget
        pinned.current = el.scrollHeight - el.scrollTop - el.clientHeight < 40
      }}
    >
      {turns.length === 0 && <p className="transcript-empty">Say hello when Mira picks up.</p>}
      {turns.map((t) =>
        t.role === 'user' ? (
          <p key={t.id} className="line line-user">
            {t.text}
            {t.interim && <span className="interim"> {t.interim}</span>}
          </p>
        ) : t.role === 'bot' ? (
          <p key={t.id} className="line line-bot">
            {t.segs.map((s) => (
              <SegText key={s.key} seg={s} />
            ))}
          </p>
        ) : (
          <ToolReceipt key={t.id} call={t.call} />
        ),
      )}
    </div>
  )
}

function SegText({ seg }: { seg: Seg }) {
  const said = seg.text.slice(0, seg.spoken)
  const rest = seg.text.slice(seg.spoken)
  return (
    <>
      <span className="said">{said}</span>
      {rest && <span className={seg.cut ? 'cut' : 'unsaid'}>{rest}</span>}{' '}
    </>
  )
}

const VERB: Record<string, [string, string]> = {
  check_doctor_availability: ['Checking the diary', 'Checked the diary'],
  book_appointment: ['Booking', 'Booking'],
  cancel_appointment: ['Cancelling', 'Cancellation'],
  find_my_appointments: ['Looking up bookings', 'Looked up bookings'],
}

function ToolReceipt({ call }: { call: ToolCall }) {
  const r = call.result
  if (call.status === 'done' && r?.ok && call.name === 'book_appointment') return <ApptCard appt={r.appointment} kind="booked" />
  if (call.status === 'done' && r?.ok && call.name === 'cancel_appointment') return <ApptCard appt={r.cancelled} kind="cancelled" />

  const [running, done] = VERB[call.name] ?? [call.name, call.name]
  const who = call.args?.doctor ? ` for ${String(call.args.doctor)}` : ''
  return (
    <div className={`receipt receipt-${call.status}`}>
      <div className="receipt-head">
        <span className="receipt-dot" />
        <span>{call.status === 'running' ? `${running}${who}` : `${done}${who}`}</span>
        {call.ms != null && <span className="receipt-ms">{call.ms} ms</span>}
      </div>
      {call.status === 'done' && r && <ReceiptBody call={call} />}
    </div>
  )
}

function ReceiptBody({ call }: { call: ToolCall }) {
  const r = call.result
  if (!r.ok) {
    const label: Record<string, string> = {
      identity_not_confirmed: 'Waiting for the caller to confirm their name and number',
      slot_unavailable: 'That time is taken',
      unknown_doctor: 'No doctor by that name',
      no_appointment: 'No booking under this name and number',
      multiple_appointments: 'Several bookings; asking which one',
      invalid_name: 'Need the full name',
      invalid_phone: 'Number incomplete',
    }
    return <p className="receipt-note">{label[r.error] ?? r.message}</p>
  }
  if (call.name === 'check_doctor_availability') {
    const rows = (r.results ?? []).filter((x: any) => x.availability.length).slice(0, 3)
    if (!rows.length) return <p className="receipt-note">No open slots</p>
    return (
      <div className="avail">
        {rows.map((x: any) => (
          <div key={x.doctor} className="avail-row" style={{ ['--line' as any]: lineColor(x.doctor_id) }}>
            <span className="avail-doc">{shortName(x.doctor)}</span>
            <span className="avail-day">{x.availability[0].date_spoken.replace(/ \(.*\)/, '')}</span>
            <span className="avail-times">{x.availability[0].free_slots.slice(0, 4).join(', ')}</span>
          </div>
        ))}
      </div>
    )
  }
  if (call.name === 'find_my_appointments') {
    const n = r.appointments?.length ?? 0
    return <p className="receipt-note">{n === 0 ? 'Nothing booked' : `${n} upcoming`}</p>
  }
  return null
}

export function ApptCard({ appt, kind }: { appt: any; kind: 'booked' | 'cancelled' }) {
  if (!appt) return null
  const d = new Date(appt.date + 'T00:00:00')
  return (
    <article className={`appt-card appt-${kind}`} style={{ ['--line' as any]: lineColor(appt.doctor_id) }}>
      <header>
        <span className="appt-state">{kind === 'booked' ? 'Booked' : 'Cancelled'}</span>
        <span className="appt-ref">{appt.ref}</span>
      </header>
      <div className="appt-main">
        <div className="appt-date">
          <span className="appt-dd">{d.getDate()}</span>
          <span className="appt-mm">{d.toLocaleDateString('en-IN', { month: 'short' })}</span>
        </div>
        <div>
          <p className="appt-doc">{appt.doctor}</p>
          <p className="appt-meta">
            {d.toLocaleDateString('en-IN', { weekday: 'long' })}, {time12(appt.time)}
          </p>
          <p className="appt-meta">
            {appt.specialty}, {appt.room}
          </p>
        </div>
      </div>
      <footer>
        <span>{appt.patient_name ?? 'Patient'}</span>
        <span>{appt.phone_masked}</span>
      </footer>
    </article>
  )
}
