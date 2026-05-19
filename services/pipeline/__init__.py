__all__ = ["PipelineEngine", "pipeline_engine"]


def __getattr__(name: str):
    if name in {"PipelineEngine", "pipeline_engine"}:
        from .engine import PipelineEngine, pipeline_engine

        mapping = {
            "PipelineEngine": PipelineEngine,
            "pipeline_engine": pipeline_engine,
        }
        return mapping[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
