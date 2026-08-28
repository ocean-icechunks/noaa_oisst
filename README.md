# NOAA OISST v2.1 Icechunked

[![Last update status][actions-badge]][actions-link]
[![Documentation Status][rtd-badge]][rtd-link]

[![PyPI version][pypi-version]][pypi-link]
[![Conda-Forge][conda-badge]][conda-link]
[![PyPI platforms][pypi-platforms]][pypi-link]

[![GitHub Discussion][github-discussions-badge]][github-discussions-link]

[![Coverage][coverage-badge]][coverage-link]

<!-- prettier-ignore-start -->
[actions-badge]:            https://github.com/oceanhackweek/ohw26_oisst_icechunk/actions/workflows/daily.yml/badge.svg
[actions-link]:             https://github.com/oceanhackweek/ohw26_oisst_icechunk/actions
[conda-badge]:              https://img.shields.io/conda/vn/conda-forge/ohw26_oisst_icechunk
[conda-link]:               https://github.com/conda-forge/ohw26_oisst_icechunk-feedstock
[github-discussions-badge]: https://img.shields.io/static/v1?label=Discussions&message=Ask&color=blue&logo=github
[github-discussions-link]:  https://github.com/oceanhackweek/ohw26_oisst_icechunk/discussions
[pypi-link]:                https://pypi.org/project/ohw26_oisst_icechunk/
[pypi-platforms]:           https://img.shields.io/pypi/pyversions/ohw26_oisst_icechunk
[pypi-version]:             https://img.shields.io/pypi/v/ohw26_oisst_icechunk
[rtd-badge]:                https://readthedocs.org/projects/ohw26_oisst_icechunk/badge/?version=latest
[rtd-link]:                 https://ohw26_oisst_icechunk.readthedocs.io/en/latest/?badge=latest
[coverage-badge]:           https://codecov.io/github/oceanhackweek/ohw26_oisst_icechunk/branch/main/graph/badge.svg
[coverage-link]:            https://codecov.io/github/oceanhackweek/ohw26_oisst_icechunk

<!-- prettier-ignore-end -->

[NOAA's Optimum Interpolation Sea Surface Temperature (OISST) v2.1][oisst]
Climate Data Record, published as an [Icechunk][icechunk] store for anonymous
public access. Maintained by [NERACOOS][neracoos] / [GMRI][gmri], started at
[OceanHackWeek 2026][ohw].

Daily data is published _virtually_ — we parse each NOAA NetCDF's chunk
references with [VirtualiZarr][vz] and commit those references. Monthly
statistics are computed data, chunked for timeseries access.

## Quick start

Anonymously readable — no AWS account, no credentials, no egress charges.

```python
import icechunk
import xarray as xr

NOAA = "s3://noaa-cdr-sea-surface-temp-optimum-interpolation-pds/"

config = icechunk.RepositoryConfig.default()
config.set_virtual_chunk_container(
    icechunk.VirtualChunkContainer(
        url_prefix=NOAA,
        store=icechunk.s3_store(region="us-east-1", anonymous=True),
    ),
)

# NOTE: destination-dependent (Stage 2). Source.coop variant shown; an Arraylake
# store opens via the arraylake client instead.
repo = icechunk.Repository.open(
    icechunk.s3_storage(
        bucket="<source-coop-bucket>",
        prefix="<account>/oisst",
        endpoint_url="https://data.source.coop",
        anonymous=True,
    ),
    config=config,
    authorize_virtual_chunk_access=icechunk.containers_credentials(
        {NOAA: icechunk.s3_anonymous_credentials()},
    ),
)
store = repo.readonly_session("main").store

monthly = xr.open_zarr(store, group="monthly", consolidated=False, zarr_format=3)
monthly["sst_mean"].sel(lat=43.5, lon=290.5, method="nearest").plot()
```

## What's in here

| Group      | Contents                                                           | Chunks            | Best for                             |
| ---------- | ------------------------------------------------------------------ | ----------------- | ------------------------------------ |
| `daily/`   | Every day 1981-09-01 → present, virtual references to NOAA's files | NOAA's            | Maps, single days, short windows     |
| `monthly/` | Per-variable `_min`/`_max`/`_mean`/`_std`, one step per month      | `(24, 1, 90, 90)` | Long timeseries at a point or region |

Source variables are `sst`, `anom`, `err`, and `ice`, so `monthly/` carries 16
variables (`sst_min`, `sst_max`, `sst_mean`, `sst_std`, `anom_min`, …), stored
as `int16` with `scale_factor=0.01` to match NOAA's own precision.

Reading 44 years at one point costs ~8.7 MB and 23 requests from `monthly/`.

## Preliminary vs final data

NOAA publishes a _preliminary_ file within a day or two of observation and
replaces it with the _final_ file roughly two weeks later. **Both live in
`daily/`.** As soon as a final file is available it replaces the preliminary one
in place, so you always get the best available data for any date without
stitching two datasets together.

A `preliminary` coordinate marks which is which:

```python
daily = xr.open_zarr(store, group="daily", consolidated=False, zarr_format=3)

# everything - most recent data, last ~2 weeks preliminary
daily["sst"].sel(time="2026-08")

# final data only
final = daily.set_xindex("preliminary").sel(preliminary=False)
```

`set_xindex` is needed once to make `preliminary` selectable;
`daily.isel(time=~daily.preliminary)` is the equivalent without it. `monthly/`
is computed from final data only and has no such coordinate.

## Update cadence

A daily GitHub Actions run ingests whatever is new: new preliminary days, finals
replacing the preliminary days they supersede, and a monthly rollup once every
day in a month is final.

## Things to know

- **Reading `daily/` reads NOAA's bucket**, so register the virtual chunk
  container as shown above. `monthly/` is real data and needs no container.
- **Preliminary references are transient.** NOAA deletes a preliminary file when
  it publishes the final one; we replace the reference on the next daily run. A
  missing-chunk error on a day still marked preliminary means you caught that
  ~24-hour window — re-read, or use `.sel(preliminary=False)`.
- **Time is calendar-ordered** in both groups; no `.sortby("time")` needed.
- **Library versions matter.** These stores are written with the
  `icechunk`/`virtualizarr` versions pinned in `pyproject.toml`; Icechunk spec
  changes across majors may require migration.

## Citation

Cite NOAA's CDR, not this repository — this is a repackaging, not a new product.
See [NOAA OISST v2.1][oisst].

## Maintaining this store

See [docs/operations.md](docs/operations.md) for the runbook and
[docs/chunking.md](docs/chunking.md) for why the chunk shapes are what they are.

```bash
pixi run oisst ingest-recent  --scan-months 2   # what the daily Action runs
pixi run oisst rollup-monthly --catch-up
pixi run oisst rechunk-slabs  --catch-up
pixi run oisst expire         --days 35
```

Every command diffs against what is already in the store and does only the
missing work, so they are safe to re-run and safe to interrupt.

[oisst]: https://www.ncei.noaa.gov/products/optimum-interpolation-sst
[icechunk]: https://icechunk.io
[vz]: https://virtualizarr.readthedocs.io
[neracoos]: https://neracoos.org
[gmri]: https://gmri.org
[ohw]: https://oceanhackweek.org
