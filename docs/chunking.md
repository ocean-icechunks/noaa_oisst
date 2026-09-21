# Chunking and encoding

_Pancakes and churros_

Numbers marked **measured** were taken from the complete local store on
2026-09-18: `daily/` 1981-09-01 to 2026-09-16 (16,452 days) and `monthly/`
1981-09 to 2026-08 (540 months).

The one exception is the encoding comparison table, which comes from an initial
test of a single month, because the rejected encodings were never written at
full scale.

The recipe at the end reproduces measured numbers.

## The shape of the problem

Each monthly variable is a grid of 540 months × 720 latitudes × 1440 longitudes,
stored as 2-byte integers: about 1.1 GB per variable before compression, and
there are 16 variables.

Zarr splits each variable into **chunks**: boxes of `t` months × `y` rows × `x`
columns, each stored as one object. A reader loads every whole chunk that
touches their selection, whether they use one value from it or all of them. So
the chunk shape decides how much data two very different readers download:

- A **timeseries** reader wants one point (or a small region) through all 540
  months. They need a column of chunks through time. Small `y` × `x` tiles and
  tall `t` help them.
- A **map** reader wants every point for one month. They need one layer of
  chunks across the whole grid. They download the entire `t`-month slab to get
  it, so tall `t` hurts them.

At a fixed chunk size (`t × y × x × 2 bytes`), making `t` taller forces the tile
smaller. The two readers trade off against each other along one dial, so the
choice is a matter of degree, not of picking a side.

`monthly/` is tuned for the timeseries reader. Daily maps are the common map
use, and `daily/` already serves those at NOAA's own one-map-per-chunk layout,
so a monthly map is the rare case and can afford to be a bit wasteful.

### Worked example: the chosen chunk shape `(time=24, zlev=1, lat=90, lon=90)`

For one variable, say `sst_mean`:

| Reader                           | Chunks touched                   | Bytes decoded | Compressed bytes on the wire (**measured**) |
| -------------------------------- | -------------------------------- | ------------- | ------------------------------------------- |
| Point timeseries, all 540 months | `ceil(540 / 24)` = 23            | 8.75 MB       | 3.1 MB                                      |
| Global map, one month            | `(720 / 90) × (1440 / 90)` = 128 | 49.8 MB       | 7.8 MB                                      |
| Monthly update (writer)          | 128 tiles of the trailing slab   | 49.8 MB       | ~10 MB (~155 MB for all 16 variables)       |

The same quantities as formulas, for a chunk `(t, 1, y, x)` over an `N`-month
record on a 720 × 1440 grid, 2 bytes per value:

| Quantity                            | Formula                               | With `(24, 1, 90, 90)`, `N = 540` |
| ----------------------------------- | ------------------------------------- | --------------------------------- |
| Bytes per chunk                     | `t × y × x × 2`                       | 0.39 MB                           |
| Timeseries requests                 | `ceil(N / t)`                         | 23                                |
| Timeseries bytes (one point)        | `N × y × x × 2`                       | 8.75 MB                           |
| Map requests (one month)            | `(720 / y) × (1440 / x)`              | 128                               |
| Map bytes (one month)               | `t × 720 × 1440 × 2`                  | 49.8 MB                           |
| Chunks rewritten per monthly update | `(720 / y) × (1440 / x)` per variable | 128 × 16 = 2,048                  |

Timeseries bytes depend only on the tile, and map bytes depend only on `t`: that
is the trade-off above written down.

## `monthly/`: `(time=24, zlev=1, lat=90, lon=90)`, int16

- **Chunks are small.** 0.39 MB uncompressed, 74 KB compressed on average
  (**measured**), comfortably inside the range where per-request overhead and
  wasted bytes are both minor.
- **The grid divides evenly.** `720 / 90 = 8` and `1440 / 90 = 16` give 128
  tiles with no ragged edges. `ceil(540 / 24) = 23` time slabs. 23 × 128 × 16
  variables = **47,104 chunks**, all present (**measured**). Each variable's
  manifest is 53–70 KB (**measured**), so Zarr v3 sharding is unnecessary.
- **A point timeseries is cheap.** 23 requests and 3.1 MB on the wire for
  `sst_mean` (**measured**; 8.75 MB once decoded). The previous layout, one
  global float32 map per chunk, needed 540 requests and 2.2 GB for the same
  answer.
- **A small region is still one round of requests.** The Gulf of Maine box (lat
  39–46 N, lon 284–294 E) straddles a tile edge in both latitude (row 540) and
  longitude (column 1170), so it touches 4 tiles: 92 chunks and 13.3 MB on the
  wire per variable (**measured**), fetched in parallel.
- **Each monthly update rewrites the trailing slab**: 2,048 chunk objects plus
  the `time` coordinate (**measured** from the 2026-08 rollup's commit diff). A
  full slab is about 155 MB compressed across all 16 variables (**measured**).
  The record is 540 months, so the 23rd slab holds 12 months, currently 71 MB,
  and stays partially filled until 2027-08.
- **The shape does not need revisiting.** The ideal `t` grows with the square
  root of the record length, so it would take decades to drift from 24 to the
  next sensible value. No rechunking treadmill.
- **Superseded chunks accumulate.** Because every rollup rewrites a slab, the
  full local backfill (540 rollups) left 43.0 GB of chunk objects on disk for
  3.47 GB of live data (**measured**). That is why expiry and garbage collection
  exist; see [operations.md](operations.md). Do not use `du` to size the store.

## Encoding (**measured**)

All four source variables are `int16` with `scale_factor=0.01` at NOAA and
decode to float32; the statistics stay float32 through numpy. Storing them as
int16 again costs no precision the source did not already lose.

Candidate encodings, from the Stage 0 spike on one month (2020-01), 16
variables, at single-timestep spatial chunks `(1, 1, 90, 90)`:

| Encoding                         | MB / month | Full record, extrapolated |
| -------------------------------- | ---------- | ------------------------- |
| float32 + zstd-3                 | 18.85      | ~10.2 GB                  |
| float32 + blosc-zstd-3 + shuffle | 20.18      | ~10.9 GB                  |
| int16 + zstd-3                   | 7.08       | ~3.8 GB                   |
| int16 + blosc-zstd-3 + shuffle   | 6.02       | ~3.2 GB                   |
| int16 + blosc-zstd-1 + shuffle   | 6.28       | ~3.4 GB                   |
| int16 + blosc-zstd-9 + shuffle   | 5.71       | ~3.1 GB                   |
| **int16 per-var scale (chosen)** | **6.77**   | **~3.7 GB**               |

Chosen: **int16, `_FillValue=-32768`, blosc-zstd level 3 with shuffle**. zstd-9
buys only ~5% at real CPU cost, and plain zstd without shuffle is ~18% worse.

On the complete store, at the real `(24, 1, 90, 90)` slabs, the chosen encoding
measures **6.42 MB / month and 3.47 GB for the full record**, a 5.3× compression
over the 18.3 GB of raw chunk bytes. The slab layout compresses about 5% better
than the spike predicted, as expected. Per variable:

| Variables                           | Full record (**measured**) | Why                                          |
| ----------------------------------- | -------------------------- | -------------------------------------------- |
| `sst_min`, `sst_max`, `sst_mean`    | 351–371 MB each            | Smooth fields, 67% of cells are ocean        |
| `anom_min`, `anom_max`, `anom_mean` | 345–362 MB each            | As above                                     |
| `sst_std`, `anom_std`               | 233–242 MB each            | Smaller values, more repeated                |
| `err_*`                             | 40–244 MB each             | Narrow range (0.11–1.71)                     |
| `ice_*`                             | 34–67 MB each              | Finite on only 14% of cells; ice-free is NaN |

On fill values: `monthly_encoding()` sets `_FillValue=-32768`, and land decodes
to NaN through xarray. The zarr array metadata itself shows `fill_value: 0`;
that is xarray's convention for `int16` with a `_FillValue` attribute, not a
mismatch.

### Per-variable `scale_factor`

A `scale_factor` of 0.01 gives 0.005 precision and a range of ±327, far beyond
the largest value in the record (`sst_max` reaches 38.58, **measured**). The
problem is small values, not large ones: `err` and `ice` statistics are 0–1
fractions, and their standard deviations are often below 0.005, which 0.01
rounds to zero. So:

| Variables         | `scale_factor` | Max round-trip error |
| ----------------- | -------------- | -------------------- |
| `sst_*`, `anom_*` | 0.01           | 0.005                |
| `err_*`, `ice_*`  | 0.001          | 0.0005               |

Over the full record (**measured**), the share of finite values that would round
to zero:

| Variable  | At scale 0.01 | At scale 0.001 (stored) |
| --------- | ------------- | ----------------------- |
| `err_std` | 19.8%         | 17.1%                   |
| `ice_std` | 12.5%         | 7.3%                    |

The Stage 0 spike measured 56% for `ice_std` on January 2020 alone, which
overstated the problem, but the finer scale still recovers a meaningful slice of
real variability for about +0.75 MB / month. The values that remain zero at
0.001 are genuinely below 0.0005.

These live in `ohw26_oisst_icechunk.config.monthly_encoding()`.

## `daily/`

`daily/` holds virtual references at NOAA's own layout rather than copied data.
Each daily NetCDF holds exactly one chunk per variable (`(1, 1, 720, 1440)`,
shuffle + zlib), so a whole-day map is one request and the preliminary-to-final
swap is four `set_virtual_ref` calls. At 16,452 days that is 65,808 virtual
references (**measured**), about 223 KB of manifest per variable.

The one real data array is the `preliminary` coordinate: a bool, stored as int8,
chunked `(1024,)`, 17 chunks for the whole record (**measured**).

## Re-measuring

Measure the live snapshot, never the directory: `du` over `chunks/` (and
`repo.chunk_storage_stats()`) count every superseded slab as well as the live
one. Read-only, from the repo root:

```python
import asyncio

import icechunk
import zarr
from zarr.core.buffer import default_buffer_prototype

repo = icechunk.Repository.open(
    icechunk.local_filesystem_storage("local-store/oisst.icechunk")
)
session = repo.readonly_session("main")
monthly = zarr.open_group(session.store, mode="r")["monthly"]
proto = default_buffer_prototype()


async def chunk_bytes(name):
    nt, _, ny, nx = monthly[name].cdata_shape
    keys = [
        f"monthly/{name}/c/{t}/0/{y}/{x}"
        for t in range(nt)
        for y in range(ny)
        for x in range(nx)
    ]
    bufs = await asyncio.gather(*(session.store.get(k, proto) for k in keys))
    return sum(len(b) for b in bufs if b is not None)


sizes = {n: asyncio.run(chunk_bytes(n)) for n, a in monthly.arrays() if a.ndim == 4}
total = sum(sizes.values())
print(f"{total / 1e9:.2f} GB live, {total / 540 / 1e6:.2f} MB/month")
```

Sum only the keys a reader would touch (one `y, x` across all `t`, or all `y, x`
for one `t`) to get the timeseries and map costs. For write amplification, take
the last "Monthly rollup" snapshot from `repo.ancestry(branch="main")` and count
`updated_chunks` in
`repo.diff(from_snapshot_id=<parent>, to_snapshot_id=<rollup>)`.
