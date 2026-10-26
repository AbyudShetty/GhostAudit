# Development

Run commands from the repository root unless a command explicitly changes directories. Python 3.10 or newer is required; the original development environment was Windows 11 with Python 3.10.0. Other operating systems have not been independently validated.

## Setup and checks

```sh
python -m pip install -r code/requirements.txt
python code/main.py validate-samples
python code/main.py inventory --json
```

Run the full suite from `code/`:

```sh
cd code
python -m unittest discover -s tests -t .
cd ..
```

Tests cover parsers, historical identities, temporal boundaries, policy treatments, account attribution, output ordering, runtime purity and packaging. Tests requiring the challenge dataset skip when it is absent. Keep `dataset/` next to `code/` to run the full integration suite.

## Reproduce the evaluation

```sh
python code/analysis/m5_evaluation.py
python evaluation/custom_cases.py
python code/main.py predict --samples --out local/samples.csv
python evaluation/main.py --pred local/samples.csv
```

The scorer compares against the 32 public sample answers. There are no available reference answers for the 518 evaluation requests, so validation of those rows measures structure and evidence conventions, not accuracy. Six synthetic scenarios exercise seven request results independently of the public samples.

The [evaluation report](../evaluation/evaluation_report.md) gives metric definitions and limitations; the [usage report](../evaluation/usage_report.md) records final-run model calls, tokens and cost.

## CLI reference

| Command | Behavior |
| --- | --- |
| `python code/main.py` | Process all evaluation requests and write root `output.csv`. |
| `python code/main.py submit --out local/predictions.csv` | Write the same evaluation predictions to another location. |
| `python code/main.py predict --out local/predictions.csv` | Predict without permitting an overwrite of root `output.csv`. |
| `python code/main.py predict --samples --out local/samples.csv` | Predict the 32 sample requests. |
| `python code/main.py validate-samples` | Validate the supplied sample answers' structure. |
| `python code/main.py inventory [--json]` | Report parser coverage and failures. |
| `python code/main.py --dataset PATH ...` | Read a dataset stored elsewhere. |

The default dataset path is relative to the repository. Explicit relative `--dataset` and `--out` paths are relative to the current directory. Commands also work from `code/` using `python main.py` or `python -m ghostaudit`.

For `submit` and `predict`, repeat `--emit-proofs KIND` to enable additional complete-case proof types: `inferred-identity`, `scoped-snapshot` or `document-or-ocr`. Inferred identity is already enabled by default. `--emit-inferred-proofs` enables all three. These switches change the final candidate's proof policy; review the measured effects before using them.

## Maintain the OCR cache

```sh
python code/tools/scrub_ocr_cache.py --dataset dataset --check
```

Rebuilding the original OCR cache requires Windows and PowerShell:

```sh
python code/tools/build_ocr_cache.py
python code/tools/scrub_ocr_cache.py --dataset dataset --write
```

## Build a challenge archive

```sh
python code/tools/package.py
python code/tools/package.py --verify code.zip --dataset dataset
```

The deterministic archive contains `README.md`, `code/`, `docs/` and `evaluation/`. The required reports live at `evaluation/evaluation_report.md` and `evaluation/usage_report.md`. Packaging excludes local data, transcripts, predictions, environments, caches, credentials and nested archives. It reopens the archive to check names, required content, Python syntax and report coverage. With `--dataset`, it also checks for dataset identifiers outside intentionally documented files.

The packager uses forward slashes in ZIP entry names on every platform. Prefer it to a generic directory archive command. `--out PATH` builds a review archive elsewhere without replacing the final `code.zip`.

## Repository housekeeping

`local/` holds private scratch notes, generated per-case reports and test predictions. `dataset/`, `output.csv`, `code.zip`, `log.txt`, agent traces and local configuration are ignored. The OCR cache is tracked because predictions need it.

An ignore rule does not remove files already tracked by Git. The [publishing guide](publishing.md) includes the one-time index cleanup and push commands. No Git history rewrite is needed for this cleanup; files in earlier commits remain in that history.
