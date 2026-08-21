window.__ModuleLoader__.load({
	id: "@investment-auto/dsh-investment-ui",
	factory: (require) => {
		var module = { exports: {} };
		var exports = module.exports;
		Object.defineProperty(exports, Symbol.toStringTag, { value: "Module" });
		let react_jsx_runtime = require("react/jsx-runtime");
		let react = require("react");

		// ── styles (design-token based, injected once per page load) ──────
		const CSS_ID = "@investment-auto/dsh-investment-ui/style";
		const css = ".inv-ui-row{display:flex;flex-wrap:wrap;gap:6px;align-items:center;min-width:0;padding:2px 0}.inv-ui-chip{display:inline-flex;align-items:center;gap:4px;border:1px solid var(--dsw-alias-border-l1, #2a3350);background:var(--dsw-alias-bg-base, transparent);color:var(--dsw-alias-label-secondary, inherit);border-radius:8px;padding:2px 8px;font-size:12px;line-height:18px;white-space:nowrap}.inv-ui-chip[data-kind=warn]{border-color:var(--dsw-alias-state-warning-primary, #d97706);color:var(--dsw-alias-state-warning-primary, #d97706)}.inv-ui-chip[data-kind=danger]{border-color:var(--dsw-alias-state-error-primary, #ef4444);color:var(--dsw-alias-state-error-primary, #ef4444)}.inv-ui-chip[data-kind=ok]{border-color:var(--dsw-alias-state-success-primary, #22c55e);color:var(--dsw-alias-state-success-primary, #22c55e)}.inv-ui-raw{white-space:pre-wrap;word-break:break-word;color:var(--dsw-alias-label-secondary, inherit);font-size:12px;font-family:var(--dsw-font-markdown-code-block-small, monospace);margin:0;padding:4px 0}.inv-ui-title{font-size:12px;font-weight:600;color:var(--dsw-alias-label-primary, inherit);margin-right:6px}";
		if (typeof document !== "undefined" && document.querySelector("style[data-plugin=" + JSON.stringify(CSS_ID) + "]") === null) {
			const tag = document.createElement("style");
			tag.dataset.plugin = CSS_ID;
			tag.textContent = css;
			document.head.appendChild(tag);
		}

		// ── helpers ────────────────────────────────────────────────────────
		/** Best-effort parse of a tool-result surface block's JSON text. */
		function parseJson(block) {
			if (block === null || typeof block !== "object") return null;
			if (block.kind !== "tool-result") return null;
			const content = Array.isArray(block.content) ? block.content : [];
			const text = content.filter((b) => b.type === "text").map((b) => b.text).join("");
			if (!text) return null;
			try {
				const parsed = JSON.parse(text);
				return parsed !== null && typeof parsed === "object" ? parsed : null;
			} catch {
				return null;
			}
		}
		const Chip = ({ label, value, kind }) => react_jsx_runtime.jsxs("span", {
			className: "inv-ui-chip",
			"data-kind": kind,
			children: [label, value !== null && value !== void 0 && String(value) !== "" ? react_jsx_runtime.jsx("b", { children: String(value) }) : null]
		});
		const RawFallback = ({ text }) => react_jsx_runtime.jsx("pre", { className: "inv-ui-raw", children: text });

		// ── investment_status row ──────────────────────────────────────────
		function StatusRow({ block }) {
			const data = parseJson(block);
			if (data === null) {
				const raw = Array.isArray(block.content) ? block.content.filter((b) => b.type === "text").map((b) => b.text).join("") : "";
				return RawFallback({ text: raw });
			}
			const control = data.control ?? {};
			const mandate = data.mandate ?? {};
			const paused = Boolean(control.paused);
			const killed = Boolean(control.kill_switch);
			return react_jsx_runtime.jsxs("div", {
				className: "inv-ui-row",
				children: [
					react_jsx_runtime.jsx("span", { className: "inv-ui-title", children: "投资引擎" }),
					Chip({ label: "模式", value: data.operation_mode ?? "-" }),
					Chip({ label: "策略", value: mandate.display_name ?? mandate.profile ?? "-" }),
					killed ? Chip({ label: "紧急停止", value: null, kind: "danger" }) : paused ? Chip({ label: "已暂停", value: null, kind: "warn" }) : Chip({ label: "风控正常", value: null, kind: "ok" }),
					data.current_time ? Chip({ label: "时间", value: String(data.current_time).slice(5, 16) }) : null
				]
			});
		}

		// ── investment_portfolio row ───────────────────────────────────────
		function PortfolioRow({ block }) {
			const data = parseJson(block);
			if (data === null) {
				const raw = Array.isArray(block.content) ? block.content.filter((b) => b.type === "text").map((b) => b.text).join("") : "";
				return RawFallback({ text: raw });
			}
			const market = String(data.market ?? "").toUpperCase();
			const cash = typeof data.cash === "number" ? data.cash : NaN;
			const holdings = Array.isArray(data.holdings) ? data.holdings : [];
			const trades = Array.isArray(data.tradeHistory) ? data.tradeHistory : [];
			return react_jsx_runtime.jsxs("div", {
				className: "inv-ui-row",
				children: [
					react_jsx_runtime.jsx("span", { className: "inv-ui-title", children: market ? "组合 " + market : "组合" }),
					Chip({ label: "现金", value: Number.isFinite(cash) ? cash.toLocaleString("zh-CN", { maximumFractionDigits: 0 }) : "-" }),
					Chip({ label: "持仓", value: holdings.length }),
					Chip({ label: "成交", value: trades.length })
				]
			});
		}

		// ── investment_mandate row ─────────────────────────────────────────
		function MandateRow({ block }) {
			const data = parseJson(block);
			if (data === null) {
				const raw = Array.isArray(block.content) ? block.content.filter((b) => b.type === "text").map((b) => b.text).join("") : "";
				return RawFallback({ text: raw });
			}
			const mandate = data.mandate ?? data;
			const minConfidence = typeof mandate.min_confidence === "number" ? mandate.min_confidence : null;
			const maxPosition = typeof mandate.max_position_pct === "number" ? mandate.max_position_pct : null;
			const maxDrawdown = typeof mandate.max_drawdown_pct === "number" ? mandate.max_drawdown_pct : null;
			return react_jsx_runtime.jsxs("div", {
				className: "inv-ui-row",
				children: [
					react_jsx_runtime.jsx("span", { className: "inv-ui-title", children: "授权书" }),
					Chip({ label: "策略", value: mandate.display_name ?? mandate.profile ?? "-" }),
					minConfidence !== null ? Chip({ label: "最低置信", value: minConfidence }) : null,
					maxPosition !== null ? Chip({ label: "单股上限", value: maxPosition + "%" }) : null,
					maxDrawdown !== null ? Chip({ label: "最大回撤", value: maxDrawdown + "%", kind: "warn" }) : null
				]
			});
		}

		// ── plugin ─────────────────────────────────────────────────────────
		const name = "@investment-auto/dsh-investment-ui";
		const inject = ["slots"];
		function apply(ctx) {
			// Keyed tool views: the slot routes by the tool name; a keyed hit
			// replaces the generic tool row for that call.
			ctx.slots.inject("tool.call.toolview", () => ctx.slots.register({
				name: "tool.call.toolview",
				key: "investment_status",
				locale: "conversation"
			}, StatusRow));
			ctx.slots.inject("tool.call.toolview", () => ctx.slots.register({
				name: "tool.call.toolview",
				key: "investment_portfolio",
				locale: "conversation"
			}, PortfolioRow));
			ctx.slots.inject("tool.call.toolview", () => ctx.slots.register({
				name: "tool.call.toolview",
				key: "investment_mandate",
				locale: "conversation"
			}, MandateRow));
		}

		exports.apply = apply;
		exports.inject = inject;
		exports.name = name;
		return module.exports;
	}
});
