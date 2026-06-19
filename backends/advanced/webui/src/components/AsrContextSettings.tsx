import { useEffect, useState } from 'react'
import { Mic, Save, Sparkles, Tag } from 'lucide-react'
import { systemApi } from '../services/api'

interface AsrModelInfo {
  name: string
  provider: string
  description: string | null
  capabilities: string[]
  hint_type: 'keyword_boosting' | 'context_prompt' | 'none'
  context: string
}

interface AsrContextData {
  batch: AsrModelInfo | null
  stream: AsrModelInfo | null
}

function HintTypeBadge({ hintType }: { hintType: AsrModelInfo['hint_type'] }) {
  if (hintType === 'context_prompt') {
    return (
      <span className="inline-flex items-center gap-1 text-xs px-2 py-0.5 rounded-full bg-purple-100 text-purple-700 dark:bg-purple-900/40 dark:text-purple-300">
        <Sparkles className="h-3 w-3" /> Context prompt (LLM)
      </span>
    )
  }
  if (hintType === 'keyword_boosting') {
    return (
      <span className="inline-flex items-center gap-1 text-xs px-2 py-0.5 rounded-full bg-blue-100 text-blue-700 dark:bg-blue-900/40 dark:text-blue-300">
        <Tag className="h-3 w-3" /> Keyword boosting (acoustic)
      </span>
    )
  }
  return (
    <span className="inline-flex items-center gap-1 text-xs px-2 py-0.5 rounded-full bg-gray-100 text-gray-600 dark:bg-gray-700 dark:text-gray-400">
      No recognition hints
    </span>
  )
}

function ProviderRow({ label, model, onSaved }: {
  label: string
  model: AsrModelInfo
  onSaved: () => void
}) {
  const [context, setContext] = useState(model.context)
  const [saving, setSaving] = useState(false)
  const [savedAt, setSavedAt] = useState<number | null>(null)
  const [error, setError] = useState<string | null>(null)

  // Keep the textarea in sync if the active provider/context changes underneath us.
  useEffect(() => { setContext(model.context) }, [model.name, model.context])

  const dirty = context !== model.context

  const save = async () => {
    setSaving(true)
    setError(null)
    try {
      await systemApi.saveAsrContext(model.name, context)
      setSavedAt(Date.now())
      onSaved()
    } catch (e: any) {
      setError(e.response?.data?.detail ?? e.message)
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="p-3 bg-gray-50 dark:bg-gray-700 rounded-md">
      <div className="flex items-center justify-between flex-wrap gap-2">
        <div className="min-w-0">
          <div className="font-medium text-gray-900 dark:text-gray-100">
            {label}
            <span className="ml-2 text-xs text-gray-500 dark:text-gray-400">{model.provider}</span>
          </div>
          {model.description && (
            <div className="text-sm text-gray-600 dark:text-gray-400 truncate">{model.description}</div>
          )}
        </div>
        <HintTypeBadge hintType={model.hint_type} />
      </div>

      {model.hint_type === 'context_prompt' ? (
        <div className="mt-3">
          <label className="block text-xs text-gray-500 dark:text-gray-400 mb-1">
            Context — describe the domain, names, or jargon to help this LLM-based model.
            It informs recognition but is never transcribed.
          </label>
          <textarea
            value={context}
            onChange={e => setContext(e.target.value)}
            rows={2}
            placeholder="e.g. A tech podcast about ASR, wearables, and the Chronicle app. Speakers: Ankush, Hermes."
            className="w-full text-sm px-2 py-1.5 rounded-md border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-800 text-gray-900 dark:text-gray-100"
          />
          <div className="mt-2 flex items-center gap-3">
            <button
              onClick={save}
              disabled={!dirty || saving}
              className="flex items-center gap-1 px-3 py-1.5 text-sm bg-blue-600 text-white rounded-md hover:bg-blue-700 transition-colors disabled:opacity-50"
            >
              <Save className="h-3.5 w-3.5" />
              <span>{saving ? 'Saving…' : 'Save context'}</span>
            </button>
            {savedAt && !dirty && (
              <span className="text-xs text-green-600 dark:text-green-400">Saved — applies on next transcription.</span>
            )}
            {error && <span className="text-xs text-red-600 dark:text-red-400">{error}</span>}
          </div>
        </div>
      ) : model.hint_type === 'keyword_boosting' ? (
        <p className="mt-2 text-xs text-gray-500 dark:text-gray-400">
          Uses acoustic keyword boosting: wake-words and keywords from enabled plugins (plus the
          <code className="px-1 mx-1 bg-gray-100 dark:bg-gray-700 rounded">asr.hot_words</code> prompt)
          bias recognition without appearing in the transcript. No context to configure here.
        </p>
      ) : (
        <p className="mt-2 text-xs text-gray-500 dark:text-gray-400">
          This provider does not accept recognition hints.
        </p>
      )}
    </div>
  )
}

export default function AsrContextSettings({ isAdmin }: { isAdmin: boolean }) {
  const [data, setData] = useState<AsrContextData | null>(null)
  const [loaded, setLoaded] = useState(false)

  const load = async () => {
    try {
      const res = await systemApi.getAsrContext()
      setData({ batch: res.data.batch, stream: res.data.stream })
    } catch {
      setData(null)
    } finally {
      setLoaded(true)
    }
  }

  useEffect(() => { if (isAdmin) load() }, [isAdmin])

  if (!isAdmin || !loaded || !data) return null

  // Show the streaming provider only when it differs from the batch one.
  const showStream = data.stream && data.stream.name !== data.batch?.name

  return (
    <div className="bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 p-6">
      <h3 className="text-lg font-semibold text-gray-900 dark:text-gray-100 mb-1 flex items-center">
        <Mic className="h-5 w-5 mr-2 text-blue-600" />
        ASR Recognition Hints
      </h3>
      <p className="text-sm text-gray-500 dark:text-gray-400 mb-4">
        How the active transcription provider takes recognition hints. Keyword-boosting models use
        wake-words as an acoustic bias; LLM-based models take a free-form context you author here.
      </p>
      <div className="space-y-3">
        {data.batch && <ProviderRow label="Batch transcription" model={data.batch} onSaved={load} />}
        {showStream && data.stream && (
          <ProviderRow label="Streaming transcription" model={data.stream} onSaved={load} />
        )}
      </div>
    </div>
  )
}
