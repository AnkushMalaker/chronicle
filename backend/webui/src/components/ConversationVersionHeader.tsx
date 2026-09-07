import ConversationVersionDropdown from './ConversationVersionDropdown';

interface ConversationVersionHeaderProps {
  conversationId: string;
  versionInfo?: {
    transcript_count: number;
    active_transcript_version?: string;
    active_transcript_version_number?: number;
  };
  onVersionChange?: () => void;
}

export default function ConversationVersionHeader({ conversationId, versionInfo, onVersionChange }: ConversationVersionHeaderProps) {
  // If no version info provided, don't show anything
  if (!versionInfo) return null;

  if (versionInfo.transcript_count <= 1) return null;

  return (
    <ConversationVersionDropdown
      conversationId={conversationId}
      versionInfo={versionInfo}
      onVersionChange={onVersionChange || (() => {})}
    />
  );
}
