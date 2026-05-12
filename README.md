# Scalper

Deal monitor that asks Codex web search for configured targets, filters the returned listings, sends ntfy alerts for new qualifying deals, and publishes the latest live matches as a static GitHub Pages site.

## Files

- `scalper.py` - monitor, filters, notifier, and static page generator.
- `deals.json` - public target configuration.
- `secrets.json` - local-only secrets, currently the ntfy topic.
- `public/` - generated GitHub Pages output.

## Run

```bash
./run_scalper.sh
```

Validate config without searching:

```bash
python scalper.py --validate-config
```

Run without notifications or state changes:

```bash
python scalper.py --dry-run
```

## GitHub Pages

The repository publishes generated `public/` output to the `gh-pages` branch. Cron runs with `--publish-pages`, so changed results are committed to `main` and the rendered site is pushed to `gh-pages` automatically.

The public page is:

```text
https://mkoltsov.github.io/scalper/
```
