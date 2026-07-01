"""core httpfs vs cache_httpfs on NRP Ceph (issue #250). cache_httpfs with a
capped fanout (unlimited fanout x high threads exhausts RGW connections ->
'Could not connect to server'). Compares full-wire read (sum all cols) in Gb/s."""
import os, time, resource
try: resource.setrlimit(resource.RLIMIT_NOFILE,(1<<18,1<<18))
except Exception as e: print("setrlimit warn",e)
import duckdb
EP=os.environ.get("MRE_ENDPOINT","rook-ceph-rgw-nautiluss3.rook")
G=f"s3://{os.environ.get('MRE_BUCKET','public-carbon')}/{os.environ.get('MRE_PREFIX','vulnerable-carbon-2024/hex/')}**"
THREADS=[int(x) for x in os.environ.get("MRE_THREADS","32,48").split(",")]
FANOUT=os.environ.get("MRE_FANOUT","8")
def con(cache):
    c=duckdb.connect()
    c.execute("INSTALL cache_httpfs FROM community; LOAD cache_httpfs" if cache else "INSTALL httpfs; LOAD httpfs")
    c.execute(f"SET threads={{}}; SET preserve_insertion_order=false; SET memory_limit='8GB'; SET temp_directory='/tmp'".format(THREADS[0]))
    c.execute(f"SET s3_endpoint='{EP}'; SET s3_url_style='path'; SET s3_use_ssl=false; SET s3_access_key_id=''; SET s3_secret_access_key=''")
    if cache: c.execute(f"SET cache_httpfs_max_fanout_subrequest={FANOUT}")
    return c
c=con(False)
cols=[r[0] for r in c.execute(f"DESCRIBE SELECT * FROM read_parquet('{G}' LIMIT 0)" if False else f"SELECT * FROM read_parquet('{G}') LIMIT 0").description]
comp=c.execute(f"SELECT sum(total_compressed_size) FROM parquet_metadata('{G}')").fetchone()[0]
q="SELECT "+",".join(f"sum({c2})" for c2 in cols)+f" FROM read_parquet('{G}')"
print(f"node={os.environ.get('NODE_NAME','?')} endpoint={EP} comp={comp/1e9:.1f}GB cols={len(cols)}",flush=True)
for cache in [False,True]:
    for T in THREADS:
        try:
            cc=con(cache); cc.execute(f"SET threads={T}")
            t0=time.perf_counter(); cc.execute(q).fetchall(); dt=time.perf_counter()-t0; cc.close()
            print(f"  {'cache_httpfs' if cache else 'core httpfs '} T={T:<3} {dt:6.1f}s {comp*8/1e9/dt:5.1f} Gb/s",flush=True)
        except Exception as e:
            print(f"  {'cache_httpfs' if cache else 'core httpfs '} T={T:<3} ERR {str(e)[:90]}",flush=True)
