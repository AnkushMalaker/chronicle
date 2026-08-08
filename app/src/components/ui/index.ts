/**
 * Chronicle mobile UI primitives.
 *
 * The React Native counterpart of `design-system/components/`, and of the web
 * dashboard's `webui/src/components/ui`. Screens should compose these rather
 * than restyling a `View`/`Text`/`TouchableOpacity` from scratch — that is what
 * keeps the app on the palette when the palette changes.
 */

export { InlineAlert } from './Alert';
export { Badge, StatusDot, toneDotColor, type Tone } from './Badge';
export { Button, ButtonRow, type ButtonSize, type ButtonVariant } from './Button';
export { Card, CardWell, Divider } from './Card';
export { Screen } from './Screen';
export { Body, Caption, Heading, Mono, SectionLabel } from './Text';
export { TextField } from './TextField';
