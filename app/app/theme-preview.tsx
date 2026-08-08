/**
 * Living style guide for the Chronicle mobile design system.
 *
 * Renders every semantic token and every primitive, in both themes, from the
 * real theme module — so it cannot drift from what the app actually renders.
 * Open it at `/theme-preview` (it is also the screen to screenshot when
 * reviewing a palette change).
 *
 * Deliberately avoids `Screen` and the safe-area context so it renders
 * standalone in a browser as well as on device.
 */

import React from 'react';
import { ScrollView, StyleSheet, Text, View } from 'react-native';

import {
  Badge,
  Button,
  ButtonRow,
  Card,
  CardWell,
  Caption,
  Divider,
  Heading,
  InlineAlert,
  Mono,
  SectionLabel,
  StatusDot,
  TextField,
  type Tone,
} from '@/components/ui';
import { ThemeProvider, useTheme, type Theme, type ThemeName } from '@/theme';

const TONES: Tone[] = ['neutral', 'success', 'danger', 'warning', 'info', 'suggest', 'accent'];

/**
 * A labelled colour chip. Always outlined, because several tokens are by design
 * the same colour as the card they sit on (`border.subtle` equals
 * `surface.raised` on dark) and would otherwise vanish from the guide.
 */
function Swatch({ name, color }: { name: string; color: string }) {
  const t = useTheme();
  const s = createStyles(t);
  return (
    <View style={s.swatch}>
      <View style={[s.swatchChip, { backgroundColor: color }]} />
      <Caption style={s.swatchName}>{name}</Caption>
    </View>
  );
}

function ThemeShowcase({ name }: { name: ThemeName }) {
  const t = useTheme();
  const s = createStyles(t);

  return (
    <View style={s.page}>
      <Heading style={s.pageTitle}>{name === 'dark' ? 'Dark' : 'Light'} theme</Heading>

      <SectionLabel>Surfaces &amp; borders</SectionLabel>
      <Card>
        <View style={s.swatchRow}>
          <Swatch name="surface.page" color={t.color.surface.page} />
          <Swatch name="surface.raised" color={t.color.surface.raised} />
          <Swatch name="surface.sunken" color={t.color.surface.sunken} />
          <Swatch name="border.base" color={t.color.border.base} />
          <Swatch name="border.subtle" color={t.color.border.subtle} />
        </View>
      </Card>

      <SectionLabel>Text</SectionLabel>
      <Card>
        <Text style={[s.sample, { color: t.color.text.primary }]}>text.primary — body copy</Text>
        <Text style={[s.sample, { color: t.color.text.secondary }]}>text.secondary — supporting</Text>
        <Text style={[s.sample, { color: t.color.text.muted }]}>text.muted — metadata</Text>
        <Text style={[s.sample, { color: t.color.text.faint }]}>text.faint — placeholders</Text>
      </Card>

      <SectionLabel>Accent &amp; status</SectionLabel>
      <Card>
        <View style={s.swatchRow}>
          <Swatch name="accent.base" color={t.color.accent.base} />
          <Swatch name="accent.hover" color={t.color.accent.hover} />
          <Swatch name="accent.fg" color={t.color.accent.fg} />
          <Swatch name="accent.navBg" color={t.color.accent.navBg} />
          <Swatch name="success.base" color={t.color.status.success.base} />
          <Swatch name="danger.base" color={t.color.status.danger.base} />
          <Swatch name="warning.base" color={t.color.status.warning.base} />
          <Swatch name="chip.bg" color={t.color.chip.bg} />
        </View>
      </Card>

      <SectionLabel>Typography</SectionLabel>
      <Card>
        <Heading>Heading — section title</Heading>
        <Text style={[s.typeSample, t.type['2xl'], { color: t.color.text.primary }]}>2xl display</Text>
        <Text style={[s.typeSample, t.type.base, { color: t.color.text.primary }]}>base — running text</Text>
        <Caption>Caption — hints, timestamps, help copy</Caption>
        <Mono>Mono — a1b2c3 · 16 kHz · v1.0.10</Mono>
      </Card>

      <SectionLabel>Buttons</SectionLabel>
      <Card>
        <ButtonRow style={s.wrapRow}>
          <Button variant="primary">Primary</Button>
          <Button variant="secondary">Secondary</Button>
          <Button variant="outline">Outline</Button>
          <Button variant="danger">Danger</Button>
          <Button variant="warning">Warning</Button>
          <Button variant="ghost">Ghost</Button>
          <Button variant="link">Link</Button>
        </ButtonRow>
        <Divider style={s.spaced} />
        <ButtonRow style={s.wrapRow}>
          <Button variant="primary" size="sm">Small</Button>
          <Button variant="primary" size="md">Medium</Button>
          <Button variant="primary" size="lg">Large</Button>
          <Button variant="primary" disabled>Disabled</Button>
          <Button variant="primary" loading>Loading</Button>
        </ButtonRow>
        <Divider style={s.spaced} />
        <Button variant="primary" size="lg" fullWidth>Full-width call to action</Button>
      </Card>

      <SectionLabel>Badges &amp; status dots</SectionLabel>
      <Card>
        <View style={s.wrapRow}>
          {TONES.map((tone) => (
            <Badge key={tone} tone={tone}>
              {tone}
            </Badge>
          ))}
        </View>
        <View style={[s.wrapRow, s.spaced]}>
          {TONES.map((tone) => (
            <View key={tone} style={s.dotPair}>
              <StatusDot tone={tone} />
              <Caption>{tone}</Caption>
            </View>
          ))}
        </View>
        <View style={s.spaced}>
          <Badge tone="neutral" mono>
            mono badge · 69d2574e
          </Badge>
        </View>
      </Card>

      <SectionLabel>Inline alerts</SectionLabel>
      <Card>
        <InlineAlert tone="info" title="Informational">
          A neutral note about the current state.
        </InlineAlert>
        <InlineAlert tone="success" title="Connected">
          Streaming audio to the backend.
        </InlineAlert>
        <InlineAlert tone="warning" title="Reconnecting">
          The connection dropped and is being retried.
        </InlineAlert>
        <InlineAlert
          tone="danger"
          title="Backend unreachable"
          action={<Button variant="outline" size="sm">Retry</Button>}
        >
          Check that Tailscale is connected.
        </InlineAlert>
      </Card>

      <SectionLabel>Cards &amp; wells</SectionLabel>
      <Card title="Card with a title" headerRight={<Badge tone="success">healthy</Badge>}>
        <CardWell>
          <Caption>CardWell — a sunken tile for a status readout</Caption>
        </CardWell>
        <Divider style={s.spaced} />
        <Caption>Divider above separates rows inside a card.</Caption>
      </Card>

      <SectionLabel>Form fields</SectionLabel>
      <Card>
        <TextField label="Backend URL" value="wss://kraken.parrot-census.ts.net/ws" hint="From the QR on the dashboard." />
        <TextField label="Email" placeholder="user@example.com" />
        <TextField label="Password" value="hunter2" secureTextEntry error="Incorrect password." />
        <TextField label="Device name" value="pendant" editable={false} hint="Disabled while streaming." />
      </Card>
    </View>
  );
}

export default function ThemePreviewScreen() {
  return (
    <ScrollView>
      <ThemeProvider forced="dark">
        <ThemeShowcase name="dark" />
      </ThemeProvider>
      <ThemeProvider forced="light">
        <ThemeShowcase name="light" />
      </ThemeProvider>
    </ScrollView>
  );
}

const createStyles = (t: Theme) =>
  StyleSheet.create({
    page: {
      backgroundColor: t.color.surface.page,
      padding: t.space[4],
    },
    pageTitle: {
      ...t.type['2xl'],
      marginBottom: t.space[4],
      color: t.color.text.primary,
    },
    sample: {
      fontFamily: t.font.sans,
      ...t.type.sm,
      marginBottom: t.space[1],
    },
    typeSample: {
      fontFamily: t.font.sans,
      marginTop: t.space[2],
    },
    swatchRow: {
      flexDirection: 'row',
      flexWrap: 'wrap',
      gap: t.space[3],
    },
    swatch: {
      alignItems: 'center',
      width: 84,
    },
    swatchChip: {
      width: 56,
      height: 40,
      borderRadius: t.radius.md,
      borderWidth: t.borderWidth,
      borderColor: t.color.text.faint,
      marginBottom: t.space[1],
    },
    swatchName: {
      textAlign: 'center',
    },
    wrapRow: {
      flexDirection: 'row',
      flexWrap: 'wrap',
      alignItems: 'center',
      gap: t.space[2],
    },
    dotPair: {
      flexDirection: 'row',
      alignItems: 'center',
      gap: t.space[1.5],
    },
    spaced: {
      marginTop: t.space[3],
    },
  });
