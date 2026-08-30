// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { DayReviewProjection, TimelineEpisode } from '../../services/api'
import EvidenceTape, { evidenceChannel } from './EvidenceTape'

afterEach(cleanup)

const mediaEpisode = {
  episode_id: 'media-1',
  episode_key: 'media-key',
  started_at: '2026-02-19T04:00:00.000Z',
  ended_at: '2026-02-19T05:00:00.000Z',
  kind: 'media',
  title: 'Watching YouTube',
  summary: 'A long-form video was playing.',
  status: 'provisional',
  confirmed_at: null,
  confirmed_fields: [],
  memory_policy: 'auto',
  salience: 'routine',
  confidence: 0.91,
  activity_mode: 'foreground',
  entities: [],
  attributes: {},
  assertions: [],
  evidence: [],
  related_episode_ids: [],
  related_conversation_ids: [],
  audio_ranges: [],
  parent_episode_id: null,
  has_thumbnail: true,
} satisfies TimelineEpisode

const conversationEpisode = {
  ...mediaEpisode,
  episode_id: 'conversation-1',
  episode_key: 'conversation-key',
  kind: 'communication',
  title: 'Planning Chronicle',
  summary: 'A project discussion.',
  started_at: '2026-02-19T06:00:00.000Z',
  ended_at: '2026-02-19T06:30:00.000Z',
} satisfies TimelineEpisode

const projection = {
  version: 'test',
  day_started_at: '2026-02-19T00:00:00.000Z',
  day_ended_at: '2026-02-20T00:00:00.000Z',
  episode_count: 2,
  group_count: 2,
  needs_attention_count: 0,
  confirmed_count: 0,
  groups: [
    {
      group_id: 'media-group',
      started_at: mediaEpisode.started_at,
      ended_at: mediaEpisode.ended_at,
      title: mediaEpisode.title,
      summary: '',
      semantic: false,
      lane: 'foreground',
      episode_ids: [mediaEpisode.episode_id],
      episode_count: 1,
      conversational_count: 0,
      confirmed_count: 0,
      duration_seconds: 3600,
      span_seconds: 3600,
      gap_seconds: 0,
      intervals: [{ episode_id: mediaEpisode.episode_id, started_at: mediaEpisode.started_at, ended_at: mediaEpisode.ended_at }],
      entities: [],
      salience: 'routine',
      attention_reasons: [],
      needs_attention: false,
    },
    {
      group_id: 'conversation-group',
      started_at: conversationEpisode.started_at,
      ended_at: conversationEpisode.ended_at,
      title: conversationEpisode.title,
      summary: '',
      semantic: false,
      lane: 'conversation',
      episode_ids: [conversationEpisode.episode_id],
      episode_count: 1,
      conversational_count: 1,
      confirmed_count: 0,
      duration_seconds: 1800,
      span_seconds: 1800,
      gap_seconds: 0,
      intervals: [{ episode_id: conversationEpisode.episode_id, started_at: conversationEpisode.started_at, ended_at: conversationEpisode.ended_at }],
      entities: [],
      salience: 'routine',
      attention_reasons: [],
      needs_attention: false,
    },
  ],
} satisfies DayReviewProjection

describe('EvidenceTape', () => {
  it('identifies media without relying on color and synchronizes selection and lenses', () => {
    const onSelectEpisode = vi.fn()
    const onLensChange = vi.fn()
    render(
      <EvidenceTape
        projection={projection}
        episodes={[mediaEpisode, conversationEpisode]}
        coverage={[{
          started_at: '2026-02-19T08:00:00.000Z',
          ended_at: '2026-02-19T09:00:00.000Z',
          kind: 'no_capture',
          label: 'No capture',
        }]}
        lens="all"
        selectedEpisodeId={null}
        onLensChange={onLensChange}
        onSelectEpisode={onSelectEpisode}
      />,
    )

    const mediaIntervals = screen.getAllByRole('button', { name: /Media.*YouTube/i })
    fireEvent.focus(mediaIntervals[0])
    expect(screen.getByTestId('tape-preview')).toHaveTextContent('Media')
    expect(screen.getByTestId('tape-preview')).toHaveTextContent('A long-form video was playing.')
    expect(screen.getByTestId('tape-preview')).toHaveTextContent('provisional')
    expect(screen.getByTestId('tape-preview')).toHaveClass('h-[7.25rem]', 'sm:h-[5.5rem]')

    fireEvent.click(mediaIntervals[0])
    expect(onSelectEpisode).toHaveBeenCalledWith('media-1')

    fireEvent.click(screen.getByRole('button', { name: /^Media 1$/ }))
    expect(onLensChange).toHaveBeenCalledWith('media')
    expect(screen.getAllByRole('button', { name: /No capture/i }).length).toBeGreaterThan(0)
  })

  it('uses media as the primary channel and conversation as a separate non-media channel', () => {
    expect(evidenceChannel(mediaEpisode, 'conversation')).toBe('media')
    expect(evidenceChannel(conversationEpisode, 'conversation')).toBe('conversation')
  })
})
