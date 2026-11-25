// Data the dashboard reads from the Python server, plus the live event stream.
import { useCallback, useEffect, useRef, useState } from 'react'

export type Slot = { id: number; day: string; start: string; booked: boolean }
export type Doctor = { id: string; name: string; specialty: string; room: string; slots: Slot[] }
export type Appointment = {
  id: number
  ref: string
  patient_name: string | null
  phone_masked: string
  reason: string | null
  doctor_id: string
  doctor_name: string
  specialty: string
  slot_id: number
  day: string
  start: string
  status: 'confirmed' | 'cancelled'
  source: 'voice' | 'walk-in'
  created_at: string
  cancelled_at: string | null
}
export type Health = { ok: boolean; elevenlabs: boolean; google: boolean; model: string }

export type Flash = { kind: 'booked' | 'cancelled'; apptId: number; slotId: number; at: number }

async function getJSON<T>(url: string): Promise<T> {
  const r = await fetch(url)
  if (!r.ok) throw new Error(`${url} answered ${r.status}`)
  return r.json()
}

export const fetchHealth = () => getJSON<Health>('/api/health')

/**
 * Loads doctors + appointments, then keeps them fresh by listening to
 * Server-Sent Events. Every booking or cancellation the agent makes arrives
 * here within milliseconds; we refetch and remember what changed so the UI
 * can highlight the exact row and slot.
 */
export function useClinic() {
  const [doctors, setDoctors] = useState<Doctor[]>([])
  const [appointments, setAppointments] = useState<Appointment[]>([])
  const [today, setToday] = useState<string>('')
  const [online, setOnline] = useState<boolean | null>(null)
  const [flash, setFlash] = useState<Flash | null>(null)
  const loading = useRef(false)

  const refresh = useCallback(async () => {
    if (loading.current) return
    loading.current = true
    try {
      const [d, a] = await Promise.all([
        getJSON<{ today: string; doctors: Doctor[] }>('/api/doctors'),
        getJSON<{ appointments: Appointment[] }>('/api/appointments'),
      ])
      setDoctors(d.doctors)
      setToday(d.today)
      setAppointments(a.appointments)
      setOnline(true)
    } catch {
      setOnline(false)
    } finally {
      loading.current = false
    }
  }, [])

  useEffect(() => {
    refresh()
    const es = new EventSource('/api/events')
    es.onopen = () => setOnline(true)
    es.onerror = () => setOnline(false)
    es.onmessage = (msg) => {
      const { event, data } = JSON.parse(msg.data)
      if (event === 'appointment.booked' || event === 'appointment.cancelled') {
        const a = data.appointment
        setFlash({
          kind: event === 'appointment.booked' ? 'booked' : 'cancelled',
          apptId: a.id,
          slotId: a.slot_id,
          at: Date.now(),
        })
      }
      refresh()
    }
    return () => es.close()
  }, [refresh])

  return { doctors, appointments, today, online, flash, refresh }
}
