export type TransitionMode = "smooth" | "snap";

export interface TransitionConfig {
  mode: TransitionMode;
  /** Seconds for the smooth glide to (mostly) settle. Ignored for "snap". */
  durationSec: number;
  /** How far in front of you the focused node ends up, in metres. */
  viewDistance: number;
}

export const DEFAULT_TRANSITION: TransitionConfig = {
  mode: "smooth",
  durationSec: 0.9,
  viewDistance: 2.4,
};

/** World metres per force-sim unit. Keeps a ~100-unit graph to ~5 m. */
export const GRAPH_SCALE = 0.05;

/** Where the graph's centre floats, in metres (x, y up, z forward is negative). */
export const GRAPH_POSITION: [number, number, number] = [0, 1.4, -3];

/** Free-flight speed (m/s) and smooth-turn speed (rad/s) for the thumbsticks. */
export const MOVE_SPEED = 4;
export const TURN_SPEED = 1.5;

/** Thumbstick deadzone. */
export const DEADZONE = 0.15;
