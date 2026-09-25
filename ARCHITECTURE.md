# L3B Architecture Record

This record documents the implementation in `src/student_agent/workflow.py` and
`src/student_agent/mcp_gateway.py`. It describes observable decisions and does
not include private prompts or chain-of-thought.

## 1. System overview

```text
Case input
   │
   ▼
Coordinator ── task_assigned ──► Entity/customer stage
   │                                  │
   │                         discovered order tools
   │                                  ▼
   │                         MCP evidence gateway
   │                                  │
   ├── handoff ◄── candidate ranking / order IDs
   ├── task_assigned ──► Order, customer, product, shipment, payment/refund stages
   │                                  │
   │                         evidence_ref + tool_result_consumed
   │                                  ▼
   ├──► Conflict resolver ◄── policy lookup when source conflicts exist
   │             │
   └──► Verifier ── schema-shaped output + verification_completed
                         │
                         ▼
                  output JSON and trace
```

The stages are deterministic in-process specialists, not separately hosted
agents. Handoffs are explicit trace events with `case_id` correlation. MCP
tool names and argument schemas come from session discovery; the workflow does
not call an undiscovered tool.

## 2. Agent ownership

| Actor | Input | Responsibility | Tool permission | Output/handoff |
| --- | --- | --- | --- | --- |
| Entity/customer | Claimed order ID, candidate IDs, customer hint | Resolve exact/candidate order IDs; cross-check order candidates against customer history | Discovered `get_order` and `get_customer_history` tools | Resolution status, selected/rejected IDs, and evidence refs to coordinator |
| Coordinator | Case, catalog, and specialist results | Assign stages, enforce the per-case call budget, correlate evidence, and assemble output | No direct domain tool; delegates to a matching specialist stage | Case-scoped stage handoffs |
| Order/item | Resolved order ID | Retrieve item facts only for payment reconciliation, unavailable-item, or unsupported-claim investigations | Discovered `get_order_items` tool | Order/item evidence to verifier and conflict resolver |
| Shipment | Resolved order or shipment IDs | Retrieve tracking timeline and delivery facts | Discovered shipment/delivery tools | Shipment verdict and timeline evidence |
| Payment/refund | Resolved order or payment references | Reconcile capture rows for financial claims; inspect refund lifecycle for pending or failed refund claims | Discovered `get_payment_timeline` and conditional `get_refund_timeline` tools | Financial verdict and payment evidence |
| Policy | Policy version and specialist evidence | Apply public policy rule for the selected issue to status, refund amount, responsible parties, and action | Discovered `get_policy` tool, called once per case | `policy_decided` and policy evidence to verifier |
| Conflict resolver | Domain evidence and conflicts | Compare same-field values and apply documented source precedence where available | No independent tool access | Conflict records with selected source or unresolved code |
| Verifier | Candidate output and all consumed refs | Check output construction, entity scope, evidence linkage, and confidence/status invariants | No MCP calls | Final output and `verification_completed` event |

The coordinator selects tools by their discovered name/description and requires
all advertised required arguments (apart from the gateway-injected `case_id`)
to be available. Each domain stage only selects a tool matching that domain.

## 3. Entity resolution and A2A protocol

Input order references are separated into exact IDs and candidate IDs. Exact
IDs are checked first. Candidate lookups are capped at three; returned records
are compared against case customer, product, seller, purchase-date, and amount
clues. A sole candidate with a successful lookup can resolve; multiple tied
candidates remain `ambiguous`. A search returning multiple orders is not
silently reduced to the first result. Rejected IDs are retained in the output.

Each message/handoff is represented by a trace event containing the case ID,
actor, target, and a short decision code. There is no free-form reasoning in the
trace. Each case emits two assignments and two handoffs around entity resolution
and verification; specialist collaboration is represented by its consumed tool
event rather than redundant per-tool assignment/handoff pairs. Tool calls are
awaited in order inside each worker and the run ends at the case call budget.

## 4. Evidence and conflict lifecycle

The gateway validates each MCP response against the public evidence envelope.
The workflow retains the server-issued `evidence_ref` and domain/data pair in
case-local memory. Once a response is incorporated, it emits
`tool_result_consumed` with the same reference. Only consumed references are
placed in output. No evidence cache is shared across cases, and references are
never synthesized or rewritten.

The case `opened_at` timestamp is the investigation cutoff. Future lifecycle
events are excluded, and when the gateway contains multiple historical event
groups the latest observable group is selected. This prevents canonical Olist
rows or injected future events from overriding the scenario visible when the
case was opened.

Conflicts are emitted only when different domains report different scalar
values for a tracked status/date field. Source precedence is explicit for
order status, payment/refund status, and shipment dates. If no preferred source
is present, the selected source remains null and the conflict is marked
unresolved. The public policy for the input's `policy_version` is fetched for
each case and its issue rule supplies the final case status, recommended
refund, responsible parties, and resolution action. Refund amounts are emitted
only when the relevant payment/refund or shipment evidence has been consumed.

## 5. Failure and efficiency policy

| Failure | Retry budget | Fallback | Trace event/code |
| --- | ---: | --- | --- |
| MCP connection timeout or retryable HTTP error | 1 transport retry | Retry once for timeout/connection failures and HTTP 408, 429, or 5xx; fail the case when entity or policy evidence is unavailable | No evidence-consumed event for a failed call |
| MCP tool execution or contract error | 0 | Preserve the server error and skip/fail according to whether the evidence is required | No evidence-consumed event for a failed call |
| Entity not found/ambiguous | 0 | Preserve candidate IDs as rejected or unresolved; do not infer an order | Entity handoff uses `entity_ambiguous` or `entity_not_found` |
| Source conflict | 0 | Apply the listed source precedence, otherwise preserve it as unresolved | Conflict output plus policy handoff when policy lookup is available |
| Invalid specialist result | 0 | Gateway contract failures are discarded; output remains conservative | `verification_completed` with `evidence_incomplete` when the case cannot be resolved |

The workflow schedules at most ten logical MCP calls per case and at most three
candidate lookups when no exact reference exists. A transient transport retry
may add one physical attempt for a failed call. With an exact claimed order,
the standard plan performs one order lookup, one customer-history lookup, payment and
policy investigation, and only the item, shipment, or refund specialist
required by the business claim. This normally uses four to seven calls. Seller
and product tools are not called unless a future
claim requires facts unavailable from item or shipment evidence.
Arguments are bound to discovered input schemas; missing required fields cause
the tool to be skipped. Duplicate `(tool, arguments)` calls are cached only for
the current case. There are no broad scans, unbounded retries, or cross-case
cache entries. The MCP gateway performs tool discovery once per session.

## 6. Verification invariants

Before returning, the verifier constructs the L3B output with the public field
sets and checks these workflow invariants:

- Resolved order IDs come from an exact reference supported by a lookup, a
  uniquely supported candidate, or a single result from a discovered search.
- Unresolved candidates remain `ambiguous`/`not_found`; missing facts do not
  become guessed shipment or payment values.
- Every output evidence ref is copied from a validated MCP response consumed
  for this case, and every consumed response is linked in the trace.
- Payment/refund totals are nullable when absent; a refund recommendation is
  emitted only for a supported refundable balance and an action-requiring issue.
- Shipment timeline completeness requires both promised and delivered dates.
- Confidence is bounded to [0, 1], and unresolved cases use a low confidence
  with `needs_investigation` status.
- Conflicts retain at least two source domains; action and refund fields are
  kept consistent with the derived issue and refundable amount.

The CLI validates the final object against `l3b-output-v2.schema.json` and
validates trace events before packaging.

## 7. Reproducibility

- Runtime: Python 3.11 or newer; dependencies are constrained in `pyproject.toml`.
- Concurrency: up to four worker sessions run concurrently by default; each
  worker owns one MCP connection. `day09 run --concurrency N` accepts values
  from 1 through 32.
  No random sampling is used by the workflow.
- Run from the repository root with `day09 run --concurrency 4`; completed cases
  are resumed by default. `--fresh` writes to `run-staging/active/` and resumes
  an incomplete fresh run there. Only after all cases validate does it replace
  current outputs and package a new submission. Previous artifacts are copied
  to `run-backups/`. Validate/package using the commands in `README.md`.
- Credentials are loaded from `.env` and must not be committed or included in
  a submission archive.

The released L3B input set and competition credentials are not stored in this
repository, so end-to-end execution requires the official input ZIP and a
registered team key.
