/* eslint-env node */

// Vitest configuration for the Trawlarr frontend.
//
// The app itself is built by Quasar's *webpack* CLI (@quasar/app-webpack), so
// this file exists purely for the test runner -- it never participates in the
// production build. Vitest brings its own Vite pipeline, which means the
// aliases Quasar's webpack config injects (`src/`, `pages/`, `layouts/`, ...)
// have to be restated here. If you add an alias to quasar.conf.js, add it here
// too or the tests will fail to resolve the import.

const path = require('path')
const vue = require('@vitejs/plugin-vue')
const { defineConfig } = require('vitest/config')

const src = path.resolve(__dirname, 'src')

module.exports = defineConfig({
  plugins: [vue()],
  resolve: {
    // Webpack resolves extensionless `.vue` imports; Vite does not by default,
    // and the app is full of `import Foo from "components/Foo"`.
    extensions: ['.mjs', '.js', '.json', '.vue'],
    alias: {
      // Quasar publishes an SSR ("node" condition) and a client build. Vitest
      // resolves with node conditions, which would hand us the SSR bundle --
      // that one has no LocalStorage, no $q.dark, and no DOM plugins, so every
      // browser-behaviour test would quietly test the wrong module. Pin the
      // client build explicitly.
      quasar: path.resolve(__dirname, 'node_modules/quasar/dist/quasar.client.js'),
      // Mirrors quasar.conf.js / jsconfig.json.
      src,
      app: __dirname,
      components: path.join(src, 'components'),
      layouts: path.join(src, 'layouts'),
      pages: path.join(src, 'pages'),
      assets: path.join(src, 'assets'),
      boot: path.join(src, 'boot'),
      stores: path.join(src, 'stores')
    }
  },
  test: {
    globals: true,
    environment: 'jsdom',
    include: ['test/unit/**/*.spec.js'],
    // Order matters: the storage polyfill must land before any module that
    // captures window.localStorage at import time.
    setupFiles: ['test/storage-polyfill.js', 'test/setup.js'],
    // Style blocks are irrelevant to behaviour and pulling the Quasar SCSS
    // variables through a Vite pipeline that the app never uses would only
    // create a second, divergent build. Leave CSS unprocessed.
    css: false,
    coverage: {
      provider: 'v8',
      reporter: ['text', 'json-summary', 'lcov'],
      include: ['src/**/*.{js,vue}'],
      // The ratchet. Each floor sits just under the number measured when it was
      // last raised; raise it as coverage rises, never lower it to turn a red
      // build green. See docs/CONTRIBUTING.md.
      //
      // Read `functions` as the honest signal. The statement/line figures are
      // flattered by the router spec, which imports every page in the app: v8
      // marks a module's top level covered merely for having been evaluated, so
      // ~80% of statements are "covered" by import alone with no assertion
      // behind them. Functions only count once something calls them.
      thresholds: {
        statements: 78,
        lines: 78,
        branches: 88,
        functions: 20
      }
    }
  }
})
