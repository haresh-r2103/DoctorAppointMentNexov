// Call sounds synthesised with the Web Audio API: no audio files to ship.

let ctx: AudioContext | null = null
const ac = () => (ctx ??= new AudioContext())

function tone(freqs: number[], start: number, dur: number, gain = 0.08) {
  const a = ac()
  const g = a.createGain()
  g.connect(a.destination)
  g.gain.setValueAtTime(0, start)
  g.gain.linearRampToValueAtTime(gain, start + 0.015)
  g.gain.setValueAtTime(gain, start + dur - 0.03)
  g.gain.linearRampToValueAtTime(0, start + dur)
  for (const f of freqs) {
    const o = a.createOscillator()
    o.type = 'sine'
    o.frequency.value = f
    o.connect(g)
    o.start(start)
    o.stop(start + dur + 0.02)
  }
}

/** Indian-style double ring (400+450 Hz, ring-ring … pause). Returns a stop function. */
export function startRinging(): () => void {
  const a = ac()
  a.resume()
  let stopped = false
  let timer = 0
  const cycle = () => {
    if (stopped) return
    const t = a.currentTime + 0.02
    tone([400, 450], t, 0.4, 0.06)
    tone([400, 450], t + 0.6, 0.4, 0.06)
    timer = window.setTimeout(cycle, 3000)
  }
  cycle()
  return () => {
    stopped = true
    clearTimeout(timer)
  }
}

export function playConnected() {
  const t = ac().currentTime + 0.01
  tone([880], t, 0.08, 0.05)
  tone([1320], t + 0.1, 0.12, 0.05)
}

export function playHangup() {
  const t = ac().currentTime + 0.01
  ;[620, 520, 420].forEach((f, i) => tone([f], t + i * 0.18, 0.14, 0.06))
}

export function playBooked() {
  const t = ac().currentTime + 0.01
  ;[659, 831, 988].forEach((f, i) => tone([f], t + i * 0.09, 0.22, 0.045))
}

export function playCancelled() {
  const t = ac().currentTime + 0.01
  ;[740, 554].forEach((f, i) => tone([f], t + i * 0.12, 0.2, 0.045))
}
