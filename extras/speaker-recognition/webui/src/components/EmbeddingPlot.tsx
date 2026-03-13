import React, { useEffect, useRef, useState } from 'react'
import { RefreshCw, Download, Settings, Info } from 'lucide-react'

// Import Plotly dynamically to avoid SSR issues
let Plotly: any = null;
if (typeof window !== 'undefined') {
  import('plotly.js-dist-min').then((module) => {
    Plotly = module.default;
  });
}

interface AnalysisData {
  visualization: {
    speakers: string[]
    embeddings_2d: number[][]
    embeddings_3d: number[][]
    cluster_labels: number[]
    colors: string[]
    speaker_names?: { [key: string]: string }
  }
  clustering: {
    method: string
    n_clusters: number
    silhouette_score?: number | null
    n_noise?: number
    inertia?: number | null
  }
  similar_speakers: Array<{
    speaker1: string
    speaker2: string
    similarity: number | null
  }>
  quality_metrics: {
    n_speakers: number
    mean_similarity: number | null
    std_similarity: number | null
    separation_quality: number | null
  }
  parameters: {
    reduction_method: string
    cluster_method: string
    similarity_threshold: number
  }
  segment_info?: {
    total_segments: number
    processed_segments: number
    enrolled_speakers?: number
    expected_speakers?: number
    analysis_type?: string
  }
  smart_suggestion?: {
    suggested_threshold: number
    confidence: string
    reasoning: string
    detected_clusters: number
    expected_speakers: number
  }
  embedding_types?: {
    segments: string[]
    enrolled: string[]
  }
}

interface AnnotationSegment {
  start: number
  end: number
  speakerLabel?: string
}

interface EmbeddingPlotProps {
  dataSource:
    | { type: 'speakers'; userId?: number }
    | { type: 'segments'; segments: AnnotationSegment[]; audioFile: File }
    | { type: 'combined'; segments: AnnotationSegment[]; audioFile: File; userId?: number; expectedSpeakers?: number }
  compact?: boolean
  title?: string
  autoAnalyze?: boolean
  onRefresh?: () => void
  onAnalysisComplete?: (analysis: AnalysisData) => void
}

export default function EmbeddingPlot({
  dataSource,
  compact = false,
  title,
  autoAnalyze = true,
  onRefresh,
  onAnalysisComplete
}: EmbeddingPlotProps) {
  const plotRef = useRef<HTMLDivElement>(null)
  const [analysisData, setAnalysisData] = useState<AnalysisData | null>(null)
  const [isLoading, setIsLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [view3D, setView3D] = useState(false)
  const [showSettings, setShowSettings] = useState(false)
  const [settings, setSettings] = useState({
    method: 'umap',
    clusterMethod: 'dbscan',
    similarityThreshold: 0.8
  })

  const loadAnalysis = async () => {
    if (!Plotly) return // Plotly not loaded yet

    setIsLoading(true)
    setError(null)

    try {
      let response: Response

      if (dataSource.type === 'speakers') {
        // Speakers analysis - existing implementation
        const params = new URLSearchParams({
          method: settings.method,
          cluster_method: settings.clusterMethod,
          similarity_threshold: settings.similarityThreshold.toString()
        })

        if (dataSource.userId) {
          params.append('user_id', dataSource.userId.toString())
        }

        response = await fetch(`/api/speakers/analysis?${params}`)
      } else if (dataSource.type === 'combined') {
        // Combined analysis - segments + enrolled speakers
        const { segments, audioFile, userId, expectedSpeakers } = dataSource

        // Validate inputs
        if (!audioFile || segments.length === 0) {
          throw new Error('No audio file or segments available for analysis')
        }

        const validSegments = segments.filter(seg => seg.speakerLabel && seg.speakerLabel.trim())
        if (validSegments.length === 0) {
          throw new Error('No segments with speaker labels found')
        }

        // Create FormData for the API request
        const formData = new FormData()
        formData.append('audio_file', audioFile)

        // Add segments data as JSON
        const segmentsData = validSegments.map(seg => ({
          start: seg.start,
          end: seg.end,
          speaker_label: seg.speakerLabel
        }))

        // Add analysis parameters
        formData.append('method', settings.method)
        formData.append('cluster_method', settings.clusterMethod)
        formData.append('similarity_threshold', settings.similarityThreshold.toString())
        formData.append('segments', JSON.stringify(segmentsData))
        formData.append('expected_speakers', (expectedSpeakers || 2).toString())

        if (userId) {
          formData.append('user_id', userId.toString())
        }

        response = await fetch('/api/annotations/analyze-with-enrolled', {
          method: 'POST',
          body: formData
        })
      } else {
        // Annotation segments analysis - segments only
        const { segments, audioFile } = dataSource

        // Validate inputs
        if (!audioFile || segments.length === 0) {
          throw new Error('No audio file or segments available for analysis')
        }

        const validSegments = segments.filter(seg => seg.speakerLabel && seg.speakerLabel.trim())
        if (validSegments.length === 0) {
          throw new Error('No segments with speaker labels found')
        }

        // Create FormData for the API request
        const formData = new FormData()
        formData.append('audio_file', audioFile)

        // Add segments data as JSON
        const segmentsData = validSegments.map(seg => ({
          start: seg.start,
          end: seg.end,
          speaker_label: seg.speakerLabel
        }))

        // Add analysis parameters
        formData.append('method', settings.method)
        formData.append('cluster_method', settings.clusterMethod)
        formData.append('similarity_threshold', settings.similarityThreshold.toString())
        formData.append('segments', JSON.stringify(segmentsData))

        response = await fetch('/api/annotations/analyze-segments', {
          method: 'POST',
          body: formData
        })
      }

      if (!response.ok) {
        const errorText = await response.text()
        throw new Error(`Analysis failed: ${response.status} - ${errorText}`)
      }

      const data = await response.json()

      if (data.error || data.status === 'error') {
        const errorMessage = data.error || data.message || 'Analysis failed'
        const details = data.details
        if (details && details.hint) {
          throw new Error(`${errorMessage}\n\n${details.hint}`)
        }
        throw new Error(errorMessage)
      }

      setAnalysisData(data)
      createPlot(data)

      // Call completion callback if provided
      if (onAnalysisComplete) {
        onAnalysisComplete(data)
      }
    } catch (err) {
      const errorMessage = err instanceof Error ? err.message : 'Failed to load analysis'
      setError(errorMessage)
      console.error('Analysis loading error:', err)
    } finally {
      setIsLoading(false)
    }
  }

  const createPlot = (data: AnalysisData) => {
    if (!plotRef.current || !Plotly || !data.visualization.speakers.length) return

    const { visualization } = data
    const embeddings = view3D ? visualization.embeddings_3d : visualization.embeddings_2d

    if (!embeddings.length) return

    // Check if this is a combined analysis with dual-color visualization
    const hasDualColors = data.embedding_types && dataSource.type === 'combined'

    let traces: any[] = []

    if (hasDualColors && data.embedding_types) {
      // Dual-color visualization for combined analysis
      const segmentIndices: number[] = []
      const enrolledIndices: number[] = []

      // Separate indices for segments vs enrolled speakers
      visualization.speakers.forEach((speakerId, index) => {
        if (data.embedding_types!.segments.includes(speakerId)) {
          segmentIndices.push(index)
        } else if (data.embedding_types!.enrolled.includes(speakerId)) {
          enrolledIndices.push(index)
        }
      })

      // Create trace for annotation segments (blue)
      if (segmentIndices.length > 0) {
        traces.push({
          x: segmentIndices.map(i => embeddings[i][0]),
          y: segmentIndices.map(i => embeddings[i][1]),
          z: view3D ? segmentIndices.map(i => embeddings[i][2]) : undefined,
          mode: 'markers+text',
          type: view3D ? 'scatter3d' : 'scatter',
          name: 'Annotation Segments',
          text: segmentIndices.map(i =>
            visualization.speaker_names?.[visualization.speakers[i]] ||
            visualization.speakers[i].split('_').pop() ||
            visualization.speakers[i]
          ),
          textposition: 'top center',
          textfont: { size: 10 },
          marker: {
            size: 12,
            color: '#3B82F6', // Blue for segments
            line: { width: 2, color: '#1E40AF' },
            opacity: 0.8,
            symbol: 'circle'
          },
          hovertemplate:
            '<b>%{text}</b><br>' +
            'Type: Annotation Segment<br>' +
            'X: %{x:.3f}<br>' +
            'Y: %{y:.3f}<br>' +
            (view3D ? 'Z: %{z:.3f}<br>' : '') +
            '<extra></extra>'
        })
      }

      // Create trace for enrolled speakers (red)
      if (enrolledIndices.length > 0) {
        traces.push({
          x: enrolledIndices.map(i => embeddings[i][0]),
          y: enrolledIndices.map(i => embeddings[i][1]),
          z: view3D ? enrolledIndices.map(i => embeddings[i][2]) : undefined,
          mode: 'markers+text',
          type: view3D ? 'scatter3d' : 'scatter',
          name: 'Enrolled Speakers',
          text: enrolledIndices.map(i =>
            visualization.speaker_names?.[visualization.speakers[i]] ||
            visualization.speakers[i].split('_').pop() ||
            visualization.speakers[i]
          ),
          textposition: 'top center',
          textfont: { size: 10 },
          marker: {
            size: 12,
            color: '#EF4444', // Red for enrolled speakers
            line: { width: 2, color: '#DC2626' },
            opacity: 0.8,
            symbol: 'diamond'
          },
          hovertemplate:
            '<b>%{text}</b><br>' +
            'Type: Enrolled Speaker<br>' +
            'X: %{x:.3f}<br>' +
            'Y: %{y:.3f}<br>' +
            (view3D ? 'Z: %{z:.3f}<br>' : '') +
            '<extra></extra>'
        })
      }
    } else {
      // Standard cluster-based coloring
      const clusterColors: { [key: number]: string } = {}
      const uniqueClusters = [...new Set(visualization.cluster_labels)]
      uniqueClusters.forEach((cluster, index) => {
        if (cluster === -1) {
          clusterColors[cluster] = '#999999' // Gray for noise points
        } else {
          clusterColors[cluster] = `hsl(${(index * 360) / uniqueClusters.length}, 70%, 50%)`
        }
      })

      traces.push({
        x: embeddings.map(point => point[0]),
        y: embeddings.map(point => point[1]),
        z: view3D ? embeddings.map(point => point[2]) : undefined,
        mode: 'markers+text',
        type: view3D ? 'scatter3d' : 'scatter',
        text: visualization.speakers.map(speaker =>
          visualization.speaker_names?.[speaker] || speaker.split('_').pop() || speaker
        ),
        textposition: 'top center',
        textfont: { size: 10 },
        marker: {
          size: 10,
          color: visualization.cluster_labels.map(label => clusterColors[label]),
          line: { width: 1, color: '#000' },
          opacity: 0.8
        },
        hovertemplate:
          '<b>%{text}</b><br>' +
          'X: %{x:.3f}<br>' +
          'Y: %{y:.3f}<br>' +
          (view3D ? 'Z: %{z:.3f}<br>' : '') +
          'Cluster: %{customdata}<br>' +
          '<extra></extra>',
        customdata: visualization.cluster_labels.map(label =>
          label === -1 ? 'Noise' : `Cluster ${label}`
        )
      })
    }

    let plotTitle = 'Speaker Embeddings Analysis'
    if (dataSource.type === 'segments') {
      plotTitle = 'Annotation Segments Analysis'
    } else if (dataSource.type === 'combined') {
      plotTitle = 'Combined Analysis: Segments vs Enrolled Speakers'
    }
    plotTitle += ` (${data.parameters.reduction_method.toUpperCase()})`

    const layout = {
      title: {
        text: plotTitle,
        font: { size: compact ? 14 : 16 }
      },
      showlegend: hasDualColors,
      legend: hasDualColors ? {
        x: 1.02,
        y: 1,
        bgcolor: 'rgba(255,255,255,0.8)',
        bordercolor: 'rgba(0,0,0,0.2)',
        borderwidth: 1
      } : undefined,
      hovermode: 'closest',
      xaxis: { title: 'Component 1' },
      yaxis: { title: 'Component 2' },
      zaxis: view3D ? { title: 'Component 3' } : undefined,
      margin: compact
        ? { l: 30, r: 30, t: 50, b: 30 }
        : { l: 40, r: 40, t: 60, b: 40 },
      plot_bgcolor: '#fafafa',
      paper_bgcolor: '#ffffff',
      height: compact ? 400 : undefined
    }

    const config = {
      responsive: true,
      displayModeBar: true,
      modeBarButtonsToRemove: ['pan2d', 'lasso2d', 'select2d'],
      displaylogo: false
    }

    Plotly.newPlot(plotRef.current, traces, layout, config)
  }

  const downloadPlot = () => {
    if (!plotRef.current || !Plotly) return

    Plotly.downloadImage(plotRef.current, {
      format: 'png',
      width: 1200,
      height: 800,
      filename: `speaker_embeddings_${view3D ? '3d' : '2d'}`
    })
  }

  const formatMetric = (value: number | null | undefined, decimals: number = 3): string => {
    if (value === null || value === undefined || isNaN(value)) {
      return 'N/A'
    }
    return value.toFixed(decimals)
  }

  useEffect(() => {
    // Only auto-analyze for speakers or when explicitly enabled
    const shouldAutoAnalyze = autoAnalyze && (dataSource.type === 'speakers' || autoAnalyze === true)

    if (!shouldAutoAnalyze) return

    // Load Plotly and then load analysis
    const loadPlotlyAndAnalysis = async () => {
      if (!Plotly) {
        // Wait a bit for Plotly to load
        setTimeout(loadPlotlyAndAnalysis, 100)
        return
      }
      loadAnalysis()
    }

    loadPlotlyAndAnalysis()
  }, [dataSource, settings, autoAnalyze])

  useEffect(() => {
    if (analysisData) {
      createPlot(analysisData)
    }
  }, [view3D, analysisData])

  return (
    <div className={compact ? "space-y-4" : "space-y-6"}>
      {/* Header */}
      <div className="flex items-center justify-between">
        <h3 className={`${compact ? 'text-md' : 'text-lg'} font-semibold text-primary`}>
          {title || '📊 Embedding Analysis'}
        </h3>
        <div className="flex space-x-2">
          <button
            onClick={() => setShowSettings(!showSettings)}
            className="p-2 text-gray-600 hover:text-gray-800 border rounded-md"
            title="Analysis Settings"
          >
            <Settings className="h-4 w-4" />
          </button>
          {/* Show Analyze button for manual mode when no data, or Refresh when data exists */}
          {!autoAnalyze && !analysisData ? (
            <button
              onClick={loadAnalysis}
              disabled={isLoading}
              className="px-4 py-2 bg-purple-600 text-white rounded-md hover:bg-purple-700 disabled:opacity-50"
              title="Analyze Segments"
            >
              {isLoading ? (
                <>
                  <RefreshCw className="h-4 w-4 animate-spin inline mr-2" />
                  Analyzing...
                </>
              ) : (
                'Analyze Segments'
              )}
            </button>
          ) : (
            <button
              onClick={loadAnalysis}
              disabled={isLoading}
              className="p-2 text-blue-600 hover:text-blue-800 border rounded-md disabled:opacity-50"
              title="Refresh Analysis"
            >
              <RefreshCw className={`h-4 w-4 ${isLoading ? 'animate-spin' : ''}`} />
            </button>
          )}
          <button
            onClick={downloadPlot}
            disabled={!analysisData}
            className="p-2 text-green-600 hover:text-green-800 border rounded-md disabled:opacity-50"
            title="Download Plot"
          >
            <Download className="h-4 w-4" />
          </button>
        </div>
      </div>

      {/* Settings Panel */}
      {showSettings && (
        <div className="card-secondary border border-gray-200 dark:border-gray-700 p-4">
          <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
            <div>
              <label className="block text-sm font-medium text-gray-700 mb-1">
                Reduction Method
              </label>
              <select
                value={settings.method}
                onChange={(e) => setSettings({ ...settings, method: e.target.value })}
                className="w-full px-3 py-2 border border-gray-300 rounded-md text-sm"
              >
                <option value="umap">UMAP</option>
                <option value="tsne">t-SNE</option>
                <option value="pca">PCA</option>
              </select>
            </div>
            <div>
              <label className="block text-sm font-medium text-gray-700 mb-1">
                Clustering Method
              </label>
              <select
                value={settings.clusterMethod}
                onChange={(e) => setSettings({ ...settings, clusterMethod: e.target.value })}
                className="w-full px-3 py-2 border border-gray-300 rounded-md text-sm"
              >
                <option value="dbscan">DBSCAN</option>
                <option value="kmeans">K-Means</option>
              </select>
            </div>
            <div>
              <label className="block text-sm font-medium text-gray-700 mb-1">
                Similarity Threshold
              </label>
              <input
                type="number"
                min="0"
                max="1"
                step="0.1"
                value={settings.similarityThreshold}
                onChange={(e) => setSettings({ ...settings, similarityThreshold: parseFloat(e.target.value) })}
                className="w-full px-3 py-2 border border-gray-300 rounded-md text-sm"
              />
            </div>
          </div>
        </div>
      )}

      {/* Plot Controls */}
      {analysisData && (
        <div className="flex items-center space-x-4">
          <div className="flex items-center space-x-2">
            <button
              onClick={() => setView3D(false)}
              className={`px-3 py-1 text-sm rounded-md ${
                !view3D
                  ? 'bg-blue-600 text-white'
                  : 'bg-gray-200 text-gray-700 hover:bg-gray-300'
              }`}
            >
              2D View
            </button>
            <button
              onClick={() => setView3D(true)}
              className={`px-3 py-1 text-sm rounded-md ${
                view3D
                  ? 'bg-blue-600 text-white'
                  : 'bg-gray-200 text-gray-700 hover:bg-gray-300'
              }`}
            >
              3D View
            </button>
          </div>
        </div>
      )}

      {/* Error State */}
      {error && (
        <div className="bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-800 rounded-lg p-4">
          <div className="flex items-center space-x-2">
            <Info className="h-5 w-5 text-red-600" />
            <div>
              <h4 className="text-red-800 font-medium">Analysis Error</h4>
              <p className="text-red-600 text-sm">{error}</p>
            </div>
          </div>
        </div>
      )}

      {/* Loading State */}
      {isLoading && (
        <div className="text-center py-8">
          <div className="inline-block animate-spin rounded-full h-8 w-8 border-b-2 border-blue-600"></div>
          <p className="mt-2 text-gray-600">Analyzing speaker embeddings...</p>
        </div>
      )}

      {/* Main Plot */}
      <div className="card p-4">
        <div ref={plotRef} style={{ width: '100%', height: compact ? '400px' : '500px' }} />
      </div>

      {/* Analysis Results */}
      {analysisData && (
        <div className={`grid grid-cols-1 ${dataSource.type === 'combined' ? 'lg:grid-cols-4' : dataSource.type === 'segments' ? 'lg:grid-cols-3' : 'lg:grid-cols-2'} gap-6`}>
          {/* Clustering Info */}
          <div className="card p-4">
            <h4 className="text-md font-semibold text-gray-900 mb-3">🔍 Clustering Results</h4>
            <div className="space-y-2 text-sm">
              <div className="flex justify-between">
                <span className="text-gray-600">Method:</span>
                <span className="font-medium">{analysisData.clustering.method.toUpperCase()}</span>
              </div>
              <div className="flex justify-between">
                <span className="text-gray-600">Clusters Found:</span>
                <span className="font-medium">{analysisData.clustering.n_clusters}</span>
              </div>
              {analysisData.clustering.silhouette_score !== undefined && (
                <div className="flex justify-between">
                  <span className="text-gray-600">Silhouette Score:</span>
                  <span className="font-medium">{formatMetric(analysisData.clustering.silhouette_score)}</span>
                </div>
              )}
              {analysisData.clustering.n_noise !== undefined && (
                <div className="flex justify-between">
                  <span className="text-gray-600">Noise Points:</span>
                  <span className="font-medium">{analysisData.clustering.n_noise}</span>
                </div>
              )}
            </div>
          </div>

          {/* Segment Info - For annotation and combined analysis */}
          {(dataSource.type === 'segments' || dataSource.type === 'combined') && analysisData.segment_info && (
            <div className="card p-4">
              <h4 className="text-md font-semibold text-primary mb-3">📋 Analysis Info</h4>
              <div className="space-y-2 text-sm">
                <div className="flex justify-between">
                  <span className="text-gray-600">Total Segments:</span>
                  <span className="font-medium">{analysisData.segment_info.total_segments}</span>
                </div>
                <div className="flex justify-between">
                  <span className="text-gray-600">Processed:</span>
                  <span className="font-medium">{analysisData.segment_info.processed_segments}</span>
                </div>
                {analysisData.segment_info.enrolled_speakers !== undefined && (
                  <div className="flex justify-between">
                    <span className="text-gray-600">Enrolled Speakers:</span>
                    <span className="font-medium">{analysisData.segment_info.enrolled_speakers}</span>
                  </div>
                )}
                {analysisData.segment_info.expected_speakers !== undefined && (
                  <div className="flex justify-between">
                    <span className="text-gray-600">Expected Speakers:</span>
                    <span className="font-medium">{analysisData.segment_info.expected_speakers}</span>
                  </div>
                )}
                {dataSource.type === 'segments' && analysisData.segment_info.unique_speakers && (
                  <div className="flex justify-between">
                    <span className="text-gray-600">Unique Speakers:</span>
                    <span className="font-medium">{analysisData.segment_info.unique_speakers.length}</span>
                  </div>
                )}
                {analysisData.segment_info.total_duration !== undefined && (
                  <div className="flex justify-between">
                    <span className="text-gray-600">Total Duration:</span>
                    <span className="font-medium">{formatMetric(analysisData.segment_info.total_duration, 1)}s</span>
                  </div>
                )}
              </div>
            </div>
          )}

          {/* Smart Suggestion - Only for combined analysis */}
          {dataSource.type === 'combined' && analysisData.smart_suggestion && (
            <div className="card p-4">
              <h4 className="text-md font-semibold text-primary mb-3">🎯 Smart Suggestion</h4>
              <div className="space-y-3">
                <div className="bg-blue-50 dark:bg-blue-900/20 border border-blue-200 dark:border-blue-800 rounded-lg p-3">
                  <div className="text-center">
                    <div className="text-2xl font-bold text-blue-600">
                      {formatMetric(analysisData.smart_suggestion.suggested_threshold, 2)}
                    </div>
                    <div className="text-sm text-blue-800 font-medium">Suggested Threshold</div>
                  </div>
                </div>

                <div className="space-y-2 text-sm">
                  <div className="flex justify-between">
                    <span className="text-gray-600">Confidence:</span>
                    <span className="font-medium capitalize">{analysisData.smart_suggestion.confidence}</span>
                  </div>
                  <div className="flex justify-between">
                    <span className="text-gray-600">Detected Clusters:</span>
                    <span className="font-medium">{analysisData.smart_suggestion.detected_clusters}</span>
                  </div>
                </div>

                <div className="text-xs text-gray-600 bg-gray-50 p-2 rounded">
                  <strong>Reasoning:</strong> {analysisData.smart_suggestion.reasoning}
                </div>
              </div>
            </div>
          )}

          {/* Quality Metrics */}
          <div className="card p-4">
            <h4 className="text-md font-semibold text-gray-900 mb-3">📈 Quality Metrics</h4>
            <div className="space-y-2 text-sm">
              <div className="flex justify-between">
                <span className="text-gray-600">Total Speakers:</span>
                <span className="font-medium">{analysisData.quality_metrics.n_speakers}</span>
              </div>
              <div className="flex justify-between">
                <span className="text-gray-600">Avg. Similarity:</span>
                <span className="font-medium">{formatMetric(analysisData.quality_metrics.mean_similarity)}</span>
              </div>
              <div className="flex justify-between">
                <span className="text-gray-600">Separation Quality:</span>
                <span className="font-medium">{formatMetric(analysisData.quality_metrics.separation_quality)}</span>
              </div>
            </div>
          </div>

          {/* Similar Speakers */}
          {analysisData.similar_speakers.length > 0 && (
            <div className={`card p-4 ${
              dataSource.type === 'combined' ? 'lg:col-span-4' :
              dataSource.type === 'segments' ? 'lg:col-span-3' :
              'lg:col-span-2'
            }`}>
              <h4 className="text-md font-semibold text-primary mb-3">⚠️ Similar Speakers</h4>
              <div className="space-y-2">
                {analysisData.similar_speakers.slice(0, 5).map((pair, index) => (
                  <div key={index} className="flex justify-between items-center p-2 bg-yellow-50 dark:bg-yellow-900/20 border border-yellow-200 dark:border-yellow-800 rounded">
                    <div className="text-sm">
                      <span className="font-medium text-gray-900 dark:text-gray-100">{pair.speaker1.split('_').pop()}</span>
                      <span className="text-gray-600 dark:text-gray-300">{' ↔ '}</span>
                      <span className="font-medium text-gray-900 dark:text-gray-100">{pair.speaker2.split('_').pop()}</span>
                    </div>
                    <span className="text-sm font-bold text-yellow-700 dark:text-yellow-300">
                      {formatMetric(pair.similarity)}
                    </span>
                  </div>
                ))}
                {analysisData.similar_speakers.length > 5 && (
                  <p className="text-xs text-gray-500 text-center">
                    ... and {analysisData.similar_speakers.length - 5} more similar pairs
                  </p>
                )}
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  )
}
