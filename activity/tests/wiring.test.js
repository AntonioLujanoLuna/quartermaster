// @vitest-environment node
//
// Reading two files as text, which needs a filesystem rather than a document.
// jsdom rewrites `import.meta.url` to a page URL, and a page URL has no `src`
// directory on it.
//
// The failure this exists for happens on a press, not on a draw.
//
// `render.js` attaches `handlers.somethingOrOther` to a click. If that name is
// not on the object `main.js` built, nothing complains: the screen renders
// perfectly, the button looks live, and the first person to press it gets a
// TypeError instead of an action — mid-evening, which is the same moment the
// render tests were written for.
//
// Neither the build nor ESLint can see it: the two halves meet through a
// parameter, so the name is only checked when it is called. Reading both files
// as text is what makes the meeting point checkable at all — `main.js` cannot
// be imported here, because importing it opens a Discord SDK and boots the
// application.

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

function source(name) {
  return readFileSync(fileURLToPath(new URL(`../src/${name}`, import.meta.url)), "utf8");
}

/** Every `handlers.name` the screens reach for. */
function handlersCalled() {
  const names = new Set();
  for (const [, name] of source("render.js").matchAll(/\bhandlers\.(\w+)/g)) names.add(name);
  return names;
}

/**
 * Every key on the object the boot sequence passes in.
 *
 * The literal is read rather than evaluated, so this depends on it staying one
 * object with its keys at one indent — which is what Prettier already enforces
 * and what the file already looks like.
 */
function handlersDefined() {
  const text = source("main.js");
  const start = text.indexOf("const handlers = {");
  expect(start).toBeGreaterThan(-1);
  const end = text.indexOf("\n};", start);
  expect(end).toBeGreaterThan(start);
  const body = text.slice(start, end);
  const names = new Set();
  for (const [, name] of body.matchAll(/^ {2}(?:async )?(\w+)\s*[(:]/gm)) names.add(name);
  return names;
}

describe("the screens and the boot sequence agree about the handlers", () => {
  it("finds both halves, so a silent regex failure cannot pass this file", () => {
    expect(handlersCalled().size).toBeGreaterThan(20);
    expect(handlersDefined().size).toBeGreaterThan(20);
  });

  it("defines every handler a screen presses", () => {
    const defined = handlersDefined();
    const missing = [...handlersCalled()].filter((name) => !defined.has(name)).sort();
    expect(missing).toEqual([]);
  });

  it("presses every handler it defines", () => {
    // The other direction is not a crash, but it is a control that was removed
    // from a screen and left wired, or one wired before the screen that was
    // going to press it. Either way it is dead weight worth seeing.
    const called = handlersCalled();
    const unused = [...handlersDefined()].filter((name) => !called.has(name)).sort();
    expect(unused).toEqual([]);
  });
});
