"use strict";
// Progressive enhancement: the complete guide and links work without JavaScript.
document.querySelectorAll("article table").forEach((table) => {
  const wrap = document.createElement("div");
  wrap.className = "table-wrap";
  wrap.tabIndex = 0;
  wrap.setAttribute("role", "region");
  wrap.setAttribute("aria-label", "Scrollable data table");
  table.before(wrap);
  wrap.append(table);
});
let pages = null;
const field = document.querySelector("#docs-search"),
  results = document.querySelector("#search-results");
field.addEventListener("input", async () => {
  const value = field.value.trim().toLowerCase();
  results.replaceChildren();
  results.hidden = !value;
  if (!value) return;
  try {
    if (!pages) {
      const response = await fetch("search.json");
      if (!response.ok) throw Error();
      pages = await response.json();
    }
    if (field.value.trim().toLowerCase() !== value) return;
    const matches = pages.filter((p) =>
      (p.title + " " + p.text).toLowerCase().includes(value),
    );
    if (!matches.length) {
      const p = document.createElement("p");
      p.textContent =
        "No matching guides. Try “research”, “budget”, or “recovery”.";
      results.append(p);
    }
    for (const page of matches) {
      const a = document.createElement("a");
      a.href = page.href;
      a.textContent = page.title;
      const small = document.createElement("small");
      const i = page.text.toLowerCase().indexOf(value);
      small.textContent =
        i < 0
          ? "Open guide"
          : page.text.slice(Math.max(0, i - 40), i + 110).replaceAll("#", "");
      a.append(small);
      results.append(a);
    }
  } catch {
    results.textContent = "Search unavailable. Use the guide navigation below.";
  }
});
