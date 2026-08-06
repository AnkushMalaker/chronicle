#!/usr/bin/env node
/**
 * Guards the design system's one invariant: colour lives in `src/theme/` only.
 *
 * Every component reads semantic tokens from `useTheme()`, so reskinning the
 * app is an edit to `src/theme/palette.ts`. A single hardcoded colour anywhere
 * else silently opts that component out of the palette — and the next reskin
 * leaves it stranded on the old one. This check makes that a build failure
 * instead of something you discover in a screenshot months later.
 *
 * Run with `npm run check:theme`.
 */

import { readFileSync, readdirSync, statSync } from 'node:fs';
import { join, relative } from 'node:path';
import { fileURLToPath } from 'node:url';

const appRoot = join(fileURLToPath(new URL('.', import.meta.url)), '..');
const SEARCH_DIRS = ['app', 'src'];
/** The theme module is where colour is *supposed* to live. */
const ALLOWED_PREFIXES = [join('src', 'theme')];

/** Hex literals, rgb()/rgba(), and the CSS colour keywords people reach for. */
const COLOUR_PATTERN =
  /#[0-9a-fA-F]{3,8}\b|\brgba?\s*\(|['"](?:white|black|red|green|blue|grey|gray|orange|yellow|purple)['"]/;

function* walk(dir) {
  for (const entry of readdirSync(dir)) {
    if (entry === 'node_modules' || entry.startsWith('.')) continue;
    const full = join(dir, entry);
    if (statSync(full).isDirectory()) {
      yield* walk(full);
    } else if (/\.tsx?$/.test(full)) {
      yield full;
    }
  }
}

const violations = [];

for (const dir of SEARCH_DIRS) {
  for (const file of walk(join(appRoot, dir))) {
    const rel = relative(appRoot, file);
    if (ALLOWED_PREFIXES.some((prefix) => rel.startsWith(prefix))) continue;

    readFileSync(file, 'utf8')
      .split('\n')
      .forEach((line, index) => {
        // Skip comments — prose may legitimately name a colour.
        const code = line.replace(/\/\/.*$/, '').replace(/\/\*.*?\*\//g, '');
        if (COLOUR_PATTERN.test(code)) {
          violations.push(`${rel}:${index + 1}: ${line.trim()}`);
        }
      });
  }
}

if (violations.length > 0) {
  console.error(
    `Found ${violations.length} hardcoded colour${violations.length === 1 ? '' : 's'} outside src/theme/:\n`
  );
  for (const violation of violations) console.error(`  ${violation}`);
  console.error(
    '\nUse a semantic token from useTheme() instead (see src/theme/index.ts).' +
      '\nIf the palette itself needs a new value, add it to src/theme/palette.ts.'
  );
  process.exit(1);
}

console.log('No hardcoded colours outside src/theme/.');
