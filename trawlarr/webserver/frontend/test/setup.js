// Global test setup, loaded by vitest.config.js before every spec file.
// test/storage-polyfill.js runs first -- see the comment in that file.

import { config } from '@vue/test-utils'
import * as QuasarExports from 'quasar'
import { Dark, Quasar } from 'quasar'
import { beforeEach } from 'vitest'

// Quasar's CLI auto-imports components at build time. Vitest does not run that
// transform, so `app.use(Quasar)` alone leaves every <q-toggle> in a template
// unresolved -- the component renders as an empty comment node and any
// assertion about the rendered output quietly passes against nothing. Register
// the whole component set explicitly instead.
const components = {}
const directives = {}
for (const [name, value] of Object.entries(QuasarExports)) {
  if (/^Q[A-Z]/.test(name)) components[name] = value
  else if (/^(Close|Intersection|Morph|Mutation|Ripple|Scroll|ScrollFire|TouchHold|TouchPan|TouchRepeat|TouchSwipe)/.test(name) && value?.mounted) {
    directives[name] = value
  }
}

config.global.plugins = [[Quasar, { components, directives }]]

beforeEach(() => {
  // Storage and dark mode are both process-global. Leftover state from an
  // earlier spec would let a persistence assertion pass without the code under
  // test having written anything.
  window.localStorage.clear()
  window.sessionStorage.clear()
  Dark.set(false)
})
