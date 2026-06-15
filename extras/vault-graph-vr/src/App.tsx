import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Canvas, useFrame, useThree } from "@react-three/fiber";
import {
  createXRStore,
  XR,
  XROrigin,
  useXRInputSourceState,
  useXR,
  XRSpace,
} from "@react-three/xr";
import * as THREE from "three";
import SpriteText from "three-spritetext";
import R3fForceGraph from "r3f-forcegraph";

import type { GraphData, GraphNode } from "./types";
import { loadGraph, nodeColor, nodeVal } from "./graph";
import {
  DEFAULT_TRANSITION,
  DEADZONE,
  GRAPH_POSITION,
  GRAPH_SCALE,
  MOVE_SPEED,
  TURN_SPEED,
  type TransitionConfig,
} from "./config";

const store = createXRStore();

/** Shared mutable focus/transition state, read inside the frame loop. */
interface FocusState {
  node: GraphNode | null;
  active: boolean; // a fly-to is in progress
  elapsed: number;
}

// ---------------------------------------------------------------------------
// Graph rendering
// ---------------------------------------------------------------------------
function Graph({
  data,
  fgRef,
}: {
  data: GraphData;
  fgRef: React.MutableRefObject<any>;
}) {
  useFrame(() => fgRef.current?.tickFrame());

  const makeNode = useCallback((node: GraphNode) => {
    const sprite = new SpriteText(node.name);
    sprite.color = nodeColor(node);
    sprite.textHeight = node.type === "note" ? 5 : 4;
    sprite.fontFace = "Inter, system-ui, sans-serif";
    sprite.backgroundColor = "rgba(11,14,20,0.55)";
    sprite.padding = 1;
    sprite.borderRadius = 2;
    sprite.material.depthWrite = false;
    // Sprites crash the XR pointer raycaster (THREE.Sprite.raycast needs
    // raycaster.camera, which @pmndrs/pointer-events doesn't set) — that
    // exception breaks ALL controller input. Labels are decorative; make them
    // non-raycastable so the node spheres stay the interaction targets.
    sprite.raycast = () => {};
    return sprite;
  }, []);

  return (
    <R3fForceGraph
      ref={fgRef}
      graphData={data}
      nodeId="id"
      nodeVal={nodeVal}
      nodeColor={nodeColor}
      nodeOpacity={0.95}
      nodeResolution={10}
      nodeThreeObject={makeNode}
      nodeThreeObjectExtend={true}
      linkColor={() => "#34425c"}
      linkWidth={0.4}
      linkOpacity={0.5}
    />
  );
}

// ---------------------------------------------------------------------------
// XR origin: free flight + smooth/snap fly-to-focus
// ---------------------------------------------------------------------------
function Rig({
  focus,
  config,
  graphGroupRef,
  aimRef,
}: {
  focus: React.MutableRefObject<FocusState>;
  config: TransitionConfig;
  graphGroupRef: React.MutableRefObject<THREE.Group | null>;
  aimRef: React.MutableRefObject<THREE.Object3D | null>;
}) {
  const originRef = useRef<THREE.Group>(null);
  const left = useXRInputSourceState("controller", "left");
  const right = useXRInputSourceState("controller", "right");
  const { camera } = useThree();

  // scratch vectors (avoid per-frame allocation)
  const fwd = useMemo(() => new THREE.Vector3(), []);
  const rightV = useMemo(() => new THREE.Vector3(), []);
  const up = useMemo(() => new THREE.Vector3(0, 1, 0), []);
  const worldPos = useMemo(() => new THREE.Vector3(), []);
  const camPos = useMemo(() => new THREE.Vector3(), []);
  const desired = useMemo(() => new THREE.Vector3(), []);
  const delta = useMemo(() => new THREE.Vector3(), []);

  useFrame((_, dt) => {
    const o = originRef.current;
    if (!o) return;
    const d = Math.min(dt, 0.05); // clamp huge frame gaps

    // head-relative horizontal basis
    camera.getWorldDirection(fwd);
    fwd.y = 0;
    if (fwd.lengthSq() < 1e-6) fwd.set(0, 0, -1);
    fwd.normalize();
    rightV.crossVectors(fwd, up).normalize();

    // --- manual flight ---
    const ls = left?.gamepad?.["xr-standard-thumbstick"];
    const rs = right?.gamepad?.["xr-standard-thumbstick"];
    let moved = false;

    if (ls) {
      const f = -(ls.yAxis ?? 0); // push up = forward
      const s = ls.xAxis ?? 0;
      if (Math.hypot(f, s) > DEADZONE) {
        o.position.addScaledVector(fwd, f * MOVE_SPEED * d);
        o.position.addScaledVector(rightV, s * MOVE_SPEED * d);
        moved = true;
      }
    }
    if (rs) {
      const climb = -(rs.yAxis ?? 0); // push up = ascend
      const turn = rs.xAxis ?? 0;
      if (Math.abs(climb) > DEADZONE) {
        o.position.y += climb * MOVE_SPEED * d;
        moved = true;
      }
      if (Math.abs(turn) > DEADZONE) {
        o.rotation.y -= turn * TURN_SPEED * d;
        moved = true;
      }
    }
    if (moved) focus.current.active = false; // any input cancels the glide

    // --- fly-to-focus ---
    const fs = focus.current;
    const group = graphGroupRef.current;
    if (fs.active && fs.node && group && fs.node.x != null) {
      worldPos.set(fs.node.x, fs.node.y ?? 0, fs.node.z ?? 0);
      group.localToWorld(worldPos);
      camera.getWorldPosition(camPos);

      // place camera viewDistance behind the node along current view dir
      desired.copy(worldPos).addScaledVector(fwd, -config.viewDistance);
      delta.copy(desired).sub(camPos);

      const alpha =
        config.mode === "snap"
          ? 1
          : 1 - Math.exp(-d / Math.max(0.05, config.durationSec / 3));
      o.position.addScaledVector(delta, alpha);

      fs.elapsed += d;
      if (
        config.mode === "snap" ||
        delta.length() < 0.03 ||
        fs.elapsed > config.durationSec * 3
      ) {
        fs.active = false;
      }
    }
  });

  // AimAnchor lives INSIDE the origin so the controller's target-ray pose is in
  // the same moved frame as you — otherwise the ray casts from world origin
  // after you fly.
  return (
    <XROrigin ref={originRef}>
      <AimAnchor hand="right" anchorRef={aimRef} />
    </XROrigin>
  );
}

// ---------------------------------------------------------------------------
// Reader panel — anchored in front of the viewer (so distant notes are still
// readable). Shows the note body plus clickable "link chips" for the notes it
// links to (the wikilinks = graph edges). Clicking a chip smoothly flies you to
// that note and the reader follows it.
// ---------------------------------------------------------------------------
const MAX_CHIPS = 6;

function linkEndId(end: string | GraphNode): string {
  return typeof end === "object" ? end.id : end;
}

function getNeighbors(node: GraphNode, graph: GraphData): GraphNode[] {
  const byId = new Map(graph.nodes.map((n) => [n.id, n]));
  const out: GraphNode[] = [];
  const seen = new Set<string>();
  for (const l of graph.links) {
    const s = linkEndId(l.source);
    const t = linkEndId(l.target);
    let otherId: string | null = null;
    if (s === node.id) otherId = t;
    else if (t === node.id) otherId = s;
    if (otherId && !seen.has(otherId)) {
      seen.add(otherId);
      const n = byId.get(otherId);
      if (n) out.push(n);
    }
  }
  return out;
}

function makeChipTexture(label: string, color: string): THREE.CanvasTexture {
  const W = 512;
  const H = 64;
  const canvas = document.createElement("canvas");
  canvas.width = W;
  canvas.height = H;
  const ctx = canvas.getContext("2d")!;
  ctx.fillStyle = "rgba(20,28,44,0.96)";
  roundRect(ctx, 0, 0, W, H, 14);
  ctx.fill();
  ctx.strokeStyle = color;
  ctx.lineWidth = 3;
  roundRect(ctx, 1.5, 1.5, W - 3, H - 3, 14);
  ctx.stroke();
  ctx.fillStyle = color;
  ctx.beginPath();
  ctx.arc(34, H / 2, 11, 0, Math.PI * 2);
  ctx.fill();
  ctx.fillStyle = "#e8eef7";
  ctx.font = "27px Inter, system-ui, sans-serif";
  ctx.textBaseline = "middle";
  ctx.fillText(truncate(ctx, label, W - 90), 58, H / 2 + 2);
  const tex = new THREE.CanvasTexture(canvas);
  tex.anisotropy = 4;
  tex.needsUpdate = true;
  return tex;
}

function Chip({
  label,
  color,
  y,
  onClick,
}: {
  label: string;
  color: string;
  y: number;
  onClick: () => void;
}) {
  const tex = useMemo(() => makeChipTexture(label, color), [label, color]);
  return (
    <mesh
      position={[0, y, 0.001]}
      onClick={(e) => {
        e.stopPropagation();
        onClick();
      }}
    >
      <planeGeometry args={[1.0, 0.12]} />
      <meshBasicMaterial map={tex} transparent toneMapped={false} />
    </mesh>
  );
}

function ReaderPanel({
  node,
  graph,
  graphGroupRef,
  onNavigate,
  onClose,
}: {
  node: GraphNode;
  graph: GraphData;
  graphGroupRef: React.MutableRefObject<THREE.Group | null>;
  onNavigate: (target: GraphNode) => void;
  onClose: () => void;
}) {
  const groupRef = useRef<THREE.Group>(null);
  const { camera } = useThree();

  const texture = useMemo(() => makeTextTexture(node), [node]);
  const neighbors = useMemo(() => getNeighbors(node, graph), [node, graph]);
  const worldPos = useMemo(() => new THREE.Vector3(), []);
  const toCam = useMemo(() => new THREE.Vector3(), []);
  const camPos = useMemo(() => new THREE.Vector3(), []);
  const camQuat = useMemo(() => new THREE.Quaternion(), []);

  useFrame(() => {
    const g = groupRef.current;
    const grp = graphGroupRef.current;
    if (!g || !grp || node.x == null) return;
    // Use the camera's WORLD transform (in XR the camera is nested, so its
    // local transform points at origin). Anchor near the node, pushed toward
    // the viewer, and billboard to always face you.
    camera.getWorldPosition(camPos);
    camera.getWorldQuaternion(camQuat);
    worldPos.set(node.x, node.y ?? 0, node.z ?? 0);
    grp.localToWorld(worldPos);
    toCam.copy(camPos).sub(worldPos);
    const dist = toCam.length();
    if (dist > 0.001) {
      toCam.divideScalar(dist);
      const radius = Math.cbrt(nodeVal(node)) * 4 * GRAPH_SCALE + 0.3;
      worldPos.addScaledVector(toCam, Math.min(radius, dist * 0.6));
    }
    g.position.copy(worldPos);
    g.quaternion.copy(camQuat);
  });

  const shown = neighbors.slice(0, MAX_CHIPS);
  const chipTop = -0.34;
  const chipStep = -0.135;

  return (
    <group ref={groupRef} scale={0.6}>
      {/* body */}
      <mesh position={[0, 0.18, 0]}>
        <planeGeometry args={[1.0, 0.75]} />
        <meshBasicMaterial map={texture} transparent toneMapped={false} />
      </mesh>
      {/* close button */}
      <mesh
        position={[0.46, 0.58, 0.001]}
        onClick={(e) => {
          e.stopPropagation();
          onClose();
        }}
      >
        <planeGeometry args={[0.1, 0.1]} />
        <meshBasicMaterial color="#3a2630" transparent opacity={0.9} toneMapped={false} />
      </mesh>
      {/* link chips */}
      {shown.length > 0 && (
        <Chip label="Links ↓" color="#5a6680" y={chipTop - chipStep} onClick={() => {}} />
      )}
      {shown.map((n, i) => (
        <Chip
          key={n.id}
          label={`→ ${n.name}`}
          color={nodeColor(n)}
          y={chipTop + i * chipStep}
          onClick={() => onNavigate(n)}
        />
      ))}
    </group>
  );
}

function makeTextTexture(node: GraphNode): THREE.CanvasTexture {
  const W = 640;
  const H = 480;
  const canvas = document.createElement("canvas");
  canvas.width = W;
  canvas.height = H;
  const ctx = canvas.getContext("2d")!;
  ctx.fillStyle = "rgba(13,18,28,0.92)";
  roundRect(ctx, 0, 0, W, H, 18);
  ctx.fill();
  ctx.strokeStyle = nodeColor(node);
  ctx.lineWidth = 4;
  roundRect(ctx, 2, 2, W - 4, H - 4, 18);
  ctx.stroke();

  const pad = 28;
  ctx.textBaseline = "top";
  ctx.fillStyle = "#e8eef7";
  ctx.font = "bold 34px Inter, system-ui, sans-serif";
  ctx.fillText(truncate(ctx, node.name, W - pad * 2), pad, pad);

  ctx.fillStyle = "#7d8aa0";
  ctx.font = "18px Inter, system-ui, sans-serif";
  ctx.fillText(truncate(ctx, node.path || node.type, W - pad * 2), pad, pad + 44);

  ctx.fillStyle = "#c4cee0";
  ctx.font = "20px Inter, system-ui, sans-serif";
  const body = (node.content || "(no content)").replace(/\r/g, "");
  wrapText(ctx, body, pad, pad + 86, W - pad * 2, 27, H - pad);

  const tex = new THREE.CanvasTexture(canvas);
  tex.anisotropy = 4;
  tex.needsUpdate = true;
  return tex;
}

function roundRect(
  ctx: CanvasRenderingContext2D,
  x: number,
  y: number,
  w: number,
  h: number,
  r: number
) {
  ctx.beginPath();
  ctx.moveTo(x + r, y);
  ctx.arcTo(x + w, y, x + w, y + h, r);
  ctx.arcTo(x + w, y + h, x, y + h, r);
  ctx.arcTo(x, y + h, x, y, r);
  ctx.arcTo(x, y, x + w, y, r);
  ctx.closePath();
}

function truncate(ctx: CanvasRenderingContext2D, text: string, maxW: number): string {
  if (ctx.measureText(text).width <= maxW) return text;
  let t = text;
  while (t.length > 1 && ctx.measureText(t + "…").width > maxW) t = t.slice(0, -1);
  return t + "…";
}

function wrapText(
  ctx: CanvasRenderingContext2D,
  text: string,
  x: number,
  y: number,
  maxW: number,
  lineH: number,
  maxY: number
) {
  let cy = y;
  for (const rawLine of text.split("\n")) {
    const words = rawLine.split(/\s+/);
    let line = "";
    for (const word of words) {
      const test = line ? line + " " + word : word;
      if (ctx.measureText(test).width > maxW && line) {
        ctx.fillText(line, x, cy);
        cy += lineH;
        line = word;
        if (cy > maxY - lineH) {
          ctx.fillText("…", x, cy);
          return;
        }
      } else {
        line = test;
      }
    }
    if (line) {
      ctx.fillText(line, x, cy);
      cy += lineH;
    }
    if (cy > maxY - lineH) return;
  }
}

// ---------------------------------------------------------------------------
// Debug HUD — a small panel locked in front of the camera showing which XR
// inputs the session actually sees (controllers vs hands) and live thumbstick
// values. Temporary, for diagnosing "nothing responds inside VR".
// ---------------------------------------------------------------------------
function DebugHUD() {
  const { camera } = useThree();
  const lc = useXRInputSourceState("controller", "left");
  const rc = useXRInputSourceState("controller", "right");
  const lh = useXRInputSourceState("hand", "left");
  const rh = useXRInputSourceState("hand", "right");
  const mode = useXR((s) => s.mode);
  const session = useXR((s) => s.session);
  const inputStates = useXR((s) => s.inputSourceStates);

  const groupRef = useRef<THREE.Group>(null);
  const sprite = useMemo(() => {
    const s = new SpriteText("…");
    s.textHeight = 0.035;
    s.color = "#8affc0";
    s.backgroundColor = "rgba(0,0,0,0.75)";
    s.padding = 0.012;
    s.fontFace = "monospace";
    s.raycast = () => {}; // never a pointer target (see makeNode note)
    return s;
  }, []);

  const fwd = useMemo(() => new THREE.Vector3(), []);
  const down = useMemo(() => new THREE.Vector3(), []);

  useFrame(() => {
    const g = groupRef.current;
    if (!g) return;
    fwd.set(0, 0, -1).applyQuaternion(camera.quaternion);
    down.set(0, -1, 0).applyQuaternion(camera.quaternion);
    g.position.copy(camera.position).addScaledVector(fwd, 1.0).addScaledVector(down, 0.32);
    g.quaternion.copy(camera.quaternion);

    const ls = lc?.gamepad?.["xr-standard-thumbstick"];
    const rs = rc?.gamepad?.["xr-standard-thumbstick"];
    const f = (n?: number) => (n ?? 0).toFixed(2);
    const blend = session?.environmentBlendMode ?? "-";
    const types = inputStates.map((s) => (s as { type?: string }).type ?? "?").join(",") || "none";
    sprite.text =
      `mode:${mode ?? "-"}  blend:${blend}\n` +
      `inputs:${inputStates.length} [${types}]\n` +
      `Lctrl:${lc ? "YES" : "no"}  stick(${f(ls?.xAxis)},${f(ls?.yAxis)})\n` +
      `Rctrl:${rc ? "YES" : "no"}  stick(${f(rs?.xAxis)},${f(rs?.yAxis)})  A:${rc?.gamepad?.["a-button"]?.state ?? "-"}\n` +
      `Lhand:${lh ? "YES" : "no"}   Rhand:${rh ? "YES" : "no"}`;
  });

  return (
    <group ref={groupRef}>
      <primitive object={sprite} />
    </group>
  );
}

// ---------------------------------------------------------------------------
// Backdrop: an opaque environment sphere + faint starfield. Without this, an
// immersive session can leave the periphery transparent (you see passthrough /
// "edges" where the scene background doesn't reach). The inward-facing sphere
// guarantees solid space in every direction; the stars give depth/parallax so
// flying feels like moving through space rather than staring at a panel.
// ---------------------------------------------------------------------------
function Backdrop({ visible }: { visible: boolean }) {
  const stars = useMemo(() => {
    const N = 1500;
    const pos = new Float32Array(N * 3);
    for (let i = 0; i < N; i++) {
      // random direction on a sphere, random radius in a big shell
      const theta = Math.random() * Math.PI * 2;
      const phi = Math.acos(2 * Math.random() - 1);
      const r = 30 + Math.random() * 320;
      pos[i * 3] = r * Math.sin(phi) * Math.cos(theta);
      pos[i * 3 + 1] = r * Math.sin(phi) * Math.sin(theta);
      pos[i * 3 + 2] = r * Math.cos(phi);
    }
    const g = new THREE.BufferGeometry();
    g.setAttribute("position", new THREE.BufferAttribute(pos, 3));
    return g;
  }, []);

  const noRaycast = () => {};
  // Drawn first (renderOrder -1000) with depth test/write off so it fills the
  // whole framebuffer with opaque color — this reliably occludes passthrough in
  // an alpha-blend AR session, giving a solid "void". Hide it to reveal
  // passthrough (graph in your room).
  return (
    <group visible={visible}>
      <mesh raycast={noRaycast} renderOrder={-1000}>
        <sphereGeometry args={[400, 32, 16]} />
        <meshBasicMaterial
          color="#0b0e14"
          side={THREE.BackSide}
          fog={false}
          depthTest={false}
          depthWrite={false}
        />
      </mesh>
      <points geometry={stars} raycast={noRaycast}>
        <pointsMaterial
          size={0.7}
          sizeAttenuation
          color="#9fb0cc"
          transparent
          opacity={0.7}
          depthWrite={false}
        />
      </points>
    </group>
  );
}

// ---------------------------------------------------------------------------
// 360° equirectangular capture — so the view can be inspected outside the
// headset. Renders the scene into a cube map from the head position, unwraps it
// to a 2:1 equirect image, and POSTs it to the dev server (/__capture), which
// writes it to ./captures. Trigger: right "A" button in VR, or the desktop
// "Capture 360°" button.
// ---------------------------------------------------------------------------
const EQUIRECT_VERT = `
  varying vec2 vUv;
  void main() { vUv = uv; gl_Position = vec4(position.xy, 0.0, 1.0); }
`;
const EQUIRECT_FRAG = `
  precision highp float;
  varying vec2 vUv;
  uniform samplerCube map;
  #define PI 3.141592653589793
  void main() {
    float lon = (vUv.x - 0.5) * 2.0 * PI;
    float lat = (vUv.y - 0.5) * PI;
    vec3 dir = vec3(cos(lat) * sin(lon), sin(lat), cos(lat) * cos(lon));
    gl_FragColor = textureCube(map, dir);
  }
`;

function capture360(gl: THREE.WebGLRenderer, scene: THREE.Scene, camera: THREE.Camera) {
  const cubeRT = new THREE.WebGLCubeRenderTarget(1024);
  const cubeCam = new THREE.CubeCamera(0.05, 1000, cubeRT);
  camera.getWorldPosition(cubeCam.position);

  const W = 2048;
  const H = 1024;
  const outRT = new THREE.WebGLRenderTarget(W, H);
  const eqScene = new THREE.Scene();
  const eqCam = new THREE.Camera();
  const mat = new THREE.ShaderMaterial({
    uniforms: { map: { value: cubeRT.texture } },
    vertexShader: EQUIRECT_VERT,
    fragmentShader: EQUIRECT_FRAG,
  });
  const quad = new THREE.Mesh(new THREE.PlaneGeometry(2, 2), mat);
  eqScene.add(quad);

  const prevXrEnabled = gl.xr.enabled;
  const prevTarget = gl.getRenderTarget();
  try {
    gl.xr.enabled = false; // render off-screen without the XR framebuffer
    cubeCam.update(gl, scene);
    gl.setRenderTarget(outRT);
    gl.render(eqScene, eqCam);

    const pixels = new Uint8Array(W * H * 4);
    gl.readRenderTargetPixels(outRT, 0, 0, W, H, pixels);

    const canvas = document.createElement("canvas");
    canvas.width = W;
    canvas.height = H;
    const ctx = canvas.getContext("2d")!;
    const img = ctx.createImageData(W, H);
    for (let y = 0; y < H; y++) {
      const src = (H - 1 - y) * W * 4; // GL framebuffer is bottom-up
      img.data.set(pixels.subarray(src, src + W * 4), y * W * 4);
    }
    ctx.putImageData(img, 0, 0);
    const dataUrl = canvas.toDataURL("image/png");
    fetch("/__capture", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ dataUrl }),
    }).catch(() => {});
  } finally {
    gl.setRenderTarget(prevTarget);
    gl.xr.enabled = prevXrEnabled;
    cubeRT.dispose();
    outRT.dispose();
    mat.dispose();
    quad.geometry.dispose();
  }
}

function ControllerButtons({
  captureRequest,
  onTogglePassthrough,
  onToggleDebug,
}: {
  captureRequest: React.MutableRefObject<boolean>;
  onTogglePassthrough: () => void;
  onToggleDebug: () => void;
}) {
  const { gl, scene, camera } = useThree();
  const right = useXRInputSourceState("controller", "right");
  const left = useXRInputSourceState("controller", "left");
  const prevA = useRef(false);
  const prevStick = useRef(false);
  const prevX = useRef(false);
  const prevY = useRef(false);

  useFrame(() => {
    const a = right?.gamepad?.["a-button"]?.state === "pressed";
    // 360 capture moved to left-thumbstick click (B is now node grab).
    const stickClick = left?.gamepad?.["xr-standard-thumbstick"]?.state === "pressed";
    const x = left?.gamepad?.["x-button"]?.state === "pressed";
    const y = left?.gamepad?.["y-button"]?.state === "pressed";
    if (a && !prevA.current) onTogglePassthrough(); // A = passthrough / void
    if (stickClick && !prevStick.current) captureRequest.current = true;
    if (x && !prevX.current) store.getState().session?.end?.(); // X = exit XR
    if (y && !prevY.current) onToggleDebug(); // Y = toggle debug overlays
    prevA.current = a;
    prevStick.current = stickClick;
    prevX.current = x;
    prevY.current = y;

    if (captureRequest.current) {
      captureRequest.current = false;
      capture360(gl, scene as THREE.Scene, camera);
    }
  });
  return null;
}

// ---------------------------------------------------------------------------
// Node moving (Obsidian-style) — hold B + point at a node to drag it; the force
// sim reheats so neighbours react. Double-tap B to anchor/unanchor a node.
// Haptics ramp with edge stretch and hard-stop against anchored neighbours.
// ---------------------------------------------------------------------------
const LINK_REST = 30; // sim units (matches d3 link default) — the "natural" edge length
const MAX_STRETCH = 1.6; // hard stop against anchored neighbours at this × current rest
const LINK_ADJUST_RATE = 50; // sim units/sec the L/R triggers grow/shrink edge rest length
const LINK_MIN = 8;
const LINK_MAX = 140;
const HOLD_MS = 170; // press longer than this = grab; shorter = tap
const DOUBLE_TAP_MS = 350;

function hapticPulse(state: any, intensity: number, ms: number) {
  try {
    const acts = state?.inputSource?.gamepad?.hapticActuators;
    acts?.[0]?.pulse?.(Math.max(0, Math.min(1, intensity)), ms);
  } catch {
    /* not all controllers support haptics */
  }
}

function NodeInteraction({
  fgRef,
  graphGroupRef,
  graph,
  anchoredRef,
  onAnchorChange,
  onOpen,
  aimRef,
  debug,
}: {
  fgRef: React.MutableRefObject<any>;
  graphGroupRef: React.MutableRefObject<THREE.Group | null>;
  graph: GraphData;
  anchoredRef: React.MutableRefObject<Set<string>>;
  onAnchorChange: () => void;
  onOpen: (node: GraphNode) => void;
  aimRef: React.MutableRefObject<THREE.Object3D | null>;
  debug: boolean;
}) {
  const right = useXRInputSourceState("controller", "right");
  const left = useXRInputSourceState("controller", "left");
  const { camera } = useThree();

  const hovered = useRef<GraphNode | null>(null);
  const trigDown = useRef(false);
  const linkDist = useRef(LINK_REST); // current edge rest length (triggers adjust it)

  const bDown = useRef(false);
  const bDownAt = useRef(0);
  const pressNode = useRef<GraphNode | null>(null); // node aimed at when B was pressed
  const grabbing = useRef<GraphNode | null>(null);
  const grabNeighbors = useRef<GraphNode[]>([]);
  const offsetVec = useRef(new THREE.Vector3()); // node pos in controller space
  const lastTapAt = useRef(0);
  const lastTapId = useRef<string | null>(null);

  const failLogged = useRef(false);
  const dbg = (m: string) => {
    if (debug) console.warn("[grab] " + m);
  };

  const raycaster = useMemo(() => new THREE.Raycaster(), []);
  const origin = useMemo(() => new THREE.Vector3(), []);
  const rayDir = useMemo(() => new THREE.Vector3(), []);
  const nodeWorld = useMemo(() => new THREE.Vector3(), []);
  const target = useMemo(() => new THREE.Vector3(), []);
  const nbPos = useMemo(() => new THREE.Vector3(), []);
  const dir = useMemo(() => new THREE.Vector3(), []);
  const hlPos = useMemo(() => new THREE.Vector3(), []);
  const FWD = useMemo(() => new THREE.Vector3(0, 0, -1), []);

  // debug/feedback visuals
  const rayRef = useRef<THREE.Group>(null);
  const hlRef = useRef<THREE.Mesh>(null);

  const toggleAnchor = (node: GraphNode) => {
    const set = anchoredRef.current;
    if (set.has(node.id)) {
      set.delete(node.id);
      node.fx = node.fy = node.fz = undefined;
    } else {
      set.add(node.id);
      node.fx = node.x;
      node.fy = node.y;
      node.fz = node.z;
    }
    onAnchorChange();
    fgRef.current?.d3ReheatSimulation?.();
    hapticPulse(right, 0.7, 90);
  };

  useFrame((_, delta) => {
    const aim = aimRef.current;
    const pressed = right?.gamepad?.["b-button"]?.state === "pressed";
    const now = performance.now();
    const g = graphGroupRef.current;

    // --- hover: raycast from the controller's aim ray against the graph ---
    if (aim && g) {
      aim.updateWorldMatrix(true, false);
      origin.setFromMatrixPosition(aim.matrixWorld);
      rayDir.set(0, 0, -1).transformDirection(aim.matrixWorld);

      // keep the debug ray glued to the actual ray we cast
      const rg = rayRef.current;
      if (rg) {
        rg.position.copy(origin);
        rg.quaternion.setFromUnitVectors(FWD, rayDir);
      }

      if (!grabbing.current) {
        g.updateMatrixWorld(true); // node positions are updated each tick — refresh
        raycaster.set(origin, rayDir);
        raycaster.camera = camera;
        const hits = raycaster.intersectObjects(g.children, true);
        let found: GraphNode | null = null;
        for (const h of hits) {
          let o: THREE.Object3D | null = h.object;
          while (o) {
            const oo = o as unknown as { __graphObjType?: string; __data?: GraphNode };
            if (oo.__graphObjType === "node" && oo.__data) {
              found = oo.__data;
              break;
            }
            o = o.parent;
          }
          if (found) break;
        }
        hovered.current = found;
      }

      // highlight the hovered node so selection is visible (debug only)
      const hm = hlRef.current;
      const hn = hovered.current;
      if (hm && debug) {
        if (hn && hn.x != null) {
          hlPos.set(hn.x, hn.y ?? 0, hn.z ?? 0);
          g.localToWorld(hlPos);
          hm.position.copy(hlPos);
          hm.scale.setScalar(Math.cbrt(nodeVal(hn)) * 4 * GRAPH_SCALE + 0.04);
          hm.visible = true;
        } else {
          hm.visible = false;
        }
      }
    }

    // --- trigger: open the hovered node (only when not dragging) ---
    const trig = right?.gamepad?.["xr-standard-trigger"]?.state === "pressed";
    if (trig && !trigDown.current && hovered.current && !grabbing.current) {
      onOpen(hovered.current);
    }
    trigDown.current = trig;

    // edge: press — capture the node we're aiming at now (forgiving grab)
    if (pressed && !bDown.current) {
      bDown.current = true;
      bDownAt.current = now;
      pressNode.current = hovered.current;
      failLogged.current = false;
      dbg(`B down: hovered=${hovered.current?.id ?? "none"} aim=${!!aim} g=${!!g}`);
    }

    // diagnose why a held B might not start a grab
    if (
      pressed &&
      bDown.current &&
      !grabbing.current &&
      now - bDownAt.current > HOLD_MS &&
      !failLogged.current &&
      !(pressNode.current && aim && g)
    ) {
      failLogged.current = true;
      dbg(`no grab: pressNode=${pressNode.current?.id ?? "null"} aim=${!!aim} g=${!!g}`);
    }

    // start grab once held past threshold (uses the node captured at press)
    if (
      pressed &&
      bDown.current &&
      !grabbing.current &&
      now - bDownAt.current > HOLD_MS &&
      pressNode.current &&
      aim &&
      g
    ) {
      const node = pressNode.current;
      dbg(`GRAB START ${node.id}`);
      grabbing.current = node;
      // keep the sim gently warm during the drag (standard d3 drag pattern) —
      // reheating to full alpha every frame is what caused the jitter.
      fgRef.current?.d3AlphaTarget?.(0.3);
      grabNeighbors.current = getNeighbors(node, graph);
      nodeWorld.set(node.x ?? 0, node.y ?? 0, node.z ?? 0);
      g.localToWorld(nodeWorld);
      aim.updateWorldMatrix(true, false);
      offsetVec.current.copy(nodeWorld).applyMatrix4(aim.matrixWorld.clone().invert());
      node.fx = node.x;
      node.fy = node.y;
      node.fz = node.z;
      hapticPulse(right, 0.4, 40);
    }

    // drag
    if (grabbing.current && aim && g) {
      const node = grabbing.current;

      // stretchier edges: left trigger lengthens the link rest length (graph
      // breathes outward), right trigger shortens it (pulls tight). Applied
      // globally to the d3 link force; persists after release.
      const lTrig = left?.gamepad?.["xr-standard-trigger"]?.button ?? 0;
      const rTrig = right?.gamepad?.["xr-standard-trigger"]?.button ?? 0;
      const adj = (lTrig - rTrig) * LINK_ADJUST_RATE * delta;
      if (adj !== 0) {
        linkDist.current = Math.max(LINK_MIN, Math.min(LINK_MAX, linkDist.current + adj));
        fgRef.current?.d3Force?.("link")?.distance?.(linkDist.current);
      }
      const rest = linkDist.current;

      aim.updateWorldMatrix(true, false);
      target.copy(offsetVec.current).applyMatrix4(aim.matrixWorld);
      g.worldToLocal(target); // sim-local target

      // tension: ratio across neighbours; hard-stop against anchored ones
      let maxRatio = 0;
      for (const nb of grabNeighbors.current) {
        if (nb.x == null) continue;
        nbPos.set(nb.x, nb.y ?? 0, nb.z ?? 0);
        const d = target.distanceTo(nbPos);
        maxRatio = Math.max(maxRatio, d / rest);
        if (anchoredRef.current.has(nb.id) && d > rest * MAX_STRETCH) {
          dir.copy(target).sub(nbPos).setLength(rest * MAX_STRETCH);
          target.copy(nbPos).add(dir);
        }
      }
      node.fx = target.x;
      node.fy = target.y;
      node.fz = target.z;
      // no per-frame reheat — alphaTarget keeps the sim running smoothly

      const intensity = Math.min(1, Math.max(0, (maxRatio - 1) / (MAX_STRETCH - 1)));
      if (intensity > 0.03) hapticPulse(right, intensity, 25);
    }

    // edge: release
    if (!pressed && bDown.current) {
      bDown.current = false;
      dbg(`B up: grabbing=${grabbing.current?.id ?? "none"} held=${Math.round(now - bDownAt.current)}ms`);
      if (grabbing.current) {
        const node = grabbing.current;
        if (!anchoredRef.current.has(node.id)) {
          node.fx = node.fy = node.fz = undefined; // let the sim take over
        }
        fgRef.current?.d3AlphaTarget?.(0); // let it cool and settle
        hapticPulse(right, 0.2, 30);
        grabbing.current = null;
      } else if (now - bDownAt.current <= HOLD_MS) {
        // a tap — check for double-tap on the same node → toggle anchor
        const tapped = pressNode.current;
        const id = tapped?.id ?? null;
        if (id && id === lastTapId.current && now - lastTapAt.current < DOUBLE_TAP_MS) {
          toggleAnchor(tapped!);
          lastTapId.current = null;
          lastTapAt.current = 0;
        } else {
          lastTapId.current = id;
          lastTapAt.current = now;
        }
      }
    }
  });

  if (!debug) return null;
  return (
    <>
      {/* debug: the exact ray we raycast with — compare to the grey aim ray */}
      <group ref={rayRef}>
        <mesh position={[0, 0, -3]} raycast={() => {}}>
          <boxGeometry args={[0.006, 0.006, 6]} />
          <meshBasicMaterial color="#39ff88" toneMapped={false} />
        </mesh>
      </group>
      {/* highlight ring around the currently-targeted node */}
      <mesh ref={hlRef} visible={false} raycast={() => {}}>
        <sphereGeometry args={[1, 16, 12]} />
        <meshBasicMaterial color="#ffffff" wireframe transparent opacity={0.55} toneMapped={false} />
      </mesh>
    </>
  );
}

// Anchors an (invisible) object to a controller's target-ray space so others
// can read where it points. Rendered BEFORE NodeInteraction so its pose is
// fresh (updated earlier in the frame) when the raycast reads it.
function AimAnchor({
  hand,
  anchorRef,
}: {
  hand: "left" | "right";
  anchorRef: React.MutableRefObject<THREE.Object3D | null>;
}) {
  const state = useXRInputSourceState("controller", hand);
  const raySpace = state?.inputSource?.targetRaySpace;
  if (!raySpace) return null;
  return (
    <XRSpace space={raySpace}>
      <object3D ref={anchorRef} />
    </XRSpace>
  );
}

function AnchorMarkers({
  graph,
  anchoredRef,
  version,
}: {
  graph: GraphData;
  anchoredRef: React.MutableRefObject<Set<string>>;
  version: number;
}) {
  const nodes = useMemo(
    () => [...anchoredRef.current].map((id) => graph.nodes.find((n) => n.id === id)).filter(Boolean) as GraphNode[],
    [graph, anchoredRef, version]
  );
  const refs = useRef<(THREE.Mesh | null)[]>([]);

  useFrame(() => {
    nodes.forEach((n, i) => {
      const m = refs.current[i];
      if (m && n.x != null) m.position.set(n.x, n.y ?? 0, n.z ?? 0);
    });
  });

  return (
    <>
      {nodes.map((n, i) => (
        <mesh
          key={n.id}
          ref={(el) => (refs.current[i] = el)}
          raycast={() => {}}
        >
          <torusGeometry args={[nodeVal(n) * 2 + 6, 0.8, 8, 24]} />
          <meshBasicMaterial color="#ffd45a" toneMapped={false} />
        </mesh>
      ))}
    </>
  );
}

// ---------------------------------------------------------------------------
// App + desktop overlay
// ---------------------------------------------------------------------------
export function App() {
  const [graph, setGraph] = useState<GraphData | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [config, setConfig] = useState<TransitionConfig>(DEFAULT_TRANSITION);
  const [focusNode, setFocusNode] = useState<GraphNode | null>(null);
  const [passthrough, setPassthrough] = useState(false);
  const [anchorVersion, setAnchorVersion] = useState(0);
  const [debug, setDebug] = useState(true);

  const focus = useRef<FocusState>({ node: null, active: false, elapsed: 0 });
  const graphGroupRef = useRef<THREE.Group>(null);
  const captureRequest = useRef(false);
  const fgRef = useRef<any>(null);
  const anchoredRef = useRef<Set<string>>(new Set());
  const aimRef = useRef<THREE.Object3D | null>(null);

  useEffect(() => {
    loadGraph().then(setGraph).catch((e) => setError(String(e)));
  }, []);

  // fly=false → just open the reader (node click); fly=true → smoothly travel
  // to the node (following a wikilink chip in the reader).
  const handleSelect = useCallback((node: GraphNode, fly = false) => {
    focus.current = { node, active: fly, elapsed: 0 };
    setFocusNode(node);
  }, []);

  return (
    <>
      <Overlay
        config={config}
        setConfig={setConfig}
        focusNode={focusNode}
        nodeCount={graph?.nodes.length ?? 0}
        linkCount={graph?.links.length ?? 0}
        error={error}
        onCapture={() => (captureRequest.current = true)}
        passthrough={passthrough}
        onTogglePassthrough={() => setPassthrough((p) => !p)}
      />
      <Canvas camera={{ position: [0, 1.6, 0], near: 0.05, far: 1000 }}>
        {/* No opaque scene background in passthrough mode, so the AR compositor
            shows your room; the Backdrop provides the void otherwise. */}
        {!passthrough && <color attach="background" args={["#0b0e14"]} />}
        <ambientLight intensity={0.85} />
        <directionalLight position={[6, 12, 6]} intensity={0.5} />
        <Backdrop visible={!passthrough} />
        <XR store={store}>
          <Rig focus={focus} config={config} graphGroupRef={graphGroupRef} aimRef={aimRef} />
          {debug && <DebugHUD />}
          <ControllerButtons
            captureRequest={captureRequest}
            onTogglePassthrough={() => setPassthrough((p) => !p)}
            onToggleDebug={() => setDebug((d) => !d)}
          />
          {graph && (
            <NodeInteraction
              fgRef={fgRef}
              graphGroupRef={graphGroupRef}
              graph={graph}
              anchoredRef={anchoredRef}
              onAnchorChange={() => setAnchorVersion((v) => v + 1)}
              onOpen={(node) => handleSelect(node, false)}
              aimRef={aimRef}
              debug={debug}
            />
          )}
          {/* Trigger (raycast in NodeInteraction) opens a node; B grabs/moves it;
              double-tap B anchors. No whole-graph grab. */}
          <group ref={graphGroupRef} position={GRAPH_POSITION} scale={GRAPH_SCALE}>
            {graph && <Graph data={graph} fgRef={fgRef} />}
            {graph && (
              <AnchorMarkers graph={graph} anchoredRef={anchoredRef} version={anchorVersion} />
            )}
          </group>
          {focusNode && graph && (
            <ReaderPanel
              node={focusNode}
              graph={graph}
              graphGroupRef={graphGroupRef}
              onNavigate={(target) => handleSelect(target, true)}
              onClose={() => setFocusNode(null)}
            />
          )}
        </XR>
      </Canvas>
    </>
  );
}

function Overlay({
  config,
  setConfig,
  focusNode,
  nodeCount,
  linkCount,
  error,
  onCapture,
  passthrough,
  onTogglePassthrough,
}: {
  config: TransitionConfig;
  setConfig: (c: TransitionConfig) => void;
  focusNode: GraphNode | null;
  nodeCount: number;
  linkCount: number;
  error: string | null;
  onCapture: () => void;
  passthrough: boolean;
  onTogglePassthrough: () => void;
}) {
  const [vrError, setVrError] = useState<string | null>(null);
  // Enter as AR so passthrough is available; the opaque Backdrop provides the
  // "void" when passthrough is toggled off (A button).
  const enterXR = async () => {
    try {
      await store.enterAR();
      setVrError(null);
    } catch (e) {
      setVrError(String(e));
    }
  };
  const box: React.CSSProperties = {
    position: "absolute",
    top: 12,
    left: 12,
    zIndex: 10,
    width: 280,
    padding: 14,
    borderRadius: 12,
    background: "rgba(13,18,28,0.85)",
    color: "#dce5f2",
    font: "13px/1.5 Inter, system-ui, sans-serif",
    border: "1px solid #243049",
  };
  const btn: React.CSSProperties = {
    padding: "8px 14px",
    borderRadius: 8,
    border: "1px solid #2f6df0",
    background: "#1b3a8a",
    color: "white",
    cursor: "pointer",
    fontWeight: 600,
  };

  return (
    <div style={box}>
      <div style={{ fontWeight: 700, fontSize: 15, marginBottom: 8 }}>Vault Graph VR</div>
      <div style={{ display: "flex", gap: 6 }}>
        <button style={{ ...btn, flex: 1 }} onClick={enterXR}>
          Enter XR
        </button>
        <button
          style={{ ...btn, background: "#234", borderColor: "#2f3a52" }}
          onClick={onCapture}
          title="Render a 360° equirect snapshot to ./captures (or press B in VR)"
        >
          📷 360°
        </button>
      </div>
      <button
        style={{ ...btn, width: "100%", marginTop: 6, background: passthrough ? "#1b3a8a" : "#16203200", borderColor: passthrough ? "#2f6df0" : "#2f3a52" }}
        onClick={onTogglePassthrough}
        title="Toggle passthrough (graph in your room) vs void (graph in space) — A button in VR"
      >
        {passthrough ? "Passthrough: ON (room)" : "Passthrough: OFF (void)"}
      </button>
      <div style={{ marginTop: 10, color: "#8b97ab" }}>
        {vrError ? (
          <span style={{ color: "#ff8080" }}>VR: {vrError}</span>
        ) : error ? (
          <span style={{ color: "#ff8080" }}>{error}</span>
        ) : (
          `${nodeCount} notes · ${linkCount} links`
        )}
      </div>

      <hr style={{ border: "none", borderTop: "1px solid #243049", margin: "12px 0" }} />

      <div style={{ marginBottom: 6 }}>Transition</div>
      <div style={{ display: "flex", gap: 6, marginBottom: 10 }}>
        {(["smooth", "snap"] as const).map((m) => (
          <button
            key={m}
            onClick={() => setConfig({ ...config, mode: m })}
            style={{
              ...btn,
              flex: 1,
              background: config.mode === m ? "#1b3a8a" : "#16203200",
              borderColor: config.mode === m ? "#2f6df0" : "#2f3a52",
            }}
          >
            {m}
          </button>
        ))}
      </div>

      <label style={{ display: "block", opacity: config.mode === "smooth" ? 1 : 0.4 }}>
        glide {config.durationSec.toFixed(2)}s
        <input
          type="range"
          min={0.2}
          max={2.5}
          step={0.05}
          value={config.durationSec}
          disabled={config.mode !== "smooth"}
          onChange={(e) => setConfig({ ...config, durationSec: +e.target.value })}
          style={{ width: "100%" }}
        />
      </label>

      <label style={{ display: "block", marginTop: 4 }}>
        view distance {config.viewDistance.toFixed(1)} m
        <input
          type="range"
          min={1}
          max={5}
          step={0.1}
          value={config.viewDistance}
          onChange={(e) => setConfig({ ...config, viewDistance: +e.target.value })}
          style={{ width: "100%" }}
        />
      </label>

      {focusNode && (
        <div style={{ marginTop: 10, color: "#8b97ab" }}>
          focused: <span style={{ color: "#dce5f2" }}>{focusNode.name}</span>
        </div>
      )}

      <hr style={{ border: "none", borderTop: "1px solid #243049", margin: "12px 0" }} />
      <div style={{ color: "#7d8aa0", fontSize: 12 }}>
        Trigger a node: open it · Link chip: fly to that note · Hold B on a node: move it ·
        Double-tap B: anchor · L-stick: fly · R-stick: up/turn · A: passthrough · X: exit · Y: debug · L-stick-click: 360.
      </div>
    </div>
  );
}
