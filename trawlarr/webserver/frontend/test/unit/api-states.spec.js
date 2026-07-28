// Characterisation of what the API layer does when the backend answers with
// nothing, or does not answer at all.
//
// Every function in unmanicGlobals returns a promise that some component
// `.then()`s. A path that neither resolves nor rejects leaves that component
// stuck on its initial state forever with no error anywhere -- the exact shape
// of failure this milestone is about. These tests pin down which paths settle.

import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('axios', () => {
  const axios = vi.fn()
  return { default: axios, axios }
})

// Identity translator: the real $t comes from vue-i18n and returns the key when
// a string is missing, which is the branch the notification code special-cases.
const $t = (key) => key

async function loadGlobals() {
  vi.resetModules()
  const axios = (await import('axios')).default
  axios.mockReset()
  const mod = await import('src/js/unmanicGlobals')
  return { axios, globals: mod.default }
}

describe('notification list', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
  })

  it('reports an empty list when the server has no notifications', async () => {
    const { axios, globals } = await loadGlobals()
    axios.mockResolvedValue({ data: { notifications: [] } })

    await expect(globals.updateUnmanicNotifications($t)).resolves.toEqual([])
    expect(globals.getUnmanicNotifications()).toEqual([])
  })

  it('starts empty rather than undefined before anything has been fetched', async () => {
    const { globals } = await loadGlobals()
    // Callers iterate this synchronously on first render. `undefined` here
    // would throw inside a template, which Vue swallows into a blank drawer.
    expect(globals.getUnmanicNotifications()).toEqual([])
  })

  it('maps server severities onto the colours the drawer renders', async () => {
    const { axios, globals } = await loadGlobals()
    axios.mockResolvedValue({
      data: {
        notifications: [
          { uuid: '1', type: 'error', label: 'a', message: 'b', icon: 'i', navigation: {} },
          { uuid: '2', type: 'warning', label: 'a', message: 'b', icon: 'i', navigation: {} },
          { uuid: '3', type: 'success', label: 'a', message: 'b', icon: 'i', navigation: {} },
          { uuid: '4', type: 'anything-else', label: 'a', message: 'b', icon: 'i', navigation: {} }
        ]
      }
    })

    const list = await globals.updateUnmanicNotifications($t)
    // An error that renders in the same neutral blue as an informational
    // message is an error nobody notices.
    expect(list.map((n) => n.color)).toEqual(['negative', 'warning', 'positive', 'info'])
  })

  it('falls back to the raw server text when no translation exists', async () => {
    const { axios, globals } = await loadGlobals()
    axios.mockResolvedValue({
      data: {
        notifications: [
          { uuid: '1', type: 'info', label: 'someUntranslatedLabel', message: 'someUntranslatedMessage', icon: 'i', navigation: {} }
        ]
      }
    })

    const [notification] = await globals.updateUnmanicNotifications($t)
    // $t returns the key unchanged for a missing string. Showing
    // "notifications.serverNotificationLabels.someUntranslatedLabel" to a user
    // is worse than showing the untranslated server text.
    expect(notification.label).toBe('someUntranslatedLabel')
    expect(notification.message).toBe('someUntranslatedMessage')
  })

  it('settles, and keeps the last known list, when the request fails', async () => {
    const { axios, globals } = await loadGlobals()

    axios.mockResolvedValueOnce({
      data: { notifications: [{ uuid: '1', type: 'error', label: 'a', message: 'b', icon: 'i', navigation: {} }] }
    })
    await globals.updateUnmanicNotifications($t)

    vi.spyOn(console, 'error').mockImplementation(() => {})
    axios.mockRejectedValueOnce(new Error('backend down'))

    // The promise must settle -- a hanging one would freeze the polling loop
    // in DrawerNotifications with no console output at all.
    const list = await globals.updateUnmanicNotifications($t)
    expect(list.map((n) => n.uuid)).toEqual(['1'])
    // ...and it must say something. Silent staleness is the failure mode.
    expect(console.error).toHaveBeenCalled()
  })
})

describe('session state', () => {
  it('rejects when the session cannot be read, instead of hanging', async () => {
    const { axios, globals } = await loadGlobals()
    axios.mockRejectedValue(new Error('backend down'))

    await expect(globals.getUnmanicSession()).rejects.toBeDefined()
  })

  it('caches a successful session and does not re-request it', async () => {
    const { axios, globals } = await loadGlobals()
    axios.mockResolvedValue({
      data: { created: 1, email: 'a@b.c', level: 1, name: 'n', picture_uri: 'p', uuid: 'u' }
    })

    const first = await globals.getUnmanicSession()
    const second = await globals.getUnmanicSession()
    expect(second).toEqual(first)
    expect(axios).toHaveBeenCalledTimes(1)
  })
})

describe('version lookup', () => {
  it('resolves the version and caches it', async () => {
    const { axios, globals } = await loadGlobals()
    axios.mockResolvedValue({ data: { version: '1.2.3' } })

    await expect(globals.getUnmanicVersion()).resolves.toBe('1.2.3')
    await expect(globals.getUnmanicVersion()).resolves.toBe('1.2.3')
    expect(axios).toHaveBeenCalledTimes(1)
  })

  it('rejects when the version endpoint fails, instead of never settling', async () => {
    const { axios, globals } = await loadGlobals()
    axios.mockRejectedValue(new Error('backend down'))

    // Before this was fixed the promise had no rejection handler at all, so
    // FooterData's `.then()` simply never ran: the footer showed its
    // placeholder forever and nothing was logged anywhere.
    await expect(globals.getUnmanicVersion()).rejects.toBeDefined()
  })
})

describe('privacy policy document', () => {
  it('joins the served content lines into one document', async () => {
    const { axios, globals } = await loadGlobals()
    axios.mockResolvedValue({ data: { content: ['# Privacy\n', 'body'] } })

    await expect(globals.getUnmanicPrivacyPolicy()).resolves.toBe('# Privacy\nbody')
  })

  it('rejects rather than rendering an empty policy when the fetch fails', async () => {
    const { axios, globals } = await loadGlobals()
    axios.mockRejectedValue(new Error('backend down'))

    // A privacy policy dialog that silently renders blank is worse than one
    // that shows an error.
    await expect(globals.getUnmanicPrivacyPolicy()).rejects.toBeDefined()
  })
})
