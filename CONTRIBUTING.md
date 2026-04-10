# Contributing

## Setup

```bash
git clone https://github.com/chancestarseeker/multi-agent-coordination-framework.git
cd multi-agent-coordination-framework
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
pip install pytest
```

Requires Python 3.10+.

## Running tests

```bash
python -m pytest cli/tests/ -v
```

All tests run without LLM API keys. To run the orchestrator itself, you
need `ANTHROPIC_API_KEY` and/or `OPENAI_API_KEY` set.

## Project structure

```
cli/
├── schema.py        # Pydantic models, enums (leaf — imports nothing internal)
├── config.py        # Paths, config loading, provider routing
├── prompts.py       # LLM prompt templates
├── parsing.py       # JSON extraction from LLM responses
├── ledger.py        # Entry persistence, querying, summary
├── signals.py       # Signal I/O, per-type handlers
├── breakers.py      # Circuit breakers (confidence, conflict, repetition, resource, timeout)
├── retry.py         # LLM call + validate-and-retry loop
├── roles.py         # Role lifecycle state machine (offer/accept/refuse/rotate/stepdown)
├── review.py        # run_review + capability routing
├── repair.py        # run_repair + verification rerun
├── synthesis.py     # run_synthesis + mode transitions
├── orchestrator.py  # CLI shell (argparse + dispatch)
└── tests/
    └── test_orchestrator.py
```

The dependency graph is a DAG rooted at `schema.py`.

## Adding a new signal handler

1. Add your handler function to `cli/signals.py`
2. Add it to the `SIGNAL_HANDLERS` dict in the same file
3. If your handler introduces a new signal type, add it to `VALID_SIGNAL_TYPES` in `cli/schema.py`
4. Add tests in `cli/tests/test_orchestrator.py`

## Adding a new circuit breaker

1. Add the breaker function to `cli/breakers.py`
2. Wire it into the review loop in `cli/review.py` (and `repair.py`/`synthesis.py` if applicable)
3. Add a config key under `circuit_breakers` in `cli/config.json`
4. Add tests

## Adding a new CLI command

1. Add an argparse subparser in `cli/orchestrator.py`
2. Add dispatch in the `main()` function
3. Implement the handler in the appropriate module

## Adding a new participant

Drop a JSON file into `cli/participants/declarations/`. See the existing
declarations for the schema. The participant will be picked up automatically
on the next command invocation.
