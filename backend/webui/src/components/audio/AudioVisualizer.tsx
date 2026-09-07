import { useRef, useEffect, useCallback } from 'react'

interface AudioVisualizerProps {
  isRecording: boolean
  analyser: AnalyserNode | null
}

export default function AudioVisualizer({ isRecording, analyser }: AudioVisualizerProps) {
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const animationIdRef = useRef<number>()

  const drawWaveform = useCallback(() => {
    if (!analyser || !canvasRef.current) return

    const canvas = canvasRef.current
    const canvasCtx = canvas.getContext('2d')
    if (!canvasCtx) return

    const bufferLength = analyser.frequencyBinCount
    const dataArray = new Uint8Array(bufferLength)

    const draw = () => {
      if (!isRecording) return

      analyser.getByteFrequencyData(dataArray)

      canvasCtx.fillStyle = 'rgb(17, 24, 39)' // gray-900
      canvasCtx.fillRect(0, 0, canvas.width, canvas.height)

      const barWidth = (canvas.width / bufferLength) * 2.5
      let barHeight
      let x = 0

      for (let i = 0; i < bufferLength; i++) {
        barHeight = (dataArray[i] / 255) * canvas.height

        // Gradient from blue to green based on intensity
        const intensity = dataArray[i] / 255
        const red = Math.floor(59 * (1 - intensity) + 34 * intensity)
        const green = Math.floor(130 * (1 - intensity) + 197 * intensity)
        const blue = Math.floor(246 * (1 - intensity) + 94 * intensity)

        canvasCtx.fillStyle = `rgb(${red},${green},${blue})`
        canvasCtx.fillRect(x, canvas.height - barHeight, barWidth, barHeight)

        x += barWidth + 1
      }

      animationIdRef.current = requestAnimationFrame(draw)
    }

    draw()
  }, [analyser, isRecording])

  useEffect(() => {
    if (isRecording && analyser) {
      drawWaveform()
    } else {
      if (animationIdRef.current) {
        cancelAnimationFrame(animationIdRef.current)
      }

      // Clear canvas
      if (canvasRef.current) {
        const canvasCtx = canvasRef.current.getContext('2d')
        if (canvasCtx) {
          canvasCtx.fillStyle = 'rgb(17, 24, 39)'
          canvasCtx.fillRect(0, 0, canvasRef.current.width, canvasRef.current.height)
        }
      }
    }

    return () => {
      if (animationIdRef.current) {
        cancelAnimationFrame(animationIdRef.current)
      }
    }
  }, [isRecording, analyser, drawWaveform])

  return (
    <div className="bg-gray-900 rounded-lg p-4 border border-gray-700">
      <canvas
        ref={canvasRef}
        width={600}
        height={100}
        className="w-full h-24 bg-gray-900 rounded"
      />
      <p className="text-center text-sm text-gray-400 mt-2">
        {isRecording ? 'Audio Waveform - Recording...' : 'Audio Waveform - Ready'}
      </p>
    </div>
  )
}
