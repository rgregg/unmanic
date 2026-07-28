// Deterministic Web Storage, installed before anything else in the test run.
//
// This file deliberately has no imports. Quasar's LocalStorage plugin captures
// `window.localStorage` when its module is evaluated, so the replacement has to
// be in place before `import { Quasar } from 'quasar'` runs anywhere -- hence a
// separate, import-free setup file listed first in vitest.config.js.
//
// Why replace it at all: what jsdom hands back depends on the Node version
// running the suite. On Node 22 it is a real Storage; on Node 25, which ships
// its own experimental localStorage global, it arrives as a bare `{}` with no
// getItem/setItem. Quasar feature-detects and degrades to a silent no-op in
// that case, so a persistence test would genuinely persist on CI and assert
// against nothing on a developer's machine -- passing green in both places
// while only one of them tested anything.

function createStorage() {
  const data = new Map()
  return {
    get length() {
      return data.size
    },
    key(i) {
      const keys = Array.from(data.keys())
      return i < keys.length ? keys[i] : null
    },
    getItem(k) {
      return data.has(String(k)) ? data.get(String(k)) : null
    },
    setItem(k, v) {
      data.set(String(k), String(v))
    },
    removeItem(k) {
      data.delete(String(k))
    },
    clear() {
      data.clear()
    }
  }
}

for (const name of ['localStorage', 'sessionStorage']) {
  const storage = createStorage()
  Object.defineProperty(window, name, { value: storage, configurable: true, writable: true })
  Object.defineProperty(globalThis, name, { value: storage, configurable: true, writable: true })
}
