# DuckDB Query Guide



## H3 Geospatial Indexing

**All data uses H3 hexagons** (https://h3geo.org) - uniform hexagonal grid covering Earth

### Key Facts:
- Each h8 hexagon = **73.7327598 hectares** (≈ 0.737 km²)
- Always report AREAS, not hex counts
- **Always use** `APPROX_COUNT_DISTINCT(h8)` when counting (avoids double-counting)

### Area Conversion:
```sql
SELECT APPROX_COUNT_DISTINCT(h8) * 73.7327598 as area_hectares FROM ...
SELECT APPROX_COUNT_DISTINCT(h8) * 0.737327598 as area_km2 FROM ...
```

### Joining Different Resolutions:
Some datasets use different H3 resolutions (h8 vs h0-h4). Use `h3_cell_to_parent()` to convert:

```sql
-- iNaturalist has h4, wetlands has h8 → convert h8 to h4
JOIN read_parquet('s3://public-inat/range-maps/hex/**') pos 
    ON h3_cell_to_parent(wetlands.h8, 4) = pos.h4 
    AND wetlands.h0 = pos.h0  -- Always include h0 for partition pruning!
```

## Query Template

**EVERY query must start with this setup:**

```sql
SET THREADS=100; SET preserve_insertion_order=false; SET enable_object_cache=true; SET temp_directory='/tmp';
INSTALL httpfs; LOAD httpfs; INSTALL h3 FROM community; LOAD h3;
CREATE OR REPLACE SECRET s3 (TYPE S3, ENDPOINT 'rook-ceph-rgw-nautiluss3.rook', URL_STYLE 'path', USE_SSL 'false', KEY_ID '', SECRET '');
CREATE OR REPLACE SECRET outputs (TYPE S3, ENDPOINT 'minio.carlboettiger.info', URL_STYLE 'path', SCOPE 's3://public-outputs');

-- Your query here
SELECT ...
```

**Why these settings?**
- `THREADS=100` - Parallel S3 reads (I/O bound)
- `preserve_insertion_order=false` - Faster aggregation
- `enable_object_cache=true` - Reduces S3 requests
- `httpfs` - Required for S3 access
- `h3` - Required for H3 functions
- Two secrets: readonly data access + write access for outputs

**Generating Output Files:**
```sql
COPY (SELECT ...) TO 's3://public-outputs/wetlands/filename.csv' (FORMAT CSV, HEADER, OVERWRITE_OR_IGNORE);
```
Then provide download link: `https://minio.carlboettiger.info/public-outputs/wetlands/filename.csv`

## Query Optimization Essentials

### 1. Filter Small Tables First
```sql
-- Good: Filter country → then join to large dataset
WITH filtered AS (
  SELECT h8, h0 FROM read_parquet('s3://public-overturemaps/hex/countries.parquet')
  WHERE country = 'US'
)
SELECT ... FROM filtered JOIN read_parquet('s3://public-wetlands/glwd/hex/**') w 
ON filtered.h8 = w.h8 AND filtered.h0 = w.h0
```

### 2. Pre-filter Taxonomy
```sql
-- Good: Filter to birds before joining position data
WITH birds AS (
  SELECT id, scientificName FROM read_parquet('s3://public-inat/taxonomy/...')
  WHERE class = 'Aves'
)
SELECT ... FROM birds JOIN read_parquet('s3://public-inat/range-maps/hex/**') ...
```

### 3. ALWAYS Include h0 in Joins
```sql
-- Enables partition pruning → 5-20x faster
JOIN table2 ON table1.h8 = table2.h8 AND table1.h0 = table2.h0
```

## Example Query Pattern

```sql
-- Setup (always include)
SET THREADS=100; SET preserve_insertion_order=false; SET enable_object_cache=true; SET temp_directory='/tmp';
INSTALL httpfs; LOAD httpfs; INSTALL h3 FROM community; LOAD h3;
CREATE OR REPLACE SECRET s3 (TYPE S3, ENDPOINT 'rook-ceph-rgw-nautiluss3.rook', URL_STYLE 'path', USE_SSL 'false', KEY_ID '', SECRET '');
CREATE OR REPLACE SECRET outputs (TYPE S3, ENDPOINT 'minio.carlboettiger.info', URL_STYLE 'path', SCOPE 's3://public-outputs');

-- Example: Wetlands by category
SELECT c.category, 
       APPROX_COUNT_DISTINCT(w.h8) as hex_count,
       ROUND(hex_count * 73.7327598, 2) as area_hectares
FROM read_parquet('s3://public-wetlands/glwd/hex/**') w
JOIN read_csv('s3://public-wetlands/glwd/category_codes.csv') c ON w.Z = c.Z
WHERE w.Z > 0 
GROUP BY c.category 
ORDER BY area_hectares DESC;
```

## DuckDB SQL Syntax Reference

Here are some DuckDB SQL syntax specifics you should be aware of:
- MotherDuck is compatible with DuckDB Syntax, Functions, Statements, Keywords
- DuckDB use double quotes (") for identifiers that contain spaces or special characters, or to force case-sensitivity and single quotes (') to define string literals
- DuckDB can query CSV, Parquet, and JSON directly without loading them first, e.g. `SELECT * FROM 'data.csv';`
- DuckDB supports CREATE TABLE AS: `CREATE TABLE new_table AS SELECT * FROM old_table;`
- DuckDB queries can start with FROM, and optionally omit SELECT *, e.g. `FROM my_table WHERE condition;` is equivalent to `SELECT * FROM my_table WHERE condition;`
- DuckDB allows you to use SELECT without a FROM clause to generate a single row of results or to work with expressions directly, e.g. `SELECT 1 + 1 AS result;`
- DuckDB supports attaching multiple databases, unsing the ATTACH statement: `ATTACH 'my_database.duckdb' AS mydb;`. Tables within attached databases can be accessed using the dot notation (.), e.g. `SELECT * FROM mydb.table_name syntax`. The default databases doesn't require the do notation to access tables. The default database can be changed with the USE statement, e.g. `USE my_db;`.
- DuckDB is generally more lenient with implicit type conversions (e.g. `SELECT '42' + 1;` - Implicit cast, result is 43), but you can always be explicit using `::`, e.g. `SELECT '42'::INTEGER + 1;`
- DuckDB can extract parts of strings and lists using [start:end] or [start:end:step] syntax. Indexes start at 1. String slicing: `SELECT 'DuckDB'[1:4];`. Array/List slicing: `SELECT [1, 2, 3, 4][1:3];`
- DuckDB has a powerful way to select or transform multiple columns using patterns or functions. You can select columns matching a pattern: `SELECT COLUMNS('sales_.*') FROM sales_data;` or transform multiple columns with a function: `SELECT AVG(COLUMNS('sales_.*')) FROM sales_data;`
- DuckDB an easy way to include/exclude or modify columns when selecting all: e.g. Exclude: `SELECT * EXCLUDE (sensitive_data) FROM users;` Replace: `SELECT * REPLACE (UPPER(name) AS name) FROM users;`
- DuckDB has a shorthand for grouping/ordering by all non-aggregated/all columns. e.g `SELECT category, SUM(sales) FROM sales_data GROUP BY ALL;` and `SELECT * FROM my_table ORDER BY ALL;`
- DuckDB can combine tables by matching column names, not just their positions using UNION BY NAME. E.g. `SELECT * FROM table1 UNION BY NAME SELECT * FROM table2;`
- DuckDB has an inutitive syntax to create List/Struct/Map and Array types. Create complex types using intuitive syntax. List: `SELECT [1, 2, 3] AS my_list;`, Struct: `{'a': 1, 'b': 'text'} AS my_struct;`, Map: `MAP([1,2],['one','two']) as my_map;`. All types can also be nested into each other. Array types are fixed size, while list types have variable size. Compared to Structs, MAPs do not need to have the same keys present for each row, but keys can only be of type Integer or Varchar. Example: `CREATE TABLE example (my_list INTEGER[], my_struct STRUCT(a INTEGER, b TEXT), my_map MAP(INTEGER, VARCHAR),  my_array INTEGER[3], my_nested_struct STRUCT(a INTEGER, b Integer[3]));`
- DuckDB has an inutive syntax to access struct fields using dot notation (.) or brackets ([]) with the field name. Maps fields can be accessed by brackets ([]).
- DuckDB's way of converting between text and timestamps, and extract date parts. Current date as 'YYYY-MM-DD': `SELECT strftime(NOW(), '%Y-%m-%d');` String to timestamp: `SELECT strptime('2023-07-23', '%Y-%m-%d')::TIMESTAMP;`, Extract Year from date: `SELECT EXTRACT(YEAR FROM DATE '2023-07-23');`
- Column Aliases in WHERE/GROUP BY/HAVING: You can use column aliases defined in the SELECT clause within the WHERE, GROUP BY, and HAVING clauses. E.g.: `SELECT a + b AS total FROM my_table WHERE total > 10 GROUP BY total HAVING total < 20;`
- DuckDB allows generating lists using expressions similar to Python list comprehensions. E.g. `SELECT [x*2 FOR x IN [1, 2, 3]];` Returns [2, 4, 6].
- DuckDB allows chaining multiple function calls together using the dot (.) operator. E.g.: `SELECT 'DuckDB'.replace('Duck', 'Goose').upper(); -- Returns 'GOOSEDB';`
- DuckDB has a JSON data type. It supports selecting fields from the JSON with a JSON-Path expression using the arrow operator, -> (returns JSON) or ->> (returns text) with JSONPath expressions. For example: `SELECT data->'$.user.id' AS user_id, data->>'$.event_type' AS event_type FROM events;`
- DuckDB has built-in functions for regex regexp_matches(column, regex), regexp_replace(column, regex), and regexp_extract(column, regex).
- DuckDB has a way to quickly get a subset of your data with `SELECT * FROM large_table USING SAMPLE 10%;`

### Common DuckDB Functions:
`count`, `sum`, `max`, `coalesce`, `trunc`, `date_trunc`, `row_number`, `unnest`, `prompt`, `min`, `concat`, `avg`, `lower`, `read_csv_auto`, `read_parquet`, `strftime`, `array_agg`, `regexp_matches`, `replace`, `round`, `length`, `query`, `read_json_auto`, `range`, `date_diff`, `lag`, `year`, `now`, `group_concat`

### Common DuckDB Statements:
`FROM`, `SELECT`, `WHERE`, `ORDER BY`, `GROUP BY`, `JOIN`, `WITH`, `LIMIT`, `CASE`, `CREATE TABLE`, `SET`, `DROP`, `ALTER TABLE`, `HAVING`, `UPDATE`, `DESCRIBE`, `USE`, `INSERT`, `DELETE`, `COPY`, `ATTACH`, `CALL`, `CREATE VIEW`, `VALUES`, `INSTALL`, `PIVOT`, `FILTER`

### Common DuckDB Types:
`VARCHAR`, `INTEGER`, `NULL`, `DATE`, `TIME`, `TIMESTAMP`, `JSON`, `STRUCT`, `LIST`, `DECIMAL`, `ARRAY`, `FLOAT`, `BIGINT`, `DOUBLE`, `INTERVAL`, `BOOLEAN`, `UNION`, `ENUM`, `TINYINT`, `UUID`, `TIMESTAMP WITH TIME ZONE`, `SMALLINT`, `BLOB`

### Common DuckDB Keywords:
`AS`, `DISTINCT`, `IN`, `OVER`, `ALL`, `LIKE`
