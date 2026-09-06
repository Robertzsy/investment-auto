/** Session-plane registration for the fixed Investment Auto workflow. */
import { defineTool } from "@deepseek-ai/dsh-tools";
import { EngineClient } from "../../dsh-investment-tools/lib/engine-client.js";
import { registerAnalysisWorkflow } from "../../dsh-investment-tools/lib/analysis-workflow.js";

export const name = "@investment-auto/dsh-investment-workflow";
export const inject = ["tools", "workflowEngine"];

const FREE_OBJECT = { type: "object", additionalProperties: true };
const render = (_args, value) => [{
  type: "text",
  text: JSON.stringify(value, null, 2).slice(0, 50000),
}];

function registerTool(ctx, toolName, description, parameters, executor, options = {}) {
  ctx.tools.register(defineTool({
    name: toolName,
    description,
    parameters,
    output: { schema: FREE_OBJECT, render },
    timeoutMs: options.timeoutMs ?? 1800000,
    execute: executor,
  }));
}

export function apply(ctx, config) {
  const engineUrl = process.env.INVESTMENT_ENGINE_URL ??
    (typeof config?.engineUrl === "string" ? config.engineUrl : "http://127.0.0.1:8790");
  const token = process.env.IA_ACCESS_TOKEN ?? (typeof config?.token === "string" ? config.token : "");
  registerAnalysisWorkflow(ctx, new EngineClient(engineUrl, token), registerTool);
}
