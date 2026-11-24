# Demo site

https://mivanit.github.io/horatio/ runs the real dashboard (`index.html` and `static/`
from `main`) against a fake backend in the browser, for an invented homelab box
(16 threads, two GPUs, an NVMe root, a RAID1 pair and a backup drive). Nothing
talks to a server: `demo/fake-api.js` replaces `fetch()` and answers every
`/api/*` and `/config.json` request. The values are smooth noise over wall-clock
time, so live charts move and the history charts show the last 30 days of the same
series (a training job on GPU 0 in multi-hour stretches, a nightly backup at 02:00).
The settings dialog loads but refuses to save.

- `demo/fake-api.js`: the fake backend. Its responses follow the shapes in
  `python/server.py`; a new endpoint in the frontend needs a route here, or
  `tests/frontend/test_demo.py` fails.
- `build-demo.sh [outdir]`: copies `index.html`, `static/` and `demo/` into
  `outdir` (default `_site`), fetches Chart.js at the version `install.sh` pins,
  makes the asset paths relative and loads `fake-api.js` before the frontend.

Run it locally with `make demo`: it writes gitignored copies of `index.html`,
`static/` and Chart.js into `docs/` (`make demo-docs` alone does just that) and
serves `docs/` at http://localhost:8000. On every push to `main`,
`.github/workflows/pages.yml` runs the checks (`check.yml`) and, once they pass,
builds and deploys it. That requires repo Settings →
Pages → Source set to "GitHub Actions".
