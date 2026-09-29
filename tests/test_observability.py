import json
import sys
import tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent.providers import LocalStore
from agent.observability_stage import run
from agent import skills

def test_bucket_mode_never_loads_collectors():
 root=Path(tempfile.mkdtemp()); st=LocalStore(root)
 result=run({"input_mode":"existing_bucket","storage_provider":"local","bucket":str(root),"prefix":"p","cloud":"local","observability_enabled":True},"2026-09-26",st)
 assert result["status"]=="skipped_existing_bucket" and not list(root.rglob("*"))

def test_per_node_paths(monkeypatch):
 root=Path(tempfile.mkdtemp()); st=LocalStore(root)
 class Fake:
  def collect_index_stats(self,c,h): return {"host":h,"type":"indexStats"}
  def atlas_query_stats(self,c,h): return {"host":h,"type":"queryStats"}
 monkeypatch.setattr(skills,"observability",lambda:Fake())
 cfg={"input_mode":"atlas_api","storage_provider":"local","bucket":str(root),"prefix":"p","cloud":"local","observability_enabled":True,"deployment_type":"atlas","index_stats_hosts":["n1","n2"]}
 result=run(cfg,"2026-09-26",st)
 assert len(result["nodes"])==2
 for host in ("n1","n2"):
  assert (root/f"p/2026-09-26/{host}/indexStats/index-stats.json").is_file()
  assert (root/f"p/2026-09-26/{host}/queryStats/query-stats.json").is_file()
