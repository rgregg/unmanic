# Trawlarr Web frontend

A simple tool for optimising your file library to a single, uniform format.

This directory contains the frontend user interface for Trawlarr, inherited
from [Unmanic](https://github.com/Unmanic/unmanic) via `git subtree`. It is a
regular part of this repository, not a submodule.

The backend rename (`unmanic` -> `trawlarr`, issue #49) moved the API to
`/trawlarr/api/v2/`. `src/js/unmanicGlobals.js` follows it: `urlPrefix` is
`/trawlarr` and the builder is `getTrawlarrApiUrl()`. File names, the
`$unmanic` global and the `Unmanic*` components were **not** renamed and are
correct as they stand.


---


## Install the dependencies
```bash
npm install -g @quasar/cli

npm install
```


## Development

### Start the app in development mode (hot-code reloading, error reporting, etc.)
```bash
quasar dev
#or
npm run serve
```

### Lint the files
```bash
npm run lint
```

### Build the app for production
```bash
quasar build
```


## License and Contribution

This projected is licensed under th GPL version 3.

Copyright (C) Josh Sunnex - All Rights Reserved

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

This project contains libraries imported from external authors.
Please refer to the source of these libraries for more information on their respective licenses.

See [docs/CONTRIBUTING.md](../../../docs/CONTRIBUTING.md) to learn how to
contribute to Trawlarr.
