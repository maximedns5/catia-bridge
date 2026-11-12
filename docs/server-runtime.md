# Serialized CATIA execution

MCP requests enqueue work on one persistent STA thread. Connection initialization,
tool execution and connection cleanup use that same thread, for CATIA V5 and
CATIA 3DEXPERIENCE alike (the platform is detected on connect).

Server-boundary failures return an `ok/code/operation_id/tool/message/data/effects/
diagnostics/warnings/recovery` envelope as both structured content and indented
JSON text, with MCP `isError=true`. Input validation failures do not access COM.
Missing Automation methods retain the guidance about release availability,
pywin32 cache and workbench licences in `message` (on 3DEXPERIENCE it also
points to `catia_inspect_api` and DSYAutomation.chm). Tools raise typed
failures where they can classify them (`INVALID_ARGUMENT`,
`NO_ACTIVE_DOCUMENT`, `WRONG_DOCUMENT_TYPE`, `OBJECT_NOT_FOUND`,
`AMBIGUOUS_TARGET`, `UNSUPPORTED_CAPABILITY`, `PERMISSION_DENIED`);
`effects.modified=false` marks failures detected before CATIA was touched. Exceptions include their
message and traceback in `catia_mcp.log`.

An ordinary error affects one call. Correct the input or inspect CATIA, then call
again; it does not require restarting the server. A cancelled client request does
not stop an already queued or executing COM operation. The worker completes it
before starting another call. Cancellation does not establish model rollback;
inspect the model before repeating a write.

## Dialogs, hangs and a busy CATIA

`catia_mcp/watchdog.py` looks at CATIA from outside COM, with plain Win32
calls (ctypes) that keep working while the worker is stuck in a call: the
CATIA processes, their visible top-level windows (title, class, child texts)
and whether the main window is disabled, the generic sign that a modal dialog
is open. On other platforms it reports itself unavailable and every check
passes through. `catia_mcp/com_guard.py` runs the worker and waits for it:

| Situation | Result | Session |
|---|---|---|
| A dialog is already open when a call arrives | `BLOCKED_BY_DIALOG`, dialog title and text, `modified=false`; nothing is sent to CATIA | unchanged: call again once it is closed |
| A modal dialog stays open longer than `CATIA_MCP_DIALOG_GRACE` s (default 3) during a call | `BLOCKED_BY_DIALOG` with the dialog's text, `modified=null` | latched |
| No answer within `CATIA_MCP_CALL_TIMEOUT` s (default 180, 0 = never) | `SESSION_BLOCKED` with what the watchdog sees, `modified=null` | latched |
| CATIA rejects a call as busy (`RPC_E_CALL_REJECTED`, `RPC_E_SERVERCALL_RETRYLATER`) | the connection check and attach retry with backoff for `CATIA_MCP_BUSY_RETRY` s (default 10); inside a tool: `CATIA_BUSY`, `modified=null` | unchanged |

A COM call cannot be cancelled in-process, so a call that ended in a latch
keeps running in CATIA. While it is still stuck (`blocked`) every call,
`catia_connect` included, is refused with `SESSION_BLOCKED` and a fresh
snapshot of the screen. Once it has returned (`unknown`: its effect on the
model was never reported) every call except `catia_connect` and
`catia_disconnect` is refused; `catia_connect` clears the latch. What to do,
as the error messages say: read and close the dialog in CATIA, check the
model, call `catia_connect`. If CATIA is frozen, end it in Task Manager,
restart it and call `catia_connect`; restart the server if the session
still reports blocked. Shutdown does not wait for a stuck call (the worker
is a daemon thread). A progress window (a child `msctls_progress32`) does
not count as a blocking dialog; whether CATIA's own progress and dialog
windows use standard Win32 classes is not verified live.

Dialogs are only reported by default. `CATIA_MCP_DIALOG_AUTODISMISS` names a
JSON allowlist, for example:

```json
[{"title": "Update diagnosis", "text": "", "button": "OK"},
 {"title": "Operation not authorized", "button": "OK", "licence": true}]
```

Every given field must be contained (case insensitive) in the dialog's title
or text. A rule applies to a dialog that mentions a licence, an
authorization or a role only with `"licence": true`; save / discard prompts
are never pressed. Dismissed dialogs are logged and listed in
`dismissed_dialogs`.

Other modules run CATIA work under the same guard with
`catia_mcp.com_guard.run_guarded(fn, *args, timeout=..., label=...)`: inside
a guarded call it runs inline on the worker, elsewhere on a process-wide
worker. `python -m catia_mcp.tree build` runs connect and the whole build on
its own guarded worker (timeout: `--deadline` + 60 s); a dialog ends the
build with a report naming the part, the feature and the dialog's text.

## Which CATIA

CATIA V5 and 3DEXPERIENCE register the same `CATIA.Application` ProgID and
`GetActiveObject` returns whichever instance registered first. `catia_connect`
therefore counts the CATIA processes that show a window (default executable
names `CNEXT.exe` and `3DEXPERIENCE.exe`, the second unverified live;
`CATIA_MCP_PROCESS_NAMES=a.exe,b.exe` overrides) and refuses with
`AMBIGUOUS_TARGET` when more than one runs (`CATIA_MCP_ALLOW_SEVERAL_CATIA=1`
attaches anyway). The connect message and `catia_diagnose` carry the
release fingerprint: platform, Version, Release, ServicePack, caption,
Windows UI language (CATIA follows it unless Tools > Options sets another),
the topology language once known, the CATIA processes and the gen_py folder.
`catia_diagnose` also includes the watchdog's snapshot.

pywin32's makepy cache is a private folder per bridge and Python version
(`%LOCALAPPDATA%\catia-mcp\gen_py\<version>-py<X.Y>`, set before
`win32com.client` loads; `CATIA_MCP_GEN_PY` names another folder, `default`
keeps pywin32's), so a stale `%TEMP%\gen_py` left by another tool cannot
change how CATIA binds. The bridge binds late and never calls
`gencache.EnsureDispatch`.

Not validated on a live CATIA yet: the 3DEXPERIENCE executable name, whether
licence and update dialogs disable the main window and expose their text to
Win32, whether a busy CATIA answers with the two rejection codes, and
whether pressing a dialog's button makes the blocked call return.
`scripts/probe_dialog_watchdog.py` records all of this in a JSON report
(`--trigger` shows one harmless VBScript message box through
`SystemService.Evaluate` and checks detection, the latch and auto-dismiss
on it; it never saves or closes documents).

Install and run the offline regressions:

```sh
python -m pip install -e ".[dev]"
python -m pytest tests test_server.py -q
```

Windows CI runs this command on Python 3.10 and 3.12. Local tests use COM doubles
for thread ownership, serialization, errors and cancellation; they do not prove
live CATIA method behavior.
