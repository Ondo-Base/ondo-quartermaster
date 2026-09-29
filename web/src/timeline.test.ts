import { describe, expect, it } from "vitest";
import { buildTimeline } from "./pages/TaskRun";

const ev = (seq: number, type: string, data: any) => ({ seq, type, source: "t", ts: 1_790_000_000 + seq, data });

describe("buildTimeline", () => {
  it("groups one model turn into one readable step, with files and typed values", () => {
    const items = buildTimeline([
      ev(0, "model_response", { text: "I'll look through the client folder first.", tool_calls: [{ id: "a", name: "read_file", arguments: {} }, { id: "b", name: "read_file", arguments: {} }] }),
      ev(1, "step", { call_id: "a", title: "Read Halleck_MSA_2026.pdf", tool: "read_file", status: "running" }),
      ev(2, "step", { call_id: "b", title: "Read Q3_Renewals.xlsx", tool: "read_file", status: "running" }),
      ev(3, "step", { call_id: "a", title: "Read Halleck_MSA_2026.pdf", tool: "read_file", status: "done", detail: { path: "/d/Halleck_MSA_2026.pdf" } }),
      ev(4, "step", { call_id: "b", title: "Read Q3_Renewals.xlsx", tool: "read_file", status: "done", detail: { path: "/d/Q3_Renewals.xlsx" } }),
      ev(5, "model_response", { text: "", tool_calls: [{ id: "c", name: "browser_fill_form", arguments: { fields: [{ name: "Annual value — Halleck Logistics", value: "193725" }] } }] }),
      ev(6, "step", { call_id: "c", title: "Filled 1 fields", tool: "browser_fill_form", status: "running" }),
      ev(7, "model_response", { text: "", tool_calls: [{ id: "d", name: "read_file", arguments: {} }] }),
      ev(8, "step", { call_id: "d", title: "Read x", tool: "read_file", status: "error", detail: { error: "Permission denied: x is excluded" } }),
    ]);
    expect(items).toHaveLength(3);
    expect(items[0]).toMatchObject({ title: "Read 2 files", status: "done", note: "I'll look through the client folder first.", chips: ["Halleck_MSA_2026.pdf", "Q3_Renewals.xlsx"] });
    expect(items[1]).toMatchObject({ status: "now", typed: [{ label: "Annual value — Halleck Logistics", value: "193725" }] });
    expect(items[2]).toMatchObject({ status: "error", error: "x is excluded" });
  });

  it("shows what was typed into a window operated by pixels", () => {
    const actions = [{ action: "type", target: "Annual value field", text: "193725", replace: true }, { action: "key", keys: "Tab" }];
    const items = buildTimeline([
      ev(0, "model_response", { text: "", tool_calls: [{ id: "s", name: "screen_act", arguments: { window: "Remote billing", actions } }] }),
      ev(1, "step", { call_id: "s", title: "Operated Remote billing (2 actions)", tool: "screen_act", status: "done" }),
    ]);
    expect(items[0]).toMatchObject({ title: "Operated Remote billing (2 actions)", typed: [{ label: "Annual value field in Remote billing", value: "193725" }] });
  });
});
