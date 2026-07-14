import { useEffect, useState } from 'react'

// Browser-persisted toggle for "auto-zoom the waveform when editing a segment".
// Backed by localStorage + a tiny module store so the hamburger toggle and the editor
// stay in sync within the same tab (the native 'storage' event only fires cross-tab).
const KEY = 'chronicle:waveformZoomDisabled'

const read = (): boolean => {
  try {
    return localStorage.getItem(KEY) === '1'
  } catch {
    return false
  }
}

let current = read()
let listeners: Array<(v: boolean) => void> = []

export function setWaveformZoomDisabled(v: boolean): void {
  current = v
  try {
    localStorage.setItem(KEY, v ? '1' : '0')
  } catch {
    /* ignore */
  }
  listeners.forEach((l) => l(v))
}

/** [disabled, setDisabled] — "disabled" = don't auto-zoom the waveform on edit. */
export function useWaveformZoomDisabled(): [boolean, (v: boolean) => void] {
  const [v, setV] = useState(current)
  useEffect(() => {
    const l = (nv: boolean) => setV(nv)
    listeners.push(l)
    return () => {
      listeners = listeners.filter((x) => x !== l)
    }
  }, [])
  return [v, setWaveformZoomDisabled]
}
