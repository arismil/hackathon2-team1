# Evaluation results

Every run of the evaluation pipeline writes a timestamped folder here:

```
evaluation-results/
├── latest.md                  # summary table of the most recent run
└── <YYYYMMDDTHHMMSSZ>/
    ├── summary.md             # all metrics, pass/fail per threshold
    ├── results.json           # machine-readable metrics + run metadata
    ├── run.log                # full log of the run (JSON events per node, agent, MCP tool, guardrail)
    ├── E2E-01-report.md       # the full Asteria executive assessment produced during the run
    ├── E2E-01-report.pdf      # the same report as PDF
    └── E2E-01-state.json      # plan, delegation, decision, findings, tool events, timings
```

Generate the Asteria results (Azure OpenAI configured in `.env`):

```bash
uv run nfs-agent ingest                          # build the ChromaDB index with Azure embeddings (first time)
uv run python -m evaluation.run_eval --judge     # retrieval + guardrail + E2E suites, with LLM-as-judge
```

The same metrics are attached as scores to the run's trace in Langfuse.
