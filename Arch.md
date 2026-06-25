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
