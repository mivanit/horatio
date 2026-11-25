# Alarm actions for agents

The **Python backend** supports the same alarm actions through HTTP and local
request files. Lua does not implement these actions. Use HTTP to inspect fresh
source status; use files when working locally as the dashboard's service user.
Both paths re-check the occurrence before changing it and preserve event history.

## Choose the action

| Situation | Action | Result |
|---|---|---|
| A device's old alarm is in `held`, and you have investigated its repair, replacement or removal | `retire` (the UI calls this **Close unchecked alarm**) | Removes that held occurrence and appends a `retired` event. Future faults can raise again. |
| A journal or BMC/SEL alarm is in `active`, and you have investigated its recorded evidence | `review` (**Mark reviewed**) | Moves that evidence out of current concerns into `reviewed`; appends a `reviewed` event. |
| A live fan, disk, temperature or other condition is still reported | Investigate the condition | Neither action is a general mute or an assertion that the machine is healthy. |

Review accepts only source `state`, with a `journal/*`, `sel/memory`, or
`sel/hardware` key and a complete `evidence` array. New underlying entries bring
reviewed evidence back to attention. Older entries aging out do not require a
new review. If a reported alarm's evidence becomes unavailable, the review no
longer hides the alarm. A journal group omitted from the collector's limited
displayed rows retains its stored review; omission alone is not new evidence
or clearing. It cannot be reviewed again while its full evidence is omitted;
when it returns to the displayed rows, its evidence is compared with the stored
review. An older collector or an incomplete/oversized capture may supply no
reviewable evidence; update the collector if needed, and never invent tokens.

If a source cannot re-check an alarm and omits it, the alarm appears under
`held` ("Not re-checked"). Its stored review, note and evidence survive, including
server restarts. When readable again, identical evidence or a subset returns
to `reviewed`; new evidence reopens it. This differs from a reported alarm with
incomplete evidence, which explicitly invalidates the review as described above.
An existing occurrence with no previous evidence metadata establishes a baseline
when evidence first becomes available; that alone is not a "New evidence" event.

The collector displays only the eight oldest distinct journal groups. Reviewing
them does not free display slots. The `journal-more` summary cannot be reviewed,
and groups outside the limit cannot be reviewed through HTTP or action files
until their full evidence is displayed. Ongoing messages can keep the overflow
present beyond 24 hours. This limit remains for this release: dismissing a summary
would hide uninspected evidence. Supporting review of overflow groups requires
exposing their complete evidence, rather than bypassing validation.

Both actions accept an optional `note` string of at most 2,000 characters;
leading/trailing whitespace is trimmed. Record what you checked or changed.
Notes are readable by anyone who can read dashboard history. There is no
standalone add/edit-note, manual reopen, snooze, or permanent suppression action.
A successful close/review leaves its note in history even after the occurrence
leaves the current list. Do not resubmit an action just to change its note.

## Identify the exact occurrence

Start with these read-only requests (substitute the dashboard address):

```bash
curl --fail --silent --show-error http://localhost:8080/api/alarms | python3 -m json.tool
curl --fail --silent --show-error 'http://localhost:8080/api/alarms/history?hours=24' | python3 -m json.tool
```

Check `sources[source].status`: anything other than `ok` means its current state
is unknown, not clear. `active`, `held`, and `reviewed` are separate lists.
`retirement` and `review` advertise `{supported, password_configured}`; these
capabilities do not mean every row is eligible. Service outages use the separate
[services API](api.md#service-checks), not these actions.

Copy fields exactly from the selected row:

| Field | How to use it |
|---|---|
| `source`, `key` | Identify the alarm, independently of its changing display text. |
| `since` | Epoch seconds identifying this occurrence. Preserve the number without rounding or converting it to a date string. A recurrence can have the same key and a different `since`. |
| `unchecked_since` | Required for closure; copy it from the selected `held` row. |
| `evidence` | Required for review; copy the full current array of opaque tokens. Do not use the display text, count, or an older review's tokens. |
| `initial` | If true, the fault was already present when tracking began; `since` is not its known physical onset. Not an action parameter. |

A null `key` or `since` cannot identify an actionable occurrence. Unkeyed findings
still appear in the history card's current concerns, but have no timeline or
action controls. Fresh source
data can also arrive before the tracker has recorded it; a conflict may require
waiting for the next tracker pass and fetching again. Always re-evaluate the
finding rather than automatically applying an old decision to a new occurrence.

History is a record of observations, not a continuous health trace. `cleared`
means the source stopped reporting the alarm under its rules; it does not prove
a repair. `retired` is an operator closure, `reviewed` records investigated
evidence, and `expired` means a held alarm aged out without a re-check. The
[history API reference](api.md#get-apialarmshistory) explains gaps, incomplete
reads, and event fields. An empty history range is not an all-clear signal.

## Act through the API

Send JSON to the selected endpoint. Passwords belong in the JSON body, not a
query parameter or authentication header for these two endpoints.

| Endpoint | JSON fields | Success response (HTTP 200) |
|---|---|---|
| `POST /api/alarms/retire` | `source`, `key`, `since`, `unchecked_since`, `password`, optional `note` | `{"status":"retired","source":"…","key":"…"}` |
| `POST /api/alarms/review` | `source`, `key`, `since`, `evidence`, `password`, optional `note` | `{"status":"reviewed","source":"…","key":"…"}` |

The following standard-library Python example fetches the selected occurrence
and submits one action. Replace `action`, `source`, `key`, and `note` after
investigating. For review, set `action = "review"` and choose an eligible `state`
key. It prompts for the settings password without writing it to a request file.
For unattended agents, supply the password from your existing secret mechanism.

```python
import getpass
import json
from urllib.error import HTTPError
from urllib.request import Request, urlopen

base = "http://localhost:8080"
action = "retire"  # or "review"
source, key = "dashboard", "fan/example"  # replace with the actual alarm
note = "Replaced the failed fan and verified its replacement."

with urlopen(base + "/api/alarms", timeout=15) as response:
    alarms = json.load(response)
capability = alarms["retirement" if action == "retire" else "review"]
if not capability["supported"] or not capability["password_configured"]:
    raise SystemExit("This server is not configured for the requested web action")
rows = alarms["held" if action == "retire" else "active"]
row = next((r for r in rows if r["source"] == source and r["key"] == key), None)
if row is None:
    raise SystemExit("Alarm is not in the expected list; inspect current state")
fields = ("source", "key", "since", "unchecked_since" if action == "retire" else "evidence")
body = {field: row[field] for field in fields}
body.update(note=note, password=getpass.getpass("Settings password: "))
request = Request(
    base + "/api/alarms/" + action,
    data=json.dumps(body).encode(),
    headers={"Content-Type": "application/json"},
    method="POST",
)
try:
    with urlopen(request, timeout=30) as response:
        print(response.status, response.read().decode())
except HTTPError as error:
    print(error.code, error.read().decode())
    raise SystemExit("Action rejected; inspect the response before retrying")
```

After success, fetch `/api/alarms` and `/api/alarms/history` again to verify the
current list and event/note. Web actions do not return a file `action_id` or
accept an idempotency key. If a request times out, its outcome is unknown: inspect
state and history before retrying. A second submission can return 409 because
the first already succeeded.

## Read files locally

All `logs/` paths below are relative to the **running installation's root**, the
directory containing `python/`, `config.json`, and `.password`. They are not
relative to an arbitrary checkout or `/etc/horatio/`. Run as the service user, or
with equivalent access to that installation's logs. No dashboard password is
required for file actions; filesystem write access is the authorization.

| File | Ownership and meaning |
|---|---|
| `logs/alarms-state.json` | Server-owned remembered state, shaped as `memory[source]["alarms"][key]`. Entries carry `since`, optional `unchecked_since`, and evidence when available. A reviewed entry has `reviewed: {timestamp, note, evidence}`. |
| `logs/events.jsonl` | Server-owned JSON object per line: observations and operator actions, including notes. Closure removes the state entry, not the history. |
| `logs/alarm-actions/<id>.json` | Agent-created action request; the supported way to change closure/review state through files. |
| `logs/alarm-actions/<id>.result.json` | Server-created receipt; inspect this to determine the action's outcome. |

Do not directly edit the state, event log, or collector snapshots to mark an
alarm closed or reviewed. The running server owns state updates; direct edits
can be overwritten and bypass validation and history. Offline edits are not a
supported action workflow either. Create a request while stopped if necessary,
then start the server and wait for its receipt.

State files can lag fresh observations and include retained evidence omitted
from a collector's displayed rows. They are not a guarantee of current health.
For example, inspect the state without changing it:

```python
import json
from pathlib import Path

memory = json.loads(Path("logs/alarms-state.json").read_text())
for source in ("state", "sensors", "dashboard"):
    for key, entry in memory.get(source, {}).get("alarms", {}).items():
        print(source, key, json.dumps(entry))
```

## Submit an action file

Use a unique ID of 1–80 ASCII letters, digits, hyphens or underscores. Write one
JSON object to a temporary file **in the same directory**, then rename it to
`<id>.json`. Requests must be regular files, not symlinks or pipes, and no larger
than 64 KiB. The server processes up to 32 pending requests per tracker pass,
roughly every 30 seconds; this is not a completion deadline.

The body matches the API fields, except it has `"action": "retire"` or
`"action": "review"` and **no password**. This example supports either action:

```python
import json
import os
from pathlib import Path
from uuid import uuid4

action = "retire"  # or "review"
source, key = "dashboard", "fan/example"  # replace with the actual alarm
memory = json.loads(Path("logs/alarms-state.json").read_text())
entry = memory[source]["alarms"][key]
request = {
    "action": action,
    "source": source,
    "key": key,
    "since": entry["since"],
    "note": "Replaced the failed fan and verified its replacement.",
}
field = "unchecked_since" if action == "retire" else "evidence"
request[field] = entry[field]
directory = Path("logs/alarm-actions")
directory.mkdir(exist_ok=True)
request_id = uuid4().hex
temporary = directory / (request_id + ".tmp")
with temporary.open("x") as output:
    json.dump(request, output)
    output.write("\n")
    output.flush()
    os.fsync(output.fileno())
os.replace(temporary, directory / (request_id + ".json"))
print(directory / (request_id + ".result.json"))
```

The server re-checks fresh source data as well as its remembered occurrence.
A request made from an older state file can therefore be rejected. Missing
`unchecked_since` or `evidence` in the example means you must inspect eligibility,
not fill in an arbitrary value. After renaming a request, treat it as immutable:
it may already be in progress. There is no cancellation protocol.

## Verify file results and handle failures

Wait for the matching `<id>.result.json` before assuming an action succeeded.
A successful receipt looks like:

```json
{
  "action_id": "example-request-id",
  "status_code": 200,
  "status": "retired",
  "source": "dashboard",
  "key": "fan/example"
}
```

Review returns `"status": "reviewed"`. Rejections have `error` instead of
`status`, for example:

```json
{
  "action_id": "example-request-id",
  "status_code": 409,
  "error": "Alarm changed or is no longer held; refresh and try again"
}
```

The following meanings apply to HTTP status codes and file receipts; password
errors occur only over HTTP:

| Code | What an agent should do |
|---|---|
| 200 | Verify the result in current state and event history. |
| 400 | Correct malformed JSON, fields, note length, or an ineligible action. Do not retry unchanged. |
| 403 | Wrong web password (or a read-only demo). Correct the credentials/target. |
| 409 | Re-read the alarm and evidence. It may have changed, become stale, returned to active, or already been acted on. Reconsider the action before resubmitting. |
| 429 | Web password attempts are throttled; respect the response's `retry_after` seconds. |
| 500 / 503 | Read the error and check storage/server/source availability or password configuration. Inspect state/history before retrying; a failed write may have appended an event. |

No receipt means pending or an unknown outcome, not success. Check that the
server is running, that you used the correct installation and filename, and that
the service user can read the request and write the directory. Check
`logs/server.log` for tracker or storage failures. An invalid filename is ignored
rather than given an error receipt. Do not create a replacement request merely
because processing took more than 30 seconds.

A receipted request, including a rejection, is not automatically retried. If a
new action is appropriate, submit a **new ID**. Keep requests and receipts
together: deleting a receipt while leaving its request permits repeat
processing. Finished pairs can be archived together once inspected.

File-action events include `action_id`, allowing receipts to be matched to
`logs/events.jsonl`. After an interruption, inspect both state and events before
retrying: the event log, saved state, and receipt are not a single atomic
transaction, and exactly-once delivery after a crash is not guaranteed. Event
lines across sources need not be chronological, so use their timestamps when
reading a timeline. File commands never erase history or change alarm thresholds.
