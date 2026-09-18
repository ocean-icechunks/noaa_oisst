# Chunking and encoding

Why the store's chunk shapes and dtypes are what they are. Numbers marked
**measured** come from the Stage 0 spikes (2026-08-28, one real month of data);
see `Stage_0.md` in the repo root for the raw runs.

## Sizing identities

Grid `A_grid = 720 × 1440`, `N ≈ 540` months, dtype width `d`, chunk
`(t_c, 1, y_c, x_c)`, `A_c = y_c · x_c`:

| Quantity                               | Formula                               |
| -------------------------------------- | ------------------------------------- |
| chunk bytes                            | `t_c · A_c · d`                       |
| timeseries bytes (point, full record)  | `N · A_c · d` — spatial tile alone    |
| timeseries requests                    | `ceil(N / t_c)`                       |
| map bytes (one month, global)          | `t_c · A_grid · d` — time chunk alone |
| write amplification per monthly update | `t_c`                                 |

The two read terms trade directly at fixed chunk size, so churro-vs-pancake is a
scalar, not a binary. Monthly maps are deprioritised (daily maps are served by
`daily/` at NOAA's own chunking), so `monthly/` tunes for timeseries.

## `monthly/`: `(time=24, zlev=1, lat=90, lon=90)`, int16

- 0.39 MB/chunk uncompressed — comfortable.
- `720/90 = 8`, `1440/90 = 16` → 128 tiles, no ragged edges. `ceil(540/24) = 23`
  slabs → 23 × 128 × 16 vars = **47,104 chunks**; manifests stay trivial, Zarr
  v3 sharding unnecessary.
- Point timeseries, full record: **8.7 MB / 23 requests** vs 2,240 MB / 540
  requests with the old `(1, 1, 720, 1440)` layout — 257× less data.
- Gulf of Maine (lat 39–46 N, lon 284–294 E) lands in a single 90×90 tile: 23
  chunks, 8.7 MB.
- Each monthly update rewrites the trailing 24-month slab (2,048 chunk objects);
  since `t_c ∝ sqrt(N)`, the optimum only reaches 33 around 2071 — no rechunking
  treadmill.

## Encoding (**measured**)

All four source variables are `int16` + `scale_factor=0.01` at NOAA, decoded to
float32; the statistics stay float32 through numpy. Candidate encodings for one
real month (2020-01), 16 variables, spatial chunks `(1, 1, 90, 90)`:

| Encoding                           | MB / month | Full record (×540) |
| ---------------------------------- | ---------- | ------------------ |
| float32 + zstd-3                   | 18.85      | ~10.2 GB           |
| float32 + blosc-zstd-3 + shuffle   | 20.18      | ~10.9 GB           |
| int16 + zstd-3                     | 7.08       | ~3.8 GB            |
| **int16 + blosc-zstd-3 + shuffle** | **6.02**   | **~3.2 GB**        |
| int16 + blosc-zstd-1 + shuffle     | 6.28       | ~3.4 GB            |
| int16 + blosc-zstd-9 + shuffle     | 5.71       | ~3.1 GB            |
| int16 per-var scale (chosen)       | 6.77       | ~3.7 GB            |

Chosen: **int16, `_FillValue=-32768`, blosc-zstd level 3 with shuffle** (zstd-9
buys only ~5% at real CPU cost; plain zstd without shuffle is ~18% worse).

### Per-variable `scale_factor`

The largest magnitude across all 16 statistics is `sst_max` ≈ 32.5, far inside
int16's ±327 budget at scale 0.01. But `err`/`ice` statistics are 0–1 fractions,
and at scale 0.01 **56% of finite `ice_std` and 19% of `err_std` values round to
zero** (measured). So:

| Variables         | `scale_factor` | max round-trip error |
| ----------------- | -------------- | -------------------- |
| `sst_*`, `anom_*` | 0.01           | 0.005                |
| `err_*`, `ice_*`  | 0.001          | 0.0005               |

The finer scale costs +0.75 MB/month (~0.5 GB over the record).

These live in `ohw26_oisst_icechunk.config.monthly_encoding()`.

## `daily/`

Virtual references at NOAA's own layout: each daily file holds exactly one chunk
per variable (`(1, 1, 720, 1440)`), so `daily/` serves whole-day maps in one
request and the prelim→final swap is four `set_virtual_ref` calls. The one real
array is the `preliminary` bool coordinate, chunked `(1024,)` (~16 KB total).

## To re-measure

The encoding table above was measured at single-timestep spatial chunks; the
real `(24, 1, 90, 90)` slabs should compress the same or slightly better. Re-run
the measurement over a written 24-month slab once a longer backfill exists, and
update the table.
