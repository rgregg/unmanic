// Routing is the one place where a typo produces a blank page instead of an
// error: an unresolvable lazy import inside a route definition compiles fine
// and only fails when a user navigates there. These tests navigate the real
// route table and force every lazy component to actually load.

import { describe, expect, it } from 'vitest'
import { createRouter, createWebHistory } from 'vue-router'
import routes from 'src/router/routes'

function newRouter() {
  return createRouter({ history: createWebHistory(), routes })
}

describe('route table', () => {
  it('resolves the settings pages that the nav drawer links to', async () => {
    const router = newRouter()
    for (const path of ['/ui/settings-library', '/ui/settings-workers', '/ui/settings-plugins']) {
      const resolved = router.resolve(path)
      expect(resolved.matched.length, `${path} matched no route`).toBeGreaterThan(0)
      // A path that fell through to the catch-all still "resolves" -- it just
      // renders the 404 page. Assert we did not land there.
      expect(resolved.matched[0].path, `${path} fell through to the catch-all`).toBe(path)
    }
  })

  it('keeps the named routes other code navigates by name', () => {
    const router = newRouter()
    // MainLayout and the login redirect push by name, not by path. Renaming a
    // route silently breaks those callers.
    expect(router.resolve({ name: 'dashboard' }).path).toBe('/ui/dashboard')
    expect(router.resolve({ name: 'data-panels' }).path).toBe('/ui/data-panels')
    // The OAuth round-trip in unmanicGlobals.login() hard-codes this URL.
    expect(router.resolve({ name: 'trigger' }).path).toBe('/ui/trigger')
  })

  it('sends unknown paths to the 404 page rather than a blank layout', async () => {
    const router = newRouter()
    const resolved = router.resolve('/ui/this-page-does-not-exist')
    expect(resolved.matched).toHaveLength(1)
    const loader = resolved.matched[0].components.default
    const mod = await loader()
    expect((mod.default ?? mod).name ?? '').toMatch(/ErrorNotFound/i)
  })

  it('keeps the catch-all last so it cannot shadow a real page', () => {
    // vue-router matches by specificity, not order, but a catch-all placed
    // mid-table is a maintenance trap and previous edits to this file appended
    // new routes to the end.
    const catchAllIndex = routes.findIndex((r) => r.path.includes(':catchAll'))
    expect(catchAllIndex).toBe(routes.length - 1)
  })

  it('loads every lazily-imported route component', async () => {
    // This is the test that earns its keep: a broken import inside a .vue file
    // anywhere in the layout/page tree currently surfaces only when the Docker
    // image builds. Awaiting each loader executes the whole import graph.
    const loaders = []
    const collect = (list) => {
      for (const route of list) {
        if (typeof route.component === 'function') loaders.push([route.path, route.component])
        if (route.children) collect(route.children)
      }
    }
    collect(routes)

    expect(loaders.length).toBeGreaterThanOrEqual(8)
    for (const [path, loader] of loaders) {
      const mod = await loader()
      expect(mod.default ?? mod, `route ${path} did not export a component`).toBeTruthy()
    }
  })
})
