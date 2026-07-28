// First-run state and settings persistence.
//
// Theme and language are the two settings the UI stores client-side. Both are
// read once at boot and written by a switch component, so a break in either
// direction is invisible: the app renders, it just quietly ignores what the
// user chose last time.

import { mount } from '@vue/test-utils'
import { LocalStorage } from 'quasar'
import { describe, expect, it } from 'vitest'
import { createI18n } from 'vue-i18n'

import App from 'src/App.vue'
import LanguageSwitch from 'components/LanguageSwitch.vue'
import ThemeSwitch from 'components/ThemeSwitch.vue'

function mountApp() {
  return mount(App, {
    global: { stubs: { 'router-view': true } }
  })
}

describe('theme persistence', () => {
  it('defaults to light on a first run, with nothing stored', () => {
    // No 'theme' key exists yet. The failure being guarded against is a boot
    // path that throws on the missing key -- App.vue is the root component, so
    // a throw here is a blank page, not a stack trace anyone sees.
    expect(LocalStorage.getItem('theme')).toBeNull()

    const wrapper = mountApp()
    expect(wrapper.vm.$q.dark.isActive).toBe(false)
  })

  it('restores a previously saved dark theme at boot', () => {
    LocalStorage.set('theme', 'dark')

    const wrapper = mountApp()
    expect(wrapper.vm.$q.dark.isActive).toBe(true)
  })

  it('writes the choice to local storage when the switch is clicked', async () => {
    const wrapper = mount(ThemeSwitch)

    // Click the rendered control rather than poking the ref: this also proves
    // the q-toggle is really mounted and bound to the model, which is the part
    // that breaks when a template is refactored.
    const toggle = wrapper.find('.q-toggle')
    expect(toggle.exists()).toBe(true)
    await toggle.trigger('click')
    await wrapper.vm.$nextTick()
    expect(LocalStorage.getItem('theme')).toBe('dark')
    expect(wrapper.vm.$q.dark.isActive).toBe(true)

    // Switching back must store 'light' explicitly. Removing the key instead
    // would work by accident today (absent reads as light) and break the
    // moment the default changes.
    await toggle.trigger('click')
    await wrapper.vm.$nextTick()
    expect(LocalStorage.getItem('theme')).toBe('light')
    expect(wrapper.vm.$q.dark.isActive).toBe(false)
  })

  it('round-trips: toggle to dark, reboot, still dark', async () => {
    const toggle = mount(ThemeSwitch)
    toggle.vm.unmanicDarkMode = true
    await toggle.vm.$nextTick()
    toggle.unmount()

    // Quasar's dark state is global, so prove the *stored* value is what drives
    // the next boot rather than leftover in-memory state.
    const app = mountApp()
    expect(app.vm.$q.dark.isActive).toBe(true)
  })
})

describe('language persistence', () => {
  const i18n = () =>
    createI18n({
      legacy: false,
      locale: 'en',
      fallbackLocale: 'en',
      messages: { en: { buttons: { language: 'Language' } }, fr: { buttons: { language: 'Langue' } } }
    })

  it('stores the selected locale under the key the i18n boot file reads', async () => {
    const wrapper = mount(LanguageSwitch, { global: { plugins: [i18n()] } })

    wrapper.vm.locale = 'fr'
    await wrapper.vm.$nextTick()

    // src/boot/i18n.js reads exactly this key. If either side is renamed the
    // language selector appears to work until the next page load.
    expect(LocalStorage.getItem('locale')).toBe('fr')
  })

  it('offers only locales that ship with a translation file', async () => {
    const wrapper = mount(LanguageSwitch, { global: { plugins: [i18n()] } })
    const offered = wrapper.vm.localeOptions.map((o) => o.value)

    const shipped = Object.keys(
      import.meta.glob('/src/language/*.json')
    ).map((p) => p.split('/').pop().replace('.json', ''))

    expect(shipped.length).toBeGreaterThan(0)
    // Offering a locale with no file gives the user a language switch that
    // silently falls back to English -- it looks like it worked.
    for (const value of offered) {
      expect(shipped, `no src/language/${value}.json for offered locale`).toContain(value)
    }
  })
})
