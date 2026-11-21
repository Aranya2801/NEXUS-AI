import { existsSync, readFileSync } from "node:fs";
import { resolve } from "node:path";
import { EvaluationResultsSchema } from "./schemas";

// The UI schema must accept the file the backend harness really writes (skipped before the first evaluation run).
const file = resolve(__dirname, "../../../experiments/results/latest.json");

describe("evaluation results contract", () => {
  it.skipIf(!existsSync(file))("parses the real experiments/results/latest.json and keeps the mock flag", () => {
    const parsed = EvaluationResultsSchema.parse(JSON.parse(readFileSync(file, "utf8")));
    expect(parsed.systems.length).toBeGreaterThanOrEqual(2);
    expect(parsed.dataset.n_questions).toBeGreaterThanOrEqual(100);
    expect(parsed.environment.mock).toBe(true);
  });
});
