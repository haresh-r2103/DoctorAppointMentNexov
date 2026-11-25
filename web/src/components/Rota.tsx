/**
 * The rota: one floor-line per doctor across the clinic day (9 am to 7 pm).
 * Open slots are lit tiles on the line, taken slots are hatched. When Mira
 * books or cancels, that tile pulses.
 */
import { useEffect, useMemo, useState } from 'react'
import type { Doctor, Flash } from '../lib/api'
import { dayParts, lineColor, minutes, relativeDay, time12 } from '../lib/format'

const DAY_START = 9 * 60
const DAY_END = 19 * 60
const SPAN = DAY_END - DAY_START
const HOURS = Array.from({ length: 11 }, (_, i) => 9 + i)

export function Rota({ doctors, today, flash }: { doctors: Doctor[]; today: string; flash: Flash | null }) {
  const days = useMemo(() => {
    const set = new Set<string>()
    doctors.forEach((d) => d.slots.forEach((s) => set.add(s.day)))
    return [...set].sort()
  }, [doctors])
  const [day, setDay] = useState<string>('')
  const active = day && days.includes(day) ? day : days[0]

  // Jump to the day of whatever Mira just booked or cancelled.
  useEffect(() => {
    if (!flash) return
    const slot = doctors.flatMap((d) => d.slots).find((s) => s.id === flash.slotId)
    if (slot) setDay(slot.day)
  }, [flash, doctors])

  const nowPct = useNowPct(active === today)

  return (
    <section className="rota" aria-labelledby="rota-h">
      <div className="section-head">
        <h2 id="rota-h">Doctors and open slots</h2>
        <div className="day-tabs" role="tablist" aria-label="Day">
          {days.map((d) => {
            const p = dayParts(d)
            const open = doctors.reduce((n, doc) => n + doc.slots.filter((s) => s.day === d && !s.booked).length, 0)
            return (
              <button
                key={d}
                role="tab"
                aria-selected={d === active}
                className="day-tab"
                onClick={() => setDay(d)}
              >
                <span className="day-name">{relativeDay(d, today)}</span>
                <span className="day-num">{p.date}</span>
                <span className="day-open">{open} open</span>
              </button>
            )
          })}
        </div>
      </div>

      <div className="rota-grid">
        <div className="rota-axis" aria-hidden="true">
          <span />
          <div className="axis-track">
            {HOURS.map((h) => (
              <span key={h} className="tick" style={{ left: `${((h * 60 - DAY_START) / SPAN) * 100}%` }}>
                {h > 12 ? h - 12 : h}
                {h === 12 ? ' pm' : h === 9 ? ' am' : ''}
              </span>
            ))}
          </div>
        </div>

        {doctors.map((doc) => {
          const slots = doc.slots.filter((s) => s.day === active)
          const open = slots.filter((s) => !s.booked).length
          return (
            <div key={doc.id} className="rota-row" style={{ ['--line' as any]: lineColor(doc.id) }}>
              <div className="doc">
                <span className="doc-swatch" aria-hidden="true" />
                <div>
                  <p className="doc-name">{doc.name}</p>
                  <p className="doc-spec">
                    {doc.specialty}, {doc.room}
                  </p>
                </div>
                <span className="doc-open" title="Open slots this day">
                  {slots.length ? open : ''}
                </span>
              </div>
              <div className="track" role="list" aria-label={`${doc.name} on ${dayParts(active ?? today).long}`}>
                {HOURS.map((h) => (
                  <span key={h} className="track-hour" style={{ left: `${((h * 60 - DAY_START) / SPAN) * 100}%` }} />
                ))}
                {slots.length === 0 && <span className="track-off">Not in clinic</span>}
                {slots.map((s) => {
                  const left = ((minutes(s.start) - DAY_START) / SPAN) * 100
                  const pulsing = flash && flash.slotId === s.id ? `pulse-${flash.kind}` : ''
                  return (
                    <span
                      role="listitem"
                      key={`${s.id}-${flash?.slotId === s.id ? flash.at : 0}`}
                      className={`slot ${s.booked ? 'slot-taken' : 'slot-open'} ${pulsing}`}
                      style={{ left: `${left}%`, width: `${(30 / SPAN) * 100}%` }}
                      title={`${time12(s.start)} ${s.booked ? 'taken' : 'open'}`}
                      aria-label={`${time12(s.start)} ${s.booked ? 'taken' : 'open'}`}
                    >
                      <span className="slot-time">{time12(s.start).replace(/ (am|pm)/, '')}</span>
                    </span>
                  )
                })}
                {nowPct != null && <span className="now-line" style={{ left: `${nowPct}%` }} />}
              </div>
            </div>
          )
        })}
      </div>
      <p className="legend">
        <span className="legend-open" /> Open <span className="legend-taken" /> Taken
      </p>
    </section>
  )
}

function useNowPct(isToday: boolean) {
  const [pct, setPct] = useState<number | null>(null)
  useEffect(() => {
    if (!isToday) return setPct(null)
    const calc = () => {
      const n = new Date()
      const ist = new Date(n.toLocaleString('en-US', { timeZone: 'Asia/Kolkata' }))
      const m = ist.getHours() * 60 + ist.getMinutes()
      setPct(m < DAY_START || m > DAY_END ? null : ((m - DAY_START) / SPAN) * 100)
    }
    calc()
    const id = setInterval(calc, 60000)
    return () => clearInterval(id)
  }, [isToday])
  return pct
}
