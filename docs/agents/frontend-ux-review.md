# Frontend UX Review Notes

Use these Chronicle-specific lessons together with the installed
`frontend-design-principles` skill and the repository's `screenshots` skill.

## Visual hierarchy

- Metadata should look like metadata. Use muted text and existing metadata rows;
  do not give transcript versions, counts, or implementation details a colored
  panel or prominent chip.
- Color must communicate action or state. Decorative blue/amber treatments make
  low-priority information compete with the conversation title and primary task.
- Do not allocate a dedicated row to one small fact. Fold secondary information
  into an existing row or omit it when it does not help a decision.
- Apply the squint test before presenting a UI: the title and primary task should
  remain dominant, and no incidental control should jump out.

## Interaction design

- Avoid duplicate actions. Destructive and maintenance actions such as
  reprocessing belong in the three-dots menu when they are not the page's primary
  task.
- When making a card clickable, preserve every nested interaction. Use semantic
  buttons for expanders and explicit event boundaries around editors, dropdowns,
  playback, and selectable content. Verify both card navigation and nested controls
  in a browser regression check.
- A large click target needs a subtle hover/focus affordance; it should not require
  a redundant "View details" link.
- Small icon-only controls need an adequate hit area, an accessible name, and a
  visible keyboard focus state.

## Review workflows

- A review surface must provide enough coverage to support judgment. One or two
  clips are anecdotes, not a useful acoustic sample.
- Keep semantically different training data separate. Chronicle uses `Noise` for
  non-speech/undeciphered audio and `Background Speech` for intelligible speech
  from TV, radio, media, or distant non-participants.
- Show source context, timestamps, playback, coverage counts, and enough diverse
  candidates to compare acoustic conditions. Balance quality with responsiveness;
  the current background sampler targets 20 candidates per bucket across eight
  conversations.

## Visual verification

- Capture the rendered page after the final change at 1440×1000 using
  `skills/screenshots/SKILL.md`; authenticated and dynamic routes must use real data.
- Inspect the screenshot rather than treating capture as proof. Look for wasted
  rows, competing accents, inconsistent control styling, tiny targets, raw IDs,
  redundant links, and controls that appear when they have no content.
- Exercise interactions separately with Playwright. A static screenshot cannot
  detect event bubbling, navigation conflicts, empty expanders, or unusable menus.
- Treat login redirects, blank pages, unresolved loading, and development-server
  reload loops as failed verification—not successful screenshots.

