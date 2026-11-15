# herdr-autoname

A [herdr](https://herdr.dev) plugin that shows what each agent pane is working on, under its space name in the sidebar. The space names you type stay as they are.

```
 ● Billing service
   Refactor invoice retry logic
   Add Stripe webhook tests
 ○ Docs site
   Fix broken sidebar links on…
 · Scratch
   main
```

Claude Code writes a short topic for each session into its terminal title, and herdr already stores that title for every agent pane. This plugin copies those titles into display-only space tokens, `topic1` to `topic4`, and a few lines of sidebar config render them. When a session moves on to a new task and its title changes, the line updates.

## Requirements

- herdr 0.9.5 or later.
- macOS. The helper uses only POSIX calls, but it is untested on Linux, so the manifest lists only `macos`.
- `/usr/bin/python3` (Python 3.9 or later, from the Xcode Command Line Tools). The plugin uses only the standard library.
- An agent that sets its terminal title. Claude Code does this by default.

## Install

1. Install the plugin from GitHub:

   ```sh
   herdr plugin install Jack-WangY/herdr-autoname
   ```

2. Start the helper once. herdr runs the plugin's startup hook only when a server starts, not when you install it:

   ```sh
   herdr plugin action invoke herdr-autoname.start
   ```

   From then on, every herdr server start launches the helper.

3. Add the sidebar rows to `~/.config/herdr/config.toml`. If your config has no `[ui]` table yet, paste this block at the end:

   ```toml
   [ui]
   sidebar_width = 34

   [ui.sidebar.spaces]
   rows = [["state_icon", "workspace"], ["$topic1"], ["$topic2"], ["$topic3"], ["$topic4"], ["branch", "git_status"]]

   [ui.sidebar.agents.rows_by_agent]
   claude = [["state_icon", "machine", "workspace", "tab"], ["terminal_title_stripped"]]
   ```

   If you already have a `[ui]` table, add the `sidebar_width` line to it instead of adding a second `[ui]` table.

4. Check the config and apply it to the running server:

   ```sh
   herdr config check
   herdr server reload-config
   ```

If you cloned this repository, `scripts/enable-sidebar.sh` does steps 3 and 4 for you. It backs up your config, appends `sidebar.toml`, checks the result and reloads herdr. It refuses to run if your config already has a `[ui]` or sidebar rows table.

## Choose what the sidebar shows

The block in step 3 sets three things. Change or drop any of them.

| Setting | Effect |
|---|---|
| `sidebar_width = 34` | Widens the sidebar from its default of 26 columns, so about 29 characters of each topic fit. herdr cuts longer topics with `…`. |
| `["$topic1"]` to `["$topic4"]` | One row per agent pane, in tab order and then left to right. A space with no agent pane shows no topic rows, because herdr hides a row whose token is unset. |
| `["branch", "git_status"]` | herdr's default second row, kept at the end. It is empty for spaces outside a Git repository. |
| `rows_by_agent.claude` | Shows each Claude session's topic in the agents panel in place of the word `claude`. This one is built into herdr and works without the plugin. |

To tag a session yourself, run `/rename <name>` in Claude Code. Claude Code's docs say `/rename` replaces the terminal title by default (the `terminalTitleFromRename` setting), so the new name should appear on the line.

## Check that it works

Run these when the sidebar shows no topic lines:

- `herdr plugin list` shows `herdr-autoname` as enabled.
- `pgrep -fl herdr-autoname.py` shows one helper process for each running herdr server.
- `herdr agent list` shows a `terminal_title_stripped` value for your agent panes. If it shows none, the agent is not setting a title. For Claude Code, check that `CLAUDE_CODE_DISABLE_TERMINAL_TITLE` is unset.
- `herdr workspace get <id>` shows the `topic1` to `topic4` tokens for that space.
- The helper's log is at `~/.local/state/herdr/plugins/herdr-autoname/herdr-autoname.log`.

If the helper is not running, start it again with `herdr plugin action invoke herdr-autoname.start`. Running `start` while a helper is already running replaces it, so you never end up with two.

## Limits

- The sidebar shows at most four agent panes per space.
- herdr truncates each token to 80 characters.
- The helper exits when herdr's event stream closes, for example when the herdr server stops. The next server start launches it again.
- herdr runs startup hooks only when a server starts. After installing or re-enabling the plugin, run the `start` action once.

## Privacy

The log records each topic change in plain text, for example `w2 topic1='Refactor invoice retry logic'`. Topics often name projects or customers. The log stays on your machine. Each time the helper starts, it moves a log larger than 1 MB to `herdr-autoname.log.1`, replacing the previous one.

## Uninstall

1. Stop the helper. Stopping it also clears the topic tokens:

   ```sh
   herdr plugin action invoke herdr-autoname.stop
   ```

2. Remove the plugin. Use `herdr plugin uninstall herdr-autoname` for a GitHub install, or `herdr plugin unlink herdr-autoname` for a linked clone.
3. Delete the sidebar rows from `~/.config/herdr/config.toml`, or restore the backup that `scripts/enable-sidebar.sh` printed. Then run `herdr server reload-config`.

## How it works

[docs/how-it-works.md](docs/how-it-works.md) explains the design: why it sets tokens instead of renaming spaces, how it stays idle, and what happens on restarts.

## Develop

Link a clone instead of installing from GitHub, so herdr runs your working copy:

```sh
git clone https://github.com/Jack-WangY/herdr-autoname.git
herdr plugin link "$PWD/herdr-autoname"
herdr plugin action invoke herdr-autoname.start
```

`verify.py` is the behaviour test:

```sh
python3 -I verify.py
```

It runs against a separate herdr session named `lab`, which it creates if needed, and it never sends requests to your default session. herdr keeps one plugin registry for all sessions. So if the plugin is not already linked, the test links it for the run and unlinks it at the end. Every step compares against a literal expected value and prints `PASS` or `FAIL`. The exit code is non-zero when any step fails.

## License

[MIT](LICENSE).
