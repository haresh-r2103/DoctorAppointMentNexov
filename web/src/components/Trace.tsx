/**
 * A two-lead "patient monitor" trace: Mira's voice on top, the caller's below.
 *
 * Every animation frame we read each voice's live audio samples from a Web
 * Audio AnalyserNode, take the strongest signed sample, and draw it at a
 * sweeping cursor, the way a bedside ECG monitor sweeps left to right and
 * erases just ahead of itself. Silence is a flat line; speech spikes.
 */
import { useEffect, useRef } from 'react'
import type { RefObject } from 'react'

type Props = {
  bot: RefObject<AnalyserNode | null>
  user: RefObject<AnalyserNode | null>
  active: boolean
  muted: boolean
}

const LEADS = [
  { color: '#9FF2D0', key: 'bot' as const },
  { color: '#9CC8FF', key: 'user' as const },
]

export function Trace({ bot, user, active, muted }: Props) {
  const canvas = useRef<HTMLCanvasElement>(null)

  useEffect(() => {
    const c = canvas.current
    if (!c) return
    const g = c.getContext('2d')!
    const dpr = Math.min(window.devicePixelRatio || 1, 2)
    const resize = () => {
      c.width = c.clientWidth * dpr
      c.height = c.clientHeight * dpr
      g.setTransform(dpr, 0, 0, dpr, 0, 0)
      g.clearRect(0, 0, c.clientWidth, c.clientHeight)
    }
    resize()
    const ro = new ResizeObserver(resize)
    ro.observe(c)

    const reduce = window.matchMedia('(prefers-reduced-motion: reduce)').matches
    const buf = new Float32Array(1024)
    const prevY = [0, 0]
    let x = 0
    let raf = 0
    let t = 0

    const sample = (an: AnalyserNode | null) => {
      if (!an) return 0
      an.getFloatTimeDomainData(buf)
      let peak = 0
      for (let i = 0; i < buf.length; i++) if (Math.abs(buf[i]) > Math.abs(peak)) peak = buf[i]
      return Math.max(-1, Math.min(1, peak * 2.4))
    }

    const draw = () => {
      const w = c.clientWidth
      const h = c.clientHeight
      const laneH = h / 2
      const step = reduce ? 0 : 2
      t += 1
      // Erase a small window ahead of the cursor (the monitor's "gap").
      g.clearRect(x, 0, 16, h)
      LEADS.forEach((lead, i) => {
        const an = lead.key === 'bot' ? bot.current : muted ? null : user.current
        const mid = laneH * i + laneH / 2
        const noise = Math.sin(t * 0.21 + i * 2) * 0.6 // tiny baseline wander
        const y = mid - (active ? sample(an) : 0) * (laneH * 0.42) + noise
        g.strokeStyle = lead.color
        g.lineWidth = 1.6
        g.shadowColor = lead.color
        g.shadowBlur = 6
        g.beginPath()
        g.moveTo(x, prevY[i] || mid)
        g.lineTo(x + step, y)
        g.stroke()
        prevY[i] = y
      })
      // Bright cursor dot on each lead.
      g.shadowBlur = 0
      x += step
      if (x > w) {
        x = 0
        prevY[0] = prevY[1] = 0
      }
      raf = requestAnimationFrame(draw)
    }
    raf = requestAnimationFrame(draw)
    return () => {
      cancelAnimationFrame(raf)
      ro.disconnect()
    }
  }, [bot, user, active, muted])

  return <canvas ref={canvas} className="trace-canvas" aria-hidden="true" />
}
