import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { AgentTimeline } from "./AgentTimeline";
import { AnswerView } from "./AnswerView";
import { BarChart } from "./BarChart";
import { GraphView, layout } from "./GraphView";
import { Meter } from "./ui";
import { WorkflowGraph } from "./WorkflowGraph";

describe("AnswerView", () => {
  it("turns known [n] markers into clickable chips and leaves unknown ones as text", async () => {
    const onSelect = vi.fn();
    render(<AnswerView text="Longformer is sparse [1][2]. Unknown [9]." citationIds={[1, 2]} onSelect={onSelect} activeId={2} />);
    const chips = screen.getAllByRole("button");
    expect(chips.map((c) => c.textContent)).toEqual(["1", "2"]);
    expect(chips[1]).toHaveClass("active");
    await userEvent.click(chips[0]!);
    expect(onSelect).toHaveBeenCalledWith(1);
    expect(screen.getByText(/Unknown \[9\]/)).toBeInTheDocument();
  });
  it("styles the 'limited support' qualifier and announces streaming updates politely", () => {
    const { container } = render(<AnswerView text="Claim (limited support) [1]" citationIds={[1]} streaming />);
    expect(screen.getByText(/limited support/)).toHaveClass("muted");
    expect(container.querySelector(".answer")).toHaveAttribute("aria-live", "polite");
  });
});

describe("AgentTimeline", () => {
  const trace = [{ agent: "orchestrator", node: "classify" }, { agent: "retrieval", node: "retrieve_branch" }, { agent: "retrieval", node: "fuse_rerank" }];
  it("marks finished agents, counts repeats and highlights the running one", () => {
    render(<AgentTimeline trace={trace} running />);
    const state = (a: string) => document.querySelector(`[data-agent="${a}"]`)?.getAttribute("data-state");
    expect(state("orchestrator")).toBe("done");
    expect(state("retrieval")).toBe("active");           // last agent to report while the run is in progress
    expect(state("verification")).toBe("idle");
    expect(screen.getByText("×2")).toBeInTheDocument();
  });
  it("greys out agents the plan did not select once the run is finished, and flags a pending review", () => {
    const { rerender } = render(<AgentTimeline trace={trace} selected={["orchestrator", "retrieval"]} />);
    expect(document.querySelector('[data-agent="knowledge_graph"]')).toHaveAttribute("data-state", "skipped");
    rerender(<AgentTimeline trace={trace} reviewPending />);
    expect(document.querySelector('[data-agent="human"]')).toHaveAttribute("data-state", "active");
  });
});

describe("WorkflowGraph", () => {
  it("highlights executed nodes with their repeat counts, failures and the paused review node", () => {
    const trace = [{ agent: "retrieval", node: "retrieve_branch", ms: 1 }, { agent: "retrieval", node: "retrieve_branch", ms: 1 }, { agent: "research", node: "rewrite", ms: 1 }];
    const { container } = render(<WorkflowGraph trace={trace} errors={["fuse_rerank: ValueError: bad"]} pendingReview />);
    const node = (id: string) => container.querySelector(`[data-node="${id}"]`)!;
    expect(node("retrieve_branch")).toHaveClass("ran");
    expect(node("retrieve_branch")).toHaveAttribute("data-count", "2");
    expect(node("rewrite")).toHaveClass("ran");
    expect(node("fuse_rerank")).toHaveClass("err");
    expect(node("human_review")).toHaveClass("pending");
    expect(node("generate")).not.toHaveClass("ran");
    expect(screen.getByRole("img", { name: /workflow graph/i })).toBeInTheDocument();
  });
});

describe("GraphView", () => {
  const nodes = { a: { id: "a", name: "Transformer", type: "concept", depth: 0 }, b: { id: "b", name: "Longformer", depth: 1 }, c: { id: "c", name: "Performer", depth: 2 } };
  const edges = [{ source_id: "b", predicate: "extends", target_id: "a", confidence: 0.7, depth: 1, chunk_id: "c1", document_id: "d", title: "T", page: null, evidence: null }];
  it("lays nodes out deterministically on rings by hop depth", () => {
    const p1 = layout(nodes), p2 = layout(nodes);
    expect(p1).toEqual(p2);
    expect(p1["a"]).toEqual({ x: 320, y: 230 });                                   // single seed sits in the centre
    const dist = (k: string) => Math.hypot(p1[k]!.x - 320, p1[k]!.y - 230);
    expect(dist("b")).toBeLessThan(dist("c"));
  });
  it("renders labelled nodes and edges and reports clicks", async () => {
    const onSelect = vi.fn();
    const { container } = render(<GraphView nodes={nodes} edges={edges} onSelect={onSelect} />);
    expect(container.querySelector('[data-edge="extends"]')).toBeInTheDocument();
    await userEvent.click(container.querySelector('[data-node="Longformer"]')!);
    expect(onSelect).toHaveBeenCalledWith("b");
  });
});

describe("BarChart and Meter", () => {
  it("scales bars to the maximum and draws confidence intervals when given", () => {
    const { container } = render(<BarChart title="nDCG@10" bars={[{ label: "Hybrid", value: 0.8, low: 0.7, high: 0.9 }, { label: "Dense", value: 0.4 }]} />);
    const fills = [...container.querySelectorAll<HTMLElement>("[data-value]")];
    const widths = fills.map((f) => parseFloat(f.style.width));
    expect(widths[0]).toBeCloseTo((0.8 / 0.9) * 100, 2);
    expect(widths[1]).toBeCloseTo((0.4 / 0.9) * 100, 2);
    expect(screen.getByText("0.800")).toBeInTheDocument();
  });
  it("exposes meter semantics and clamps out-of-range values", () => {
    render(<Meter value={1.7} label="Confidence" />);
    expect(screen.getByRole("meter", { name: "Confidence" })).toHaveAttribute("aria-valuenow", "1");
  });
});
