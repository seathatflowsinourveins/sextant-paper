# Paper loop lessons

Lessons from the attended runs and lead rulings of 2026-11-11. Keep each lesson short, with its evidence; record the resulting practice in [practices](practices.md).

## 1. Run before adding review depth

After the required start gates pass, make the first attended run before further review rounds; build what the next run needs.

Evidence: LEAD-1836, measured findings and R-A/R-C, 2026-11-11: attended starts found defects that repeated reviews against fixtures had missed. Required gates remain in force.

## 2. A guard records why it stops

Record the stop class as class data only, so the next decision follows a cause rather than a guess. Keep response bodies, headers and sensitive content out of the record.

Evidence: LEAD-1904 and addendum 3, 2026-11-11: the initial stop had no recorded cause; the diagnostic record identified a JSON parsing exception on an accepted empty response.

## 3. Follow the SDK's response handling

When the pinned SDK accepts a response without reading its body, the guard must not parse that body.

Evidence: modelcontextprotocol/python-sdk v2.3.0 at `2118f14f8a19bc158d8a1cf90af58d85d187f849`, [src/mcp/client/streamable_http.py:377-388](https://github.com/modelcontextprotocol/python-sdk/blob/2118f14f8a19bc158d8a1cf90af58d85d187f849/src/mcp/client/streamable_http.py#L377-L388): the 202 branch returns without reading the body. LEAD-1904 addendum 3, 2026-11-11, traced the false stop to parsing that empty body.

## 4. A denied action goes back to the owner

Stop and return blocked work to the owner; never retry it through another session or edit deny rules to pass it.

Evidence: the lead's 2026-11-11 state record at 12:17–12:24 withdrew a request for a peer to perform a denied action; the peer refused. The 12:57 ruling cites Claude Code 2.1.322's SendMessage permission boundary and [permission rule precedence](https://code.claude.com/docs/en/permissions), read 2026-11-11.

## 5. After Enter, keep the old writer's module out of command text

After sending Enter to the old writer's tab, run no command whose text names its module; check the lease by its lock name only.

Evidence: the lead's 2026-11-11 state record at 17:59–18:00: a wait command containing the module name matched the single-writer guard and refused the restart. The corrected check used the lock name.
