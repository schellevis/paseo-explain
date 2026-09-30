# Showing the page

Read this when opening, updating or stopping a rendered explanation in Paseo.

1. Run `explain.py show --session S`; keep its `local_url` and optional `public_url`. The service runs from a scratch Paseo project and serves the session on loopback. If Paseo is unavailable, the command gives a local `serve` fallback; do not claim the page opened in Desktop.
2. Open `local_url` with `browser_new_tab`. If the browser reports `browser_no_host` or equivalent, continue to step 5 without a tab.
3. On the same tab, evaluate `() => !!document.querySelector('meta[name="paseo-explain"]')`. Retry up to three times over three seconds. When true, run `explain.py tab --session S --opened true`.
4. If false, Desktop may resolve `*.localhost` on a different machine. Run `explain.py inject --session S` and pass its stdout verbatim as the `function` argument to `browser_evaluate` on that tab. Repeat the meta check, then record `explain.py tab --session S --opened true|false`. Tell the user an injected tab empties on reload and can be re-shown.
5. Summarize in the user's language in 3–5 lines at the default reading level. State the honest check label and any flagged-content notice. Give `local_url` and, if present, `public_url` with “open this on your phone”. If no public URL exists, explain once that phone access needs the Paseo daemon's `serviceProxy.publicBaseUrl` and wildcard DNS; offer the Markdown twin. The skill never changes daemon configuration.
6. After an update, reload a proxy-served tab with `browser_reload`; for an injected tab, repeat step 4. Recompute `explain.py result` after SHOW, so callers get the final URLs and tab state.

A public service URL is available only when the daemon is configured to expose it. A loopback URL is local to the relevant Paseo host and may not open on a phone. Leave the service running for later viewing. Use `explain.py stop` only when the user asks to stop it.
