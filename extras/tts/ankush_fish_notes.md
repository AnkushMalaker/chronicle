# Fish Speech Tuning Notes (S2-Pro)

## S2-Pro Model Overview

- 5B total params (4B slow AR + 400M fast AR + codec)
- Trained on 10M+ hours, 80+ languages
- Free-form `[bracket]` tag syntax — 15,000+ tags, accepts arbitrary natural language descriptions
- RTF ~0.195 on H200, ~100ms time-to-first-audio
- License: Fish Audio Research License (free for research/non-commercial)
- Needs ~24GB VRAM

## Generation Parameters

All passed via JSON body to fish-speech's `/v1/tts` endpoint.

| Parameter | Default | Range | Effect |
|-----------|---------|-------|--------|
| `temperature` | 0.7 | 0–1.0 | Expressiveness. Lower = stable, higher = varied |
| `top_p` | 0.7 | 0–1.0 | Nucleus sampling. Lower = more focused |
| `repetition_penalty` | 1.2 | >1.0 | Penalizes repeated audio patterns |
| `max_new_tokens` | 1024 | int | Max audio tokens per chunk |
| `seed` | null | int | Fix for reproducible output |
| `chunk_length` | 300 | 100–300 | Text segment size for processing |
| `normalize` | true | bool | Text normalization. Set `false` to preserve intonation tags (but may break numbers/dates) |
| `references` | [] | list | Reference audio clips for voice cloning (base64 audio + text) |
| `format` | wav | wav/pcm/mp3 | Output audio format |

## Expression Tags (S2-Pro uses `[brackets]`)

S2-Pro accepts **free-form natural language** inside brackets — not a fixed set. The model generalizes to novel descriptions.

### Well-tested tags

**Emotions:** `[excited]`, `[happy]`, `[sad]`, `[angry]`, `[calm]`, `[nervous]`, `[surprised]`, `[sarcastic]`, `[delight]`, `[shocked]`

**Tone/delivery:** `[whispering]`, `[shouting]`, `[screaming]`, `[loud]`, `[low voice]`, `[low volume]`, `[volume up]`, `[volume down]`, `[excited tone]`, `[laughing tone]`, `[professional broadcast tone]`

**Audio effects:** `[laughing]`, `[chuckling]`, `[chuckle]`, `[sigh]`, `[panting]`, `[gasping]`, `[inhale]`, `[exhale]`, `[clearing throat]`, `[tsk]`, `[moaning]`

**Pacing:** `[pause]`, `[short pause]`, `[fast]`, `[slow]`

**Special:** `[singing]`, `[echo]`, `[audience laughter]`, `[with strong accent]`, `[interrupting]`, `[emphasis]`

### Free-form examples (these also work)

```
[whisper in small voice]
[pitch up]
[dead tired, end of a very long shift]
[voice rough from crying, trying to sound normal]
[professional broadcast tone]
```

## Tag Placement Rules

- **Placement = scope.** Tag applies from its position until the next tag or end of sentence.
- Place tags **immediately before** the text they should affect.
- `[whispering] Don't let them hear you.` — whispers entire line
- `I was fine [whispering] until I saw it.` — whispers only from "until" onward
- **One emotion per sentence.** Don't stack conflicting emotions.
- **Pair physical + emotion for realism:** `[panting] [tired] I've been running.` sounds better than `[panting]` alone.
- **Don't orphan tags** at the end of text — always follow with spoken text.
- Write tags in the same language as your script for best results.

## Quality Best Practices

- **Start minimal.** One well-placed tag, then add more. Excessive tagging kills naturalness.
- **Preview frequently.** Different voices respond differently to the same tags.
- **Use `seed`** to lock in a good generation once found.
- **Lower temp for consistency:** `temperature: 0.3`, `top_p: 0.5` for stable bot intros.
- **Higher temp for expression:** defaults (0.7/0.7) for conversational speech.
- **Set `normalize: false`** when using intonation tags (but numbers/dates may read wrong).
- If output sounds robotic with voice cloning, use longer reference clips (30-60s).

## Voice Cloning / Reference Audio Tips

- **Duration:** 10-30s minimum, 30-60s is ideal
- **Best format:** 2-3 clips of 15-20s each forming a complete paragraph
- **Single speaker only** — no multiple voices
- **Minimize background noise**, echo, competing audio
- **Consistent volume/tone** throughout
- **Brief pauses** between sentences (~0.5s)
- **Speak naturally**, don't rush
- USB mic, gaming headset, or phone recorder all work fine
- Quiet room (bedroom, parked car, office)

## Multi-Speaker

Use `<|speaker:i|>` tokens in text to switch speakers within a single generation (where `i` is speaker index).

## Differences from S1

S1 uses `(parentheses)` with 69 fixed predefined tags. S2-Pro uses `[brackets]` with free-form natural language — much more flexible. Don't mix the two syntaxes.
