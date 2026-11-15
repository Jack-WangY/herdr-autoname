# How herdr-autoname works

## Where the topic comes from

Claude Code names each session with a few words and writes them into the terminal title, behind a spinner or status glyph. herdr reads every pane's title and stores a cleaned copy as `terminal_title_stripped`. herdr also knows which panes run an agent. So the information was already in herdr. Before this plugin, the sidebar did not show it next to the space.

The plugin takes a pane's title only when the pane runs an agent and the title is not blank. Plain shell panes have titles like `user@host:~`, which say nothing about the work.

## Why it sets tokens instead of renaming spaces

A space you reuse for different work ends up with a stale name. Renaming spaces automatically fixes that, but it overwrites the names people type on purpose, such as a space kept for one long-running project. With two agents in one space, a rename can also show only one of them.

herdr has display-only metadata for spaces. A client reports named tokens with `workspace.report_metadata`, and `[ui.sidebar.spaces] rows` can show a token as `$name`. herdr hides a row whose token is unset. The plugin reports one token per agent pane, `topic1` to `topic4`, in tab order and then left to right. Your space name stays on the first row, and each agent's topic gets a row of its own.

Tokens also fail safely. When the helper stops, it clears its tokens, and the sidebar falls back to your space names.

## How it stays idle

There is one helper process per herdr server. It opens an `events.subscribe` stream and blocks on it with no timeout. It subscribes to these events:

- `pane.updated`, `pane.created`, `pane.closed`, `pane.exited`, `pane.moved` and `pane.agent_detected`.
- `tab.moved`.
- `workspace.created` and `workspace.closed`.

A title change produces one `pane.updated`. A spinner frame that leaves the cleaned title unchanged produces none. With six Claude sessions open, a 60-second sample saw no events at all.

On each event the helper waits 0.3 s to absorb a burst. It then makes one `session.snapshot` request and works out the topics for every space. It reports only the spaces whose topics changed since its last report. The helper never polls, never runs a subprocess and imports only the standard library. After 30 s idle, its measured physical footprint was 4.6 MB in a three-pane session and 5.2 MB in an eleven-pane session.

## How it starts, restarts and stops

herdr runs a plugin's `[[startup]]` command once when a server starts, and treats it as a one-shot command rather than a daemon it supervises. The `start` mode therefore forks, and the parent exits at once. The child calls `setsid()` and moves its standard streams to `/dev/null` and the log file, which lets herdr record the startup as finished. The child then runs the helper.

herdr shares the plugin state directory across all sessions. So the helper keeps a lock file per server, named from a hash of that server's socket path. `start` takes the lock. If another helper holds it, `start` sends that helper SIGTERM and takes the lock once it frees. The newest helper always wins. A startup hook and a repeated `start` action both end with exactly one helper per server, and `verify.py` tests both. A live handoff to a new server reruns the startup hook, so it should end the same way, but that path is untested.

The helper exits when its event stream closes, because that means the server has gone. herdr does not stop the helper itself. A snapshot request that fails or times out does not end the helper. It logs the error and tries again on the next event. On SIGTERM, for example from the `stop` action, it reports empty values for every token it set and then exits.

Every connection rebuilds the full picture from a fresh snapshot instead of trusting earlier state. A helper that crashed or was killed leaves at worst some stale tokens. The next helper overwrites them on its first pass.
