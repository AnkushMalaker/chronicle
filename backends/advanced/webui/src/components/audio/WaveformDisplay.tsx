import React, { useEffect, useRef, useState } from 'react';
import { api } from '../../services/api';

interface WaveformData {
  samples: number[];
  sample_rate: number;
  duration_seconds: number;
}

// A transcript-segment band rendered on the waveform.
// 'playing': segment audio is currently playing — strong highlight
// 'anchor':  last-played segment — faint highlight kept as a location reference
// 'hover':   segment row is hovered — faintest, dashed edges
export interface SegmentMarker {
  start: number;
  end: number;
  kind: 'playing' | 'anchor' | 'hover';
}

interface WaveformDisplayProps {
  conversationId: string;
  duration: number;
  currentTime?: number;  // Current playback position in seconds
  onSeek?: (time: number) => void;  // Callback when user clicks to seek
  height?: number;  // Canvas height in pixels (default: 100)
  segments?: { start: number; end: number }[];  // All transcript segments — drawn as faint base bands when >1
  segmentMarkers?: SegmentMarker[];  // Transcript segment bands (playing/anchor/hover)
}

export const WaveformDisplay: React.FC<WaveformDisplayProps> = ({
  conversationId,
  duration,
  currentTime,
  onSeek,
  height = 100,
  segments,
  segmentMarkers,
}) => {
  const [waveformData, setWaveformData] = useState<WaveformData | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const canvasRef = useRef<HTMLCanvasElement>(null);

  // Fetch waveform data on component mount
  useEffect(() => {
    const fetchWaveform = async () => {
      setLoading(true);
      setError(null);

      try {
        const response = await api.get(`/api/conversations/${conversationId}/waveform`);
        setWaveformData(response.data);
      } catch (err: any) {
        const errorMsg = err?.response?.data?.detail || err?.message || 'Failed to load waveform';
        console.error('Waveform fetch failed:', errorMsg);
        setError(errorMsg);
      } finally {
        setLoading(false);
      }
    };

    fetchWaveform();
  }, [conversationId]);

  // Draw waveform when data changes
  useEffect(() => {
    if (!waveformData || !canvasRef.current) return;

    const canvas = canvasRef.current;
    const ctx = canvas.getContext('2d');
    if (!ctx) return;

    // Set canvas size
    const rect = canvas.getBoundingClientRect();
    canvas.width = rect.width * window.devicePixelRatio;
    canvas.height = height * window.devicePixelRatio;
    ctx.scale(window.devicePixelRatio, window.devicePixelRatio);

    // Clear canvas
    ctx.clearRect(0, 0, rect.width, height);

    // Draw waveform bars
    drawWaveform(ctx, waveformData.samples, rect.width, height);

    // Draw faint base bands for every transcript segment (only meaningful when there's >1)
    if (segments && segments.length > 1 && duration > 0) {
      segments.forEach(seg =>
        drawSegmentMarker(ctx, { start: seg.start, end: seg.end, kind: 'base' }, duration, rect.width, height)
      );
    }

    // Draw transcript segment bands (under the playhead so the line stays visible)
    if (segmentMarkers && duration > 0) {
      segmentMarkers.forEach(marker => drawSegmentMarker(ctx, marker, duration, rect.width, height));
    }

    // Draw playback position indicator
    if (currentTime !== undefined && duration > 0) {
      drawPlaybackIndicator(ctx, currentTime, duration, rect.width, height);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [waveformData, currentTime, duration, height, segments, JSON.stringify(segmentMarkers)]);

  const drawWaveform = (
    ctx: CanvasRenderingContext2D,
    samples: number[],
    width: number,
    height: number
  ) => {
    const barWidth = width / samples.length;
    const centerY = height / 2;

    ctx.fillStyle = '#3b82f6'; // Blue bars (Tailwind blue-500)

    samples.forEach((amplitude, i) => {
      const x = i * barWidth;
      const barHeight = Math.max(1, amplitude * centerY); // Ensure minimum 1px height

      // Draw bar centered vertically
      ctx.fillRect(x, centerY - barHeight, barWidth - 1, barHeight * 2);
    });
  };

  const drawSegmentMarker = (
    ctx: CanvasRenderingContext2D,
    marker: Omit<SegmentMarker, 'kind'> & { kind: SegmentMarker['kind'] | 'base' },
    duration: number,
    width: number,
    height: number
  ) => {
    const x1 = (marker.start / duration) * width;
    // Ensure even sub-second segments stay visible (min 3px band)
    const x2 = Math.max((marker.end / duration) * width, x1 + 3);

    const styles = {
      // 'base': every segment, drawn faintly so the timeline's segmentation is always visible
      base: { fill: 'rgba(99, 102, 241, 0.05)', edge: 'rgba(99, 102, 241, 0.20)', dash: [] as number[] },
      playing: { fill: 'rgba(99, 102, 241, 0.22)', edge: 'rgba(99, 102, 241, 0.9)', dash: [] as number[] },
      anchor: { fill: 'rgba(99, 102, 241, 0.10)', edge: 'rgba(99, 102, 241, 0.45)', dash: [] as number[] },
      hover: { fill: 'rgba(99, 102, 241, 0.08)', edge: 'rgba(99, 102, 241, 0.5)', dash: [3, 3] },
    }[marker.kind];

    ctx.fillStyle = styles.fill;
    ctx.fillRect(x1, 0, x2 - x1, height);

    ctx.strokeStyle = styles.edge;
    ctx.lineWidth = 1.5;
    ctx.setLineDash(styles.dash);
    ctx.beginPath();
    ctx.moveTo(x1, 0);
    ctx.lineTo(x1, height);
    ctx.moveTo(x2, 0);
    ctx.lineTo(x2, height);
    ctx.stroke();
    ctx.setLineDash([]);
  };

  const drawPlaybackIndicator = (
    ctx: CanvasRenderingContext2D,
    currentTime: number,
    duration: number,
    width: number,
    height: number
  ) => {
    const progress = currentTime / duration;
    const x = progress * width;

    // Draw vertical line
    ctx.strokeStyle = '#ef4444'; // Red line (Tailwind red-500)
    ctx.lineWidth = 2;
    ctx.beginPath();
    ctx.moveTo(x, 0);
    ctx.lineTo(x, height);
    ctx.stroke();
  };

  const handleClick = (e: React.MouseEvent<HTMLCanvasElement>) => {
    console.log('🖱️ Waveform clicked!');

    if (!onSeek) {
      console.warn('⚠️ No onSeek callback provided');
      return;
    }

    if (!canvasRef.current) {
      console.warn('⚠️ Canvas ref not available');
      return;
    }

    const rect = canvasRef.current.getBoundingClientRect();
    const x = e.clientX - rect.left;
    const seekProgress = x / rect.width;
    const seekTime = seekProgress * duration;

    console.log(`🎵 Waveform seek: clicked at ${x}px (${(seekProgress * 100).toFixed(1)}%) → ${seekTime.toFixed(2)}s`);

    onSeek(seekTime);
  };

  // Render loading state
  if (loading) {
    return (
      <div
        className="w-full bg-gray-100 rounded animate-pulse flex items-center justify-center"
        style={{ height: `${height}px` }}
      >
        <span className="text-gray-400 text-sm">Generating waveform...</span>
      </div>
    );
  }

  // Render error state
  if (error) {
    return (
      <div
        className="w-full bg-gray-50 border border-gray-200 rounded flex items-center justify-center"
        style={{ height: `${height}px` }}
      >
        <span className="text-gray-400 text-sm">No waveform available</span>
      </div>
    );
  }

  // Render waveform
  return (
    <canvas
      ref={canvasRef}
      onClick={handleClick}
      className="w-full cursor-pointer hover:opacity-80 transition-opacity rounded"
      style={{ height: `${height}px` }}
      title="Click to seek to position"
    />
  );
};
