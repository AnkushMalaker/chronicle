import type { GraphData, GraphNode } from "./types";

function hashHue(s: string): number {
  let h = 0;
  for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) >>> 0;
  return h % 360;
}

/** Node colour: notes coloured by folder, unresolved/dir/file have fixed hues. */
export function nodeColor(n: GraphNode): string {
  switch (n.type) {
    case "unresolved":
      return "#4a5160";
    case "dir":
      return "#ffcf6b";
    case "file":
      return "#7fd1b9";
    default:
      return `hsl(${hashHue(n.folder || "root")}, 65%, 62%)`;
  }
}

/** Bigger nodes = more connected. */
export function nodeVal(n: GraphNode): number {
  return 1 + (n.degree || 0);
}

export async function loadGraph(url = "graph.json"): Promise<GraphData> {
  const res = await fetch(url, { cache: "no-cache" });
  if (!res.ok) throw new Error(`Failed to load ${url}: ${res.status}`);
  return (await res.json()) as GraphData;
}
