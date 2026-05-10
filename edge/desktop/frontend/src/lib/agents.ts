// Agent registry shared across screens.
export type AgentId =
  | "personal-agent"
  | "coding-agent"
  | "financial-agent"
  | "health-agent"
  | "research-agent"
  | "librarian"
  | "news-agent";

export type AgentMeta = {
  id: AgentId;
  label: string;
  color: string;
  blurb: string;
};

export const AGENTS: AgentMeta[] = [
  { id: "personal-agent",  label: "Personal",  color: "#7C5CFF", blurb: "default delegate" },
  { id: "coding-agent",    label: "Coding",    color: "#22D3EE", blurb: "PR review · GitHub" },
  { id: "financial-agent", label: "Financial", color: "#4ADE80", blurb: "spend · budget" },
  { id: "health-agent",    label: "Health",    color: "#F472B6", blurb: "wellness · local-only" },
  { id: "research-agent",  label: "Research",  color: "#FBBF24", blurb: "Notion · web · summarise" },
  { id: "librarian",       label: "Librarian", color: "#9CA0AB", blurb: "memory · always-on" },
  { id: "news-agent",      label: "News",      color: "#60A5FA", blurb: "feeds · digest" },
];

export const agent = (id: AgentId): AgentMeta =>
  AGENTS.find((a) => a.id === id) ?? AGENTS[0];
