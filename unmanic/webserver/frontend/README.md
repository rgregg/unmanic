# Trawlarr web frontend

This directory contains the Vue 3 and Quasar 2 frontend bundled with
the Trawlarr Python backend. It is part of the monorepo, not a Git
submodule. Internal routes and source identifiers may retain the
`unmanic` name for engine and plugin compatibility.

## Setup

Use Node 22 for parity with the Dockerfile and CI. `package.json`
also declares the supported Node/npm ranges. From this directory:

```bash
npm ci
```

The project-local Quasar CLI is invoked by npm scripts; no global
`@quasar/cli` installation is required.

## Development

Start a backend on port 8888, then run the frontend dev server on
port 8889:

```bash
cp .env.example .env
npm run dev
```

Open <http://localhost:8889/unmanic/ui/dashboard>. `UNMANIC_BACKEND_URL` in
`.env` changes the backend target. The Quasar dev server proxies the
API, panel, Swagger, and WebSocket routes, so use the frontend URL
rather than opening generated files directly.

Available scripts:

```bash
npm run dev            # Quasar development server
npm run serve          # development server with Quasar debug output
npm run lint           # ESLint for JavaScript and Vue files
npm run format         # Prettier write pass
npm test               # current no-test placeholder
npm run build          # production SPA in dist/spa
npm run build:publish  # packaging build used by setup.py
```

For a complete application package, run the wheel build from the
repository root. `setup.py` runs `npm ci` and `npm run build:publish`,
then moves `dist/spa` into the Python package's
`unmanic/webserver/public` assets. See the
[container build guide](../../../docker/README.md#build-the-current-monorepo).

## Contributing

Read [`AGENTS.md`](AGENTS.md) before editing. In brief: follow the
existing Vue/Quasar structure; use Composition API with
`<script setup>` for new components and major refactors; localize all
user-facing text; preserve dark-theme, responsive, and flat-design
conventions; reuse shared UI components; and run `npm run lint` plus
`npm run build` for frontend changes. Repository-wide contribution
guidance is in [`../../../docs/CONTRIBUTING.md`](../../../docs/CONTRIBUTING.md).

Trawlarr is GPL-3.0; see the repository [`LICENSE`](../../../LICENSE).
