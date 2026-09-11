# Third-Party Licences

Ettok AI is built on open-source software. This file records what it is built on
and under what terms, as those licences require.

## Hermes Agent

Ettok AI is a derivative of **Hermes Agent** by Nous Research
(<https://github.com/NousResearch/hermes-agent>), used under the MIT Licence.

Hermes Agent supplies the agent runtime that Ettok AI is built on: the agent
loop, scheduling, local state and search, model provider adapters, browser
tooling, and the operator interface. Ettok AI adds hate-speech monitoring —
collection, classification against expert-curated knowledge, evidence capture,
and delivery to the Ettok platform.

Internal module names, environment variables and state paths inherited from
Hermes Agent are deliberately unchanged, so that upstream fixes can continue to
be merged. They are implementation detail and do not indicate authorship of the
surrounding work.

```
MIT License

Copyright (c) 2025 Nous Research

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

The upstream licence is also retained verbatim at [`LICENSE`](LICENSE).

## Trademarks

"Hermes Agent" and "Nous Research" are names of their respective owners. The MIT
Licence grants no trademark rights, and their appearance here is attribution
only — it does not imply that Nous Research endorses, sponsors or is affiliated
with Ettok AI.

## Python and JavaScript dependencies

Ettok AI installs third-party packages declared in [`pyproject.toml`](pyproject.toml)
and [`package.json`](package.json), each under its own licence. To regenerate a
full dependency licence inventory:

```
uv pip install pip-licenses && pip-licenses --format=markdown --with-urls
npx license-checker --summary
```

Run this before any public release and check the output for copyleft terms
incompatible with your intended distribution.
