// A stand-in for the browser around the page's app.js in the tests (tests/test_web.py, run with Node.js): just enough
// of a page for its code to run, and /api/state answered when the test says so.
//
//     node page_stand_in.js <app.js> <state.json>
//
// The page asks for the state when it starts, then the user presses "Look for videos" while that answer is on its way:
// its own refresh asks again. Both answers come back with the same log lines. Prints {requests, log}: how many state
// requests were answered and what the Log tab shows.
"use strict";
const fs = require("fs");
const vm = require("vm");

const [appPath, statePath] = process.argv.slice(2);
const state = fs.readFileSync(statePath, "utf8");

const elements = new Map();
function element(selector) {
  if (!elements.has(selector)) {
    let html = "";
    elements.set(selector, {
      handlers: {}, dataset: {}, style: {}, value: "", checked: false, hidden: false, disabled: false,
      scrollTop: 0, clientHeight: 0, scrollHeight: 0, firstElementChild: null,
      classList: { toggle() {}, add() {}, remove() {}, contains: () => false },
      get innerHTML() { return html; },
      set innerHTML(value) { html = String(value); },
      get textContent() { return html; },
      set textContent(value) { html = String(value); },
      addEventListener(type, handler) { this.handlers[type] = handler; },
      setAttribute() {},
      removeAttribute() {},
      focus() {},
      insertAdjacentHTML(where, more) { html += more; },
      querySelector: inner => element(`${selector} ${inner}`),
      querySelectorAll: () => [],
      closest: () => null,
    });
  }
  return elements.get(selector);
}

globalThis.document = {
  querySelector: element, querySelectorAll: () => [], addEventListener() {},
  documentElement: { dataset: {} }, body: element("body"), activeElement: null,
};
globalThis.matchMedia = () => ({ matches: false });
globalThis.localStorage = { getItem: () => null, setItem() {} };
globalThis.location = { host: "127.0.0.1:8767" };
globalThis.confirm = () => true;

const waiting = []; // answers to /api/state, sent when the test says so
globalThis.fetch = (url, options = {}) => {
  if (options.method === "POST") return Promise.resolve({ ok: true, status: 200, json: async () => ({}) });
  return new Promise(resolve => waiting.push(() => resolve({ ok: true, status: 200, json: async () => JSON.parse(state) })));
};
const settle = async () => { for (let i = 0; i < 10; i++) await new Promise(resolve => setImmediate(resolve)); };

(async () => {
  vm.runInThisContext(fs.readFileSync(appPath, "utf8"), { filename: appPath });
  await settle();
  element("#scanBtn").handlers.click();
  await settle();
  const answers = waiting.splice(0);
  for (const answer of answers) {
    answer();
    await settle();
  }
  process.stdout.write(JSON.stringify({ requests: answers.length, log: element("#log").innerHTML }));
  process.exit(0);
})().catch(err => {
  console.error(err && err.stack ? err.stack : err);
  process.exit(1);
});
