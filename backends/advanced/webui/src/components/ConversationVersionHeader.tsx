import { useState } from 'react';
import { RotateCcw } from 'lucide-react';
import { conversationsApi } from '../services/api';
import ConversationVersionDropdown from './ConversationVersionDropdown';

interface ConversationVersionHeaderProps {
  conversationId: string;
  versionInfo?: {
    transcript_count: number;
    memory_count: number;
    active_transcript_version?: string;
    active_memory_version?: string;
    active_transcript_version_number?: number;
    active_memory_version_number?: number;
  };
  onVersionChange?: () => void;
}

export default function ConversationVersionHeader({ conversationId, versionInfo, onVersionChange }: ConversationVersionHeaderProps) {
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleReprocessTranscript = async (event: React.MouseEvent) => {
    event.preventDefault();
    event.stopPropagation();

    try {
      setLoading(true);
      await conversationsApi.reprocessTranscript(conversationId);
      onVersionChange?.();
    } catch (err) {
      console.error('Failed to reprocess transcript:', err);
      setError('Failed to reprocess transcript');
    } finally {
      setLoading(false);
    }
  };

  // If no version info provided, don't show anything
  if (!versionInfo) return null;

  // Only show if there are multiple versions or reprocessing capability
  if (versionInfo.transcript_count <= 1 && versionInfo.memory_count <= 1) {
    return (
      <div className="bg-gradient-to-r from-blue-50 to-indigo-50 border border-blue-200 rounded-lg p-3 mb-4">
        <div className="flex items-center justify-between">
          <div className="text-sm text-gray-600">
            {versionInfo.transcript_count} transcript version, {versionInfo.memory_count} memory version
          </div>
          <button
            type="button"
            onClick={handleReprocessTranscript}
            disabled={loading}
            className="inline-flex items-center px-2 py-1 border border-transparent text-xs font-medium rounded text-blue-700 bg-blue-100 hover:bg-blue-200 focus:outline-none focus:ring-1 focus:ring-offset-1 focus:ring-blue-500 disabled:opacity-50"
          >
            {loading ? (
              <>
                <RotateCcw className="h-3 w-3 animate-spin mr-1" />
                Processing...
              </>
            ) : (
              <>
                <RotateCcw className="h-3 w-3 mr-1" />
                Reprocess
              </>
            )}
          </button>
        </div>
      </div>
    );
  }

  // Show multiple version info with reprocess option and version selector
  return (
    <div className="bg-gradient-to-r from-blue-50 to-indigo-50 border border-blue-200 rounded-lg p-3 mb-4">
      <div className="flex items-center justify-between">
        <div className="flex items-center space-x-4">
          <div className="text-sm text-gray-700">
            <span className="font-medium">{versionInfo.transcript_count}</span> transcript versions,
            <span className="font-medium ml-1">{versionInfo.memory_count}</span> memory versions
            {error && <div className="text-red-600 text-xs mt-1">{error}</div>}
          </div>

          {/* Version Selector Dropdowns */}
          <ConversationVersionDropdown
            conversationId={conversationId}
            versionInfo={versionInfo}
            onVersionChange={onVersionChange || (() => {})}
          />
        </div>

        <button
          type="button"
          onClick={handleReprocessTranscript}
          disabled={loading}
          className="inline-flex items-center px-2 py-1 border border-transparent text-xs font-medium rounded text-blue-700 bg-blue-100 hover:bg-blue-200 focus:outline-none focus:ring-1 focus:ring-offset-1 focus:ring-blue-500 disabled:opacity-50"
        >
          {loading ? (
            <>
              <RotateCcw className="h-3 w-3 animate-spin mr-1" />
              Processing...
            </>
          ) : (
            <>
              <RotateCcw className="h-3 w-3 mr-1" />
              Reprocess
            </>
          )}
        </button>
      </div>
    </div>
  );
}