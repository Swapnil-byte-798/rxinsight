# RxInsight study material

Updated 29 October 2026. The Markdown and PDF editions below are generated from the same study sources and describe the repaired implementation reviewed at `da15d2c`.

| Read this for | Markdown | PDF |
|---|---|---|
| Architecture, data flow, worked examples and measured results | [How it works](how-it-works.md) | [Architecture guide](RxInsight-How-It-Works.pdf) |
| Technical interview preparation and defensible answers | [Interview questions](interview-questions.md) | [Interview Q&A](RxInsight-Interview-Questions.pdf) |
| Fundamentals, analogies and a beginner-to-advanced study path | [Tool primer](tools-primer.md) | [Tool primer PDF](RxInsight-Tools-Primer.pdf) |

Read the primer first if SQL/ETL concepts are new, then the architecture guide, then practise the interview questions. [The verification record](verification.md) contains the execution evidence.

The current facts are 2,229,747 loaded prescriptions, 500 verified rejects, 68,619 sales calls and 67 passing tests per dependency configuration. Call analysis counts 47,184 person-months across SCD changes. The selective index benchmark improved 58.5x in its measured environment; the full aggregate still used a sequential scan.

All commercial records are synthetic. Incremental ingestion, history backfill, a real quota feed, production publication and sharding are not implemented. These documents distinguish those future ideas from working features.

## Rebuild the PDFs

Install the separate documentation dependencies with `python -m pip install -r requirements-docs.txt`, then run `python scripts/render_study_docs.py` from the repository. The renderer reads these Markdown files, builds all three PDFs and checks horizontal text bounds. It uses DejaVu Sans fonts; set `STUDY_FONT_DIR` to the directory containing those fonts if they are not in the default Linux font location. App/model dependencies are unchanged.
