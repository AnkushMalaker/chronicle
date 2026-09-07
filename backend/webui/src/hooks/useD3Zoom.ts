import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { select, selectAll } from 'd3-selection'
import { zoom, zoomIdentity, type ZoomTransform, type D3ZoomEvent } from 'd3-zoom'

interface UseD3ZoomOptions {
  onZoom?: (transform: ZoomTransform) => void
  scaleExtent?: [number, number]
  wheelDelta?: (event: WheelEvent) => number
}

export function useD3Zoom(options: UseD3ZoomOptions = {}) {
  const {
    onZoom,
    scaleExtent = [0.5, 5],
    wheelDelta = (event) => -event.deltaY * 0.002
  } = options

  const svgRef = useRef<SVGSVGElement>(null)
  const [transform, setTransform] = useState<ZoomTransform>(zoomIdentity)
  const initializedRef = useRef(false)

  const handleZoom = useCallback(
    (event: D3ZoomEvent<SVGSVGElement, unknown>) => {
      const t = event.transform
      setTransform(t)
      onZoom?.(t)

      // Synchronize zoom across all zoomable SVG elements
      selectAll<SVGSVGElement, unknown>('.zoomable').each(function (this: SVGSVGElement) {
        const svg = select(this)
        const node = svg.node()

        // Skip the source element
        if (!node || node.contains(event.sourceEvent?.target as Element)) {
          return
        }

        svg.property('__zoom', t)
      })
    },
    [onZoom]
  )

  const zoomBehavior = useMemo(
    () =>
      zoom<SVGSVGElement, unknown>()
        .scaleExtent(scaleExtent)
        .on('zoom', handleZoom)
        .wheelDelta(wheelDelta)
        .touchable(() => true)
        .filter((event: Event) => {
          const mouseEvent = event as MouseEvent
          if (event.type === 'dblclick') return false
          if (mouseEvent.button && mouseEvent.button !== 0) return false
          return true
        }),
    [handleZoom, scaleExtent, wheelDelta]
  )

  // Set initial transform once on mount
  useEffect(() => {
    if (!svgRef.current || initializedRef.current) return

    const svg = select(svgRef.current)
    svg.property('__zoom', zoomIdentity)
    initializedRef.current = true
  }, [])

  // Setup zoom behavior (only when zoomBehavior changes)
  useEffect(() => {
    if (!svgRef.current) return

    const svg = select(svgRef.current)
    const node = svg.node()

    if (node) {
      node.style.touchAction = 'none'
      node.style.webkitUserSelect = 'none'
      node.style.userSelect = 'none'
    }

    svg.call(zoomBehavior as any)

    return () => {
      svg.on('.zoom', null)
    }
  }, [zoomBehavior])

  return {
    svgRef,
    transform,
    zoomBehavior
  }
}
