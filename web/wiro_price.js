// Shows a Wiro node's price on its title bar before it runs, and what the
// run cost afterwards. Prices come from the Wiro AI catalog (the model's
// pricing tiers for the selected settings), served by this pack at
// /wiro/prices. The figures are list prices in USD; the cost after a run is
// the amount Wiro reported for that task.
import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

const SKIP = new Set(["WiroApiKey"]);
const COUNT_KEYS = ["samples", "numImages", "num_images", "maxImages", "max_images", "numberOfImages", "outputs"];
let prices = null;

async function loadPrices() {
  try {
    const response = await api.fetchApi("/wiro/prices");
    if (response.ok) prices = await response.json();
  } catch (error) {
    console.warn("[Wiro] could not load prices", error);
  }
}

function usd(value) {
  if (!Number.isFinite(value)) return "";
  if (value >= 1) return `$${value.toFixed(2)}`;
  if (value >= 0.01) return `$${value.toFixed(3).replace(/0$/, "")}`;
  return `$${Number(value.toPrecision(2))}`;
}

function widgetValue(node, name) {
  return node.widgets?.find((w) => w.name === name)?.value;
}

function settings(node, prefix) {
  const values = {};
  for (const widget of node.widgets ?? []) {
    if (!widget.name) continue;
    if (prefix && widget.name.startsWith(prefix)) values[widget.name.slice(prefix.length)] = widget.value;
    else if (!prefix) values[widget.name] = widget.value;
  }
  return values;
}

// Category nodes name their settings in snake_case; price tiers use Wiro's ids.
function snake(key) {
  return String(key).replace(/([a-z0-9])([A-Z])/g, "$1_$2").toLowerCase().replace(/[^a-z0-9_]+/g, "_");
}

function lookup(values, key) {
  return values[key] !== undefined ? values[key] : values[snake(key)];
}

function matches(entry, values) {
  const inputs = entry.inputs ?? {};
  return Object.entries(inputs).every(([key, want]) => {
    if (String(want).startsWith("QUANTITY:")) return false;
    const value = lookup(values, key);
    return value !== undefined && String(value) === String(want);
  });
}

export function priceLabel(info, values, promptText = "") {
  if (!info) return "";
  const entries = info.price ?? [];
  if (!entries.length) {
    // Priced by processing time: Wiro's typical cost per run, else the rate.
    if (info.approx) return `~${usd(info.approx)}/run`;
    return info.cps ? `${usd(info.cps)}/s` : "";
  }
  const specific = entries.filter((e) => Object.keys(e.inputs ?? {}).length);
  const hit = specific.find((e) => matches(e, values)) ?? entries.find((e) => !Object.keys(e.inputs ?? {}).length);
  if (!hit) {
    const lowest = Math.min(...entries.map((e) => Number(e.price) || 0).filter((p) => p > 0));
    return Number.isFinite(lowest) ? `from ${usd(lowest)}` : "";
  }
  const price = Number(hit.price) || 0;
  switch (hit.method) {
    case "cpr":
      return `${usd(price)}/run`;
    case "cpo": {
      const key = COUNT_KEYS.find((k) => Number(lookup(values, k)) > 1);
      const count = key ? Number(lookup(values, key)) : 1;
      return count > 1 ? `${usd(price * count)}/run` : `${usd(price)}/output`;
    }
    case "cps":
      return `${usd(price)}/s`;
    case "cpt":
      return `${usd(price * 1e6)}/1M tokens`;
    case "cp-pixel":
      return `${usd(price)}/MP`;
    case "cp-promptlength": {
      const length = String(promptText ?? "").length;
      return length ? `~${usd(price * length)}` : `${usd(price)}/char`;
    }
    case "cp-audiosecondslength":
      return `${usd(price)}/audio s`;
    case "cp-outputVideoLength": {
      const seconds = Number(lookup(values, "duration") ?? lookup(values, "durationSeconds") ?? lookup(values, "length"));
      return seconds > 0 ? `~${usd(price * seconds)}` : `${usd(price)}/video s`;
    }
    case "cp-readoutput":
      // Billed from what the model reports (tokens, seconds of output): known after the run.
      return "priced by usage";
    default:
      return price ? usd(price) : "";
  }
}

function estimate(node) {
  if (!prices) return "";
  const type = node.comfyClass;
  if (type === "WiroLLM") {
    const p = prices.llm?.[widgetValue(node, "model")];
    if (!p || p.prompt == null) return "token priced";
    return `${usd(p.prompt * 1e6)} in, ${usd(p.completion * 1e6)} out /1M tok`;
  }
  if (type === "WiroRunWorkflow") return "billed per step";
  if (prices.fixed?.[type]) {
    const values = settings(node, "");
    return priceLabel(prices.models?.[prices.fixed[type]], values, values.prompt);
  }
  if (type === "WiroRunModel") {
    let values = {};
    try {
      values = JSON.parse(widgetValue(node, "parameters") || "{}") ?? {};
    } catch (error) {
      values = {};
    }
    const prompt = widgetValue(node, "prompt") || values.prompt || "";
    return priceLabel(prices.models?.[String(widgetValue(node, "model") || "").trim()], values, prompt);
  }
  const values = settings(node, "model.");
  const prompt = widgetValue(node, "prompt") ?? widgetValue(node, "text") ?? "";
  return priceLabel(prices.models?.[widgetValue(node, "model")], values, prompt);
}

function signature(node) {
  // Settings only: "$$" widgets are the frontend's own (progress text, previews)
  // and change while and after the node runs.
  const settingsWidgets = (node.widgets ?? []).filter((w) => !String(w.name ?? "").startsWith("$$"));
  return JSON.stringify(settingsWidgets.map((w) => [w.name, w.value]));
}

function badgeText(node) {
  const paid = node.__wiroPaid;
  if (paid && paid.signature === signature(node)) return `paid ${usd(paid.cost)}`;
  return estimate(node);
}

function attach(node) {
  if (!node?.comfyClass?.startsWith("Wiro") || SKIP.has(node.comfyClass) || node.__wiroBadge) return;
  node.__wiroBadge = true;
  const Badge = window.LGraphBadge;
  if (!Badge || !Array.isArray(node.badges)) return;
  node.badges.push(() => {
    const text = badgeText(node);
    const paid = text.startsWith("paid");
    return new Badge({
      text,
      fgColor: paid ? "#d8f5df" : "#e8eaed",
      bgColor: paid ? "#1f5130" : "#3a3f44",
      fontSize: 11,
    });
  });
}

api.addEventListener("executed", (event) => {
  const detail = event.detail ?? {};
  const cost = Number(detail.output?.wiro_cost?.[0]);
  if (!Number.isFinite(cost)) return;
  const node = app.graph?.getNodeById?.(detail.display_node ?? detail.node);
  if (!node) return;
  node.__wiroPaid = { cost, signature: signature(node) };
  node.setDirtyCanvas?.(true, true);
});

app.registerExtension({
  name: "wiro.Price",
  async setup() {
    await loadPrices();
    for (const node of app.graph?.nodes ?? []) attach(node);
    app.canvas?.setDirty?.(true, true);
  },
  nodeCreated(node) {
    attach(node);
  },
  loadedGraphNode(node) {
    attach(node);
  },
});
