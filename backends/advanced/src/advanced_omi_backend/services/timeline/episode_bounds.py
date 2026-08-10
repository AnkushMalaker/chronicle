"""How long an episode may run, and where it is allowed to break.

The segmentation agent decides what an episode *is*, and mostly it is right: the median
episode here is 16 minutes and the 90th percentile is 64. The tail is not right. 26 of
521 episodes exceed two hours, 8 exceed three, and one claims 956 minutes — sixteen
hours filed as a single "mixed audio session", which is not a thing that happened.

The rule is deliberately weak, because the strong version is the bug this codebase
already fixed once for recordings: cutting at a fixed length severed 94 of 176 capture
windows mid-sentence. So a long episode is *offered* a cut and only takes one where the
audio agrees there is a seam — a real silence of at least five minutes. A dense
three-hour meeting has no such seam and stays whole, which is correct. Nothing here
forces a cut; an over-long episode with no seam is reported, not butchered.

That report is the point as much as the split is. Given working VAD, a genuinely long
stretch of audio almost always contains five quiet minutes somewhere. One that does not
is either a real marathon or a broken VAD, and the assessment carries the numbers
needed to tell those apart rather than guessing.

**Silence is necessary for a boundary, never sufficient.** Media has VAD: a film, a
stream, and a game all produce speech-shaped scores, so the audio alone cannot say
whether a quiet stretch ended an activity or merely fell between two lines of dialogue.
What is computed here is therefore a *candidate* boundary — where the audio would
permit a break — which the segmentation agent then accepts or rejects with the context
it has and this module does not: application identity, window titles, and who was
speaking. Screen *content* is deliberately not part of that; boundaries inferred from
it were evaluated and rejected, while boundaries from app identity proved reliable
(docs/screenpipe.md).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum

from advanced_omi_backend.models.audio_chunk import AudioChunkDocument
from advanced_omi_backend.services.transcript_time import as_utc
from advanced_omi_backend.utils.vad_analysis import SPEECH_PROB_THRESHOLD

logger = logging.getLogger(__name__)

# One bucket per stored chunk. Finer resolution would be false precision: the profile
# is assembled from chunk-level VAD frames, and the decision it feeds is measured in
# minutes.
BUCKET_SECONDS = 10.0
# Episodes may exceed this; it is where a seam is looked for, not a limit.
EPISODE_TARGET = timedelta(hours=2)
# Shortest silence that may carry an episode boundary. A conversation has pauses; five
# quiet minutes is someone leaving, not someone thinking.
EPISODE_MIN_QUIET = timedelta(minutes=5)
# Below this share of measured buckets, "no seam was found" says more about the
# coverage than about the audio, and must not be reported as a property of the audio.
MIN_COVERAGE = 0.5


class BucketState(StrEnum):
    """What is known about one bucket of audio.

    Four states, not a number and a hole. Modelling "nothing known" as ``None`` in a
    ``list[float | None]`` cost two real bugs in the recording path, because
    ``not None`` is ``True`` and unknown therefore read as silent: an unscored window
    looked uniformly quiet, so the cut landed back on the blind target the code existed
    to avoid, and hid itself by also reporting no speech.

    ``NO_CAPTURE`` and ``UNSCORED`` are then split apart because they are opposite
    kinds of fact. Nothing being recorded is *knowledge* — and a strong boundary, since
    an activity that continued would have kept producing audio. A chunk nobody scored
    is genuine ignorance, and nothing may be concluded from it. Before the VAD backfill
    these were indistinguishable; afterwards, all 5,357 unmeasured buckets across this
    deployment's long episodes turned out to be capture gaps and none were unscored, so
    conflating them was reporting known gaps as missing data.
    """

    SPEECH = "speech"
    SILENT = "silent"
    NO_CAPTURE = "no_capture"
    UNSCORED = "unscored"


@dataclass(frozen=True, slots=True)
class SpeechBucket:
    """One bucket's verdict, and the speech share behind it."""

    state: BucketState
    speech_share: float = 0.0

    @classmethod
    def measured(cls, speech_share: float) -> SpeechBucket:
        return cls(
            state=BucketState.SPEECH if speech_share else BucketState.SILENT,
            speech_share=speech_share,
        )

    @classmethod
    def no_capture(cls) -> SpeechBucket:
        return cls(state=BucketState.NO_CAPTURE)

    @classmethod
    def unscored(cls) -> SpeechBucket:
        return cls(state=BucketState.UNSCORED)

    @property
    def is_measured(self) -> bool:
        """VAD returned a verdict for this bucket."""

        return self.state in (BucketState.SPEECH, BucketState.SILENT)

    @property
    def is_known(self) -> bool:
        """Something is known here — a VAD verdict, or that nothing was recorded."""

        return self.state is not BucketState.UNSCORED

    @property
    def is_quiet(self) -> bool:
        """No speech happened here, and that is known rather than assumed.

        True for measured silence and for a capture gap; never for an unscored bucket.
        """

        return self.state in (BucketState.SILENT, BucketState.NO_CAPTURE)


@dataclass(frozen=True, slots=True)
class QuietRun:
    """A half-open bucket range of measured silence."""

    first: int
    last: int

    @property
    def buckets(self) -> int:
        return self.last - self.first

    def seconds(self, bucket_seconds: float) -> float:
        return self.buckets * bucket_seconds

    def midpoint(self) -> float:
        """Bucket index at the middle of the run; a cut goes here."""

        return (self.first + self.last) / 2


@dataclass(frozen=True)
class SpeechProfile:
    """Speech activity across a span of wall-clock time, bucket by bucket."""

    buckets: tuple[SpeechBucket, ...] = ()
    bucket_seconds: float = BUCKET_SECONDS

    def __len__(self) -> int:
        return len(self.buckets)

    def __bool__(self) -> bool:
        return bool(self.buckets)

    @property
    def measured_buckets(self) -> int:
        """Buckets VAD actually scored — the evidence that audio existed."""

        return sum(1 for bucket in self.buckets if bucket.is_measured)

    @property
    def known_buckets(self) -> int:
        return sum(1 for bucket in self.buckets if bucket.is_known)

    @property
    def speech_buckets(self) -> int:
        return sum(1 for bucket in self.buckets if bucket.state is BucketState.SPEECH)

    @property
    def known_fraction(self) -> float:
        """Share of the span something is known about, gap or verdict alike."""

        return self.known_buckets / len(self) if self.buckets else 0.0

    @property
    def measured_fraction(self) -> float:
        return self.measured_buckets / len(self) if self.buckets else 0.0

    @property
    def speech_fraction(self) -> float:
        measured = self.measured_buckets
        return self.speech_buckets / measured if measured else 0.0

    def slice(self, first: int, last: int) -> SpeechProfile:
        return SpeechProfile(self.buckets[first:last], self.bucket_seconds)

    def quiet_runs(self) -> list[QuietRun]:
        """Runs where no speech happened, by measurement or by absence of capture.

        An unscored bucket never joins one — that is ignorance, not quiet.
        """

        runs: list[QuietRun] = []
        start: int | None = None
        for index, bucket in enumerate(self.buckets):
            if bucket.is_quiet:
                start = index if start is None else start
            elif start is not None:
                runs.append(QuietRun(start, index))
                start = None
        if start is not None:
            runs.append(QuietRun(start, len(self.buckets)))
        return runs

    @property
    def longest_quiet_seconds(self) -> float:
        runs = self.quiet_runs()
        return max((run.buckets for run in runs), default=0) * self.bucket_seconds


class BoundsVerdict(StrEnum):
    """Why an episode was, or was not, given a boundary."""

    WITHIN_TARGET = "within_target"
    SPLIT = "split"
    NO_AUDIO = "no_audio"
    UNANALYZED = "unanalyzed"
    LOW_COVERAGE = "low_coverage"
    NO_SEAM = "no_seam"


@dataclass
class EpisodeBoundsAssessment:
    """What the audio says about an episode's length, and where it could break.

    The cuts are *candidates*: places the audio permits a boundary. Accepting one is
    the segmentation agent's call, because silence alone cannot distinguish the end of
    an activity from a pause in a film's dialogue.
    """

    started_at: datetime
    ended_at: datetime
    verdict: BoundsVerdict
    profile: SpeechProfile = field(default_factory=SpeechProfile)
    cuts: list[datetime] = field(default_factory=list)

    @property
    def duration_seconds(self) -> float:
        return (self.ended_at - self.started_at).total_seconds()

    @property
    def vad_suspect(self) -> bool:
        """A long episode whose audio reports no quiet stretch anywhere at all.

        The test is the *longest measured silence*, not whether a cut was placed: a
        seam can exist and still be rejected for leaving a stub, and calling that a
        VAD fault cries wolf — it did, on two real episodes of 303 and 305 minutes
        that each held a usable gap.

        Given working VAD, hours of audio with no quiet minutes is close to
        impossible; the shape belongs to a VAD that failed, or to continuous media
        being scored as speech. Not an error by itself — a genuine marathon exists —
        but worth surfacing rather than silently accepting a sixteen-hour episode.
        """

        return (
            self.verdict is BoundsVerdict.NO_SEAM
            and self.duration_seconds > 2 * EPISODE_TARGET.total_seconds()
            and self.profile.longest_quiet_seconds < EPISODE_MIN_QUIET.total_seconds()
        )


async def speech_profile_for_range(
    started_at: datetime,
    ended_at: datetime,
    *,
    bucket_seconds: float = BUCKET_SECONDS,
) -> SpeechProfile:
    """Speech activity over an absolute range, assembled from stored chunk VAD.

    Each chunk is positioned by its immutable ``captured_at``, which makes the profile
    independent of whichever container currently owns the audio — the same reason an
    episode's claim is expressed in absolute time.

    A bucket no chunk covers is ``NO_CAPTURE`` — nothing was recorded then, which is
    knowledge. A bucket a chunk covers but no VAD frame scored is ``UNSCORED``, which
    is not.
    """

    start, end = as_utc(started_at), as_utc(ended_at)
    span = (end - start).total_seconds()
    if span <= 0 or bucket_seconds <= 0:
        return SpeechProfile(bucket_seconds=bucket_seconds)
    count = max(1, int(span // bucket_seconds))
    speech_frames = [0] * count
    total_frames = [0] * count
    captured = [False] * count

    chunks = await AudioChunkDocument.find(
        {
            "captured_at": {"$gte": start - timedelta(seconds=60), "$lt": end},
            "deleted": {"$ne": True},
        }
    ).to_list()
    for chunk in chunks:
        if chunk.captured_at is None:
            continue
        offset = (as_utc(chunk.captured_at) - start).total_seconds()
        # Coverage is recorded from the chunk's own span, independently of whether it
        # carries VAD, so a gap in *recording* stays distinguishable from a gap in
        # *scoring*.
        first = int(offset // bucket_seconds)
        last = int((offset + (chunk.duration or 0.0)) // bucket_seconds)
        for bucket in range(max(0, first), min(count - 1, last) + 1):
            captured[bucket] = True

        vad = chunk.vad
        if vad is None or not vad.scores:
            continue
        hop = (vad.frame_hop_ms or 0.0) / 1000.0
        if hop <= 0:
            continue
        threshold = vad.threshold or SPEECH_PROB_THRESHOLD
        for index, score in enumerate(vad.scores):
            bucket = int((offset + index * hop) // bucket_seconds)
            if 0 <= bucket < count:
                total_frames[bucket] += 1
                if score >= threshold:
                    speech_frames[bucket] += 1

    def _bucket(index: int) -> SpeechBucket:
        if total_frames[index]:
            return SpeechBucket.measured(speech_frames[index] / total_frames[index])
        return SpeechBucket.unscored() if captured[index] else SpeechBucket.no_capture()

    return SpeechProfile(
        buckets=tuple(_bucket(index) for index in range(count)),
        bucket_seconds=bucket_seconds,
    )


def plan_episode_cuts(
    profile: SpeechProfile,
    *,
    target: timedelta = EPISODE_TARGET,
    min_quiet: timedelta = EPISODE_MIN_QUIET,
) -> list[float]:
    """Offsets where an over-long episode may break, in seconds from its start.

    Deliberately not ``plan_session_cuts``. That searches a band around a preferred
    *length*, because a recording is a unit of storage and should come out near 30
    minutes. An episode is a unit of meaning with no preferred length, so the whole
    remaining span is searched and the longest real silence wins. A five-hour episode
    with one nine-minute gap at its twentieth minute breaks there, which the banded
    search cannot see — measured on two real episodes of 303 and 305 minutes.

    A cut is only ever offered in measured silence. When there is none, the episode is
    returned whole: forcing a boundary through speech is the failure being avoided.
    """

    if profile.bucket_seconds <= 0 or not profile:
        return []
    bucket_seconds = profile.bucket_seconds
    min_buckets = max(1, int(min_quiet.total_seconds() / bucket_seconds))
    # Neither side of a cut may be a stub. A quarter of the target is generous enough
    # to keep a real short coda while refusing to shear off two minutes.
    edge = max(1, int(target.total_seconds() * 0.25 / bucket_seconds))
    target_buckets = target.total_seconds() / bucket_seconds

    cuts: list[float] = []
    start = 0
    end = len(profile)
    while end - start > target_buckets:
        window = profile.slice(start, end)
        candidates = [
            run
            for run in window.quiet_runs()
            if run.buckets >= min_buckets
            and run.first >= edge
            and run.last <= len(window) - edge
        ]
        if not candidates:
            break
        # Longest silence wins; ties go to the earliest, so a profile with equal gaps
        # breaks left to right and each pass leaves the rest still searchable.
        best = max(candidates, key=lambda run: (run.buckets, -run.first))
        cut = start + best.midpoint()
        cuts.append(round(cut * bucket_seconds, 3))
        start = int(cut)
    return cuts


def assess_profile(
    started_at: datetime,
    ended_at: datetime,
    profile: SpeechProfile,
    *,
    target: timedelta = EPISODE_TARGET,
    min_quiet: timedelta = EPISODE_MIN_QUIET,
) -> EpisodeBoundsAssessment:
    """Decide whether an episode may break, and where, from its speech profile."""

    start, end = as_utc(started_at), as_utc(ended_at)
    assessment = EpisodeBoundsAssessment(
        started_at=start,
        ended_at=end,
        verdict=BoundsVerdict.WITHIN_TARGET,
        profile=profile,
    )
    if assessment.duration_seconds <= target.total_seconds():
        return assessment
    if not profile:
        assessment.verdict = BoundsVerdict.NO_AUDIO
        return assessment
    if not profile.measured_buckets:
        # No VAD verdict anywhere. If nothing was recorded either, the audio simply has
        # no opinion about this episode and must not be given one — a three-hour film
        # watched in silence is one episode, and cutting it in half because it produced
        # no audio would be absurd. If chunks exist but went unscored, that is a gap in
        # analysis rather than in capture.
        assessment.verdict = (
            BoundsVerdict.UNANALYZED
            if profile.known_buckets < len(profile)
            else BoundsVerdict.NO_AUDIO
        )
        return assessment

    offsets = plan_episode_cuts(profile, target=target, min_quiet=min_quiet)
    assessment.cuts = [start + timedelta(seconds=offset) for offset in offsets]
    if offsets:
        assessment.verdict = BoundsVerdict.SPLIT
    elif profile.known_fraction < MIN_COVERAGE:
        # Finding no seam in audio that was barely *scored* is a statement about the
        # scoring, not the audio, so it must not be reported as "no silence anywhere".
        # Note this tests known coverage, not measured: a capture gap is knowledge and
        # counts towards it. Before the VAD backfill this fired on six long episodes;
        # every one turned out to be capture gaps rather than unscored audio, which is
        # exactly the confusion the split states remove.
        assessment.verdict = BoundsVerdict.LOW_COVERAGE
    else:
        assessment.verdict = BoundsVerdict.NO_SEAM
    return assessment


async def assess_episode_bounds(
    started_at: datetime,
    ended_at: datetime,
    *,
    bucket_seconds: float = BUCKET_SECONDS,
    target: timedelta = EPISODE_TARGET,
    min_quiet: timedelta = EPISODE_MIN_QUIET,
) -> EpisodeBoundsAssessment:
    """Read the audio under an episode and decide whether it may break."""

    profile = await speech_profile_for_range(
        started_at, ended_at, bucket_seconds=bucket_seconds
    )
    assessment = assess_profile(
        started_at, ended_at, profile, target=target, min_quiet=min_quiet
    )
    if assessment.vad_suspect:
        logger.warning(
            "Episode %s->%s runs %.1fh with no %.0f-minute silence anywhere "
            "(%.0f%% of buckets measured, %.0f%% speech, longest quiet %.1f min): "
            "either a genuine marathon or a VAD fault",
            assessment.started_at,
            assessment.ended_at,
            assessment.duration_seconds / 3600,
            min_quiet.total_seconds() / 60,
            100 * profile.measured_fraction,
            100 * profile.speech_fraction,
            profile.longest_quiet_seconds / 60,
        )
    return assessment
