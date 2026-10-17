# Benchmarks


`bench/harbor_nimcode.py` runs nimcode headless inside [Harbor](https://github.com/laude-institute/harbor) tasks (Terminal-Bench 2.0, Aider Polyglot, ...).

```bash
uv tool install harbor
python -m build --wheel --outdir bench/dist
export PYTHONPATH=. NVIDIA_API_KEY=nvapi-...
harbor run -d terminal-bench@2.0 -a bench.harbor_nimcode:NimcodeAgent -m <nim-model-id> -l 10
```

`NIMCODE_API_BASE_URL` points nimcode at any OpenAI-compatible endpoint. Real runs spend NVIDIA credits.
