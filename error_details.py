"""Turn nested async failures into actionable, bounded messages."""


def describe_error(error: BaseException) -> str:
    if isinstance(error, BaseExceptionGroup):
        details = [describe_error(item) for item in error.exceptions]
        return "; ".join(dict.fromkeys(details))[:800] or type(error).__name__
    detail = str(error).strip()
    if not detail and error.__cause__:
        return describe_error(error.__cause__)
    return (detail or type(error).__name__)[:800]
