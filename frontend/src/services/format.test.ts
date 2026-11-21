import { answerTone, pageLabel, pct, relTime, splitCitations, statusTone, truncate, ms } from "./format";

describe("format helpers", () => {
  it("splits citation markers", () => {
    expect(splitCitations("A [1][2] b [12].")).toEqual([
      { kind: "text", value: "A " }, { kind: "cite", id: 1 }, { kind: "cite", id: 2 }, { kind: "text", value: " b " }, { kind: "cite", id: 12 }, { kind: "text", value: "." },
    ]);
    expect(splitCitations("no markers [x] or [1234]")).toEqual([{ kind: "text", value: "no markers [x] or [1234]" }]);
  });
  it("labels pages", () => {
    expect(pageLabel(null)).toBe(""); expect(pageLabel(3)).toBe("p.3"); expect(pageLabel(3, 3)).toBe("p.3"); expect(pageLabel(3, 5)).toBe("pp.3–5");
  });
  it("formats numbers and times", () => {
    expect(pct(0.256)).toBe("26%"); expect(pct(null)).toBe("–"); expect(ms(12.4)).toBe("12 ms"); expect(ms(1500)).toBe("1.50 s");
    const now = new Date("2026-01-02T12:00:00Z");
    expect(relTime("2026-01-02T11:59:50Z", now)).toBe("just now"); expect(relTime("2026-01-02T11:00:00Z", now)).toBe("1 h ago"); expect(relTime("2025-12-31T12:00:00Z", now)).toBe("2 d ago");
    expect(truncate("abcdef", 4)).toBe("abc…"); expect(truncate("abc", 4)).toBe("abc");
  });
  it("maps statuses to tones", () => {
    expect(answerTone("grounded")).toBe("good"); expect(answerTone("partial")).toBe("warn"); expect(answerTone("insufficient_evidence")).toBe("bad"); expect(answerTone(null)).toBe("neutral");
    expect(statusTone("ready")).toBe("good"); expect(statusTone("dead")).toBe("bad"); expect(statusTone("needs_review")).toBe("warn");
  });
});
