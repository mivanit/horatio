# Alarm actions through files

Local agents can read alarm state and submit actions without HTTP. The running
Python server applies the same checks as the dashboard, writes the event history,
and saves the resulting state. Filesystem access as the dashboard user is the
authorization for this interface. Web actions still require the dashboard
password. Never put that password in a request file.

## Read the current state and history

- `logs/alarms-state.json` contains each source's remembered alarms, keyed by
  alarm key. `since` identifies an occurrence; `unchecked_since` identifies an
  unchecked observation. Reviewable journal/BMC alarms include `evidence`.
- `logs/events.jsonl` records observations and operator actions, including notes.
  Closing an alarm does not delete these records.
- Do not edit either file while the server runs: it owns their updates. Use the
  request interface below to avoid overwriting another action or tracker update.

The state file is remembered evidence, not a guarantee of current health. The
server re-checks action eligibility when it processes a request. A changed alarm
can therefore reject a request prepared from an older state file.

## Submit an action

Write one JSON object to `logs/alarm-actions/<id>.json`, using a unique identifier
of at most 80 letters, digits, hyphens or underscores. Write to a temporary file in the
same directory, then rename it to the final `.json` name so the server never sees
a partially written request. The server must be running; processing follows the
alarm tracker's roughly 30-second cycle. Requests must be regular files no larger
than 64 KiB. Up to 32 pending requests are processed per cycle.

A closure request contains:

```json
{
  "action": "retire",
  "source": "dashboard",
  "key": "fan/example",
  "since": 1790000000,
  "unchecked_since": 1790000060,
  "note": "Replaced the failed fan and verified its replacement."
}
```

Those identifiers are illustrative. Copy the exact source, key and timestamps
from the current state; the server rejects stale or active occurrences. The
optional note is limited to 2,000 characters.

For review, use `"action": "review"`, and copy the alarm's full `evidence` array
instead of `unchecked_since`. Only supported journal/BMC evidence can be
reviewed. New evidence restores the alarm to current concerns; review never
suppresses a live fan, disk or temperature condition.

For example, after choosing the correct source and key, this submits a closure
from the checkout root:

```python
import json
import os
from pathlib import Path
from uuid import uuid4

source, key = "dashboard", "fan/example"  # replace with the actual alarm
memory = json.loads(Path("logs/alarms-state.json").read_text())
entry = memory[source]["alarms"][key]
request = {
    "action": "retire",
    "source": source,
    "key": key,
    "since": entry["since"],
    "unchecked_since": entry["unchecked_since"],
    "note": "Replaced the failed fan and verified its replacement.",
}
directory = Path("logs/alarm-actions")
directory.mkdir(exist_ok=True)
request_id = uuid4().hex
temporary = directory / (request_id + ".tmp")
temporary.write_text(json.dumps(request) + "\n")
os.replace(temporary, directory / (request_id + ".json"))
print(directory / (request_id + ".result.json"))
```

## Read the result

The server writes a corresponding `<id>.result.json` receipt, for example:

```json
{
  "action_id": "example-request-id",
  "status_code": 200,
  "status": "retired",
  "source": "dashboard",
  "key": "fan/example"
}
```

Errors contain `error` instead of a success status. Wait for the receipt before
assuming an action succeeded. A conflict means the occurrence or evidence
changed: inspect the current state and submit a new request with a new ID only
if the action still makes sense. A failed request is not automatically retried.

Keep requests and receipts together. Do not reuse an ID or delete a receipt while
leaving its request in place; the receipt prevents routine repeat processing.
The event for a file action includes its `action_id`, allowing the receipt to be
checked against the history. After an interruption, inspect both before retrying:
the event log, saved alarm state and result receipt are not a single atomic
transaction. The interface does not promise exactly-once delivery after a crash.

Finished request/receipt pairs can be archived together once inspected. Notes and
closure/review history remain in `logs/events.jsonl`. File commands do not erase
history, alter collector thresholds or permanently suppress alarms.
