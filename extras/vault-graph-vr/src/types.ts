export type NodeType = "note" | "unresolved" | "dir" | "file";

export interface GraphNode {
  id: string;
  name: string;
  type: NodeType;
  path?: string;
  folder?: string;
  content?: string;
  ext?: string;
  degree?: number;
  // injected by the force simulation at runtime:
  x?: number;
  y?: number;
  z?: number;
  // d3-force fixed-position pins (set to pin/drag a node, undefined to release):
  fx?: number;
  fy?: number;
  fz?: number;
}

export interface GraphLink {
  // strings before the sim runs, node refs after r3f-forcegraph processes them:
  source: string | GraphNode;
  target: string | GraphNode;
  kind?: string;
}

export interface GraphData {
  nodes: GraphNode[];
  links: GraphLink[];
}
