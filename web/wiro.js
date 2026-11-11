// Keeps the key typed into a Wiro API Key node out of saved workflows.
//
// - The prompt sent to the server carries a reference ("wiro-secret:..."),
//   never the key. The key is handed to the ComfyUI server once and only
//   lives in its memory (see wiro_comfyui/keystore.py).
// - Workflow files, autosaves, copies and file metadata store the widget empty.
// - On the canvas the key is shown as dots.
import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

const KEY_NODE = "WiroApiKey";
const SECRET_WIDGETS = new Set(["api_key", "api_secret"]);
const REF_PREFIX = "wiro-secret:";

async function toReference(value) {
  if (typeof value !== "string") return "";
  const text = value.trim();
  if (!text || text.startsWith(REF_PREFIX)) return text;
  const response = await api.fetchApi("/wiro/secret", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ value: text }),
  });
  if (!response.ok) {
    throw new Error("Wiro AI: the ComfyUI server did not accept the API key.");
  }
  const body = await response.json();
  return body.ref;
}

function hideValue(widget) {
  try {
    Object.defineProperty(widget, "_displayValue", {
      configurable: true,
      get() {
        return this.value ? "•".repeat(12) : "";
      },
    });
  } catch (error) {
    // Display only; the key is still kept out of saved files.
  }
}

app.registerExtension({
  name: "wiro.ApiKey",
  async beforeRegisterNodeDef(nodeType, nodeData) {
    if (nodeData?.name !== KEY_NODE) return;

    const onNodeCreated = nodeType.prototype.onNodeCreated;
    nodeType.prototype.onNodeCreated = function () {
      const result = onNodeCreated?.apply(this, arguments);
      for (const widget of this.widgets ?? []) {
        if (!SECRET_WIDGETS.has(widget.name)) continue;
        widget.serializeValue = () => toReference(widget.value);
        hideValue(widget);
      }
      return result;
    };

    // Every saved copy of the graph (workflow files, autosave, tabs, copy and
    // paste, file metadata) comes from node.serialize().
    const serialize = nodeType.prototype.serialize;
    nodeType.prototype.serialize = function () {
      const data = serialize.apply(this, arguments);
      emptySecrets(this, data);
      return data;
    };
  },
});

function emptySecrets(node, data) {
  if (!data) return;
  if (Array.isArray(data.widgets_values)) {
    (node.widgets ?? []).forEach((widget, index) => {
      if (SECRET_WIDGETS.has(widget.name) && index < data.widgets_values.length) {
        data.widgets_values[index] = "";
      }
    });
  }
  // Newer frontends also save values by widget name.
  const named = data.widgets_values_named;
  if (named && typeof named === "object") {
    for (const name of SECRET_WIDGETS) {
      if (name in named) named[name] = "";
    }
  }
}
