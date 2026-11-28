---
name: decision-browser
description: Run bounded browser checks on a trusted loopback fixture through the native Decision Browser MCP server.
---

# Decision Browser

Read `capabilities` and the schemas of the connected tools. This candidate
supports isolated tasks on trusted `http://127.0.0.1` fixtures. Use only a fixture
owned by the current test. `decision-browser doctor` checks local installation
prerequisites. Use the installed executable and the current project path.

1. Call `start_task` once with the agreed URL, goal, checks, values, and limits.
   Keep its run ID. If the response is lost, use `get_task` without a run ID to
   recover the retained run. A missing run does not establish what happened.
2. Poll `get_task` with the run ID. When it returns a pending proposal, call
   `present_admission` with the exact run ID, proposal ID, and digest. The host
   asks the user to review the scope. The agent must never submit the host's
   answer or synthesize its `reviewed` value.
3. Read evidence by artifact ID using `read_artifact`. Report the functional
   result separately from any visual diagnostics. A result of `outcome_unknown`
   requires inspecting the fixture before deciding whether another run is safe.
4. If asked to stop, call `cancel_task` for the retained run and poll until
   cleanup finishes. Preserve the original request limits throughout the run.

Never pass credentials, session data, or unrelated form values to the server.
