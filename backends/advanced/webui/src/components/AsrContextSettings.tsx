import { useEffect, useState } from 'react'
import { Mic, Save, Sparkles, Tag } from 'lucide-react'
import { systemApi } from '../services/api'
import { Button, Card, StateBadge, Textarea } from './ui'

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
      <StateBadge tone="suggest" className="inline-flex items-center gap-1">
        <Sparkles className="h-3 w-3" /> Context prompt (LLM)
      </StateBadge>
    )
  }
  if (hintType === 'keyword_boosting') {
    return (
      <StateBadge tone="info" className="inline-flex items-center gap-1">
        <Tag className="h-3 w-3" /> Keyword boosting (acoustic)
      </StateBadge>
    )
  }
  return <StateBadge tone="neutral">No recognition hints</StateBadge>
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
          <Textarea
            value={context}
            onChange={e => setContext(e.target.value)}
            rows={2}
            placeholder="e.g. A tech podcast about ASR, wearables, and the Chronicle app. Speakers: Ankush, Hermes."
          />
          <div className="mt-2 flex items-center gap-3">
            <Button
              variant="primary"
              onClick={save}
              disabled={!dirty || saving}
              icon={<Save className="h-3.5 w-3.5" />}
            >
              {saving ? 'Saving…' : 'Save context'}
            </Button>
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
    <Card raised padded={false} className="p-6">
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
    </Card>
  )
}
