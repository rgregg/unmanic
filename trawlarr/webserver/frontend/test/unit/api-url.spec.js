// The frontend talks to the backend through exactly one URL builder. If that
// builder drifts from the backend's URL_PREFIX, every request 404s at runtime
// and nothing in the build catches it -- the app compiles perfectly and then
// shows empty panels. That is the failure mode this milestone exists to stop,
// so the prefix is asserted literally rather than derived from the source.

import { beforeEach, describe, expect, it, vi } from 'vitest'

describe('backend URL construction', () => {
  beforeEach(() => {
    // getTrawlarrApiUrl memoises the base URL in module scope, so each test
    // needs a fresh copy of the module or it would assert against the first
    // test's cached value.
    vi.resetModules()
  })

  it('serves every API call from under /trawlarr, never /unmanic', async () => {
    const { urlPrefix, getTrawlarrApiUrl } = await import('src/js/unmanicGlobals')

    // Must match URL_PREFIX in trawlarr/libs/runtimepaths.py and
    // build.publicPath in quasar.conf.js.
    expect(urlPrefix).toBe('/trawlarr')

    const url = getTrawlarrApiUrl('v2', 'version/read')
    expect(url).toBe('http://localhost:3000/trawlarr/api/v2/version/read')
    expect(url).not.toContain('/unmanic')
  })

  it('builds against the origin the page was served from', async () => {
    const { getUnmanicServerUrl, getTrawlarrApiUrl } = await import('src/js/unmanicGlobals')

    // Reverse-proxied installs are the normal deployment. A hard-coded
    // localhost would work on the developer's machine and break for everyone
    // running behind a proxy, which is precisely the kind of bug that only
    // shows up in someone else's logs.
    expect(getUnmanicServerUrl()).toBe(window.location.protocol + '//' + window.location.host)
    expect(getTrawlarrApiUrl('v2', 'session/state').startsWith(getUnmanicServerUrl())).toBe(true)
  })

  it('does not swallow the endpoint path segments', async () => {
    const { getTrawlarrApiUrl } = await import('src/js/unmanicGlobals')

    // Endpoints with a slash in them ('notifications/read') are the common
    // case; a builder that URL-encoded or trimmed them would still return a
    // plausible-looking string.
    expect(getTrawlarrApiUrl('v2', 'notifications/read')).toMatch(
      /\/trawlarr\/api\/v2\/notifications\/read$/
    )
    expect(getTrawlarrApiUrl('v1', 'session/logout')).toMatch(/\/trawlarr\/api\/v1\/session\/logout$/)
  })
})
