"""CLI entry point for the EthSyncAuditor pipeline."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from typing import Any

from dotenv import load_dotenv

load_dotenv()

from config import (
    ANTHROPIC_BASE_URL,
    DEEPSEEK_BASE_URL,
    DEEPSEEK_MODEL,
    GEMINI_BASE_URL,
    GEMINI_MODEL,
    LLM_MODEL,
    LLM_PROVIDER,
    OUTPUT_PATH,
)
from file_io.checkpoint import (
    latest_checkpoint,
    list_checkpoints,
    load_checkpoint,
    save_checkpoint,
)
from file_io.writer import (
    write_all_final_lsgs,
    write_diff_report,
    write_diff_report_json,
    write_false_positives_report,
)
from graph import compile_graph, configure_graph, make_initial_state

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def _proxy_url() -> str | None:
    return (os.environ.get("https_proxy")
            or os.environ.get("HTTPS_PROXY")
            or os.environ.get("all_proxy")
            or os.environ.get("ALL_PROXY"))


class _DeepSeekStructuredOutputWrapper:
    """Wraps ChatOpenAI so that ``with_structured_output`` defaults to
    ``method="function_calling"``, which DeepSeek's API supports (unlike
    the ``json_schema`` mode, and unlike ``json_mode`` which requires the
    word "json" in every prompt).  All other attribute accesses are
    transparently delegated to the underlying ChatOpenAI instance."""

    def __init__(self, chat_openai: Any) -> None:
        self._llm = chat_openai

    def with_structured_output(
        self, schema: Any, *, method: str = "function_calling", **kwargs: Any
    ) -> Any:
        return self._llm.with_structured_output(schema, method=method, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._llm, name)


def _init_llm(model_name: str, *, provider: str, base_url: str) -> Any:
    """Instantiate an LLM for *provider* or return ``None`` to fall back to mock."""
    if provider == "gemini":
        try:
            from langchain_google_genai import ChatGoogleGenerativeAI  # type: ignore[import-untyped]
        except ImportError:
            logger.warning("langchain-google-genai not installed; falling back to mock")
            return None
        if not os.environ.get("GOOGLE_API_KEY"):
            logger.warning("GOOGLE_API_KEY not set; falling back to mock")
            return None

        kwargs: dict[str, Any] = {"model": model_name}
        url = base_url or os.environ.get("GOOGLE_API_BASE", "")
        if url:
            kwargs["base_url"] = url
        proxy = _proxy_url()
        if proxy:
            kwargs["client_args"] = {"proxy": proxy}
        logger.info("Initializing Gemini LLM model=%s", model_name)
        return ChatGoogleGenerativeAI(**kwargs)

    if provider == "deepseek":
        try:
            from langchain_openai import ChatOpenAI  # type: ignore[import-untyped]
        except ImportError:
            logger.warning("langchain-openai not installed; falling back to mock")
            return None
        if not os.environ.get("DEEPSEEK_API_KEY"):
            logger.warning("DEEPSEEK_API_KEY not set; falling back to mock")
            return None

        kwargs: dict[str, Any] = {
            "model": model_name,
            "api_key": os.environ["DEEPSEEK_API_KEY"],
            "base_url": base_url or os.environ.get("DEEPSEEK_BASE_URL", ""),
            # DeepSeek's chat model has thinking enabled by default, but
            # thinking mode conflicts with tool_choice (used by
            # function_calling structured output).  Disable it explicitly.
            "model_kwargs": {"extra_body": {"thinking": {"type": "disabled"}}},
        }
        logger.info("Initializing DeepSeek LLM model=%s", model_name)
        llm = ChatOpenAI(**kwargs)
        # DeepSeek does not support OpenAI's json_schema response_format
        # (which langchain-openai uses by default in with_structured_output),
        # and json_mode requires the word "json" to appear in every prompt.
        # We wrap the LLM to force method="function_calling" instead, which
        # uses tool-calling — fully supported by DeepSeek once thinking is
        # disabled.
        return _DeepSeekStructuredOutputWrapper(llm)

    # anthropic (default)
    try:
        from langchain_anthropic import ChatAnthropic  # type: ignore[import-untyped]
    except ImportError:
        logger.warning("langchain-anthropic not installed; falling back to mock")
        return None
    if not os.environ.get("ANTHROPIC_API_KEY"):
        logger.warning("ANTHROPIC_API_KEY not set; falling back to mock")
        return None

    kwargs = {"model": model_name}
    url = base_url or os.environ.get("ANTHROPIC_BASE_URL", "")
    if url:
        kwargs["anthropic_api_url"] = url
    logger.info("Initializing Anthropic LLM model=%s", model_name)
    return ChatAnthropic(**kwargs)


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="EthSyncAuditor — LSG extraction & comparison")
    p.add_argument("--mock", action="store_true", help="Run with mock agents (no LLM)")
    p.add_argument("--provider", choices=["anthropic", "gemini", "deepseek"], default=None,
                   help="LLM provider (default: config.LLM_PROVIDER)")
    p.add_argument("--resume", action="store_true",
                   help="Resume from the latest checkpoint")
    p.add_argument("--resume-from", default=None, metavar="PHASE:ITER",
                   help="Resume from a specific checkpoint, e.g. 1:5")
    p.add_argument("--list-checkpoints", action="store_true",
                   help="List available checkpoints and exit")
    p.add_argument("--max-iter", type=int, default=None,
                   help="Override MAX_ITER_PHASE2")
    p.add_argument("--max-iter-phase2", type=int, default=None,
                   help="Override MAX_ITER_PHASE2 only")
    p.add_argument("--anthropic-base-url", default=None,
                   help="Custom API base URL for Anthropic")
    p.add_argument("--gemini-base-url", default=None,
                   help="Custom API base URL for Gemini")
    p.add_argument("--deepseek-base-url", default=None,
                   help="Custom API base URL for DeepSeek")
    p.add_argument("--skip-verify", action="store_true",
                   help="Skip Phase 3 B-class verification")
    return p


def _load_initial_state(args: argparse.Namespace) -> dict[str, Any]:
    if args.resume_from:
        try:
            phase_str, iter_str = args.resume_from.split(":")
            r_phase, r_iter = int(phase_str), int(iter_str)
        except (ValueError, IndexError):
            logger.error("Invalid --resume-from. Expected PHASE:ITER (e.g. 1:5)")
            sys.exit(1)
        try:
            initial = load_checkpoint(r_phase, r_iter)
            logger.info("Resuming checkpoint phase=%d iter=%d", r_phase, r_iter)
            return initial
        except FileNotFoundError as exc:
            logger.error("Checkpoint not found: %s", exc)
            sys.exit(1)

    if args.resume:
        ckpt = latest_checkpoint()
        if ckpt is not None:
            phase, iteration, initial = ckpt
            logger.info("Resuming checkpoint phase=%d iter=%d", phase, iteration)
            return initial
        logger.info("No checkpoint found; starting fresh")

    return make_initial_state()


def main() -> None:
    args = _build_arg_parser().parse_args()

    if args.list_checkpoints:
        ckpts = list_checkpoints()
        if not ckpts:
            print("No checkpoints found.")
        else:
            print(f"{'Phase':<8}{'Iter':<8}File")
            print("-" * 60)
            for phase, iteration, path in ckpts:
                print(f"{phase:<8}{iteration:<8}{path.name}")
        sys.exit(0)

    import config as _cfg
    if args.max_iter is not None:
        _cfg.MAX_ITER_PHASE2 = args.max_iter
    if args.max_iter_phase2 is not None:
        _cfg.MAX_ITER_PHASE2 = args.max_iter_phase2
    if args.skip_verify:
        _cfg.VERIFY_ENABLED = False

    provider = args.provider or LLM_PROVIDER
    logger.info("EthSyncAuditor starting (mock=%s, provider=%s, resume=%s, max_iter_p2=%d)",
                args.mock, provider, args.resume, _cfg.MAX_ITER_PHASE2)

    OUTPUT_PATH.mkdir(parents=True, exist_ok=True)

    callbacks: list[Any] = []
    if not args.mock:
        from file_io.audit_logger import AuditLogCallback
        callbacks.append(AuditLogCallback(phase=0, iteration=0, agent_type="preprocess"))

    use_mock = args.mock
    llm: Any = None
    if not use_mock:
        if provider == "gemini":
            model_name = GEMINI_MODEL
            base_url = args.gemini_base_url or GEMINI_BASE_URL
        elif provider == "deepseek":
            model_name = DEEPSEEK_MODEL
            base_url = args.deepseek_base_url or DEEPSEEK_BASE_URL
        else:
            model_name = LLM_MODEL
            base_url = args.anthropic_base_url or ANTHROPIC_BASE_URL
        llm = _init_llm(model_name, provider=provider, base_url=base_url)
        if llm is None:
            logger.warning("LLM unavailable; switching to mock mode")
            use_mock = True

    configure_graph(llm=llm, mock=use_mock, callbacks=callbacks)
    app = compile_graph()
    initial = _load_initial_state(args)

    logger.info("Executing graph …")
    final_state = app.invoke(initial)
    if not final_state:
        logger.error("Graph produced no output")
        sys.exit(1)

    if final_state.get("current_phase", 0) >= 2:
        write_all_final_lsgs(final_state)
        write_diff_report(final_state)
        write_diff_report_json(final_state)
        write_false_positives_report(final_state)

    save_checkpoint(
        final_state,
        phase=final_state.get("current_phase", 0),
        iteration=max(
            final_state.get("phase1_iteration", 0),
            final_state.get("phase2_iteration", 0),
        ),
    )

    if final_state.get("force_stopped", False):
        logger.warning("Pipeline finished with force_stopped=True")
    else:
        logger.info("Pipeline finished successfully")


if __name__ == "__main__":
    main()

