def _lower_strip(value: str) -> str:
    """Normalizes a string to lowercase and removes whitespace"""
    return (value or "").lower().strip()


def _strip(value: str) -> str:
    """Removes leading and trailing whitespace from a string"""
    return (value or "").strip()

