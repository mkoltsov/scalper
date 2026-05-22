const form = document.querySelector("#target-form");
const buildButton = document.querySelector("#build-target");
const copyButton = document.querySelector("#copy-target");
const output = document.querySelector("#target-output");
const message = document.querySelector("#target-message");
const issueLink = document.querySelector("#issue-link");

function lines(value) {
  return value.split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
}

function slug(value) {
  return value.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");
}

function buildTarget() {
  const data = new FormData(form);
  const name = String(data.get("name") || "").trim();
  const id = slug(String(data.get("id") || name));
  const price = Number(data.get("max_price_usd"));
  const queries = lines(String(data.get("search_queries") || ""));
  const intent = String(data.get("search_intent") || "").trim();

  if (!id || !name || !price || !intent || queries.length === 0) {
    throw new Error("Fill ID, name, max price, search intent, and at least one search query.");
  }

  const target = {
    id,
    name,
    max_price_usd: price,
    search_intent: intent,
    acceptance_criteria: [
      "The listing clearly matches the requested item.",
      `The total price including shipping is less than ${price} USD.`,
      "It is currently available to buy.",
      "The listing URL opens as an active listing page.",
      "It ships directly to at least one configured destination; local pickup only is not acceptable unless a configured local-marketplace exception applies."
    ],
    search_queries: queries
  };

  const required = lines(String(data.get("required_any_patterns") || ""));
  const reject = lines(String(data.get("reject_patterns") || ""));
  if (required.length) target.required_any_patterns = required;
  if (reject.length) target.reject_patterns = reject;
  return target;
}

function refresh() {
  try {
    const target = buildTarget();
    const json = JSON.stringify(target, null, 2);
    output.textContent = json;
    message.textContent = "Add this object to deals.json under targets.";
    message.className = "empty";
    const body = [
      "Please add this scalper target to deals.json:",
      "",
      "```json",
      json,
      "```"
    ].join("\n");
    issueLink.href = "https://github.com/mkoltsov/scalper/issues/new?title=" +
      encodeURIComponent("Add scalper target: " + target.name) +
      "&body=" + encodeURIComponent(body);
  } catch (error) {
    output.textContent = "";
    message.textContent = error.message;
    message.className = "error";
    issueLink.href = "https://github.com/mkoltsov/scalper/issues/new";
  }
}

buildButton?.addEventListener("click", refresh);
form?.addEventListener("input", () => {
  if (output.textContent) refresh();
});
copyButton?.addEventListener("click", async () => {
  refresh();
  if (!output.textContent) return;
  await navigator.clipboard.writeText(output.textContent);
  message.textContent = "Copied target JSON.";
  message.className = "empty";
});
