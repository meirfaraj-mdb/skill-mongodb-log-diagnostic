"""Optional indexStats / Atlas Query Shape Insights collection stage."""
from __future__ import annotations
import json
from .common import Layout, logger
from . import skills

def run(config: dict, log_date: str, store=None) -> dict:
    from .providers import get_store
    if config.get("input_mode") == "existing_bucket":
        return {"status":"skipped_existing_bucket","reason":"No MongoDB or Atlas access in bucket-only mode","nodes":[]}
    if not config.get("observability_enabled", False):
        return {"status":"disabled","nodes":[]}
    store=store or get_store(config); layout=Layout.from_config(config); collector=skills.observability()
    hosts=config.get("index_stats_hosts") or config.get("hostnames") or []
    if not hosts: raise ValueError("Set index_stats_hosts (or hostnames) when observability_enabled=true")
    results=[]
    # Sequential on purpose: direct node reads and uploads complete before next node starts.
    for host in hosts:
        node={"host":host,"uploads":{},"errors":[]}
        try:
            data=collector.collect_index_stats(config, host)
            node["uploads"]["indexStats"]=store.put_text(layout.index_stats(log_date,host),json.dumps(data,default=str,indent=2),"application/json")
        except Exception as exc:
            logger.exception("indexStats failed host=%s",host); node["errors"].append({"indexStats":str(exc)[:1000]})
        try:
            data=collector.atlas_query_stats(config, host)
            node["uploads"]["queryStats"]=store.put_text(layout.query_stats(log_date,host),json.dumps(data,default=str,indent=2),"application/json")
        except Exception as exc:
            logger.exception("queryStats failed host=%s",host); node["errors"].append({"queryStats":str(exc)[:1000]})
        results.append(node)
    return {"status":"completed","nodes":results}
