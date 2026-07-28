"""Re-run ONLY the parameter / behavior-divergence track (3 subsystem domains).

Bypasses the LangGraph pipeline so phase1 + the 7 FSM workflows are not
re-executed. Extraction is fresh against the existing preprocessed indexes.
Overwrites output/Parameter_Divergence_Report.md.
"""
from __future__ import annotations

import logging
import os

from dotenv import load_dotenv

load_dotenv()

from config import CLIENT_NAMES, GLM_BASE_URL, GLM_MODEL, subsystem_domains
from main import _init_llm
from agents.param_sub_agent import build_param_sub_agent
from agents.param_main_agent import compare_domain
from file_io.writer import write_parameter_divergence_report

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("rerun_param")


def main() -> None:
    llm = _init_llm(GLM_MODEL, provider="glm", base_url=GLM_BASE_URL)
    if llm is None:
        raise SystemExit("GLM LLM unavailable (GLM_API_KEY not set?)")

    client_aspects: dict = {}    # {client: {aspect_id: pv}}
    behavior_aspects: dict = {}  # {aspect_id: aspect_meta}
    all_divs: list[dict] = []

    for dom in subsystem_domains():
        logger.info("=== domain %s (%s) ===", dom.id, dom.name)
        state = {"current_domain": dom.id}
        for client in CLIENT_NAMES:
            res = build_param_sub_agent(client, llm=llm)(state)
            for c, m in (res.get("client_aspects") or {}).items():
                client_aspects.setdefault(c, {}).update(m)
            behavior_aspects.update(res.get("behavior_aspects") or {})
            n = sum(len(v) for v in (res.get("client_aspects") or {}).values())
            logger.info("[%s/%s] extracted aspects=%d", dom.id, client, n)

        divs = compare_domain(dom.id, client_aspects, behavior_aspects)
        all_divs.extend(divs)
        logger.info("[param_main] %s -> %d divergences", dom.id, len(divs))

    final_state = {
        "diff_report": {"parameter_divergences": all_divs},
        "parameter_divergences": all_divs,
    }
    write_parameter_divergence_report(final_state)
    print(f"DONE. {len(all_divs)} parameter divergences written.")


if __name__ == "__main__":
    main()
