import os

if os.getenv("COVERAGE_PROCESS_START"):
    try:
        import coverage

        coverage.process_startup()
    except Exception:
        # Do not break runtime if coverage is unavailable.
        pass
