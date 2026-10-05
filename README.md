# Xray Config Checker Simple

Automatic collection, multi-stage checking and continuous maintenance of a list
of working **Xray** configurations — the “set it and forget it” edition.

The program fetches subscriptions on its own, runs the servers through a funnel
(ping → real core → control traffic), keeps the pool in order and delivers the
result as a single text file.  Nothing has to be configured: all parameters are
created on the first launch, and the window is only needed to view statistics,
add a subscription or set up an external command.

This is a standalone project extracted from the larger “Xray Config Checker”.
The checking core (the `app/` package shipped alongside) is included, so
**there are no third-party dependencies** — all you need is Python 3.8+ with
Tkinter, and PyInstaller to build a single exe.

> **How it was made.** The program was written by an agent (an agentic
> development system) using the **DeepSeek** AI model — this is an AI/LLM
> project.  The code passes the built-in self-tests, but you should still review
> it yourself before relying on it for anything important.

> **Interface language.** Russian and English are supported. Pick the language in
> **Settings → Interface → Interface language**; it takes effect after the
> program restarts.  The Russian documentation is in
> [`README.ru.md`](README.ru.md).

```bash
python simple_main.py                 # window
python simple_main.py --headless      # loop without the GUI
python tests_simple.py                # self-tests (offline)
```

---

## How it works: "set it and forget it"

Checking is multi-level: parsing share links, ping, the real Xray-core /
sing-box core, and control traffic.  Instead of manual server management there
is an **automatic endless loop**, and the result is delivered as a single text
file.

```bash
# graphical interface (main window; tabs "Lists and paste", "Servers",
# "External commands", "Settings", "Journal")
python simple_main.py
python simple_main.py --tray            # minimized to the tray (as on autostart)
pythonw simple_main.py                 # no console window (or double-click simple_main.pyw)
# launching it again does not open a second window — it raises the running one

# headless: the loop in the console (Ctrl+C to stop)
python simple_main.py --headless

# one pass through the funnel and exit
python simple_main.py --once

# add a list from a file to the pool
python simple_main.py --import my_list.txt

# pool statistics
python simple_main.py --stats

# print the pool contents (JSON) and exit — works with a compressed pool.json.gz too
python simple_main.py --pool-dump
```

### What the loop does

1. **Fetches subscriptions** every N hours (6 by default) and adds the new
   configurations to the pool.  Protocols disabled in the settings (for example
   Trojan) are dropped on import.
2. **Stage 1 — ping.** All “new” entries go through a quick DNS/TCP check.  The
   list is immediately cut by tens of percent: such servers do not waste the
   core’s time.
3. **Stage 2 — core.** The survivors are checked by the real core (a genuine
   TLS/Reality/WS/gRPC handshake).  Configurations the core cannot even build
   are marked as “not supported” and are no longer checked.
4. **Stage 3 — traffic.** The best of the best must pass a control HTTP request
   through the tunnel.  Such a server goes straight into the output file.
5. **Scheduled maintenance.** Working servers are pinged once an hour and fully
   re-checked once every 6 hours.  Any failure **demotes** a server back to
   “new” and removes it from the output file; the server goes through the funnel
   again from scratch.

### Defaults — there is nothing to fill in

On the first launch `data/simple/settings.json` is created with every parameter
already set (the interface never shows empty fields), and broken or empty values
are “healed” on every start: the default is substituted instead, so neither the
interface nor the checking loop can break because of an unfilled parameter.

| Parameter | Default value |
| --- | --- |
| Subscription refresh | 6 hours |
| Ping of working servers | 60 minutes |
| Traffic of working servers | 6 hours |
| Consecutive failures before “Not working” | 5 |
| Pause between stage attempts | 15 minutes |
| Delete failed ones automatically | **off** — the server stays in the pool with the status “Not working” |
| “Not working” lifetime | 72 hours: the entry is then removed from the pool automatically (`0` — never) |
| Writing `pool.json` | no more than once every 30 seconds (changes accumulate in memory; `0` — write immediately) |
| Pool compression | `simple_pool_gzip` (on by default): `pool.json.gz` is about 6 times smaller; inspect it with 7-Zip/WinRAR or `python simple_main.py --pool-dump` |
| Do not re-add removed | 12 hours |
| Threads: ping / core | 128 / 8 |
| Batches: ping / core / traffic | 512 / 48 / 24 |
| Timeouts DNS / TCP / core / traffic | 4 / 4 / 12 / 8 s |
| Fallback targets for the control request | on |
| Protocols to import | all except **Trojan** (enable it with a checkbox) |
| Core auto-download | on (without a core, stages 2–3 do not work) |
| Output file | `data/simple/working.txt` |
| Interface language | Russian (switchable to English; applied after a restart) |

### Failure policy

- Each stage failure is counted by its own counter (ping / core / traffic) and is
  retried after a pause (15 minutes by default) rather than immediately —
  failures are “spread out in time”.
- After **5 consecutive failures** on one stage a server is removed and put into
  the “excluded” list: it is not returned from subscriptions for the configured
  time (12 hours by default), so deleted junk is not pulled in again.
- A missing core on the machine is not a server failure: stages 2–3 are simply
  skipped and the entries remain candidates.

### Deleting servers, the “Not working” status and the address blacklist

These are three different things that are easy to confuse:

| What | How it appears | How long it lives |
| --- | --- | --- |
| **“Not working (checked)”** — a pool status | the server has exhausted its attempts (`Failures before removal`, 5 by default) | lives for the `“Not working” lifetime` (72 hours by default), then is removed from the pool automatically; bring it back earlier with “Reset to new” |
| **Automatic “graveyard”** of configurations | the same event, but **only if** the `Delete failed ones automatically` checkbox is on | `Do not re-add removed` (12 hours by default), then it may return from a subscription |
| **Permanent address blacklist** | the **“Add to blacklist (do not check)”** button | forever, until you remove it manually |

---

## The window

The main window has a toolbar, a status bar and five tabs:

- **Lists and paste** — subscriptions and list files; you can paste links or a
  subscription URL here using `Ctrl+V` from anywhere in the window.
- **Servers** — the pool table: state, protocol, endpoint, latency, source and
  the queue of upcoming checks.  Filter by state and sort by any column.
- **External commands** — presets for running your own commands and scripts.
- **Settings** — every parameter, grouped into blocks (Interface, Sources and
  schedule, Retry policy, Output, Cores and control traffic, Window/tray/network,
  Journal).
- **Journal** — the running log; optional writing to `logs/checker.log`.

Useful hotkeys: `Ctrl+V` paste, `Ctrl+C` copy the links of the selected servers,
`Ctrl+A` select all, `Del` delete, `Shift+Del` blacklist, `Insert` enable/disable,
`F2` edit a preset, `F5` one pass, `Ctrl+T` day/night theme, `F1` help.

## External commands

The **External commands** tab runs arbitrary console commands and scripts, but
the application knows nothing about the transport: you describe what to send,
when and where.  Your secrets are safe — the password from the profile never
appears in the command line (it is passed via the `COMMAND_PASSWORD` environment
variable and masked in the log).

- **When**: on list change, on a schedule, manually, on automation start/stop,
  after a check round, on network loss/restore.
- **What**: the file with the links (`{file}`), the JSON config for v2rayN
  (`{json}`), the server count (`{count}`), the file folder (`{out_dir}`), and
  profile fields (`{host}`, `{port}`, `{user}`, `{password}`, `{remote_dir}`).
- **Ready-made examples**: scp/pscp upload to a VPS, `rclone` copy to cloud,
  a private GitHub gist, a Telegram bot, and an atomic upload script
  (`scripts/upload-verify.cmd`, with an offline `--selftest`).

A **hidden** preset masks the command and its output in the interface and the
journal until you explicitly press **Show output**.

## Output files

The format is chosen in Settings → Output:

- `uri` — a plain text list of links (`data/simple/working.txt`);
- `v2rayn` — a ready Xray JSON config for offline import into v2rayN (one entry
  whose `leastPing` balancer picks the fastest working server);
- `both` — both files.

`observatory` in the v2rayN JSON is required: without it `leastPing` does not
work and the core refuses to start.  Hysteria2 and TUIC are sing-box protocols,
so they are not included in the Xray JSON and remain only in the text list.

---

## Interface language (i18n)

The user interface is bilingual (Russian / English).  Russian is the source
language and the identity translation: the code is written with Russian literals
wrapped in `app.i18n.t(...)`, and `t(x) == x` whenever Russian is active, so the
Russian behaviour is exact.  English is delivered as a translation layer on top
of the same strings.

- The catalog lives in `app/locale_en.py` (`{russian: english}`).
- The active language is stored in `settings.json` under the `language` key and
  applied at start-up (`app.i18n.set_language`).
- Switch it in **Settings → Interface → Interface language**; the change is
  applied after a restart.
- A missing translation falls back to the Russian original, so an incomplete
  catalog can never show an empty label.
- The runtime *status* values (`ок` / `успешно` / `ошибка: …`) are persisted and
  compared as data, so they are kept language-neutral and translated only on
  display (`app.i18n.status_text`).

## Project layout

```
simple_main.py               entry point (GUI / headless / one pass)
simple_main.pyw              same without a console window (double-click)
app/                         checking core
  parser.py                  share-URI parsing and validation
  models.py                  normalised configuration model
  core_config.py             generating configs for the cores
  core_manager.py            locating / downloading / running Xray-core & sing-box
  checker.py                 three-level check (DNS/TCP, handshake, traffic)
  fetcher.py                 source download (URL / file)
  settings.py                settings store and the shared log
  i18n.py                    runtime translation (RU source, EN catalog)
  locale_en.py               English translation catalog
  gui/                       shared Tkinter widgets and the tray icon
simple/                      the "simple" layer on top of the core
  cli.py                     command line (argparse)
  gui.py                     the main window
  pipeline.py                the endless loop and the supervisor
  state.py                   server states, the pool and the output file
  commands.py                external commands and scripts
  config.py                  paths, defaults and the retry policy
scripts/                     helper tools
  i18n_wrap.py               wraps source literals with t(...) (dev tool)
  upload-verify.cmd          atomic SSH upload with sha256 verification
  make-ico.ps1               builds ico.ico from the vector source
tests_simple.py              self-tests of the core and the pipeline
tests_simple_gui.py          self-tests of the interface (Tk)
data/                        runtime data (settings, pool, output) — not tracked
logs/  tmp/  core/           journal, temporary files, downloaded cores — not tracked
```

## Development

```bash
python tests_simple.py       # core and pipeline self-tests (offline)
python tests_simple_gui.py   # interface self-tests (a display is required)
python build-exe.cmd         # build dist/XrayConfigCheckerSimple.exe (PyInstaller)
```

The self-tests are offline and need no network.  To add another language, copy
`app/locale_en.py`, translate the values (keeping the `{}` placeholders intact)
and register the language in `app/i18n.py` (`LANGUAGES`).  The
`scripts/i18n_wrap.py` helper documents how the existing literals were routed
through `t(...)`.

## License

The project is provided “as is” for personal use.



