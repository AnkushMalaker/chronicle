// The iOS share extension hands off by opening a URL that is a signal, not a
// path: `chronicle:///dataUrl=chronicleShareKey?nonce=…`. The shared image never
// travels in that URL — it sits in the app-group container, and `useShareIntent`
// reads it back out of the native module.
//
// Expo Router still tries to match the URL against a file route, finds nothing,
// and renders Unmatched Route. This hook rewrites it to the confirm screen.

import { getShareExtensionKey } from 'expo-share-intent';

export function redirectSystemPath({ path }: { path: string; initial: boolean }): string {
  try {
    // Key is derived from the app scheme, so it stays correct if the scheme moves.
    if (path.includes(`dataUrl=${getShareExtensionKey()}`)) {
      return '/share';
    }
    return path;
  } catch {
    // Throwing here takes down link handling for every deep link, not just this
    // one, so an unreadable path degrades to the home route.
    return '/';
  }
}
