"""Classification of every ```sql block in the guidance files (#369).

Keyed by `<file>:<sha1(block-text)[:10]>` — see run.py. Editing a block changes
its key, forcing re-classification (a stale entry then fails CI).

EXECUTABLE[key] = {placeholder: value} — every `<placeholder>` in the block must
be mapped; the substituted statement is executed against public data.

FRAGMENTS[key] = "reason" — an illustrative snippet that is not a standalone
statement (a bare clause, a CTE referencing an undefined relation, a write-path
COPY, or a real query whose datasets are not yet mapped).

Placeholders map to a SINGLE `h0=` partition so a block binds every path and
column and runs cheaply — enough to catch a moved column (#364), a renamed
function, or a path that no longer resolves, which is what the model gate cannot
see. It is not a result-value check (that is the model gate's job).

Datasets/paths verified 2026-08-11. To grow coverage, promote a FRAGMENT whose
reason is "not yet mapped" to EXECUTABLE with real mappings and confirm it runs.
"""

# One CA partition present in every dataset used below (verified).
_H0 = "577762574070710271"

# ca30x30 conserved-areas assessment family (the #364 coarse-overlay surface)
ECO8 = f"s3://public-ca30x30/ecoregion/hex-res8/h0={_H0}/data_0.parquet"          # h8, nland, land_area_km2, h0
CW8 = f"s3://public-ca30x30/conserved-areas-terrestrial-2025/hex-weights-res8/h0={_H0}/data_0.parquet"  # h8, w1..w4, h0
# res-10 members of the same family (the #367 design-pass overlay/presence examples)
ECO10 = f"s3://public-ca30x30/ecoregion/hex/h0={_H0}/data_0.parquet"              # h8, h9, h10 land grid, h0
CWHR = f"s3://public-ca30x30/cwhr13/hex-fractions/h0={_H0}/data_0.parquet"        # whr13num, frac, h10, h0
CW10 = f"s3://public-ca30x30/conserved-areas-terrestrial-2025/hex-weights/h0={_H0}/data_0.parquet"  # h10, w1..w4, h0
# line + presence layers (#363 line-length, #355 presence-only)
NHD = f"s3://public-usgs-nhd/nhdplus-hr/flowline/hex/h0={_H0}/data_0.parquet"     # _cng_fid, lengthkm, innetwork, streamorde, h8, h0
NWI = f"s3://public-wetlands/nwi-v2/hex/h0={_H0}/data_0.parquet"                  # WETLAND_TYPE, state_code, h10, h0
ACE = f"s3://public-cdfw/ace/terrestrial-biodiversity-summary/hex/h0={_H0}/data_0.parquet"  # BioRankSW, h8, h0
# line + AOI GeoParquet pair for the AOI-clip recipe (#394). Deliberately a pair
# whose geometry columns DIFFER — trails `geometry` vs census `geom` — since asserting
# one name for both is exactly the defect this block shipped with.
TRAILS_HEX = f"s3://public-trails/federal-trails-2026/hex/h0={_H0}/data_0.parquet"   # _cng_fid, length_miles, h8, h0
TRAILS_GPQ = "s3://public-trails/federal-trails-2026.parquet"                        # _cng_fid, length_miles, geometry
CD_HEX = f"s3://public-census/census-2024/cd/hex/h0={_H0}/data_0.parquet"            # _cng_fid, GEOID, h8, h0
CD_GPQ = "s3://public-census/census-2024/cd.parquet"                                 # _cng_fid, GEOID, NAMELSAD, geom

# cross-dataset staples
CENSUS = f"s3://public-census/census-2024/state/hex/h0={_H0}/data_0.parquet"      # h8, STUSPS, GEOID, h0
CARBON = f"s3://public-carbon/irrecoverable-carbon-2024/hex/h0={_H0}/data_0.parquet"  # carbon, h5..h9, h0

EXECUTABLE = {
    # [1] region/feature area in acres — DISTINCT cells, h3_cell_area
    "h3-guide.md:eec81dd65f": {"<hex>": ECO8, "<scope>": "nland > 0"},
    # [3] feature centroid via h3_cell_to_lat/lng
    "h3-guide.md:c39c327be6": {"<hex parquet path>": ECO8, "<feature filter>": "nland > 0"},
    # [5] DESCRIBE — which resolution columns exist
    "h3-guide.md:7a099891c4": {"<STAC_PATH>": ECO8},
    # [8] value masked to a state via IN (h0) + IN (h8) subqueries
    "h3-guide.md:3b2c2a2998": {"<value_hex>": CARBON, "<census_state_hex>": CENSUS},
    # [9] same, via SEMI JOIN (the recommended mask-before-aggregate form)
    "h3-guide.md:b6baef4cdf": {"<value_hex>": CARBON, "<census_state_hex>": CENSUS},
    # [16] res-8 coarse-feature overlay weighted by land_area_km2 + (w1+w2) —
    #      the exact shape #364 shipped broken; binds land_area_km2 (ecoregion)
    #      and w1/w2 (weights) so a moved column fails here.
    "h3-guide.md:40c748d887": {
        "<res-8 feature hex>": ECO8,
        "<feature filter>": "nland > 0",
        "<ecoregion hex-res8>": ECO8,
        "<conserved-areas hex-weights-res8>": CW8,
    },
    # res-10 fractional overlay (#289/#367): frac x GAP weight x area, weight
    #   inside the SUM; binds cwhr13 fractions + res-10 land grid + res-10 weights.
    "h3-guide.md:a3af36a53b": {
        "<ecoregion hex>": ECO10,
        "<cwhr13 hex-fractions>": CWHR,
        "<conserved-areas hex-weights>": CW10,
    },
    # line-network conserved share (#363): dedup lengthkm per _cng_fid, length-
    #   weight the res-8 GAP fraction. This is the shape that crashed pre-#378
    #   (nhd-flowline SEMI JOIN), so it also guards that regression.
    "h3-guide.md:0c6ddd1d29": {
        "<ecoregion hex-res8>": ECO8,
        "<line hex>": NHD,
        "<feature filter>": "s.innetwork = 1 AND s.streamorde IN (1, 2)",
        "<conserved-areas hex-weights-res8>": CW8,
    },
    # AOI-clipped line mileage (#394). Was a FRAGMENT ("AOI geoparquets not
    #   mapped") — which is why the block shipped with `tg.geometry` against
    #   datasets whose column is `geom`, and with ST_Length (degrees) divided by
    #   1609.344. Binding this pair is what catches the first of those; the
    #   trap cell tplca-trail-miles-siskiyou (geo-agent-benchmark#34) catches
    #   the second. Mapped to a line/AOI pair with DIFFERENT geometry column
    #   names on purpose.
    "h3-guide.md:989f348f87": {
        "<line_hex>": TRAILS_HEX,
        "<aoi_hex>": CD_HEX,
        "<line_geoparquet>": TRAILS_GPQ,
        "<aoi_geoparquet>": CD_GPQ,
        "<line_geom>": "geometry",
        "<aoi_geom>": "geom",
        "<aoi_name>": "NAMELSAD",
    },
    # presence-only over res-10 land children (#355): mean of a 0/1 flag, never
    #   promoted to the res-8 parent. ACE feature x NWI wetlands, no GAP column.
    "h3-guide.md:da766d501c": {
        "<res-8 feature hex>": ACE,
        "<feature filter>": "BioRankSW = 5",
        "<ecoregion hex>": ECO10,
        "<presence hex>": NWI,
        "<presence filter>": (
            "state_code = 'CA' AND WETLAND_TYPE IN ("
            "'Freshwater Emergent Wetland','Freshwater Forested/Shrub Wetland',"
            "'Estuarine and Marine Wetland')"
        ),
    },
    # [19] rows-per-hex profiling on a single partition
    "h3-guide.md:2aab9a5de8": {"<STAC_HEX_PATH_SINGLE_PARTITION>": ECO8},
    # query-setup.md — the required SET/LOAD preamble (no S3); validates it runs
    "query-setup.md:0cc5a10e39": {},
}

FRAGMENTS = {
    # --- h3-guide.md ---
    "h3-guide.md:523cd143ac": "two-query block; the per-group form needs a literal `state` column not present in a clean mapping",
    "h3-guide.md:21d7da17ad": "incomplete — `FROM ...` ellipsis (illustrative approx-area shortcut)",
    "h3-guide.md:da9876be19": "incomplete — `FROM ...` ellipsis (great-circle distance illustration)",
    "h3-guide.md:39e204c12b": "GEBCO x geomorphology h6 join; two co-indexed datasets not yet mapped",
    "h3-guide.md:e258e8fec8": "bare JOIN clauses over undefined dataset_a/b, wdpa/gfw (resolution-conversion idiom)",
    "h3-guide.md:598ddf3355": "needs dataset-specific columns (_cng_fid, amount, state_id); no representative mapping",
    "h3-guide.md:e2dfe983ec": "CTE fragment referencing an undefined `flat_funding` relation",
    "h3-guide.md:ec06ef4087": "CTE fragment referencing undefined `countries`/`carbon_data` relations",
    "h3-guide.md:377ec327e2": "two-query block over generic `value`/`class` columns (SUM/AVG vs MODE illustration)",
    "h3-guide.md:b774ac61d1": "single-layer fractional overlay over a literal `class` column; no mapped dataset uses that column name (cwhr13 uses `whr13num`)",
    "h3-guide.md:1ccc1fb7d7": "references an undefined `mask` relation (DPP mask-first illustration)",
    "h3-guide.md:ae094c7f8c": "COPY to a write path with a `SELECT ...` ellipsis (export idiom)",
    # --- query-optimization.md ---
    "query-optimization.md:2f7793ba77": "bare JOIN clause (include-h0-in-join idiom)",
    "query-optimization.md:a941d3b637": "regions x padus x carbon 3-way; regions/padus hex paths not yet mapped",
    "query-optimization.md:0681d81d61": "references an undefined `scope` CTE (join-before-aggregate illustration)",
    "query-optimization.md:48c32ed53a": "`<bucket>/<dataset>` with `_cng_fid` and `data_00.parquet`; dataset not yet mapped",
    "query-optimization.md:e103e0c492": "`read_parquet('…')` ellipsis placeholder (DESCRIBE-to-find-a-column idiom)",
    "query-optimization.md:b344ccbf7e": "bare WHERE clause (fuzzy text match)",
    "query-optimization.md:557a390cc8": "bare WHERE clause (exact text match)",
    "query-optimization.md:b63ace2673": "bare WHERE clauses incl. a deliberate parse-error illustration; not executable",
    "query-optimization.md:29b78ebc22": "generic `value`/`lc_class` columns with sentinel exclusion; dataset not yet mapped",
}
