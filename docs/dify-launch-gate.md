# Dify Launch Gate

`dify-aio` is intentionally split into two tracks:

- workflow consolidation, which can merge with the rest of the fleet;
- Community Apps XML launch, which stays gated until source/runtime validation is clean.

`fleet.yml` now marks `dify-aio` as `catalog_published: true` because the
source/template/runtime launch gate passed. This means `aio-fleet
sync-catalog` may sync `dify-aio.xml` into `awesome-unraid`, and catalog
validation expects that XML to exist in the catalog repo.

Before changing or re-syncing `dify-aio.xml` into `awesome-unraid`, run:

```bash
python scripts/validate-template.py --all
python scripts/generate_dify_template.py --check
bash scripts/validate-derived-repo.sh .
python -m pytest tests/unit tests/template
python -m pytest tests/integration -m 'not extended_integration'
AIO_PYTEST_USE_PREBUILT_IMAGE=true python -m pytest tests/integration -m extended_integration
git diff --check
```

Then confirm:

- required template fields remain minimal;
- secret-like fields are masked;
- generated defaults do not include upstream placeholders;
- advanced vector, storage, database, cache, mail, and security fields still match the intended source surface;
- `TemplateURL` and `Icon` point to `awesome-unraid`;
- `python -m aio_fleet sync-catalog --repo dify-aio --catalog-path ../awesome-unraid --dry-run` reports the exact catalog XML/icon changes;
- catalog PRs sync Dify XML only after the source repo is ready and the manifest flag remains `true`.
