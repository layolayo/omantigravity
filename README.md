# Omantigravity — Antigravity CLI Usage & Quota Plugin for Omarchy

An Omarchy shell bar widget and popup panel plugin that monitors and displays **Google Antigravity CLI** (`agy`) limits, 5-hour rolling windows, weekly quotas, and active model status in real time.

> **Disclaimer:** *This project is an unofficial community plugin for Omarchy. It is not developed by, endorsed by, affiliated with, or in any way officially connected to [Google LLC](https://google.com), [Anthropic PBC](https://anthropic.com), [OpenAI](https://openai.com), or their respective subsidiaries. All product names, logos, brands, trademarks, and registered trademarks (including Google, Google Antigravity, Gemini, Anthropic, Claude, OpenAI, and GPT) are the property of their respective owners and are used solely for identification, reference, and interoperability purposes.*

<p align="center">
  <img src="preview.png" alt="Omantigravity Preview" />
</p>

## Benefits & Features

- **At-a-Glance Quota in Your Bar**: Real-time remaining quota displayed directly on your status bar (`λ 86%`). Automatically turns urgent red with an alert glyph (`󰀨`) when quota is low.
- **Interactive Metric Selector**: Minimalist rectangular chips in the panel to select which metric the bar tracks:
  - **Gemini**: Shows the most constrained limit for Gemini models (Flash, Pro).
  - **Claude & GPT**: Shows the most constrained limit for third-party models (Opus, Sonnet, GPT-OSS).
  - **Lowest**: Dynamically tracks the absolute lowest limit across all groups and windows.
- **Pin Any Specific Limit**: Click any individual 5-hour or weekly progress bar in the panel to pin that exact limit to the status bar (indicated by a clean `󰄬 On bar` badge).
- **Customizable Alert System**:
  - Configurable alert threshold percentage (default: **20%** remaining).
  - Prominent in-panel alert banner highlighting critical quotas and their exact reset countdown.
  - Native desktop notifications via `notify-send` when limits drop to or below your threshold.
  - In-panel quick selectors (`[10%]`, `[15%]`, `[20%]`, `[25%]`, `[30%]`) to set your threshold. Toggle notifications on/off entirely via the `enableNotifications` setting.
- **Antigravity Branding & Active Model**:
  - Displays the clean Lambda (`λ`) glyph, current active model, and reasoning effort tier (e.g. `Gemini 3.8 Flash · Reasoning: Medium`).
- **Compact Non-Scroll Design**: Fully fitted layout tailored to Omarchy's design language (`Style.cornerRadius`, no scrollbars).
- **Instant Launch via Local Cache**: Loads immediately from local cache (`~/.cache/omarchy/antigravity-usage.json`) without lag, refreshing fresh data in the background.
- **Automatic Background Refresh**: Quota data refreshes on its own on a configurable interval (`pollIntervalSec`, default 300s) and whenever you open the panel — no manual refresh needed. Press <kbd>Esc</kbd> to close the panel.

---

## Requirements

Before using the plugin, ensure the following dependencies and tools are available on your system:

- **Omarchy Linux**: Quickshell-powered desktop shell with third-party plugin support.
- **Google Antigravity CLI (`agy`)**:
  - The `agy` executable must be installed somewhere on disk, and you need to know its **full path** (e.g. `~/.local/share/mise/shims/agy`).
  - You must have logged in / authenticated at least once so `agy` can query your usage limits.
- **Python 3**:
  - `python3` (3.8+) for running the background usage fetcher and cache engine (`scripts/fetch_usage.py`). Only uses Python standard library modules; no external `pip` dependencies are needed.
- **Desktop Notifications** *(Optional)*:
  - `libnotify` (`notify-send`) for system notification alerts when quota drops below your configured threshold.
- **Nerd Font**:
  - Any Nerd Font (e.g. `JetBrainsMono Nerd Font`, default in Omarchy) for iconography and status indicators.

---

## Installation

Add and enable the plugin directly in Omarchy using the official plugin manager:

```bash
omarchy plugin add https://github.com/asdfsnlr/omantigravity.git --enable
```

If you wish to position it in a specific bar section (e.g. `right`):

```bash
omarchy plugin enable asdfsnlr.omantigravity --section right
```

To update the plugin to the latest version at any time:

```bash
omarchy plugin update asdfsnlr.omantigravity
```

### Required: point the plugin at your `agy` binary

For your safety, this plugin **never** auto-discovers or guesses the location of the `agy`
executable — it will not search your `PATH` or probe common install directories. Until you
tell it exactly where `agy` lives, the widget shows an "Antigravity CLI path not configured"
error instead of running anything.

Find the full path to your `agy` binary (e.g. `which agy` or `readlink -f "$(which agy)"`),
then set it once via the `agyPath` setting:

```bash
omarchy bar set asdfsnlr.omantigravity agyPath "/home/YOUR_USER/.local/share/mise/shims/agy" --json
```

You can also set it from the panel's settings form, wherever Omarchy exposes per-widget
settings for bar plugins. The widget starts querying `agy` as soon as a valid path is saved.

---

## Configuration Options

Settings can be toggled directly in the panel UI, configured via `omarchy bar set`, or specified in `~/.config/omarchy/shell.json`:

| Option | Type | Default | Description |
| --- | --- | --- | --- |
| `agyPath` | string | `""` | **Required.** Full path to your `agy` binary. Never auto-discovered — the widget errors until this is set. |
| `barMetric` | enum | `"gemini"` | Quota limit to show on the bar: `gemini`, `3p`, `lowest`, `gemini-5h`, `gemini-weekly`, `3p-5h`, `3p-weekly` |
| `alertThresholdPct` | integer | `20` | Threshold percentage (5% – 50%) for low quota alerts |
| `enableNotifications` | boolean | `true` | Send desktop notifications via `notify-send` when quota is critical |
| `enableNetworkHealth` | boolean | `false` | Enable live ICMP ping and HTTPS TTFB probes to `daily-cloudcode-pa.googleapis.com`. When disabled (default), latency is derived solely from local CLI turn logs (zero unsolicited network beaconing). |
| `showPercentageInBar` | boolean | `true` | Display remaining percentage next to the bar icon |
| `pollIntervalSec` | integer | `300` | Background refresh interval in seconds (30s – 3600s) |
| `barIcon` | string | `"λ"` | Icon glyph displayed on the bar |

### CLI Configuration Examples

```bash
# Point the plugin at your agy binary (required, see above)
omarchy bar set asdfsnlr.omantigravity agyPath "/home/YOUR_USER/.local/share/mise/shims/agy" --json

# Set metric to Gemini models (default)
omarchy bar set asdfsnlr.omantigravity barMetric gemini

# Set metric to Claude and GPT models
omarchy bar set asdfsnlr.omantigravity barMetric 3p

# Set metric to overall lowest remaining quota
omarchy bar set asdfsnlr.omantigravity barMetric lowest

# Pin to a specific window
omarchy bar set asdfsnlr.omantigravity barMetric gemini-5h
omarchy bar set asdfsnlr.omantigravity barMetric gemini-weekly

# Change the critical alert threshold (e.g. to 25%)
omarchy bar set asdfsnlr.omantigravity alertThresholdPct 25 --json

# Toggle desktop notifications
omarchy bar set asdfsnlr.omantigravity enableNotifications false --json

# Toggle active network health probes (ICMP ping & HTTPS TTFB)
omarchy bar set asdfsnlr.omantigravity enableNetworkHealth true --json

# Change poll interval (e.g. every 2 minutes)
omarchy bar set asdfsnlr.omantigravity pollIntervalSec 120 --json
```

---

## Panel Controls & Shortcuts

| Action | Shortcut / Trigger |
| --- | --- |
| **Open / Close Panel** | Left-click bar widget or press <kbd>Esc</kbd> |
| **Force Fresh Refresh** | Right-click / Middle-click bar widget |
| **Toggle Desktop Notifications** | Click `[Notify / Muted]` button in the panel header |
| **Toggle Network Probes** | Click `[PROBES: ON / OFF]` button in the Cloud Service Status card header |
| **Switch Active Group** | Click `[Gemini]`, `[Claude & GPT]`, or `[Lowest]` chips |
| **Pin Specific Limit** | Click any progress bar row in the panel |
| **Set Alert Threshold** | Click `[10%]`, `[15%]`, `[20%]`, `[25%]`, or `[30%]` chips |
| **IPC Controls** | `omarchy-shell omantigravity toggle`, `open`, `close`, `refresh`, `state` |

---

## CLI Script Usage

The backend query engine `scripts/fetch_usage.py` can also be run standalone. Since the
script never auto-discovers `agy`, pass its full path with `--agy-path` (or export
`OMANTIGRAVITY_AGY_PATH` once instead of repeating the flag):

```bash
# Return cached data if recent (<300s), otherwise fetch fresh from agy
./scripts/fetch_usage.py --agy-path ~/.local/share/mise/shims/agy --cached

# Force a fresh real-time fetch from agy
./scripts/fetch_usage.py --agy-path ~/.local/share/mise/shims/agy --force

# Return current cache immediately without waiting (no agy path needed)
./scripts/fetch_usage.py --cached-only

# Or set it once for the session
export OMANTIGRAVITY_AGY_PATH=~/.local/share/mise/shims/agy
./scripts/fetch_usage.py --force
```

### Latency & Health Tracker (`scripts/check_latency.py`)

A diagnostic utility that monitors real-time network ping, HTTPS TTFB, multi-turn AI response durations, and detects upstream HTTP 503 capacity exhaustion errors from Google's endpoint:

```bash
# Run one-shot health and latency diagnostic report
./scripts/check_latency.py

# Output structured JSON for automation or panel integration
./scripts/check_latency.py --json

# Continuously monitor every 10 seconds
./scripts/check_latency.py --watch 10
```

---

## Plugin Management

```bash
# List all discovered plugins and their status
omarchy plugin list

# Validate plugin manifest and schema
omarchy plugin validate ~/.config/omarchy/plugins/asdfsnlr.omantigravity

# Disable plugin from status bar
omarchy plugin disable asdfsnlr.omantigravity

# Remove plugin
omarchy plugin remove asdfsnlr.omantigravity
```

---

## License

MIT
