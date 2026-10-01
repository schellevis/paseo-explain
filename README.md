# paseo-explain

**Status: early draft.** Paseo Explain turns a plan, specification, or idea into a source-linked explanation at a chosen reading level. It creates a page inside Paseo with an overview diagram, a step walkthrough, what changes, decisions, risks, coverage where applicable, and clickable evidence. A Markdown twin is available when the page cannot be opened.

## Examples

- `/paseo-explain garden-booking-plan.md` — explain a community-garden booking plan.
- `/paseo-explain "Neighbours could lend tools from a shared shelf"` — explore a neighbourhood tool-lending idea.
- `/paseo-explain research-data-pipeline-spec.md --level 4` — explain a research data-pipeline specification.
- `/paseo-explain shop-inventory-plan.md --deep` — explain a small-shop inventory plan with a reader test and fact-check.

Use `--quick` for validation alone, standard mode for an independent fact-check, or `--deep` for a reader test followed by a fact-check. `--no-delegate` forces quick behavior. Check labels reflect the checks that actually ran; a correction applied after fact-checking is labelled as such.

## Requirements and installation

Python 3.10+ and Paseo agent tools or the `paseo` CLI are required. Paseo Desktop can show the page in a browser tab. A second model family is recommended for independent fact-checking. The scripts use Python's standard library; there is no Python package install step.

Copy or symlink the `paseo-explain/` directory into a skills directory such as `~/.agents/skills/`, `~/.claude/skills/`, or `~/.codex/skills/`. The skill invokes `python3` on its `scripts/explain.py` entry point. Use `python3 paseo-explain/scripts/explain.py --help` to inspect the CLI from a checkout.

## Viewing and reading levels

By default, the skill starts a loopback service through Paseo and opens the page in a Desktop tab. If the tab cannot reach the service through the proxy, it can inject the page into that tab; reload then clears the injected page, and the skill can re-show it. Other clients receive a local URL and can use the Markdown twin.

One phone-access option uses the Paseo daemon's `serviceProxy.publicBaseUrl` and wildcard DNS. For example, a generic base `https://paseo.example.org` can expose services at `https://explain--<project>.paseo.example.org`. Paseo Explain uses the daemon's public URL when available; the skill never edits daemon settings.

### Reaching the page from other devices

For direct serving from a remote or Docker daemon, publish port 8300 in your container or firewall, then set `serve_host`, `serve_port` to 8300, and `public_base_url` in the Paseo Explain config file:

```json
{"serve_host": "0.0.0.0", "serve_port": 8300, "public_base_url": "http://my-host:8300"}
```

Port 8300 is the recommended port for direct serving. `serve_host` chooses the bind address, `serve_port` fixes the service port, and `public_base_url` supplies the URL opened first in Desktop and returned for other devices. The read-only pages are reachable by anyone who can reach that port. The default service remains bound to loopback.

Written levels are 1 Simple (B1), 2 Accessible, 3 Mixed, 4 Technical and 5 Expert. The default output writes levels 1, 3 and 5; `--levels 5` writes all five. `--level` selects the initial level. Preferences may be stored at `$XDG_CONFIG_HOME/paseo-explain/config.json`, or `~/.config/paseo-explain/config.json` when that variable is unset, with optional `reading_level`, `levels`, `theme`, and `lang` keys. The skill reads this file but never writes it.

## Autopilot integration

A calling skill can invoke `/paseo-explain --autopilot <run-dir> --doc spec|plan --unattended`. The run directory is read-only input. The output is a page plus a versioned `result.json` with URLs, check states and findings; a stable slug keeps the URL across revisions. `--out <dir>` exports copies of the HTML, Markdown and result while the session remains in its normal root. See the [integration contract](paseo-explain/references/integration.md).

## Privacy

The scripts make no external requests. The page loads Mermaid from jsDelivr in the reader's browser, so that browser may contact the CDN. Explained material is sent to the model providers used by the orchestrator, fact-checker and reader through their prompts and file reads. The page reaches Desktop or a phone through the Paseo daemon's own connection. Review source material and provider choices before using the skill with sensitive content.

See [third-party licences](THIRD_PARTY_LICENSES.md) for visual credits and licence text.
