# Challenge origin and attribution

GhostAudit was built for **HackerRank Orchestrate 2026 — October edition**, for the challenge **Ghost Data Still Exists**. The challenge specification, supplied fictional dataset and event instructions originate from HackerRank; GhostAudit is the participant implementation. This project does not claim HackerRank endorsement or an official evaluation score.

Challenge and submission URL:

https://wwww.hackerrank.com/contests/hackerrank-orchestrate-october26/challenges/ghost-data-still-exists

The organizer-provided [problem statement](problem_statement.md) and [agent trace instructions](agent_trace_instructions.md) are preserved here for reference. The root `AGENTS.md` retains the coding-agent and logging contract. No challenge end time is configured in the repository.

## Challenge artifacts

| Artifact | How it is produced |
| --- | --- |
| `output.csv` | `python code/main.py`; 518 evaluation rows at the repository root. |
| `code.zip` | `python code/tools/package.py`; runnable code, documentation and evaluation reports. |
| Chat transcript | Root `log.txt`, maintained separately from the public repository and archive. |
| Agent trace | Follow the supplied trace instructions; review the relevant session files before sharing. |

The supplied `dataset/` directory is an external input and is excluded from the code archive. It is not bundled as part of the public project's source distribution. Predictions use only participant-facing data; organizer-only answers and generator internals are not inputs.
