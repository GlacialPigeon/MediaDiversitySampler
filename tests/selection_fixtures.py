# Media Diversity Sampler
# Author: GlacialPigeon
# GitHub: https://github.com/GlacialPigeon

"""Deterministic synthetic candidate pools and a fingerprint of the selection result.

The expected fingerprints in test_regressions.py were computed with the v2.0.1 selection code
BEFORE the M7 optimisation, so they prove the optimised code selects exactly the same files.
"""
import hashlib, random

SDR_V = {
    "Codec": ["H264", "HEVC", "AV1", "MPEG4", "VC1", "MPEG2"], "ResolutionClass": ["SD", "720p", "1080p", "4K"],
    "BitDepth": [8, 10], "Chroma": ["4:2:0", "4:2:2", "4:4:4"], "FpsClass": ["23.976/24", "25", "29.97", "30", "50", "Other"],
    "ScanType": ["Progressive", "Interlaced", None, "unknown"], "DurationClass": ["<20 min", "20-<60 min", "60-<120 min", ">=120 min"],
    "Container": ["matroska", "mov", "avi", "mpegts"], "AspectRatioClass": ["16:9", "4:3", "Cinema/Wide", "Other"],
}
PROFILES = {"H264": ["Baseline", "Main", "High"], "HEVC": ["Main", "Main 10"], "AV1": ["Main"],
            "MPEG4": ["Advanced Simple Profile"], "VC1": ["Advanced"], "MPEG2": ["Main", "unknown"]}
HDR_V = {"HDRType": ["HDR10", "HLG", "Dolby Vision", "HDR10+"], "ColorTransfer": ["smpte2084", "arib-std-b67", "unknown"],
         "HDRMetadataClass": ["Static", "Dynamic", None], "ColorPrimaries": ["bt2020", "bt709"], "ColorSpace": ["bt2020nc", "bt2020c", "unknown"],
         "ColorRange": ["tv", "pc"]}


def build_records(model, n, seed):
    rng = random.Random(seed)
    out = []
    for i in range(n):
        codec = rng.choice(SDR_V["Codec"])
        media = {k: rng.choice(v) for k, v in SDR_V.items()}
        media.update(Codec=codec, Profile=rng.choice(PROFILES[codec]), BitRateBitsPerSecond=rng.choice([None, rng.randint(500_000, 80_000_000)]),
                     DurationSeconds=rng.choice([600.0, 1200.0, rng.uniform(600, 9000)]), DolbyVisionProfile=None)
        cls = {"Anomalies": [], "HDRType": "SDR", "HDRMetadataClass": None}
        pool = "SDR"
        if model == "HDR":
            hv = {k: rng.choice(v) for k, v in HDR_V.items()}
            media.update({k: hv[k] for k in ("ColorTransfer", "ColorPrimaries", "ColorSpace", "ColorRange")})
            cls = {"Anomalies": rng.choice([[], ["MissingMetadata"], ["UnusualCombination"]]), "HDRType": hv["HDRType"], "HDRMetadataClass": hv["HDRMetadataClass"]}
            if cls["HDRType"] == "Dolby Vision": media["DolbyVisionProfile"] = rng.choice(["5", "8", "7"])
            pool = rng.choice(["HDR", "HDR Uncertain"])
        out.append({"Path": f"/mnt/m/{i:05d}/Movie {i}.mkv", "Pool": pool, "Media": media, "Classification": cls})
    return out


def fingerprint(sel, model, n, requested, seed):
    plan = sel.new_diversity_selection_plan(build_records(model, n, seed), model)
    res = sel.invoke_diversity_selection(plan, requested)
    text = "\n".join(f"{e['Record']['Path']}|{e['ReasonForSelection']['BalancedFill']}|{e['ReasonForSelection']['RarityBonus']}|"
                     f"{','.join(e['ReasonForSelection']['NewPrimary'])}|{','.join(e['ReasonForSelection']['NewSupplemental'])}" for e in res["Selected"])
    return hashlib.sha256(text.encode()).hexdigest()[:16], plan["RecommendedMinimum"], len(res["Selected"])
