import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Canvas, useFrame, useThree } from "@react-three/fiber";
import {
  createXRStore,
  XR,
  XROrigin,
  useXRInputSourceState,
} from "@react-three/xr";
import * as THREE from "three";
import SpriteText from "three-spritetext";
import R3fForceGraph from "r3f-forcegraph";

import type { GraphData, GraphLink, GraphNode } from "./types";
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
  onSelect,
}: {
  data: GraphData;
  onSelect: (node: GraphNode) => void;
}) {
  const fg = useRef<any>(null);
  useFrame(() => fg.current?.tickFrame());

  const makeNode = useCallback((node: GraphNode) => {
    const sprite = new SpriteText(node.name);
    sprite.color = nodeColor(node);
    sprite.textHeight = node.type === "note" ? 5 : 4;
    sprite.fontFace = "Inter, system-ui, sans-serif";
    sprite.backgroundColor = "rgba(11,14,20,0.55)";
    sprite.padding = 1;
    sprite.borderRadius = 2;
    sprite.material.depthWrite = false;
    return sprite;
  }, []);

  return (
    <R3fForceGraph
      ref={fg}
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
      onNodeClick={(n: GraphNode) => onSelect(n)}
      onLinkClick={(l: GraphLink) =>
        onSelect((typeof l.target === "object" ? l.target : l.source) as GraphNode)
      }
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
}: {
  focus: React.MutableRefObject<FocusState>;
  config: TransitionConfig;
  graphGroupRef: React.MutableRefObject<THREE.Group | null>;
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

  return <XROrigin ref={originRef} />;
}

// ---------------------------------------------------------------------------
// In-world note panel (billboarded, follows the focused node)
// ---------------------------------------------------------------------------
function NotePanel({
  node,
  graphGroupRef,
}: {
  node: GraphNode;
  graphGroupRef: React.MutableRefObject<THREE.Group | null>;
}) {
  const groupRef = useRef<THREE.Group>(null);
  const { camera } = useThree();

  const texture = useMemo(() => makeTextTexture(node), [node]);
  const worldPos = useMemo(() => new THREE.Vector3(), []);
  const camRight = useMemo(() => new THREE.Vector3(), []);

  useFrame(() => {
    const g = groupRef.current;
    const group = graphGroupRef.current;
    if (!g || !group || node.x == null) return;
    worldPos.set(node.x, node.y ?? 0, node.z ?? 0);
    group.localToWorld(worldPos);
    camRight.set(1, 0, 0).applyQuaternion(camera.quaternion);
    g.position.copy(worldPos).addScaledVector(camRight, 0.9);
    g.position.y += 0.15;
    g.quaternion.copy(camera.quaternion); // billboard
  });

  return (
    <group ref={groupRef}>
      <mesh>
        <planeGeometry args={[1.2, 0.9]} />
        <meshBasicMaterial map={texture} transparent toneMapped={false} />
      </mesh>
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
// App + desktop overlay
// ---------------------------------------------------------------------------
export function App() {
  const [graph, setGraph] = useState<GraphData | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [config, setConfig] = useState<TransitionConfig>(DEFAULT_TRANSITION);
  const [focusNode, setFocusNode] = useState<GraphNode | null>(null);

  const focus = useRef<FocusState>({ node: null, active: false, elapsed: 0 });
  const graphGroupRef = useRef<THREE.Group>(null);

  useEffect(() => {
    loadGraph().then(setGraph).catch((e) => setError(String(e)));
  }, []);

  const handleSelect = useCallback((node: GraphNode) => {
    focus.current = { node, active: true, elapsed: 0 };
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
      />
      <Canvas camera={{ position: [0, 1.6, 0], near: 0.05, far: 1000 }}>
        <color attach="background" args={["#0b0e14"]} />
        <ambientLight intensity={0.85} />
        <directionalLight position={[6, 12, 6]} intensity={0.5} />
        <XR store={store}>
          <Rig focus={focus} config={config} graphGroupRef={graphGroupRef} />
          <group ref={graphGroupRef} position={GRAPH_POSITION} scale={GRAPH_SCALE}>
            {graph && <Graph data={graph} onSelect={handleSelect} />}
          </group>
          {focusNode && <NotePanel node={focusNode} graphGroupRef={graphGroupRef} />}
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
}: {
  config: TransitionConfig;
  setConfig: (c: TransitionConfig) => void;
  focusNode: GraphNode | null;
  nodeCount: number;
  linkCount: number;
  error: string | null;
}) {
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
      <button style={btn} onClick={() => store.enterVR()}>
        Enter VR
      </button>
      <div style={{ marginTop: 10, color: "#8b97ab" }}>
        {error ? (
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
        Left stick: fly · Right stick: up/down + turn · Point + trigger a node/link to fly to it.
      </div>
    </div>
  );
}
