## Tenant isolation for the vector index — one shared FAISS index with namespace filtering, or one index per tenant? What are the memory, latency, and data-leakage trade-offs?

| Option | Memory | Latency | Data leakage risk | Operational burden |
|---|---|---|---|---|
| Shared index + namespace filter | Lowest memory usage, one copy of infra | Usually fastest to start, but can degrade with many tenants | Highest risk if filter is missed, bypassed, or misconfigured | Lowest upfront, but hardest to secure |
| One index per tenant | Highest memory usage and more index overhead | Best isolation, often more predictable latency per tenant | Lowest risk of cross-tenant leakage | Highest operational overhead |

One index per tenant is a better approch, however a hybrid approach could be a better option.

**The Hybrid Approach:**

To balance data sovereignty with infrastructure costs, we can deploy a two-layered hybrid model:

1. Regional Isolation (Sovereignty Layer)
We host three completely independent regional deployments: us-east, eu-west, and a domestic KSA cloud node. Data never leaves its home region, ensuring full compliance with GDPR and KSA regulations at rest.

2. Selective Tenant Tiering (Cost & Risk Layer)
Inside each regional pod, customers are split by size and risk profile:

Tier 1 (Large / High-Audit Enterprises): Dedicated per-tenant index files for absolute process isolation, zero data leakage, and noise-free query performance.

Tier 2 (Standard / Small-to-Medium Tenants): A single shared regional index with server-side namespace pre-filtering to minimize baseline RAM and graph-link overhead. It can also have multiple small indexes where small ones can be combined \. (Compaction)

3. Cold Storage Optimization
To save active memory costs, idle Tier-1 indices are serialized in regional block storage. An LRU cache dynamically loads the .faiss files into server RAM only when an active user session begins, releasing them after a period of inactivity.

## LLM backend per tenant
Route model selection in a dedicated LLM gateway / control plane that sits between the app and all model backends, not inside the prompt template layer. The application should resolve the tenant first, then ask the gateway which backend to use: cloud API for most tenants, private/on-prem Llama for customers that require it.
<img width="1420" height="278" alt="Screenshot 2026-06-25 at 10 53 12 AM" src="https://github.com/user-attachments/assets/80f1c1a7-52da-4412-8047-1b93501ba940" />
Keep tenant routing and residency policy in the gateway layer.
Keep the prompt template layer generic and output-agnostic.
Keep provider-specific transforms in thin adapters.
Store tenant policy in config or a policy service so that it can switch backends without changing prompts.

## PII in the NL→SQL pipeline 
I would implement following Guardrails:
- redact or mask PII in the question, like "Show orders for customer C001", extract 'C001' and replace with [CUSTOMER_REDACTED_1]
- expose only an allowlisted semantic schema
- validate generated SQL (use LLM-as-a-judge)
- SQL policy checks: reject queries that attempt to select raw PII, broad exports, or unbounded joins.
- enforce row/column-level security at the DB (Governance layer like Unity Catalog in Databricks)

My answer for Guardrails will not change for on-prem because private hosting lowers exposure, but it does not eliminate the risk of the model seeing or revealing more than it should. The model can make mistakes even in a private environment:
- wrong joins,
- missing filters,
- unbounded queries,
- leaking columns

## Highest-leverage architectural choice - Single-Tenant File Isolation + Application LRU Caching
The major trade-offs are:
- Cold-Start Latency: When a user from an idle enterprise account logs in after hours of inactivity, their first query hits a performance bottleneck. The system will experience a 1-to-2 second delay while the application server fetches their specific .faiss vector graph and SQLite file from disk and loads them into RAM.
- Operational State Management: We moved the burden of data lifecycle management out of the database engine and into our own code. We now have to explicitly write, test, and maintain code that handles file locks, concurrent read/write syncs, and memory release policies—increasing the complexity of our core application logic.

These trade-offs are acceptable because they completely eliminate the risk of cross-tenant data leakage and allowed us to clear strict enterprise legal reviews (GDPR and KSA compliance) with zero friction.
