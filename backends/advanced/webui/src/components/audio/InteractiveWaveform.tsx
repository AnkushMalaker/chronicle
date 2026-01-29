import React, { useEffect, useRef, useState, useMemo } from 'react';
import * as d3 from 'd3';
import { api } from '../../services/api';

interface WaveformData {
  samples: number[];
  sample_rate: number;
  duration_seconds: number;
}

export interface Segment {
  text: string;
  speaker: string;
  start: number;
  end: number;
  confidence?: number;
}

interface InteractiveWaveformProps {
  conversationId: string;
  duration: number;
  segments?: Segment[];
  currentTime?: number;
  onSeek?: (time: number) => void;
  onSegmentChange?: (index: number, newStart: number, newEnd: number) => void;
  height?: number;
}

export const InteractiveWaveform: React.FC<InteractiveWaveformProps> = ({
  conversationId,
  duration,
  segments = [],
  currentTime = 0,
  onSeek,
  onSegmentChange,
  height = 160 // Increased default height for segments
}) => {
  const containerRef = useRef<HTMLDivElement>(null);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const svgRef = useRef<SVGSVGElement>(null);
  
  const [waveformData, setWaveformData] = useState<WaveformData | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // Zoom state
  const [transform, setTransform] = useState<d3.ZoomTransform>(d3.zoomIdentity);
  const zoomBehavior = useRef<d3.ZoomBehavior<SVGSVGElement, unknown> | null>(null);

  // Fetch waveform
  useEffect(() => {
    const fetchWaveform = async () => {
      setLoading(true);
      try {
        const response = await api.get(`/api/conversations/${conversationId}/waveform`);
        setWaveformData(response.data);
      } catch (err: any) {
        console.error('Waveform fetch failed:', err);
        setError('Failed to load waveform');
      } finally {
        setLoading(false);
      }
    };
    fetchWaveform();
  }, [conversationId]);

  // Setup D3 Zoom
  useEffect(() => {
    if (!svgRef.current || !containerRef.current) return;

    const width = containerRef.current.clientWidth;
    const extent: [[number, number], [number, number]] = [[0, 0], [width, height]];

    const zoom = d3.zoom<SVGSVGElement, unknown>()
      .scaleExtent([1, 50]) // Zoom up to 50x
      .translateExtent(extent)
      .extent(extent)
      .on('zoom', (event) => {
        setTransform(event.transform);
      });
      
    zoomBehavior.current = zoom;

    const svg = d3.select(svgRef.current);
    svg.call(zoom);
    
    // Initial call to set transform if needed, but usually identity is fine
  }, [height]);

  const xScale = useMemo(() => {
    if (!containerRef.current) return d3.scaleLinear().domain([0, duration]).range([0, 100]);
    const width = containerRef.current.clientWidth;
    return transform.rescaleX(d3.scaleLinear().domain([0, duration]).range([0, width]));
  }, [duration, transform, containerRef.current?.clientWidth]);

  // Draw Waveform on Canvas
  useEffect(() => {
    if (!canvasRef.current || !waveformData || !containerRef.current) return;

    const canvas = canvasRef.current;
    const ctx = canvas.getContext('2d');
    if (!ctx) return;

    const width = containerRef.current.clientWidth;
    const pixelRatio = window.devicePixelRatio || 1;
    
    canvas.width = width * pixelRatio;
    canvas.height = height * pixelRatio;
    ctx.scale(pixelRatio, pixelRatio);
    
    // Clear
    ctx.clearRect(0, 0, width, height);

    // Draw params
    const samples = waveformData.samples;
    const totalSamples = samples.length;
    // We only want to draw the visible part for performance ideally, 
    // but for simplicity drawing all with transform is okay for first pass 
    // or we map visible domain to sample indices.
    
    // Optimization: Draw only visible samples
    const [t0, t1] = xScale.domain();
    const s0 = Math.floor((t0 / duration) * totalSamples);
    const s1 = Math.ceil((t1 / duration) * totalSamples);
    
    const visibleSamples = samples.slice(Math.max(0, s0), Math.min(totalSamples, s1));
    const sampleWidth = (width / (t1 - t0)) * (duration / totalSamples); // approximate pixel width per sample

    // Using a step approach if too many samples
    const step = Math.ceil(visibleSamples.length / width); // roughly 1 bar per pixel
    
    ctx.fillStyle = '#3b82f6'; // Blue
    const centerY = height / 2;
    const waveHeight = height * 0.4; // 40% height for waveform

    ctx.beginPath();
    for (let i = 0; i < visibleSamples.length; i += step) {
       // Find max in chunk to avoid aliasing
       let maxAmp = 0;
       for (let j = 0; j < step && i + j < visibleSamples.length; j++) {
         maxAmp = Math.max(maxAmp, Math.abs(visibleSamples[i+j]));
       }
       
       const sampleIndex = s0 + i;
       const time = (sampleIndex / totalSamples) * duration;
       const x = xScale(time);
       const h = Math.max(1, maxAmp * waveHeight);
       
       ctx.rect(x - (sampleWidth*step)/2, centerY - h, Math.max(1, sampleWidth * step), h * 2);
    }
    ctx.fill();

  }, [waveformData, xScale, height, duration]);

  // Custom Hook or Ref-callback for Draggable Elements
  const useDraggable = (
      type: 'move' | 'resize-left' | 'resize-right', 
      segment: Segment, 
      index: number
  ) => {
      const ref = useRef<any>(null);

      useEffect(() => {
          if (!ref.current || !onSegmentChange) return;

          const selection = d3.select(ref.current);
          
          const drag = d3.drag()
              .on('start', (e) => {
                  e.sourceEvent.stopPropagation(); // Prevent zoom pan
              })
              .on('drag', (e) => {
                const dx = e.dx;
                if (!containerRef.current) return;
                const width = containerRef.current.clientWidth;
                if (width === 0) return;
                
                // transform.k is scale factor.
                // 1 unit pixel = duration / (width * k) seconds
                
                // Use xScale directly to get precise time delta
                const x0 = xScale(0);
                const x1 = xScale(1); // 1 sec
                const pixelsPerSecond = x1 - x0;
                
                if (pixelsPerSecond === 0) return;
                
                const dt = dx / pixelsPerSecond;
                
                let newStart = segment.start;
                let newEnd = segment.end;
                
                if (type === 'move') {
                    const dur = Math.max(0, segment.end - segment.start);
                    // Standard clamp
                    newStart = Math.max(0, Math.min(duration - dur, segment.start + dt));
                    newEnd = newStart + dur;
                } else if (type === 'resize-left') {
                    newStart = Math.max(0, Math.min(segment.end - 0.1, segment.start + dt));
                } else if (type === 'resize-right') {
                    newEnd = Math.max(segment.start + 0.1, Math.min(duration, segment.end + dt));
                }
                
               // Call parent
               onSegmentChange(index, newStart, newEnd);
              });

          selection.call(drag as any);
          
          return () => {
              selection.on('.drag', null);
          };
      }, [segment, index, xScale, type, duration, onSegmentChange]); 

      return ref;
  };
  
  // Draggable Segment Component
  const DraggableSegment = ({ seg, index }: { seg: Segment, index: number, key?: any }) => {
      const x1 = xScale(seg.start);
      const x2 = xScale(seg.end);
      const w = Math.max(2, x2 - x1);
      
      const moveRef = useDraggable('move', seg, index);
      const leftRef = useDraggable('resize-left', seg, index);
      const rightRef = useDraggable('resize-right', seg, index);

      // Visibility optimization
      if (x2 < -100 || x1 > (containerRef.current?.clientWidth || 0) + 100) return null;

      const segmentHeight = 24;
      const segmentY = height - segmentHeight - 10;
      
      return (
        <g transform={`translate(${x1}, ${segmentY})`}>
            {/* Main Body */}
            <rect 
                ref={moveRef}
                width={w} 
                height={segmentHeight} 
                fill={index % 2 === 0 ? "rgba(59, 130, 246, 0.2)" : "rgba(16, 185, 129, 0.2)"}
                stroke={index % 2 === 0 ? "#3b82f6" : "#10b981"}
                strokeWidth={1}
                rx={4}
                className="cursor-grab active:cursor-grabbing hover:fill-opacity-50"
                onClick={(e) => {
                    e.stopPropagation();
                    onSeek?.(seg.start);
                }}
            />
            {/* Left Handle */}
            <rect
                ref={leftRef}
                x={-2}
                y={0}
                width={4}
                height={segmentHeight}
                fill="transparent"
                style={{ cursor: 'ew-resize' }}
                onMouseOver={(e) => e.currentTarget.style.fill = 'rgba(0,0,0,0.1)'}
                onMouseOut={(e) => e.currentTarget.style.fill = 'transparent'}
            />
            {/* Right Handle */}
            <rect
                ref={rightRef}
                x={w - 2}
                y={0}
                width={4}
                height={segmentHeight}
                fill="transparent"
                style={{ cursor: 'ew-resize' }}
                onMouseOver={(e) => e.currentTarget.style.fill = 'rgba(0,0,0,0.1)'}
                onMouseOut={(e) => e.currentTarget.style.fill = 'transparent'}
            />
            
            <text
                x={4}
                y={16}
                fontSize={11}
                fill="#1f2937"
                className="pointer-events-none select-none font-medium"
            >
                {seg.text.slice(0, Math.max(0, Math.floor(w / 7)))}
                {seg.text.length > Math.floor(w / 7) ? '...' : ''}
            </text>
        </g>
      );
  };

  // Render SVG Overlay
  const renderSegments = () => {
    if (!segments) return null;
    return segments.map((seg: Segment, i: number) => (
      <DraggableSegment key={i} seg={seg} index={i} />
    ));
  };

  // Click to seek on background
  const handleBgClick = (e: React.MouseEvent) => {
      if (!containerRef.current) return;
      const rect = containerRef.current.getBoundingClientRect();
      const x = e.clientX - rect.left;
      const time = xScale.invert(x);
      onSeek?.(time);
  };

  if (loading) return <div className="animate-pulse bg-gray-100 h-40 rounded w-full"></div>;
  if (error) return <div className="text-red-500 bg-red-50 p-4 rounded">{error}</div>;

  return (
    <div 
        ref={containerRef} 
        className="relative w-full overflow-hidden select-none bg-white dark:bg-gray-800 rounded border border-gray-200 dark:border-gray-700"
        style={{ height }}
    >
      <canvas 
        ref={canvasRef}
        className="absolute top-0 left-0 w-full h-full pointer-events-none"
      />
      
      <svg 
        ref={svgRef}
        className="absolute top-0 left-0 w-full h-full cursor-text"
        onClick={handleBgClick}
      >
        {/* Playback Head */}
        <line 
            x1={xScale(currentTime)} 
            y1={0} 
            x2={xScale(currentTime)} 
            y2={height} 
            stroke="#ef4444" 
            strokeWidth={2}
        />

        {/* Segments */}
        {renderSegments()}
      </svg>
      
      {/* Zoom Controls Overlay (Optional, D3 zoom handles scroll/pinch) */}
      <div className="absolute top-2 right-2 flex space-x-1">
          <button 
            className="p-1 bg-white shadow rounded hover:bg-gray-50 text-xs"
            onClick={() => {
                if (!svgRef.current || !zoomBehavior.current) return;
                const svg = d3.select(svgRef.current);
                svg.transition().call(zoomBehavior.current.scaleBy, 1.2);
            }}
          >
              +
          </button>
          <button 
            className="p-1 bg-white shadow rounded hover:bg-gray-50 text-xs"
            onClick={() => {
                if (!svgRef.current || !zoomBehavior.current) return;
                const svg = d3.select(svgRef.current);
                svg.transition().call(zoomBehavior.current.scaleBy, 0.8);
            }}
          >
              -
          </button>
      </div>
    </div>
  );
};
