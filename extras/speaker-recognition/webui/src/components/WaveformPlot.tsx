import { useEffect, useRef } from 'react'
import Plotly from 'plotly.js-dist-min'

interface WaveformPlotProps {
  samples: Float32Array
  sampleRate: number
  showSpectrogram?: boolean
  height?: number
  onTimeClick?: (time: number) => void
  playbackPosition?: number | null
  isPlaying?: boolean
}

export default function WaveformPlot({
  samples,
  sampleRate,
  showSpectrogram = false,
  height = 400,
  onTimeClick,
  playbackPosition,
  isPlaying
}: WaveformPlotProps) {
  const plotRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!plotRef.current) return

    // Downsample for display performance
    const maxPoints = 10000
    const step = Math.max(1, Math.floor(samples.length / maxPoints))
    const downsampledSamples: number[] = []
    const timePoints: number[] = []

    for (let i = 0; i < samples.length; i += step) {
      downsampledSamples.push(samples[i])
      timePoints.push(i / sampleRate)
    }

    const traces: any[] = []

    // Main waveform trace
    traces.push({
      x: timePoints,
      y: downsampledSamples,
      type: 'scatter',
      mode: 'lines',
      name: 'Waveform',
      line: { color: '#c2551f', width: 1 },
      hovertemplate: 'Time: %{x:.2f}s<br>Amplitude: %{y:.3f}<extra></extra>'
    })

    // Add playback position indicator
    if (playbackPosition !== null && playbackPosition !== undefined) {
      traces.push({
        x: [playbackPosition, playbackPosition],
        y: [-1.2, 1.2],
        type: 'scatter',
        mode: 'lines',
        name: 'Playback Position',
        line: { color: 'red', width: 2 },
        hovertemplate: `Position: ${playbackPosition.toFixed(2)}s<extra></extra>`,
        showlegend: false
      })
    }

    const layout: any = {
      title: { text: 'Audio Waveform' + (isPlaying ? ' (Playing)' : ' (Click to play)') },
      xaxis: {
        title: { text: 'Time (seconds)' },
        range: [0, Math.max(...timePoints)]
      },
      yaxis: {
        title: { text: 'Amplitude' },
        range: [-1.2, 1.2]
      },
      height,
      hovermode: 'x unified',
      showlegend: false,
      margin: { l: 60, r: 30, t: 50, b: 50 },
      dragmode: false
    }

    const config: any = {
      responsive: true,
      displayModeBar: true,
      modeBarButtonsToRemove: ['lasso2d', 'select2d'],
      displaylogo: false
    }

    Plotly.newPlot(plotRef.current, traces, layout, config)

    // Handle click events for playback
    const handleClick = (data: any) => {
      if (!onTimeClick) return

      const clickTime = data.points[0]?.x
      if (typeof clickTime === 'number') {
        onTimeClick(clickTime)
      }
    }

    ;(plotRef.current as any).on('plotly_click', handleClick)

    return () => {
      if (plotRef.current) {
        Plotly.purge(plotRef.current)
      }
    }
  }, [samples, sampleRate, height, onTimeClick, playbackPosition, isPlaying])

  // Add spectrogram subplot if requested
  useEffect(() => {
    if (!showSpectrogram || !plotRef.current) return

    // For now, we'll skip the spectrogram implementation to keep it simple
    // In a full implementation, you would compute the STFT and add a heatmap subplot
    console.log('Spectrogram view requested - not implemented yet')
  }, [showSpectrogram])

  return (
    <div className="w-full">
      <div ref={plotRef} className="w-full cursor-pointer" />
      {showSpectrogram && (
        <div className="mt-4 p-4 bg-gray-100 rounded-lg text-center text-gray-500">
          Spectrogram view not implemented yet
        </div>
      )}
    </div>
  )
}
