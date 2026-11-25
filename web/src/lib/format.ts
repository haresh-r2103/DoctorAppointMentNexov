// Small formatting helpers and the wayfinding colour each doctor is given.

/** Each doctor owns a hospital-style floor-line colour, used everywhere they appear. */
export const WAYFINDING: Record<string, string> = {
  rao: '#2E9E6A', // green
  mehta: '#E5533D', // red
  iyer: '#8A5CD0', // violet
  nair: '#E3A21A', // amber
  khan: '#2F6FDB', // blue
  sethi: '#1597A8', // cyan
}
export const lineColor = (doctorId: string) => WAYFINDING[doctorId] ?? '#5B6B6A'

export function time12(hhmm: string) {
  const [h, m] = hhmm.split(':').map(Number)
  const suffix = h >= 12 ? 'pm' : 'am'
  const h12 = h % 12 || 12
  return m ? `${h12}:${String(m).padStart(2, '0')} ${suffix}` : `${h12} ${suffix}`
}

export function dayParts(iso: string) {
  const d = new Date(iso + 'T00:00:00')
  return {
    weekday: d.toLocaleDateString('en-IN', { weekday: 'short' }),
    date: d.getDate(),
    month: d.toLocaleDateString('en-IN', { month: 'short' }),
    long: d.toLocaleDateString('en-IN', { weekday: 'long', day: 'numeric', month: 'long' }),
  }
}

export function relativeDay(iso: string, today: string) {
  if (iso === today) return 'Today'
  const diff = Math.round((+new Date(iso) - +new Date(today)) / 86400000)
  if (diff === 1) return 'Tomorrow'
  return dayParts(iso).weekday
}

export function clock(secs: number) {
  const m = Math.floor(secs / 60)
  const s = Math.floor(secs % 60)
  return `${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`
}

export const minutes = (hhmm: string) => {
  const [h, m] = hhmm.split(':').map(Number)
  return h * 60 + m
}

export const shortName = (name: string) => name.replace(/^Dr\.\s*/, '').split(' ').slice(-1)[0]
