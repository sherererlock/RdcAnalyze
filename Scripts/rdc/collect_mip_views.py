# -*- coding: utf-8 -*-
"""Mip view collection script — runs inside rdc-cli daemon via 'rdc-cli script'.

rd and controller are injected as globals by the daemon runtime.
__ANALYSIS_DIR__ is replaced with the actual path by collect.py before execution.

Output: writes json/binding_views.json with per-pass mip view ranges.
"""

import json
from pathlib import Path

analysis_dir = Path("__ANALYSIS_DIR__")
json_dir = analysis_dir / "json"

passes_path = json_dir / "pass_details.json"
try:
    passes = json.loads(passes_path.read_text(encoding="utf-8"))
except Exception:
    passes = []

# Collect all action EIDs so we can find actual draws within each pass range.
# Sampling at begin_eid (BeginRenderPass) misses descriptor bindings — we need
# an EID inside the pass where vkCmdBindDescriptorSets has already executed.
def _iter_all(actions):
    for a in actions:
        yield a
        if a.children:
            yield from _iter_all(a.children)

try:
    action_eids = sorted(a.eventId for a in _iter_all(controller.GetRootActions()))
except Exception:
    action_eids = []

# For each pass, pick the last action EID strictly inside [begin, end).
# Falls back to end_eid (or begin_eid for single-event passes).
sample_eids = []
seen: set = set()
for p in passes:
    if not isinstance(p, dict):
        continue
    begin = p.get("begin_eid")
    end = p.get("end_eid")
    if begin is None:
        continue
    if end is None:
        end = begin
    in_range = [e for e in action_eids if begin < e < end]
    eid = in_range[-1] if in_range else (end if end != begin else begin)
    if eid not in seen:
        seen.add(eid)
        sample_eids.append(eid)


def _extract_desc_entries(ds_list, entries):
    for set_idx, ds in enumerate(ds_list):
        try:
            dr = rd.DescriptorRange()
            dr.offset = 0
            dr.count = 64
            descs = controller.GetDescriptors(ds.descriptorSetResourceId, [dr])
            for d in descs:
                try:
                    rid = int(str(d.resource).split("::")[-1])
                    if rid == 0:
                        continue
                    entries.append({
                        "set": set_idx,
                        "bind": int(d.byteOffset),
                        "resource_id": rid,
                        "first_mip": int(d.firstMip),
                        "num_mips": int(d.numMips),
                    })
                except Exception:
                    continue
        except Exception:
            continue


result = {}  # {str(eid): [{set, bind, resource_id, first_mip, num_mips}]}

for eid in sample_eids:
    try:
        controller.SetFrameEvent(eid, False)
        state = controller.GetVulkanPipelineState()
        if not state:
            continue

        entries = []
        if state.graphics and state.graphics.descriptorSets:
            _extract_desc_entries(state.graphics.descriptorSets, entries)
        if state.compute and state.compute.descriptorSets:
            _extract_desc_entries(state.compute.descriptorSets, entries)

        if entries:
            result[str(eid)] = entries

    except Exception:
        continue

output_path = json_dir / "binding_views.json"
output_path.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
