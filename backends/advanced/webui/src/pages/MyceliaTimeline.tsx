import { useState, useEffect, useRef } from 'react'
import { Calendar, RefreshCw, AlertCircle } from 'lucide-react'
import { useNavigate } from 'react-router-dom'
import { select } from 'd3-selection'
import { scaleTime, scaleBand } from 'd3-scale'
import { axisBottom } from 'd3-axis'
import { timeFormat } from 'd3-time-format'
import { min, max } from 'd3-array'
import { zoom, type D3ZoomEvent } from 'd3-zoom'
import { memoriesApi } from '../services/api'
import { useAuth } from '../contexts/AuthContext'

interface TimeRange {
  start: string
  end: string
  name?: string
}

interface MemoryWithTimeRange {
  id: string
  content: string
  created_at: string
  metadata?: {
    name?: string
    timeRanges?: TimeRange[]
    isPerson?: boolean
    isEvent?: boolean
    isPlace?: boolean
  }
}

interface TimelineTask {
  id: string
  name: string
  start: Date
  end: Date
  color: string
  type: 'event' | 'person' | 'place'
}

export default function MyceliaTimeline() {
  const [memories, setMemories] = useState<MemoryWithTimeRange[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [useDemoData, setUseDemoData] = useState(false)
  const svgRef = useRef<SVGSVGElement>(null)
  const containerRef = useRef<HTMLDivElement>(null)
  const tooltipRef = useRef<HTMLDivElement>(null)
  const [dimensions, setDimensions] = useState({ width: 1000, height: 400 })
  const { user } = useAuth()
  const navigate = useNavigate()

  // Demo data
  const getDemoMemories = (): MemoryWithTimeRange[] => {
    return [
      {
        id: 'demo-wedding',
        content: "Sarah and Tom's wedding ceremony and reception",
        created_at: '2025-12-07T15:00:00',
        metadata: {
          name: "Wedding",
          isEvent: true,
          timeRanges: [
            {
              name: 'Ceremony',
              start: '2025-12-07T15:00:00',
              end: '2025-12-07T16:30:00'
            },
            {
              name: 'Reception',
              start: '2025-12-07T18:00:00',
              end: '2025-12-07T23:00:00'
            }
          ]
        }
      },
      {
        id: 'demo-conference',
        content: 'Tech conference with keynote and workshops',
        created_at: '2026-01-15T09:00:00',
        metadata: {
          name: 'Tech Conference',
          isEvent: true,
          timeRanges: [
            {
              name: 'Keynote',
              start: '2026-01-15T09:00:00',
              end: '2026-01-15T11:00:00'
            }
          ]
        }
      }
    ]
  }

  const loadMemories = async () => {
    if (!user?.id) return

    try {
      setLoading(true)
      setError(null)
      const response = await memoriesApi.getAll(user.id)
      const memoriesData = response.data.memories || response.data || []
      const memoriesWithTime = memoriesData.filter((m: MemoryWithTimeRange) =>
        m.metadata?.timeRanges && m.metadata.timeRanges.length > 0
      )
      setMemories(memoriesWithTime)
    } catch (err: any) {
      setError(err.message || 'Failed to load timeline data')
    } finally {
      setLoading(false)
    }
  }

  const convertToTasks = (memories: MemoryWithTimeRange[]): TimelineTask[] => {
    const tasks: TimelineTask[] = []
    memories.forEach((memory) => {
      const timeRanges = memory.metadata?.timeRanges || []
      timeRanges.forEach((range, index) => {
        let type: 'event' | 'person' | 'place' = 'event'
        let color = '#3b82f6'

        if (memory.metadata?.isEvent) {
          type = 'event'
          color = '#3b82f6'
        } else if (memory.metadata?.isPerson) {
          type = 'person'
          color = '#10b981'
        } else if (memory.metadata?.isPlace) {
          type = 'place'
          color = '#f59e0b'
        }

        tasks.push({
          id: `${memory.id}-${index}`,
          name: range.name || memory.metadata?.name || memory.content.substring(0, 30),
          start: new Date(range.start),
          end: new Date(range.end),
          color,
          type
        })
      })
    })
    return tasks
  }

  useEffect(() => {
    if (!useDemoData) {
      loadMemories()
    } else {
      setMemories(getDemoMemories())
    }
  }, [user?.id, useDemoData])

  // Handle container resize
  useEffect(() => {
    if (!containerRef.current) return
    const resizeObserver = new ResizeObserver(([entry]) => {
      setDimensions({
        width: entry.contentRect.width,
        height: 400
      })
    })
    resizeObserver.observe(containerRef.current)
    return () => resizeObserver.disconnect()
  }, [])

  // D3 visualization
  useEffect(() => {
    if (!svgRef.current || memories.length === 0) return

    const tasks = convertToTasks(useDemoData ? getDemoMemories() : memories)
    if (tasks.length === 0) return

    const svg = select(svgRef.current)
    svg.selectAll('*').remove()

    const margin = { top: 60, right: 40, bottom: 60, left: 150 }
    const width = dimensions.width - margin.left - margin.right
    const height = dimensions.height - margin.top - margin.bottom

    // Find time range
    const allDates = tasks.flatMap(t => [t.start, t.end])
    const minDate = min(allDates)!
    const maxDate = max(allDates)!

    // Create scales
    const xScale = scaleTime()
      .domain([minDate, maxDate])
      .range([0, width])

    const yScale = scaleBand()
      .domain(tasks.map(t => t.id))
      .range([0, height])
      .padding(0.3)

    // Create main group
    const g = svg.append('g')
      .attr('transform', `translate(${margin.left},${margin.top})`)
      .attr('class', 'zoomable')

    // Add axes
    const xAxis = axisBottom(xScale)
      .ticks(6)
      .tickFormat(timeFormat('%b %d, %Y') as any)

    g.append('g')
      .attr('class', 'x-axis')
      .attr('transform', `translate(0,${height})`)
      .call(xAxis)
      .selectAll('text')
      .style('fill', 'currentColor')

    // Add task bars
    const bars = g.append('g')
      .attr('class', 'bars')
      .selectAll('rect')
      .data(tasks)
      .enter()

    // Bar background with click and hover
    bars.append('rect')
      .attr('x', (d: TimelineTask) => xScale(d.start))
      .attr('y', (d: TimelineTask) => yScale(d.id)!)
      .attr('width', (d: TimelineTask) => Math.max(2, xScale(d.end) - xScale(d.start)))
      .attr('height', yScale.bandwidth())
      .attr('fill', (d: TimelineTask) => d.color)
      .attr('rx', 4)
      .style('opacity', 0.8)
      .style('cursor', 'pointer')
      .on('mouseover', function(this: SVGRectElement, event: MouseEvent, d: TimelineTask) {
        select(this).style('opacity', 1)

        // Show tooltip
        if (tooltipRef.current) {
          const tooltip = select(tooltipRef.current)
          const startDate = d.start.toLocaleDateString('en-US', {
            year: 'numeric',
            month: 'short',
            day: 'numeric',
            hour: '2-digit',
            minute: '2-digit'
          })
          const endDate = d.end.toLocaleDateString('en-US', {
            year: 'numeric',
            month: 'short',
            day: 'numeric',
            hour: '2-digit',
            minute: '2-digit'
          })

          // Build tooltip using DOM APIs to prevent XSS
          tooltip
            .style('opacity', 1)
            .style('left', `${event.pageX + 10}px`)
            .style('top', `${event.pageY - 10}px`)
            .html('') // Clear existing content

          // Add title (user-controlled content via textContent)
          tooltip
            .append('div')
            .attr('class', 'font-semibold text-sm mb-1')
            .text(d.name) // Safe: uses textContent, not innerHTML

          // Add details container
          const detailsDiv = tooltip
            .append('div')
            .attr('class', 'text-xs text-gray-600 dark:text-gray-300')

          // Add start date
          const startDiv = detailsDiv.append('div')
          startDiv.append('strong').text('Start: ')
          startDiv.append('span').text(startDate) // Safe: uses textContent

          // Add end date
          const endDiv = detailsDiv.append('div')
          endDiv.append('strong').text('End: ')
          endDiv.append('span').text(endDate) // Safe: uses textContent

          // Add static click instruction
          detailsDiv
            .append('div')
            .attr('class', 'mt-1 text-blue-600 dark:text-blue-400')
            .text('Click to view memory')
        }
      })
      .on('mouseout', function(this: SVGRectElement) {
        select(this).style('opacity', 0.8)

        // Hide tooltip
        if (tooltipRef.current) {
          select(tooltipRef.current).style('opacity', 0)
        }
      })
      .on('click', function(this: SVGRectElement, event: MouseEvent, d: TimelineTask) {
        event.stopPropagation()
        // Extract memory ID from task ID (format: "memory-id-rangeIndex")
        // Use lastIndexOf to handle memory IDs that contain dashes (e.g., UUIDs)
        const lastDashIndex = d.id.lastIndexOf('-')
        const memoryId = lastDashIndex !== -1 ? d.id.slice(0, lastDashIndex) : d.id

        if (memoryId) {
          navigate(`/memories/${memoryId}`)
        }
      })

    // Add labels
    g.append('g')
      .attr('class', 'labels')
      .selectAll('text')
      .data(tasks)
      .enter()
      .append('text')
      .attr('x', -10)
      .attr('y', (d: TimelineTask) => yScale(d.id)! + yScale.bandwidth() / 2)
      .attr('dy', '0.35em')
      .attr('text-anchor', 'end')
      .text((d: TimelineTask) => d.name)
      .style('fill', 'currentColor')
      .style('font-size', '12px')

    // Zoom behavior
    const zoomBehavior = zoom<SVGSVGElement, unknown>()
      .scaleExtent([0.5, 5])
      .on('zoom', (event: D3ZoomEvent<SVGSVGElement, unknown>) => {
        const transform = event.transform

        // Update x scale
        const newXScale = transform.rescaleX(xScale)

        // Update axis
        g.select<SVGGElement>('.x-axis').call(
          axisBottom(newXScale)
            .ticks(6)
            .tickFormat(timeFormat('%b %d, %Y') as any) as any
        )

        // Update bars
        g.selectAll<SVGRectElement, TimelineTask>('.bars rect')
          .attr('x', (d: TimelineTask) => newXScale(d.start))
          .attr('width', (d: TimelineTask) => Math.max(2, newXScale(d.end) - newXScale(d.start)))
      })

    svg.call(zoomBehavior as any)

  }, [memories, dimensions, useDemoData])

  if (loading) {
    return (
      <div className="space-y-6">
        <div className="flex items-center justify-center py-12">
          <div className="flex items-center space-x-3 text-gray-500 dark:text-gray-400">
            <RefreshCw className="h-5 w-5 animate-spin" />
            <span>Loading timeline data...</span>
          </div>
        </div>
      </div>
    )
  }

  if (error) {
    return (
      <div className="space-y-6">
        <div className="flex items-center justify-center py-12">
          <div className="flex items-center space-x-3 text-red-500">
            <AlertCircle className="h-5 w-5" />
            <span>{error}</span>
          </div>
        </div>
      </div>
    )
  }

  return (
    <div className="space-y-6 relative">
      {/* Tooltip */}
      <div
        ref={tooltipRef}
        className="absolute pointer-events-none bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-lg shadow-lg p-3 z-50 opacity-0 transition-opacity"
        style={{ maxWidth: '300px' }}
      />

      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-3xl font-bold text-gray-900 dark:text-gray-100 flex items-center gap-2">
            <Calendar className="w-8 h-8" />
            Timeline (Mycelia D3)
          </h1>
          <p className="text-sm text-gray-500 dark:text-gray-400 mt-1">
            Interactive D3-based timeline with smooth pan and zoom • Click events to view details
          </p>
        </div>
        <div className="flex items-center space-x-3">
          {useDemoData ? (
            <button
              onClick={() => setUseDemoData(false)}
              className="flex items-center space-x-2 px-4 py-2 bg-gray-600 text-white rounded-lg hover:bg-gray-700 transition-colors"
            >
              <Calendar className="h-4 w-4" />
              <span>Show Real Data</span>
            </button>
          ) : (
            <button
              onClick={() => setUseDemoData(true)}
              className="flex items-center space-x-2 px-4 py-2 bg-green-600 text-white rounded-lg hover:bg-green-700 transition-colors"
            >
              <Calendar className="h-4 w-4" />
              <span>Show Demo</span>
            </button>
          )}
          <button
            onClick={loadMemories}
            className="flex items-center space-x-2 px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700 transition-colors"
          >
            <RefreshCw className="h-4 w-4" />
            <span>Refresh</span>
          </button>
        </div>
      </div>

      {/* Timeline */}
      {memories.length === 0 && !useDemoData ? (
        <div className="flex flex-col items-center justify-center py-12 space-y-4">
          <Calendar className="h-16 w-16 text-gray-300 dark:text-gray-600" />
          <div className="text-center">
            <h3 className="text-lg font-medium text-gray-900 dark:text-gray-100 mb-2">No Timeline Events</h3>
            <p className="text-gray-500 dark:text-gray-400">
              No memories with time information found. Try the demo to see how it works.
            </p>
          </div>
        </div>
      ) : (
        <div className="space-y-4">
          <div
            ref={containerRef}
            className="bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 p-4"
          >
            <svg
              ref={svgRef}
              width={dimensions.width}
              height={dimensions.height}
              className="w-full"
              style={{ touchAction: 'none', userSelect: 'none' }}
            />
          </div>

          <div className="flex items-center justify-center space-x-6 text-xs text-gray-500 dark:text-gray-400">
            <span>💡 Scroll to zoom, drag to pan</span>
            <span>🖱️ Click bars to view memory details</span>
            <span>👆 Hover for info</span>
          </div>

          <div className="flex items-center justify-center space-x-6 text-sm">
            <div className="flex items-center space-x-2">
              <div className="w-4 h-4 bg-blue-500 rounded"></div>
              <span className="text-gray-700 dark:text-gray-300">Event</span>
            </div>
            <div className="flex items-center space-x-2">
              <div className="w-4 h-4 bg-green-500 rounded"></div>
              <span className="text-gray-700 dark:text-gray-300">Person</span>
            </div>
            <div className="flex items-center space-x-2">
              <div className="w-4 h-4 bg-orange-500 rounded"></div>
              <span className="text-gray-700 dark:text-gray-300">Place</span>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
