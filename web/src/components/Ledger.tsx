/** The appointment list. Rows Mira just touched light up and scroll into view. */
import { useEffect, useMemo, useRef, useState } from 'react'
import type { Appointment, Flash } from '../lib/api'
import { dayParts, lineColor, relativeDay, time12 } from '../lib/format'

type View = 'mira' | 'upcoming' | 'cancelled'

/** "YYYY-MM-DDHH:MM" for now in clinic time, comparable with day + start. */
function nowKey() {
  const p = new Intl.DateTimeFormat('en-CA', {
    timeZone: 'Asia/Kolkata', year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hourCycle: 'h23',
  }).formatToParts(new Date())
  const g = (t: string) => p.find((x) => x.type === t)!.value
  return `${g('year')}-${g('month')}-${g('day')}${g('hour')}:${g('minute')}`
}

export function Ledger({ appointments, today, flash }: { appointments: Appointment[]; today: string; flash: Flash | null }) {
  const [view, setView] = useState<View>('upcoming')
  const rowRefs = useRef(new Map<number, HTMLTableRowElement>())

  const lists = useMemo(() => {
    const byWhen = (a: Appointment, b: Appointment) => (a.day + a.start).localeCompare(b.day + b.start)
    const byRecent = (a: Appointment, b: Appointment) =>
      (b.cancelled_at ?? b.created_at).localeCompare(a.cancelled_at ?? a.created_at)
    return {
      mira: appointments.filter((a) => a.source === 'voice').sort(byRecent),
      upcoming: appointments.filter((a) => a.status === 'confirmed' && a.day + a.start > nowKey()).sort(byWhen),
      cancelled: appointments.filter((a) => a.status === 'cancelled').sort(byRecent),
    }
  }, [appointments, today])

  // A booking or cancellation from a call: show it under "Booked by Mira" and scroll to it.
  useEffect(() => {
    if (flash) setView('mira')
  }, [flash])
  useEffect(() => {
    if (!flash) return
    const el = rowRefs.current.get(flash.apptId)
    el?.scrollIntoView({ block: 'nearest', behavior: 'smooth' })
  }, [flash, view, appointments])

  const rows = lists[view]
  const tabs: [View, string][] = [
    ['upcoming', 'Upcoming'],
    ['mira', 'Booked by Mira'],
    ['cancelled', 'Cancelled'],
  ]

  return (
    <section className="ledger" aria-labelledby="ledger-h">
      <div className="section-head">
        <h2 id="ledger-h">Appointments</h2>
        <div className="seg" role="tablist" aria-label="Filter appointments">
          {tabs.map(([k, label]) => (
            <button key={k} role="tab" aria-selected={view === k} onClick={() => setView(k)}>
              {label} <span className="seg-n">{lists[k].length}</span>
            </button>
          ))}
        </div>
      </div>

      <div className="ledger-scroll">
        {rows.length === 0 ? (
          <p className="empty">
            {view === 'mira' ? 'Nothing yet. Call Mira and book a slot; it shows up here.' : 'Nothing here.'}
          </p>
        ) : (
          <table>
            <thead>
              <tr>
                <th>Ref</th>
                <th>Patient</th>
                <th>Doctor</th>
                <th>When</th>
                <th>Status</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((a) => {
                const hit = flash?.apptId === a.id
                return (
                  <tr
                    key={`${a.id}-${hit ? flash!.at : 0}`}
                    ref={(el) => {
                      if (el) rowRefs.current.set(a.id, el)
                      else rowRefs.current.delete(a.id)
                    }}
                    className={`${a.status === 'cancelled' ? 'is-cancelled' : ''} ${hit ? `row-flash row-${flash!.kind}` : ''}`}
                    style={{ ['--line' as any]: lineColor(a.doctor_id) }}
                  >
                    <td className="ref" data-label="Ref">
                      {a.ref}
                    </td>
                    <td data-label="Patient">
                      <span className="pt-phone">{a.patient_name}</span>
                      {a.phone_masked && <span className="pt-name">{a.phone_masked}</span>}
                    </td>
                    <td data-label="Doctor">
                      <span className="doc-dot" aria-hidden="true" />
                      {a.doctor_name}
                    </td>
                    <td data-label="When">
                      {relativeDay(a.day, today)} {dayParts(a.day).date} {dayParts(a.day).month}, {time12(a.start)}
                    </td>
                    <td data-label="Status">
                      <span className={`status status-${a.status}`}>{a.status === 'confirmed' ? 'Confirmed' : 'Cancelled'}</span>
                      {a.source === 'voice' && <span className="by-mira">by Mira</span>}
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        )}
      </div>
    </section>
  )
}
