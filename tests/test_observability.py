import json
import sys
import tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent.providers import LocalStore
from agent.observability_stage import run
from agent import skills


def cfg(root, **values):
    base={"input_mode":"atlas_api","storage_provider":"local","bucket":str(root),"prefix":"p","cloud":"local","observability_enabled":True,"deployment_type":"atlas","index_stats_hosts":["n1","n2"]}
    base.update(values); return base


def test_existing_bucket_never_loads_collectors_and_reuses_query_shapes():
    root=Path(tempfile.mkdtemp()); st=LocalStore(root)
    key="p/2026-09-26/n1/queryStats/query-stats.json"
    st.put_text(key, json.dumps({"source":"uploaded"}), "application/json")
    result=run({"input_mode":"existing_bucket","storage_provider":"local","bucket":str(root),"prefix":"p","cloud":"local","observability_enabled":True},"2026-09-26",st)
    assert result["status"]=="reused_existing_bucket"
    assert result["nodes"][0]["host"]=="n1"


def test_atlas_api_per_node_paths(monkeypatch):
    root=Path(tempfile.mkdtemp()); st=LocalStore(root)
    class Fake:
        def resolve_query_shape_source(self,c): return "atlas_api"
        def collect_index_stats(self,c,h): return {"host":h,"type":"indexStats"}
        def collect_query_shapes(self,c,h): return {"host":h,"source":"atlas"}
    monkeypatch.setattr(skills,"observability",lambda:Fake())
    result=run(cfg(root,query_shape_source="atlas_api"),"2026-09-26",st)
    assert result["query_shape_source"]=="atlas_api"
    for host in ("n1","n2"):
        assert (root/f"p/2026-09-26/{host}/indexStats/index-stats.json").is_file()
        assert (root/f"p/2026-09-26/{host}/queryStats/query-stats.json").is_file()


def test_mongodb_query_shape_source(monkeypatch):
    root=Path(tempfile.mkdtemp()); st=LocalStore(root)
    class Fake:
        def resolve_query_shape_source(self,c): return "mongodb"
        def collect_index_stats(self,c,h): return {"host":h}
        def collect_query_shapes(self,c,h): return {"host":h,"source":"mongodb_$queryStats"}
    monkeypatch.setattr(skills,"observability",lambda:Fake())
    result=run(cfg(root,deployment_type="ops_manager",query_shape_source="mongodb"),"2026-09-26",st)
    assert result["query_shape_source"]=="mongodb"
    assert json.loads((root/"p/2026-09-26/n1/queryStats/query-stats.json").read_text())["source"]=="mongodb_$queryStats"
