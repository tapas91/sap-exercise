## Tenant isolation for the vector index — one shared FAISS index with namespace filtering, or one index per tenant? What are the memory, latency, and data-leakage trade-offs?

| Option | Memory | Latency | Data leakage risk | Operational burden |
|---|---|---|---|---|
| Shared index + namespace filter | Lowest memory usage, one copy of infra | Usually fastest to start, but can degrade with many tenants | Highest risk if filter is missed, bypassed, or misconfigured | Lowest upfront, but hardest to secure |
| One index per tenant | Highest memory usage and more index overhead | Best isolation, often more predictable latency per tenant | Lowest risk of cross-tenant leakage | Highest operational overhead |

One index per tenant is a better approch, however a hybrid approach could be a better option.
**The Hybrid Approach:**

To balance strict data sovereignty with infrastructure costs, we can group and split the 50 tenants using a two-layered hybrid model:

1. Hard Regional Separation (Sovereignty Layer)
First, isolate the infrastructure into three completely independent regional deployments: us-east, eu-west, and a domestic KSA cloud node. Data never leaves its home region, keeping it fully compliant with GDPR and KSA regulations at rest.

2. Selective Tenant Tiering (Cost & Risk Layer)
Inside each regional pod, split the customers by size and liability rather than treating everyone the same:

Tier 1 (Large / High-Audit Enterprises): Give these accounts their own dedicated per-tenant index files. This ensures absolute process isolation, guarantees zero data leakage, and delivers clean, noise-free query performance.

Tier 2 (Standard / Small-to-Medium Tenants): Combine smaller accounts into a single shared regional index using server-side namespace pre-filtering. This prevents the baseline RAM and graph-link overhead from spiking unnecessarily.

3. Cold Storage Optimization
Keep idle Tier-1 indices serialized in regional block storage and dynamically load them into memory via an LRU cache only when active sessions begin.
