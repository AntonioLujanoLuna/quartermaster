// Rendering, kept apart from the handshake so the screen can grow without the
// boot sequence growing with it.
//
// Everything is built with createElement rather than innerHTML. Item names and
// provenance are typed by people at the table, and a stash that renders them as
// markup is a stash that renders whatever someone types.

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

export function renderStatus(message) {
  return element("p", "status", message);
}

export function renderError(message) {
  const box = element("div", "error");
  box.append(element("h2", null, "Quartermaster could not open"), element("p", null, message));
  return box;
}

function renderRoster(participants) {
  const aside = element("aside", "roster");
  aside.append(element("h2", null, `At the table (${participants.length})`));
  const list = element("ul");
  for (const person of participants) {
    const name = person.nickname || person.global_name || person.username || "Someone";
    list.append(element("li", null, name));
  }
  if (participants.length === 0) {
    list.append(element("li", "muted", "Nobody else yet"));
  }
  aside.append(list);
  return aside;
}

function renderSummary(home) {
  const bar = element("div", "summary");
  if (!home) return bar;
  const session =
    home.active_session_number !== null
      ? `Session ${home.active_session_number} · in progress`
      : "No session in progress";
  bar.append(element("span", "session", session));
  const coin = home.treasury || {};
  bar.append(element("span", "coin", `${coin.gp ?? 0} gp · ${coin.sp ?? 0} sp · ${coin.cp ?? 0} cp`));
  if (home.character) {
    bar.append(element("span", "character", `Playing ${home.character.name}`));
  } else {
    bar.append(element("span", "character muted", "No active character"));
  }
  return bar;
}

function renderItems(stash) {
  const table = element("table", "stash");
  const head = element("thead");
  const headRow = element("tr");
  for (const label of ["Item", "Qty", "Where it came from"]) {
    headRow.append(element("th", null, label));
  }
  head.append(headRow);
  table.append(head);

  const body = element("tbody");
  // No truncation and no "dropped N entries". The list scrolls, which is the
  // whole reason this surface exists.
  for (const item of stash.items) {
    const row = element("tr");
    row.append(element("td", "name", item.item_name));
    row.append(element("td", "quantity", item.quantity));
    row.append(element("td", "provenance muted", item.provenance || "—"));
    body.append(row);
  }
  if (stash.items.length === 0) {
    const row = element("tr");
    const cell = element("td", "muted", "The Party Stash is empty.");
    cell.colSpan = 3;
    row.append(cell);
    body.append(row);
  }
  table.append(body);
  return table;
}

export function renderStash(state) {
  const root = element("div", "layout");

  const header = element("header");
  header.append(element("h1", null, "Party Stash"));
  header.append(renderSummary(state.home));
  root.append(header);

  const main = element("main");
  main.append(renderItems(state.stash || { items: [] }));
  main.append(
    element("p", "count muted", `${state.stash?.total ?? 0} stacks · read-only for now`),
  );
  root.append(main);

  root.append(renderRoster(state.participants));
  return root;
}
