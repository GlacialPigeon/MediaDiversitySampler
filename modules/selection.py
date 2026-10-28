# Media Diversity Sampler
# Author: GlacialPigeon
# GitHub: https://github.com/GlacialPigeon

from __future__ import annotations

import math
from collections import Counter, OrderedDict
from typing import Any, Optional

from . import scan as scan_module
from .classification import convert_scan_to_classified_records, reliable_diversity_value
from .interrupts import graceful_stop_requested, scoped_graceful_stop
from .logger import write_sampler_log

SDR_PRIMARY_WEIGHTS = OrderedDict([
    ("Codec", 300), ("ResolutionClass", 180), ("Profile", 150),
    ("BitDepth", 140), ("Chroma", 130), ("FpsClass", 120),
    ("ScanType", 110), ("DurationClass", 90),
])
SDR_SECONDARY_WEIGHTS = OrderedDict([
    ("BitRateClass", 50), ("Container", 40), ("AspectRatioClass", 40),
])
SDR_PAIR_WEIGHTS = [
    ("Codec", "ResolutionClass", 180), ("Codec", "BitDepth", 160),
    ("Codec", "Profile", 160), ("Codec", "Chroma", 140),
    ("ResolutionClass", "FpsClass", 120),
]
HDR_PRIMARY_WEIGHTS = OrderedDict([
    ("HDRType", 300), ("ColorTransfer", 250), ("HDRMetadataClass", 180),
    ("ColorPrimaries", 180), ("ColorSpace", 160), ("BitDepth", 150),
    ("Chroma", 140),
])
HDR_SECONDARY_WEIGHTS = OrderedDict([
    ("Codec", 120), ("ResolutionClass", 100), ("ColorRange", 80),
    ("FpsClass", 60), ("DurationClass", 30),
])
HDR_PAIR_WEIGHTS = [
    ("HDRType", "Codec", 180), ("HDRType", "BitDepth", 160),
    ("HDRType", "Chroma", 140), ("HDRType", "ResolutionClass", 120),
    ("ColorTransfer", "ColorPrimaries", 160), ("ColorTransfer", "ColorSpace", 150),
    ("HDRType", "HDRMetadataClass", 150),
]
HDR_ANOMALY_WEIGHT = 220
DOLBY_VISION_PROFILE_SUPPLEMENTAL_WEIGHT = 100
RARITY_MAXIMUM_FRACTION = 0.25

def percentile_value(values: list[float], percentile: float) -> Optional[float]:
    if not values: return None
    values = sorted(values)
    if len(values) == 1: return values[0]
    pos = (len(values)-1) * percentile; lo = math.floor(pos); hi = math.ceil(pos)
    if lo == hi: return values[lo]
    frac = pos - lo
    return values[lo]*(1-frac) + values[hi]*frac


def set_current_pool_bitrate_classes(candidates: list[dict[str, Any]]) -> None:
    known = [float(c["Media"]["BitRateBitsPerSecond"]) for c in candidates if c["Media"].get("BitRateBitsPerSecond") is not None and float(c["Media"]["BitRateBitsPerSecond"]) > 0]
    if not known:
        for c in candidates: c["Media"]["BitRateClass"] = None
        return
    lower, upper = percentile_value(known, 1/3), percentile_value(known, 2/3)
    for c in candidates:
        value = c["Media"].get("BitRateBitsPerSecond")
        if value is None or float(value) <= 0: c["Media"]["BitRateClass"] = None
        elif float(value) <= lower: c["Media"]["BitRateClass"] = "Low"
        elif float(value) <= upper: c["Media"]["BitRateClass"] = "Medium"
        else: c["Media"]["BitRateClass"] = "High"


def diversity_property_value(record: dict[str, Any], property_name: str) -> Any:
    if record.get("Media") is None: return None
    classification = record.get("Classification")
    if property_name == "ColorTransfer" and classification and "InconsistentColorMetadata" in classification.get("Anomalies", []): return None
    if property_name == "Profile":
        media = record["Media"]
        return f"{media['Codec']}: {media['Profile']}" if reliable_diversity_value(media["Profile"]) and reliable_diversity_value(media["Codec"]) else None
    if property_name == "HDRType": return classification.get("HDRType") if classification else None
    if property_name == "HDRMetadataClass": return classification.get("HDRMetadataClass") if classification else None
    return record["Media"].get(property_name)


def new_diversity_token(kind: str, name: str, value: str, weight: float, description: str) -> dict[str, Any]:
    return {"Kind":kind,"Name":name,"Value":value,"Key":f"{kind}|{name}|{value}".lower(),"Weight":weight,"Description":description}


def candidate_diversity_tokens(record: dict[str, Any], model: str) -> list[dict[str, Any]]:
    tokens = []
    if model == "SDR": primary, secondary, pairs = SDR_PRIMARY_WEIGHTS, SDR_SECONDARY_WEIGHTS, SDR_PAIR_WEIGHTS
    else: primary, secondary, pairs = HDR_PRIMARY_WEIGHTS, HDR_SECONDARY_WEIGHTS, HDR_PAIR_WEIGHTS
    for name, weight in primary.items():
        value = diversity_property_value(record, name)
        if reliable_diversity_value(value): tokens.append(new_diversity_token("Primary", name, str(value), float(weight), f"New {name}: {value}"))
    for left, right, weight in pairs:
        lv, rv = diversity_property_value(record,left), diversity_property_value(record,right)
        if reliable_diversity_value(lv) and reliable_diversity_value(rv):
            value = f"{left}={lv} x {right}={rv}"
            tokens.append(new_diversity_token("PrimaryPair", f"{left}x{right}", value, float(weight), f"New combination: {value}"))
    for name, weight in secondary.items():
        value = diversity_property_value(record,name)
        if reliable_diversity_value(value): tokens.append(new_diversity_token("Secondary", name, str(value), float(weight), f"Secondary diversity: {name}={value}"))
    if record["Pool"] in {"SDR Uncertain", "HDR Uncertain"}:
        for anomaly in record["Classification"].get("Anomalies", []):
            if reliable_diversity_value(anomaly): tokens.append(new_diversity_token("Supplemental", "HDRAnomaly", str(anomaly), float(HDR_ANOMALY_WEIGHT), f"HDR anomaly: {anomaly}"))
    if model == "HDR" and str(record["Classification"]["HDRType"]).lower() == "dolby vision" and reliable_diversity_value(record["Media"]["DolbyVisionProfile"]):
        value = record["Media"]["DolbyVisionProfile"]
        tokens.append(new_diversity_token("Supplemental", "DolbyVisionProfile", str(value), float(DOLBY_VISION_PROFILE_SUPPLEMENTAL_WEIGHT), f"Additional Dolby Vision profile: {value}"))
    return tokens


def new_candidate_info(record: dict[str, Any], model: str) -> dict[str, Any]:
    duration = float(record["Media"].get("DurationSeconds") or 0.0)
    return {"Record":record,"Path":str(record["Path"]),"PathKey":str(record["Path"]).lower(),"DurationSeconds":duration,"Tokens":candidate_diversity_tokens(record,model)}


def primary_universe(infos: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    result = {}
    for info in infos:
        for t in info["Tokens"]:
            if t["Kind"] in {"Primary","PrimaryPair"}: result[t["Key"]] = t
    return result


def rarity_index(infos: list[dict[str, Any]]) -> dict[str,int]:
    freq: dict[str,int] = {}
    for info in infos:
        for t in info["Tokens"]:
            if t["Kind"] in {"Primary","PrimaryPair"}: freq[t["Key"]] = freq.get(t["Key"],0)+1
    return freq


def rarity_factor(info: dict[str, Any], index: dict[str,int], pool_size: int) -> float:
    # Constant for a candidate within one plan (index and pool size do not change), so cache it on the info.
    cache = info.setdefault("RarityFactorCache", {})
    key = (id(index), pool_size)
    value = cache.get(key)
    if value is None:
        value = cache[key] = _compute_rarity_factor(info, index, pool_size)
    return value


def _compute_rarity_factor(info: dict[str, Any], index: dict[str,int], pool_size: int) -> float:
    if pool_size <= 1: return 0.0
    factors=[]
    for t in info["Tokens"]:
        if t["Kind"] not in {"Primary","PrimaryPair"} or t["Key"] not in index: continue
        count=index[t["Key"]]; factor=1.0-((count-1.0)/(pool_size-1.0)); factors.append(max(0.0,min(1.0,factor)))
    return sum(factors)/len(factors) if factors else 0.0


def new_token_score(info: dict[str, Any], covered_primary:set[str], covered_secondary:set[str], covered_supp:set[str], index:dict[str,int], pool_size:int, minimum_phase:bool=False) -> dict[str,Any]:
    new_p=[]; new_s=[]; new_u=[]
    for t in info["Tokens"]:
        if t["Kind"] in {"Primary","PrimaryPair"} and t["Key"] not in covered_primary: new_p.append(t)
        elif t["Kind"] == "Secondary" and t["Key"] not in covered_secondary: new_s.append(t)
        elif t["Kind"] == "Supplemental" and t["Key"] not in covered_supp: new_u.append(t)
    ps=sum(float(t["Weight"]) for t in new_p); ss=sum(float(t["Weight"]) for t in new_s); us=sum(float(t["Weight"]) for t in new_u)
    if minimum_phase and ps <= 0: return {"Score":-1.0,"BaseScore":0.0,"RarityBonus":0.0,"NewPrimary":[],"NewSecondary":[],"NewSupplemental":[]}
    base=ps+ss+us
    if not minimum_phase and base <= 0: return {"Score":0.0,"BaseScore":0.0,"RarityBonus":0.0,"NewPrimary":new_p,"NewSecondary":new_s,"NewSupplemental":new_u}
    bonus=min(base*RARITY_MAXIMUM_FRACTION*rarity_factor(info,index,pool_size), base*RARITY_MAXIMUM_FRACTION)
    return {"Score":base+bonus,"BaseScore":base,"RarityBonus":bonus,"NewPrimary":new_p,"NewSecondary":new_s,"NewSupplemental":new_u}


def add_candidate_tokens_to_coverage(info: dict[str, Any], cp:set[str], cs:set[str], cu:set[str]) -> None:
    for t in info["Tokens"]:
        if t["Kind"] in {"Primary","PrimaryPair"}: cp.add(t["Key"])
        elif t["Kind"] == "Secondary": cs.add(t["Key"])
        elif t["Kind"] == "Supplemental": cu.add(t["Key"])


def selection_reason(score: dict[str, Any], balanced: bool) -> dict[str, Any]:
    return {"NewPrimary":[t["Description"] for t in score["NewPrimary"]],"NewSecondary":[t["Description"] for t in score["NewSecondary"]],"NewSupplemental":[t["Description"] for t in score["NewSupplemental"]],"RarityBonus":round(float(score["RarityBonus"]),3),"BalancedFill":balanced}


def select_minimum_coverage(infos:list[dict[str,Any]], universe:dict[str,Any], index:dict[str,int]) -> dict[str,Any]:
    cp:set[str]=set(); cs:set[str]=set(); cu:set[str]=set(); paths:set[str]=set(); selected=[]
    if not universe and infos:
        ranked=[]
        for info in infos:
            score=new_token_score(info,cp,cs,cu,index,len(infos))
            ranked.append((info,score,float(score["Score"]),float(info["DurationSeconds"]),info["PathKey"]))
        ranked.sort(key=lambda x:(-x[2],-x[3],x[4]))
        info,score,_,_,_=ranked[0]; paths.add(info["PathKey"]); selected.append({"CandidateInfo":info,"ReasonForSelection":selection_reason(score,False)}); add_candidate_tokens_to_coverage(info,cp,cs,cu)
        return {"Selected":selected,"CoveredPrimary":cp,"CoveredSecondary":cs,"CoveredSupplemental":cu,"SelectedPaths":paths}
    while len(cp) < len(universe):
        ranked=[]
        for info in infos:
            if info["PathKey"] in paths: continue
            score=new_token_score(info,cp,cs,cu,index,len(infos),True)
            if score["Score"] < 0: continue
            ranked.append((info,score,len(score["NewPrimary"]),float(score["Score"]),float(info["DurationSeconds"]),info["PathKey"]))
        if not ranked: break
        ranked.sort(key=lambda x:(-x[3],-x[2],-x[4],x[5]))
        info,score,*_=ranked[0]; paths.add(info["PathKey"]); add_candidate_tokens_to_coverage(info,cp,cs,cu); selected.append({"CandidateInfo":info,"ReasonForSelection":selection_reason(score,False)})
    return {"Selected":selected,"CoveredPrimary":cp,"CoveredSecondary":cs,"CoveredSupplemental":cu,"SelectedPaths":paths}


def _balance_weights(model:str) -> "OrderedDict[str, int]":
    return SDR_PRIMARY_WEIGHTS if model == "SDR" else OrderedDict(list(HDR_PRIMARY_WEIGHTS.items()) + [("Codec",120),("ResolutionClass",100)])


def _balance_keys(info:dict[str,Any], model:str) -> list[tuple[str, int, Any, Any]]:
    """Per-candidate (name, weight, display value, comparison key) for the balanced fill; cached on the info.

    The comparison key is the lower-cased value, or None when the value is not reliable.
    """
    cached = info.get("BalanceKeys")
    if cached is None:
        cached = []
        for name, weight in _balance_weights(model).items():
            value = diversity_property_value(info["Record"], name)
            cached.append((name, weight, value, str(value).lower() if reliable_diversity_value(value) else None))
        info["BalanceKeys"] = cached
    return cached


def _register_balance_counts(counts:dict[str, Counter], info:dict[str,Any], model:str) -> None:
    for name, _weight, _value, key in _balance_keys(info, model):
        if key is not None: counts.setdefault(name, Counter())[key] += 1


def _balance_score_counted(info:dict[str,Any], counts:dict[str, Counter], model:str, index:dict[str,int], pool_size:int) -> dict[str,Any]:
    base=0.0; reasons=[]
    for name, weight, value, key in _balance_keys(info, model):
        if key is None: continue
        count = counts[name][key] if name in counts else 0
        base += float(weight)/(1.0+count)
        if count==0: reasons.append(f"Underrepresented {name}: {value}")
    bonus=min(base*RARITY_MAXIMUM_FRACTION*rarity_factor(info,index,pool_size),base*RARITY_MAXIMUM_FRACTION)
    return {"Score":base+bonus,"BaseScore":base,"RarityBonus":bonus,"BalanceReasons":reasons}


def balance_score(info:dict[str,Any], already:list[dict[str,Any]], model:str, index:dict[str,int], pool_size:int) -> dict[str,Any]:
    counts: dict[str, Counter] = {}
    for selected in already: _register_balance_counts(counts, selected["CandidateInfo"], model)
    return _balance_score_counted(info, counts, model, index, pool_size)


def new_diversity_selection_plan(candidates:list[dict[str,Any]], model:str) -> dict[str,Any]:
    if not candidates: return {"Model":model,"Candidates":[],"CandidateInfos":[],"PrimaryUniverse":{},"RarityIndex":{},"MinimumSelection":[],"RecommendedMinimum":0}
    set_current_pool_bitrate_classes(candidates)
    infos=sorted((new_candidate_info(r,model) for r in candidates), key=lambda x:x["PathKey"])
    universe=primary_universe(infos); index=rarity_index(infos); minimum=select_minimum_coverage(infos,universe,index)
    return {"Model":model,"Candidates":candidates,"CandidateInfos":infos,"PrimaryUniverse":universe,"RarityIndex":index,"MinimumSelection":minimum["Selected"],"RecommendedMinimum":len(minimum["Selected"])}


def invoke_diversity_selection(plan:dict[str,Any], requested_count:int) -> dict[str,Any]:
    available=len(plan["CandidateInfos"])
    if available==0 or requested_count<=0: return {"RequestedCount":requested_count,"AvailableCount":available,"RecommendedMinimum":int(plan["RecommendedMinimum"]),"Selected":[]}
    if requested_count>available: raise ValueError("Requested count exceeds the available candidates.")
    selected=[]; paths:set[str]=set(); cp:set[str]=set(); cs:set[str]=set(); cu:set[str]=set()
    balance_counts: dict[str, Counter] = {}  # incremental per-property value counts of everything selected so far
    for entry in plan["MinimumSelection"]:
        if len(selected)>=requested_count: break
        selected.append(entry); paths.add(entry["CandidateInfo"]["PathKey"]); add_candidate_tokens_to_coverage(entry["CandidateInfo"],cp,cs,cu)
        _register_balance_counts(balance_counts, entry["CandidateInfo"], plan["Model"])
    coverage_exhausted=False  # covered token sets only grow, so once no candidate adds anything it stays that way
    while len(selected)<requested_count:
        ranked=[]
        if not coverage_exhausted:
            for info in plan["CandidateInfos"]:
                if info["PathKey"] in paths: continue
                score=new_token_score(info,cp,cs,cu,plan["RarityIndex"],available)
                if score["BaseScore"]<=0: continue
                new_count=len(score["NewPrimary"])+len(score["NewSecondary"])+len(score["NewSupplemental"])
                ranked.append((info,score,float(score["Score"]),new_count,float(info["DurationSeconds"]),info["PathKey"]))
            if not ranked: coverage_exhausted=True
        if ranked:
            ranked.sort(key=lambda x:(-x[2],-x[3],-x[4],x[5])); info,score,*_=ranked[0]; reason=selection_reason(score,False)
        else:
            balanced=[]
            for info in plan["CandidateInfos"]:
                if info["PathKey"] in paths: continue
                bal=_balance_score_counted(info,balance_counts,plan["Model"],plan["RarityIndex"],available)
                balanced.append((info,bal,float(bal["Score"]),float(info["DurationSeconds"]),info["PathKey"]))
            if not balanced: break
            balanced.sort(key=lambda x:(-x[2],-x[3],x[4])); info,bal,*_=balanced[0]
            reason={"NewPrimary":[],"NewSecondary":[],"NewSupplemental":list(bal["BalanceReasons"]),"RarityBonus":round(float(bal["RarityBonus"]),3),"BalancedFill":True}
        paths.add(info["PathKey"]); add_candidate_tokens_to_coverage(info,cp,cs,cu); selected.append({"CandidateInfo":info,"ReasonForSelection":reason})
        _register_balance_counts(balance_counts, info, plan["Model"])
    final=[]
    for order,entry in enumerate(selected,1): final.append({"SelectionOrder":order,"Record":entry["CandidateInfo"]["Record"],"ReasonForSelection":entry["ReasonForSelection"]})
    return {"RequestedCount":requested_count,"AvailableCount":available,"RecommendedMinimum":int(plan["RecommendedMinimum"]),"Selected":final}


def candidates_for_pool(records:list[dict[str,Any]], pool:str) -> list[dict[str,Any]]:
    return [r for r in records if r["Pool"]==pool]



def invoke_scan_and_classification(config: dict[str, Any], renderer: Any) -> Optional[dict[str, Any]]:
    # The current scan/classification state is owned by ui.py; this function only produces the result.
    scan_module.reset_scan_state()
    scan = scan_module.invoke_media_scan(config, renderer)
    if scan is None:
        return None
    with scoped_graceful_stop("SCAN"):
        classified = convert_scan_to_classified_records(scan)
        if graceful_stop_requested():
            write_sampler_log("Classification stopped gracefully. No current scan was created.", "INFO", "SCAN")
            scan_module.reset_scan_state()
            return None
    if hasattr(renderer, "show_classification"):
        renderer.show_classification(Counter(r["Pool"] for r in classified))
    if hasattr(renderer, "show_probe_failures"):
        renderer.show_probe_failures(scan)
    return {"Scan": scan, "ClassifiedRecords": classified}


# Selection preparation helpers. Console rendering is supplied by ui.py.
def new_pool_selection_context(classified_records: list[dict[str, Any]], pool: str, model: str) -> dict[str, Any]:
    candidates = candidates_for_pool(classified_records, pool)
    return {"Pool": pool, "Model": model, "Candidates": candidates, "Plan": new_diversity_selection_plan(candidates, model)}


def selection_pool_specs(selection_mode: str) -> list[tuple[str, str]]:
    if selection_mode == "SDR":
        return [("SDR", "SDR")]
    if selection_mode == "HDR":
        return [("HDR", "HDR")]
    if selection_mode == "Uncertain":
        return [("SDR Uncertain", "SDR"), ("HDR Uncertain", "HDR")]
    if selection_mode == "ALL":
        return [("SDR", "SDR"), ("HDR", "HDR"), ("SDR Uncertain", "SDR"), ("HDR Uncertain", "HDR")]
    raise ValueError(f"Unknown selection mode: {selection_mode}")


def analyze_selection_diversity(
    classified_records: list[dict[str, Any]],
    selection_mode: str,
    *,
    renderer: Any = None,
    live_preview: bool = False,
) -> list[dict[str, Any]]:
    import time
    from .logger import format_elapsed

    contexts: list[dict[str, Any]] = []
    for pool, model in selection_pool_specs(selection_mode):
        if renderer is not None:
            renderer.selection_analysis_started(pool, live_preview)
        started = time.perf_counter()
        context = new_pool_selection_context(classified_records, pool, model)
        contexts.append(context)
        if renderer is not None:
            renderer.selection_analysis_prepared(pool, live_preview)
        write_sampler_log(
            f"Diversity analysis pool={pool} candidates={len(context['Candidates'])} "
            f"recommended={context['Plan']['RecommendedMinimum']} "
            f"elapsed={format_elapsed(time.perf_counter()-started)}",
            "INFO", "SELECTION"
        )
    if live_preview and renderer is not None:
        renderer.clear_progress()
    return contexts


def selection_default_count(context: dict[str, Any]) -> int:
    candidates = len(context["Candidates"])
    if candidates <= 0:
        return 0
    recommended = int(context["Plan"]["RecommendedMinimum"])
    return min(candidates, max(1, recommended))


def finalize_pool_selections(
    contexts: list[dict[str, Any]],
    requested: dict[str, int],
    *,
    renderer: Any = None,
    quiet: bool = False,
) -> list[dict[str, Any]]:
    pool_selections: list[dict[str, Any]] = []
    for context in contexts:
        pool = str(context["Pool"])
        count = int(requested.get(pool, 0))
        if renderer is not None and not quiet:
            renderer.selection_started(pool)
        if count <= 0:
            result = {
                "RequestedCount": 0,
                "AvailableCount": len(context["Candidates"]),
                "RecommendedMinimum": int(context["Plan"]["RecommendedMinimum"]),
                "Selected": [],
            }
        else:
            result = invoke_diversity_selection(context["Plan"], count)
        pool_selections.append({"Pool": pool, "Selection": result})
        selected_count = len(result["Selected"])
        write_sampler_log(f"Selected {selected_count} files.", "INFO", "SELECTION")
        if renderer is not None and not quiet:
            renderer.selection_finished(pool, selected_count)
    return pool_selections


def write_selection_details_to_log(pool_selections: list[dict[str, Any]]) -> None:
    import json
    for pool_selection in pool_selections:
        selected = pool_selection["Selection"]
        write_sampler_log(
            "Selection pool={}; requested={}; available={}; recommendedMinimum={}; selected={}.".format(
                pool_selection["Pool"], selected["RequestedCount"], selected["AvailableCount"],
                selected["RecommendedMinimum"], len(selected["Selected"])
            )
        )
        for item in selected["Selected"]:
            record = item["Record"]
            reason_json = json.dumps(item["ReasonForSelection"], ensure_ascii=False, separators=(",", ":"))
            media = record["Media"]
            write_sampler_log(
                "Selected [{}] {} | Codec={}; Resolution={}; Profile={}; BitDepth={}; FPS={}; HDR={}; Reasons={}".format(
                    item["SelectionOrder"], record["Path"], media.get("Codec"), media.get("ResolutionClass"), media.get("Profile"),
                    media.get("BitDepth"), media.get("FpsClass"), (record.get("Classification") or {}).get("HDRType"), reason_json
                )
            )
