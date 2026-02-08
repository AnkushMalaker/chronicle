import React, { useEffect, useRef, useState } from 'react';
import { api } from '../../services/api';

interface WaveformData {
  samples: number[];
  sample_rate: number;
  duration_seconds: number;
}

interface WaveformDisplayProps {
  conversationId: string;
  duration: number;
  currentTime?: number;  // Current playback position in seconds
  onSeek?: (time: number) => void;  // Callback when user clicks to seek
  height?: number;  // Canvas height in pixels (default: 100)
}

export const WaveformDisplay: React.FC<WaveformDisplayProps> = ({
  conversationId,
  duration,
  currentTime,
  onSeek,
  height = 100
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

    // Draw playback position indicator
    if (currentTime !== undefined && duration > 0) {
      drawPlaybackIndicator(ctx, currentTime, duration, rect.width, height);
    }
  }, [waveformData, currentTime, duration, height]);

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
